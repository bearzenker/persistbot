"""The link to the runner: transports, the account check, the separation
check, and what happens when the runner misbehaves or goes away."""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from persistbot import link, runner, tools
from persistbot.link import Link, Lost, Refused, Unavailable

ME = os.getuid()

# Becomes the runner the way systemd socket activation starts it: the
# listening socket on descriptor 3, LISTEN_FDS and LISTEN_PID set.
ACTIVATE = (
    "import os, sys; os.dup2(int(sys.argv[1]), 3); "
    "os.environ.update(LISTEN_FDS='1', LISTEN_PID=str(os.getpid())); "
    "os.execv(sys.executable, [sys.executable, '-I', sys.argv[2]])"
)


class Activated:
    """A runner behind a Unix socket, optionally on another account (root only)."""

    def __init__(self, directory, uid=None, program=runner.__file__):
        self.path = Path(directory) / "runner.sock"
        self.path.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.path))
        listener.listen(4)
        account = {} if uid is None else {"user": uid, "group": uid, "extra_groups": []}
        self.proc = subprocess.Popen(
            [sys.executable, "-c", ACTIVATE, str(listener.fileno()), str(program)],
            pass_fds=(listener.fileno(),), cwd="/", **account,
        )
        listener.close()

    def stop(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()


@pytest.fixture
def activated(tmp_path):
    started = []

    def start(**kwargs):
        started.append(Activated(tmp_path, **kwargs))
        return started[-1]

    yield start
    for one in started:
        one.stop()


@pytest.fixture
def as_raven(monkeypatch):
    """Make `user: raven` mean an account of our choosing."""

    def choose(uid):
        monkeypatch.setattr(link, "_account", lambda user: uid)

    return choose


def separated(tmp_path, path, **kwargs):
    return Link(work=tmp_path / "work", user="raven", socket_path=path, **kwargs)


# the child-process transport ------------------------------------------------


def test_without_a_user_the_runner_is_a_child_and_says_it_is_not_separated(tmp_path):
    one = Link(work=tmp_path / "work")
    one.open()
    try:
        assert not one.separated and one.uid == ME
        assert "not separated" in one.describe() and str(tmp_path / "work") in one.describe()
        (tmp_path / "work" / "f").write_text("hi")
        assert one.call({"op": "read", "path": "f"}, 5) == {"ok": True, "text": "hi"}
    finally:
        one.close()
    assert one._child is None


def test_a_working_directory_that_cannot_be_made_refuses(tmp_path):
    (tmp_path / "file").write_text("in the way")
    one = Link(work=tmp_path / "file" / "work")
    with pytest.raises(Refused, match="working directory"):
        one.open()
    assert one._sock is None and one._child is None


def test_the_runner_program_cannot_be_started(tmp_path, monkeypatch):
    monkeypatch.setattr(link.sys, "executable", str(tmp_path / "no-python"))
    with pytest.raises(Unavailable, match="cannot start the runner"):
        Link(work=tmp_path / "work").open()


def test_closing_the_connection_stops_the_runner_and_its_commands(tmp_path):
    one = Link(work=tmp_path / "work")
    one.open()
    child = one._child
    request = {"op": "run", "commands": [{"argv": ["tail", "-f", "/dev/null"]}], "timeout": 60}
    one._sock.setblocking(True)
    one._sock.sendall(json.dumps(request).encode() + b"\n")
    time.sleep(0.5)  # the command is running
    one._sock.close()
    assert child.wait(timeout=5) == 0
    one._sock = one._child = None


# a runner that misbehaves ---------------------------------------------------


def played_by(behaviour):
    """A link whose runner is this test: `behaviour(sock)` runs in a thread."""
    ours, theirs = socket.socketpair()
    one = Link(work=Path("/nowhere"))
    one._expected = ME
    one._adopt(ours)
    thread = threading.Thread(target=behaviour, args=(theirs,), daemon=True)
    thread.start()
    return one


def test_a_reply_over_the_size_cap_is_lost():
    def flood(sock):
        sock.recv(65536)
        try:
            sock.sendall(b"x" * (link.MAX_REPLY + 200_000))
        except OSError:
            pass

    one = played_by(flood)
    with pytest.raises(Lost, match="passed"):
        one.call({"op": "read", "path": "f"}, 10)
    assert one._sock is None  # closed, so the next call starts afresh


def test_a_reply_past_the_deadline_is_lost():
    one = played_by(lambda sock: time.sleep(3))
    started = time.monotonic()
    with pytest.raises(Lost, match="in time"):
        one.call({"op": "read", "path": "f"}, 0.3)
    assert time.monotonic() - started < 2


@pytest.mark.parametrize("reply", [b"not json\n", b"[1, 2]\n", b'"text"\n'])
def test_a_reply_that_is_not_a_json_object_is_lost(reply):
    def answer(sock):
        sock.recv(65536)
        sock.sendall(reply)

    with pytest.raises(Lost, match="not a JSON object"):
        played_by(answer).call({"op": "read", "path": "f"}, 5)


def test_a_runner_that_hangs_up_is_lost():
    def hang_up(sock):
        sock.recv(65536)
        sock.close()

    with pytest.raises(Lost, match="closed the connection|went away"):
        played_by(hang_up).call({"op": "read", "path": "f"}, 5)


def test_a_runner_reply_cannot_exceed_the_limit_the_tool_sets(ctx, monkeypatch):
    monkeypatch.setattr(ctx.link, "call", lambda request, wait: {"ok": True, "text": "y" * 50_000})
    result = tools.call(ctx, "run_shell", json.dumps({"command": "ls"}))
    assert len(result.text) < 5000 and result.text.endswith("[cut off]")
    monkeypatch.setattr(ctx.link, "call", lambda request, wait: {"ok": "yes", "text": 7})
    assert not tools.call(ctx, "read_file", json.dumps({"path": "f"})).ok


def test_a_runner_killed_by_the_models_own_command_is_a_tool_error_then_reconnects(ctx):
    first = ctx.link._child.pid
    result = tools.call(ctx, "run_shell", json.dumps({"command": f"kill -9 {first}"}))
    assert not result.ok and not result.malformed
    assert "outcome is unknown" in result.text
    result = tools.call(ctx, "run_shell", json.dumps({"command": "pwd"}))
    assert result.ok and str(ctx.work) in result.text
    assert ctx.link._child.pid != first


# the socket transport: who is on the other end ------------------------------


def test_a_socket_runner_on_the_expected_account_is_accepted(tmp_path, activated, as_raven):
    as_raven(ME)
    served = activated()
    one = separated(tmp_path, served.path)
    one.open()
    assert one.separated and one.uid == ME
    assert one.describe().startswith(f"separated as raven (uid {ME})")
    assert one.call({"op": "write", "path": "a.txt", "content": "x"}, 5)["ok"]
    one.close()
    assert served.proc.wait(timeout=5) == 0  # one connection, then it exits


def test_replies_from_any_other_account_are_refused(tmp_path, activated, as_raven):
    as_raven(ME + 1)  # the runner is really us, which is not who was asked for
    one = separated(tmp_path, activated().path)
    with pytest.raises(Refused, match=f"came from uid {ME}, not from 'raven'"):
        one.open()
    assert one._sock is None and not (tmp_path / "work" / "a.txt").exists()


def test_a_runner_that_lies_about_its_account_is_refused(tmp_path, as_raven):
    """The reply's text says one uid; the kernel's stamp on it says another."""
    liar = tmp_path / "liar.py"
    liar.write_text(
        "import json, os, socket\n"
        "conn, _ = socket.socket(fileno=3).accept()\n"
        "conn.recv(65536)\n"
        f"conn.sendall(json.dumps({{'ok': True, 'uid': {ME + 1}, 'complete': True}}).encode() + b'\\n')\n"
        "conn.recv(65536)\n"
    )
    for claimed, pattern in ((ME + 1, "came from uid"), (ME, "says it is uid")):
        as_raven(claimed)
        served = Activated(tempfile.mkdtemp(dir=tmp_path), program=liar)
        try:
            with pytest.raises(Refused, match=pattern):
                separated(tmp_path, served.path).open()
        finally:
            served.stop()


@pytest.mark.parametrize("complete", ["False", "None", "'yes'"])
def test_a_separation_check_that_did_not_finish_is_refused(tmp_path, as_raven, complete):
    """Nothing found, but not everything looked at: that is not a pass."""
    partial = tmp_path / "partial.py"
    partial.write_text(
        "import json, os, socket\n"
        "conn, _ = socket.socket(fileno=3).accept()\n"
        "conn.recv(65536)\n"
        "reply = {'ok': True, 'uid': os.getuid(), 'writable': [], 'readable': [], 'checked': 250000}\n"
        f"if {complete} is not None: reply['complete'] = {complete}\n"
        "conn.sendall(json.dumps(reply).encode() + b'\\n')\n"
        "conn.recv(65536)\n"
    )
    as_raven(ME)
    served = Activated(tmp_path, program=partial)
    one = separated(tmp_path, served.path)
    try:
        with pytest.raises(Refused, match="stopped after 250000 files"):
            one.open()
        assert one._sock is None
    finally:
        served.stop()


@pytest.mark.parametrize("uid, problem", [(None, "no Linux account"), (0, "root"), ("self", "harness itself")])
def test_the_account_must_exist_and_be_neither_root_nor_the_harness(monkeypatch, uid, problem):
    class Entry:
        pw_uid = 1234 if uid == "self" else uid

    def lookup(name):
        if uid is None:
            raise KeyError(name)
        return Entry

    monkeypatch.setattr(link.pwd, "getpwnam", lookup)
    monkeypatch.setattr(link.os, "geteuid", lambda: 1234)
    with pytest.raises(Refused, match=problem):
        link._account("raven")


def test_nothing_listening_is_unavailable(tmp_path, as_raven):
    as_raven(ME)
    with pytest.raises(Unavailable, match="cannot reach the runner.*persistbot-runner@raven.socket"):
        separated(tmp_path, tmp_path / "nobody.sock").open()
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(tmp_path / "stale.sock"))
    stale.close()
    with pytest.raises(Unavailable, match="Connection refused"):
        separated(tmp_path, tmp_path / "stale.sock").open()


# the separation check -------------------------------------------------------


def test_a_runner_that_can_reach_protected_paths_is_refused(tmp_path, activated, as_raven):
    as_raven(ME)
    code = tmp_path / "install" / "src"
    code.mkdir(parents=True)
    (code / "allowlist.py").write_text("x")
    keys = tmp_path / "install" / "keys.json"
    keys.write_text("{}")
    one = separated(tmp_path, activated().path, deny_write=[str(tmp_path / "install")])
    with pytest.raises(Refused, match="can write .*install"):
        one.open()
    one = separated(tmp_path, activated().path, deny_read=[str(keys)])
    with pytest.raises(Refused, match="can read .*keys.json"):
        one.open()


def test_the_check_looks_inside_directories_but_not_into_the_work_directory(tmp_path):
    install = tmp_path / "install"
    (install / "src").mkdir(parents=True)
    (install / "src" / "a.py").write_text("x")
    work = install / "work"
    here = runner.Runner()
    reply = here.hello({"work": str(work), "deny_write": [str(install)], "deny_read": [str(install / "src" / "a.py")]})
    assert reply["ok"] and reply["complete"] and reply["uid"] == ME
    # Whatever this account can write, the work directory is never reported.
    assert not any(str(work) in path for path in reply["writable"])
    if ME != 0:  # root can write anything, so only then is the rest meaningful
        assert str(install) in reply["writable"]
        os.chmod(install / "src" / "a.py", 0o444)
        os.chmod(install / "src", 0o555)
        os.chmod(install, 0o555)
        try:
            reply = runner.Runner().hello({"work": str(tmp_path / "w2"), "deny_write": [str(install)]})
            assert reply["writable"] == []
        finally:
            os.chmod(install, 0o755)
            os.chmod(install / "src", 0o755)


def test_the_check_says_when_it_ran_out_of_budget(tmp_path, monkeypatch):
    install = tmp_path / "install"
    install.mkdir()
    for number in range(20):
        (install / f"f{number}").symlink_to("/nonexistent")  # links are looked at, never reported
    monkeypatch.setattr(runner, "CHECK_BUDGET", 10)
    reply = runner.Runner().hello({"work": str(tmp_path / "work"), "deny_write": [str(install)]})
    assert reply["checked"] == 10 and not reply["complete"]


def test_mode_bits_count_even_where_the_kernel_says_no(tmp_path, monkeypatch):
    """Under a read-only mount access() refuses a file whose mode allows it."""
    target = tmp_path / "code.py"
    target.write_text("x")
    target.chmod(0o666)
    monkeypatch.setattr(runner.os, "access", lambda path, mode: False)
    monkeypatch.setattr(runner.os, "getuid", lambda: 54321)
    monkeypatch.setattr(runner.os, "getgid", lambda: 54321)
    monkeypatch.setattr(runner.os, "getgroups", lambda: [])
    assert runner._allowed(str(target), 2) and runner._allowed(str(target), 4)
    target.chmod(0o640)
    assert not runner._allowed(str(target), 2) and not runner._allowed(str(target), 4)
    monkeypatch.setattr(runner.os, "getgroups", lambda: [target.stat().st_gid])
    assert runner._allowed(str(target), 4) and not runner._allowed(str(target), 2)


def test_a_writable_directory_above_the_install_is_reported(tmp_path, monkeypatch):
    install = tmp_path / "loose" / "install"
    install.mkdir(parents=True)
    monkeypatch.setattr(runner.os, "access", lambda path, mode: False)
    monkeypatch.setattr(runner.os, "getuid", lambda: 54321)
    monkeypatch.setattr(runner.os, "getgid", lambda: 54321)
    monkeypatch.setattr(runner.os, "getgroups", lambda: [])
    for mode in (0o755, 0o1777):  # closed, or open but sticky: entries cannot be swapped
        (tmp_path / "loose").chmod(mode)
        assert runner._replaceable(str(install)) is None
    (tmp_path / "loose").chmod(0o777)
    assert runner._replaceable(str(install)) == str(tmp_path / "loose")
    (tmp_path / "loose").chmod(0o755)


def test_for_config_lists_what_the_tools_account_must_not_reach(make_conf, tmp_path):
    from persistbot.config import load

    config = load(make_conf("user: raven\n"))
    one = Link.for_config(config)
    venv = tmp_path / "opt"
    assert one.separated and one.work == tmp_path / "bot-raven"
    assert str(one.socket_path) == "/run/persistbot/raven.sock"
    # The conf directory (here the parent of the install directory) is listed;
    # what lies inside a listed directory is not, because the check walks down.
    assert str(tmp_path) in one.deny_write
    assert str(venv) not in one.deny_write and str(venv / "raven") not in one.deny_write
    assert str(Path(runner.__file__).parent) in " ".join(one.deny_write) or any(
        Path(listed) in Path(runner.__file__).parents for listed in one.deny_write)
    assert str(tmp_path / "keys.json") in one.deny_read and str(venv / "raven") in one.deny_read
    assert str(venv / "raven" / "state.db") in one.deny_read
    plain = Link.for_config(load(make_conf()))
    assert not plain.separated and plain.deny_write == [] and plain.work == venv / "raven" / "work"


# the real thing, when the suite runs as root --------------------------------


@pytest.mark.skipif(ME != 0, reason="needs root to start a runner on another account")
def test_a_runner_on_another_account_cannot_touch_the_harness(as_raven):
    bot_uid = 54321
    top = Path(tempfile.mkdtemp(dir="/tmp", prefix="persistbot-sep-"))
    try:
        os.chmod(top, 0o755)
        install, state, work = top / "install", top / "state", top / "work"
        install.mkdir()
        state.mkdir(mode=0o700)
        work.mkdir()
        os.chown(work, bot_uid, bot_uid)
        (install / "allowlist.py").write_text("ORIGINAL")
        (install / "keys.json").write_text('{"key": "sk-secret"}')
        os.chmod(install / "keys.json", 0o600)
        (state / "state.db").write_text("goals")
        program = shutil.copy(runner.__file__, top / "runner.py")  # standalone: it needs nothing else
        deny = {"deny_write": [str(install), str(state)], "deny_read": [str(install / "keys.json"), str(state)]}
        as_raven(bot_uid)

        served = Activated(top, uid=bot_uid, program=program)
        one = Link(work=work, user="raven", socket_path=served.path, **deny)
        try:
            one.open()
            assert one.uid == bot_uid

            def run(*argv):
                return one.call({"op": "run", "commands": [{"argv": list(argv)}], "timeout": 5}, 10)

            assert one.call({"op": "write", "path": "new.py", "content": "PWNED"}, 5)["ok"]
            assert "Permission denied" in run("cp", "new.py", str(install / "allowlist.py"))["text"]
            assert "Permission denied" in run("cat", str(install / "keys.json"))["text"]
            assert "Permission denied" in run("ls", str(state))["text"]
            assert "Permission denied" in run("rm", str(state / "state.db"))["text"]
            assert not one.call({"op": "read", "path": str(install / "keys.json")}, 5)["ok"]
            assert f"uid={bot_uid}" in run("find", ".", "-maxdepth", "0", "-exec", "id", ";")["text"]
            assert (install / "allowlist.py").read_text() == "ORIGINAL"
            assert (state / "state.db").read_text() == "goals"
            assert (work / "new.py").stat().st_uid == bot_uid
        finally:
            one.close()
            served.stop()

        # One file loosened: the next tick refuses to run any tool at all.
        os.chmod(install / "allowlist.py", 0o666)
        served = Activated(top, uid=bot_uid, program=program)
        try:
            with pytest.raises(Refused, match="can write .*allowlist.py"):
                Link(work=work, user="raven", socket_path=served.path, **deny).open()
        finally:
            served.stop()

        # A file whose mode has drifted is reported even where it cannot be
        # written at this moment, and so is a directory above the install
        # that the account could rename things in.
        os.chmod(install / "allowlist.py", 0o644)
        os.chmod(top, 0o777)
        served = Activated(top, uid=bot_uid, program=program)
        try:
            with pytest.raises(Refused, match=f"can write {top}[,;]"):
                Link(work=work, user="raven", socket_path=served.path, **deny).open()
        finally:
            served.stop()
            os.chmod(top, 0o755)

        # The runner started as the wrong account (here: root itself).
        served = Activated(top, program=program)
        try:
            with pytest.raises(Refused, match="came from uid 0, not from 'raven'"):
                Link(work=work, user="raven", socket_path=served.path, **deny).open()
        finally:
            served.stop()
    finally:
        shutil.rmtree(top, ignore_errors=True)
