import json
import os
import time

import pytest

from persistbot import memory, tools
from persistbot.store import Store
from persistbot.tools import files, shell, state


@pytest.fixture
def ctx(tmp_path):
    base = tmp_path / "raven"
    base.mkdir()
    with Store(base / "state.db") as db:
        goal_id, _ = db.add_goal("fix the thing", "cli")
        attempt_id = db.start_attempt(goal_id)
        yield tools.Context(db=db, base=base, goal_id=goal_id, attempt_id=attempt_id, shell_timeout=5)


def run(ctx, name, **args):
    return tools.call(ctx, name, json.dumps(args))


# registry ---------------------------------------------------------------


def test_exactly_the_six_mvp_tools():
    assert list(tools.TOOLS) == [
        "run_shell", "read_file", "write_file", "remember", "note", "goal_done",
    ]
    assert len(tools.schemas()) <= tools.MAX_TOOLS == 6


def test_schemas_are_small_and_well_formed():
    schemas = tools.schemas()
    for schema in schemas:
        function = schema["function"]
        assert schema["type"] == "function"
        assert len(function["description"]) <= 160 and "\n" not in function["description"]
        params = function["parameters"]
        assert params["type"] == "object"
        assert params["required"] == list(params["properties"])
        assert all(p["type"] == "string" for p in params["properties"].values())
    # Roughly 500 tokens: leaves room for the system prompt under the 1,500 budget.
    assert len(json.dumps(schemas)) < 2200


def test_unknown_tool_is_a_malformed_call_naming_the_real_ones(ctx):
    result = tools.call(ctx, "delete_everything", "{}")
    assert not result.ok and result.malformed
    assert "run_shell" in result.text and "goal_done" in result.text


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ("{not json", "not valid JSON"),
        ('["ls"]', "JSON object"),
        ("", "'command' is missing"),
        ("{}", "'command' is missing"),
        ('{"cmd": "ls"}', "'command' is missing"),
        ('{"command": 5}', "must be text"),
        ('{"command": null}', "must be text"),
    ],
)
def test_bad_arguments_are_malformed_not_raised(ctx, arguments, fragment):
    result = tools.call(ctx, "run_shell", arguments)
    assert not result.ok and result.malformed and fragment in result.text


def test_extra_arguments_are_ignored(ctx):
    result = run(ctx, "note", text="kept", confidence="high")
    assert result.ok and not result.malformed


def test_a_handler_that_raises_becomes_a_tool_error(ctx, monkeypatch):
    def boom(ctx, text):
        raise RuntimeError("disk on fire")

    monkeypatch.setitem(
        tools.TOOLS, "note", tools.Tool("note", "x", {"text": "x"}, boom)
    )
    result = run(ctx, "note", text="hello")
    assert not result.ok and not result.malformed
    assert "RuntimeError" in result.text and "disk on fire" in result.text


# run_shell --------------------------------------------------------------


def test_run_shell_returns_exit_code_and_output(ctx):
    (ctx.base / "hello.txt").write_text("hi there\n")
    result = run(ctx, "run_shell", command="cat hello.txt")
    assert result.ok and result.text == "exit code 0\nhi there"


def test_run_shell_runs_in_the_base_directory(ctx):
    result = run(ctx, "run_shell", command="pwd")
    assert result.text.splitlines()[1] == str(ctx.base.resolve())


def test_run_shell_nonzero_exit_is_reported_with_stderr(ctx):
    result = run(ctx, "run_shell", command="cat no-such-file")
    assert not result.ok and not result.malformed
    assert result.text.startswith("exit code 1\n") and "No such file" in result.text


def test_run_shell_no_output(ctx):
    result = run(ctx, "run_shell", command="mkdir sub")
    assert result.ok and result.text == "exit code 0\n(no output)"


@pytest.mark.parametrize(
    "command",
    ["ls\nrm\x20/tmp/x", "cat hello.txt | python3", "cat $(which ls)", "/bin/ls", "sudo ls",
     "python3 -V", "ls; ls", "ls && ls", "cat $HOME/x", "ls > ../out.txt", "ls > state.db",
     "ls 2> err.txt"],
)
def test_run_shell_rejections_are_tool_results(ctx, command):
    result = run(ctx, "run_shell", command=command)
    assert not result.ok and not result.malformed
    assert result.text.startswith("error: command not run:")


def test_run_shell_pipeline(ctx):
    (ctx.base / "f.txt").write_text("b 2\na 1\nb 3\n")
    result = run(ctx, "run_shell", command="cat f.txt | sort | head -2")
    assert result.ok and result.text == "exit code 0\na 1\nb 2"
    result = run(ctx, "run_shell", command="awk '{print $1}' f.txt | sort | uniq -c | sort -rn")
    assert result.ok and result.text.split() == ["exit", "code", "0", "2", "b", "1", "a"]


def test_run_shell_pipeline_reports_the_last_exit_code_and_every_error(ctx):
    (ctx.base / "f.txt").write_text("x\n")
    result = run(ctx, "run_shell", command="cat missing f.txt | grep -c x")
    assert result.ok and "No such file" in result.text and result.text.endswith("1")
    result = run(ctx, "run_shell", command="cat f.txt | grep nothing")
    assert not result.ok and result.text == "exit code 1\n(no output)"


def test_run_shell_stderr_redirects(ctx):
    quiet = run(ctx, "run_shell", command="cat missing 2>/dev/null")
    assert quiet.text == "exit code 1\n(no output)"
    merged = run(ctx, "run_shell", command="cat missing 2>&1 | grep -c 'No such file'")
    assert merged.ok and merged.text == "exit code 0\n1"
    unmerged = run(ctx, "run_shell", command="cat missing | grep -c 'No such file'")
    assert unmerged.text.startswith("exit code 1\n")  # the error bypassed the pipe


def test_run_shell_writes_and_appends_to_a_file_under_base(ctx):
    (ctx.base / "f.txt").write_text("one\n")
    assert run(ctx, "run_shell", command="cat f.txt > sub/out.txt").text == "exit code 0\n(no output)"
    assert run(ctx, "run_shell", command="cat f.txt | sort >> sub/out.txt").ok
    assert (ctx.base / "sub" / "out.txt").read_text() == "one\none\n"
    assert run(ctx, "run_shell", command="cat f.txt > sub/out.txt").ok
    assert (ctx.base / "sub" / "out.txt").read_text() == "one\n"
    assert run(ctx, "run_shell", command="cat f.txt > /dev/null").text == "exit code 0\n(no output)"


def test_run_shell_redirect_cannot_leave_base_or_touch_harness_state(ctx, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    os.link(victim, ctx.base / "alias")
    os.link(ctx.base / "state.db", ctx.base / "alias-db")
    (ctx.base / "link").symlink_to(tmp_path)
    before = (ctx.base / "state.db").read_bytes()
    for target in ("../victim.txt", str(victim), "link/victim.txt", "alias", "alias-db",
                   "state.db", "bot.lock", "."):
        for operator in (">", ">>"):
            result = run(ctx, "run_shell", command=f"pwd {operator} {target}")
            assert not result.ok and result.text.startswith("error: command not run:"), target
    assert victim.read_text() == "keep me"
    assert (ctx.base / "state.db").read_bytes() == before


def test_run_shell_pipeline_with_a_missing_command_runs_nothing(ctx):
    result = run(ctx, "run_shell", command="rm -r . | ls-no-such-command_persistbot")
    assert not result.ok and "command not found" in result.text
    result = run(ctx, "run_shell", command="ls-no-such-command_persistbot | cat > out.txt")
    assert not result.ok and "command not found" in result.text


def test_run_shell_pipeline_is_capped_and_timed(ctx):
    started = time.monotonic()
    result = run(ctx, "run_shell", command="cat /dev/zero | cat")
    assert not result.ok and "cut off" in result.text
    ctx.shell_timeout = 0.5
    result = run(ctx, "run_shell", command="tail -f /dev/null | cat | cat")
    assert not result.ok and "still running after 0.5 seconds" in result.text
    assert time.monotonic() - started < 8


def test_run_shell_does_not_use_a_shell(ctx):
    (ctx.base / "a.txt").write_text("x")
    result = run(ctx, "run_shell", command="ls *.txt")
    assert not result.ok  # no glob expansion: the literal name does not exist
    result = run(ctx, "run_shell", command="ls ~")
    assert not result.ok


def test_run_shell_missing_command_is_a_tool_error(ctx):
    # Allowed by the `ls*` wildcard, and certain not to be installed. `umask`
    # is no use here: some systems (Fedora) ship /usr/bin/umask, which runs
    # and changes nothing.
    result = run(ctx, "run_shell", command="ls-no-such-command_persistbot -x")
    assert not result.ok and not result.malformed
    assert "command not found" in result.text and "ls-no-such-command_persistbot" in result.text


def test_run_shell_output_is_capped_and_the_command_stopped(ctx):
    (ctx.base / "big.txt").write_text("0123456789\n" * 100_000)
    started = time.monotonic()
    result = run(ctx, "run_shell", command="cat big.txt")
    assert not result.ok and "cut off" in result.text
    assert len(result.text) < shell.OUTPUT_CAP + 200
    assert time.monotonic() - started < 5


def test_run_shell_times_out(ctx):
    ctx.shell_timeout = 0.5
    started = time.monotonic()
    result = run(ctx, "run_shell", command="tail -f /dev/null")
    assert not result.ok and "still running after 0.5 seconds" in result.text
    assert time.monotonic() - started < 5


def test_run_shell_hides_the_harness_environment(ctx, monkeypatch):
    monkeypatch.setenv("KEY", "sk-secret-value")
    result = run(ctx, "run_shell", command="awk 'BEGIN { print ENVIRON[\"KEY\"] \"#\" ENVIRON[\"PATH\"] }'")
    assert result.ok and "sk-secret-value" not in result.text
    assert os.environ["PATH"] in result.text


# read_file --------------------------------------------------------------


def test_read_file_relative_to_base_and_absolute(ctx, tmp_path):
    (ctx.base / "notes.txt").write_text("inside")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    assert run(ctx, "read_file", path="notes.txt").text == "inside"
    result = run(ctx, "read_file", path=str(outside))  # reads may leave the base
    assert result.ok and result.text == "outside"


def test_read_file_caps_long_files(ctx):
    (ctx.base / "long.txt").write_text("x" * (files.READ_CAP * 10))
    result = run(ctx, "read_file", path="long.txt")
    assert result.ok and "cut off after" in result.text
    assert len(result.text) < files.READ_CAP + 100


@pytest.mark.parametrize("path", ["missing.txt", "/dev/zero", "", "."])
def test_read_file_errors_are_tool_results(ctx, path):
    result = run(ctx, "read_file", path=path)
    assert not result.ok and result.text.startswith("error:")


def test_read_file_refuses_binary_and_reports_empty(ctx):
    (ctx.base / "blob").write_bytes(b"\x7fELF\x00\x00")
    (ctx.base / "empty").write_text("")
    assert "not a text file" in run(ctx, "read_file", path="blob").text
    assert run(ctx, "read_file", path="empty").text == "(the file is empty)"


# write_file -------------------------------------------------------------


def test_write_file_under_base_creates_directories(ctx):
    result = run(ctx, "write_file", path="scripts/fix.sh", content="#!/bin/sh\necho ok\n")
    assert result.ok
    assert (ctx.base / "scripts" / "fix.sh").read_text() == "#!/bin/sh\necho ok\n"
    absolute = ctx.base / "abs.txt"
    assert run(ctx, "write_file", path=str(absolute), content="a").ok
    assert absolute.read_text() == "a"


def test_write_file_refuses_paths_that_escape_the_base(ctx, tmp_path):
    victim = tmp_path / "victim.txt"
    sibling = tmp_path / "raven-evil" / "x.txt"  # shares the base's name as a prefix
    (ctx.base / "link").symlink_to(tmp_path)
    (ctx.base / "filelink").symlink_to(victim)
    for path in (
        "../victim.txt",
        "sub/../../victim.txt",
        str(victim),
        "/etc/persistbot-test",
        "link/victim.txt",
        "filelink",
        str(sibling),
    ):
        result = run(ctx, "write_file", path=path, content="pwned")
        assert not result.ok and "only under" in result.text, path
    assert not victim.exists() and not sibling.exists()


@pytest.mark.parametrize("name", ["state.db", "state.db-wal", "bot.lock"])
def test_write_file_refuses_harness_state(ctx, name):
    result = run(ctx, "write_file", path=name, content="x")
    assert not result.ok and "belongs to the harness" in result.text


def test_write_file_refuses_directories_and_oversize(ctx):
    (ctx.base / "dir").mkdir()
    assert "is a directory" in run(ctx, "write_file", path="dir", content="x").text
    assert "is a directory" in run(ctx, "write_file", path=".", content="x").text
    big = run(ctx, "write_file", path="big", content="x" * (files.WRITE_CAP + 1))
    assert not big.ok and not (ctx.base / "big").exists()


def test_write_file_through_a_hard_link_leaves_the_other_name_alone(ctx, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me")
    os.link(outside, ctx.base / "alias-out")
    os.link(ctx.base / "state.db", ctx.base / "alias-db")
    before = (ctx.base / "state.db").read_bytes()
    assert run(ctx, "write_file", path="alias-out", content="new").ok
    assert run(ctx, "write_file", path="alias-db", content="new").ok
    assert outside.read_text() == "keep me"
    assert (ctx.base / "state.db").read_bytes() == before
    assert (ctx.base / "alias-out").read_text() == "new"
    assert ctx.db.goal(ctx.goal_id) is not None  # the database still works
    assert not list(ctx.base.glob(".write-*"))


def test_write_file_keeps_the_mode_of_a_file_it_replaces(ctx):
    script = ctx.base / "fix.sh"
    script.write_text("old")
    script.chmod(0o750)
    assert run(ctx, "write_file", path="fix.sh", content="new").ok
    assert script.read_text() == "new" and script.stat().st_mode & 0o777 == 0o750
    assert run(ctx, "write_file", path="plain.txt", content="x").ok
    umask = os.umask(0)
    os.umask(umask)
    assert (ctx.base / "plain.txt").stat().st_mode & 0o777 == 0o666 & ~umask


# remember, note, goal_done ----------------------------------------------


def test_remember_stores_a_memory_row_once(ctx):
    assert run(ctx, "remember", text="  logs live in\n/var/log/app  ").text == "remembered"
    assert run(ctx, "remember", text="logs live in /var/log/app").text == "already remembered"
    rows = ctx.db.memories()
    assert [(r["kind"], r["text"]) for r in rows] == [("memory", "logs live in /var/log/app")]


def test_remember_refuses_empty_and_long_text(ctx):
    assert not run(ctx, "remember", text="   ").ok
    assert not run(ctx, "remember", text="x" * (state.FACT_CAP + 1)).ok
    assert ctx.db.memories() == []


def test_note_is_attached_to_the_goal_and_attempt(ctx):
    assert run(ctx, "note", text="tried restarting; no change").ok
    (row,) = ctx.db.notes(ctx.goal_id)
    assert row["text"] == "tried restarting; no change"
    assert row["attempt_id"] == ctx.attempt_id and row["kind"] == "note"
    assert not run(ctx, "note", text="").ok
    long = run(ctx, "note", text="y" * (state.NOTE_CAP + 50))
    assert long.ok and "cut to" in long.text
    assert len(ctx.db.notes(ctx.goal_id)[-1]["text"]) == state.NOTE_CAP


def test_goal_done_signals_the_tick_and_leaves_the_goal_open(ctx):
    result = run(ctx, "goal_done", summary=" restarted app; port 80 answers ")
    assert result.ok and result.done == "restarted app; port 80 answers"
    assert ctx.db.goal(ctx.goal_id)["state"] == "queued"  # only the tick closes a goal


def test_goal_done_without_a_summary_does_not_finish(ctx):
    for arguments in ('{"summary": "  "}', "{}"):
        result = tools.call(ctx, "goal_done", arguments)
        assert not result.ok and result.done is None


def test_only_goal_done_sets_done(ctx):
    (ctx.base / "f").write_text("x")
    calls = [
        ("run_shell", {"command": "ls"}),
        ("read_file", {"path": "f"}),
        ("write_file", {"path": "g", "content": "x"}),
        ("remember", {"text": "a fact"}),
        ("note", {"text": "a note"}),
    ]
    for name, args in calls:
        result = run(ctx, name, **args)
        assert result.ok and result.done is None, name


# memory in the prompt ---------------------------------------------------


def test_memory_block_is_newest_first_and_capped(ctx):
    assert memory.prompt_block(ctx.db) == ""
    for number in range(5):
        run(ctx, "remember", text=f"fact number {number} " + "z" * 40)
    block = memory.prompt_block(ctx.db, max_chars=180)
    lines = block.splitlines()[1:]
    assert [line.split()[3] for line in lines] == ["4", "3"]
    assert len(block) <= 180  # the cap covers the heading and bullets too
    assert memory.prompt_block(ctx.db, max_chars=60) == ""
    assert "fact number 0" in memory.prompt_block(ctx.db)


def test_memory_block_ignores_other_kinds(ctx):
    ctx.db.remember("Antelope was right about cron", kind="gossip")
    run(ctx, "remember", text="cron runs as root here")
    block = memory.prompt_block(ctx.db)
    assert "cron runs as root" in block and "Antelope" not in block
