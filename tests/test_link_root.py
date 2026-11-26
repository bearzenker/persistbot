"""The separation check for real: a runner on another account, against a
harness's files it must not touch.

Every test here needs root, and is skipped for anyone else. Only root can
give the work directory to another account (chown) and start the runner as
that account's uid, which is what separation means. The rest of the link's
behaviour is covered in test_link.py, which any account can run; this file
is the only proof that two real accounts are kept apart, so run the suite as
root before trusting an install.
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from activation import Activated
from persistbot import runner
from persistbot.link import Link, Refused

pytestmark = pytest.mark.skipif(
    os.getuid() != 0, reason="needs root: starts a runner on another account (chown, setuid)"
)


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

        # A link in the install that leads into the bot's own directory.
        (install / "helper.py").symlink_to(work / "new.py")
        served = Activated(top, uid=bot_uid, program=program)
        try:
            with pytest.raises(Refused, match="lead into the tools account's working directory: .*helper.py points at .*new.py"):
                Link(work=work, user="raven", socket_path=served.path, **deny).open()
        finally:
            served.stop()
            (install / "helper.py").unlink()

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
