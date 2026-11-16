"""The MVP's acceptance tests: a tick against the scripted model, with faults."""

import json
import time

import pytest

import fakellm as fake
from persistbot import EXIT_CONFIG, EXIT_FAILED, EXIT_OK, EXIT_TEMPFAIL, store, tick, tools
from persistbot.cli import main

GOAL = "Create hello.txt containing hello."


class Bot:
    def __init__(self, conf, tmp_path, llm):
        self.conf, self.llm = conf, llm
        self.base = tmp_path / "opt" / "raven"
        self.work = self.base / "work"
        self.venv = tmp_path / "opt"

    def tick(self, *words):
        return main([str(self.conf), *words])

    def db(self):
        return store.Store(self.base / "state.db")

    def goal(self, goal_id=1):
        with self.db() as db:
            return dict(db.goal(goal_id))

    def attempts(self, goal_id=1):
        with self.db() as db:
            return [dict(row) for row in db.attempts(goal_id)]

    def due(self, goal_id=1):
        """Let the backoff pass."""
        with self.db() as db:
            db.update_goal(goal_id, not_before=0)

    def log(self, name):
        return (self.base / "logs" / name).read_text()

    def sent(self, index=-1):
        """The messages of one request to the model."""
        return self.llm.requests[index]["body"]["messages"]


@pytest.fixture
def bot(make_conf, tmp_path, fakellm):
    conf = make_conf("limits: { max_turns: 4, max_failures: 3 }\n", url=fakellm.url)
    return Bot(conf, tmp_path, fakellm)


def call(name, **args):
    return fake.tool_call(name, args)


def happy_path():
    return (
        call("write_file", path="hello.txt", content="hello\n"),
        call("read_file", path="hello.txt"),
        call("goal_done", summary="wrote hello.txt and read it back"),
    )


# the plain loop ----------------------------------------------------------


def test_a_goal_runs_to_goal_done(bot):
    bot.llm.add(*happy_path())
    assert bot.tick(GOAL) == EXIT_OK
    assert (bot.work / "hello.txt").read_text() == "hello\n"
    goal = bot.goal()
    assert goal["state"] == "done" and goal["summary"] == "wrote hello.txt and read it back"
    (attempt,) = bot.attempts()
    assert attempt["outcome"] == "completed" and attempt["ended_at"] is not None
    with bot.db() as db:
        calls = db.tool_calls(attempt["id"])
    assert [c["name"] for c in calls] == ["write_file", "read_file", "goal_done"]
    assert all(c["ended_at"] is not None and c["ok"] == 1 for c in calls)
    assert "goal 1 done: wrote hello.txt" in bot.log("report.log")
    assert bot.llm.unscripted == 0
    assert bot.tick() == EXIT_OK  # nothing left: the model is not called again
    assert len(bot.llm.requests) == 3


def test_the_request_carries_the_prompt_the_tools_and_the_tool_results(bot):
    (bot.base).mkdir(parents=True)
    (bot.base / "system.txt").write_text("This node is a print server.")
    bot.llm.add(*happy_path())
    bot.tick(GOAL)
    first = bot.llm.requests[0]["body"]
    assert first["tools"] == tools.schemas() and len(first["tools"]) <= 6
    system, user = first["messages"]
    assert system["role"] == "system"
    assert system["content"].startswith("You are a test agent. Use tools.")
    assert "This node is a print server." in system["content"]
    assert system["content"].endswith("Your name for this session is Raven.")
    assert user == {"role": "user", "content": f"Goal: {GOAL}"}
    last = bot.sent(-1)
    assert [m["role"] for m in last] == ["system", "user", "assistant", "tool", "assistant", "tool"]
    assert last[3]["content"].startswith("wrote 6 characters") and last[5]["content"] == "hello\n"
    assert last[3]["tool_call_id"] == last[2]["tool_calls"][0]["id"]


def test_the_transcript_is_written_to_chat_log(bot):
    bot.llm.add(*happy_path())
    bot.tick(GOAL)
    chat = bot.log("chat.log")
    for fragment in ("[system] You are a test agent", f"[user] Goal: {GOAL}", "[assistant] ",
                     '"name": "write_file"', "[tool] ", "wrote 6 characters"):
        assert fragment in chat


def test_only_goal_done_closes_a_goal(bot):
    # The model says it is finished, in words. That closes nothing.
    bot.llm.add(call("write_file", path="hello.txt", content="hello\n"),
                fake.text("All done! The file has been created."),
                fake.text("The task is complete."),
                fake.text("Finished."))
    assert bot.tick(GOAL) == EXIT_FAILED
    assert bot.goal()["state"] == "active"
    assert bot.attempts()[0]["outcome"] == "invalid"
    nudges = [m for m in bot.sent(-1) if m["role"] == "user"][1:]
    assert nudges and all("did not call a tool" in m["content"] for m in nudges)


def test_goal_done_with_an_empty_summary_does_not_close_the_goal(bot):
    bot.llm.add(call("goal_done", summary=" "), call("goal_done", summary="checked it"))
    assert bot.tick(GOAL) == EXIT_OK
    assert bot.goal()["summary"] == "checked it"
    assert len(bot.llm.requests) == 2


def test_one_goal_per_tick_active_before_queued(bot):
    bot.llm.add(call("note", text="started"), fake.error(503))
    assert bot.tick("first goal") == EXIT_TEMPFAIL
    bot.due(1)
    bot.llm.add(call("goal_done", summary="first finished"))
    assert bot.tick("second goal") == EXIT_OK
    assert (bot.goal(1)["state"], bot.goal(2)["state"]) == ("done", "queued")
    bot.llm.add(call("goal_done", summary="second finished"))
    assert bot.tick() == EXIT_OK
    assert bot.goal(2)["state"] == "done"


def test_at_most_four_tool_calls_run_from_one_reply(bot):
    many = [("write_file", {"path": f"f{n}.txt", "content": "x"}) for n in range(6)]
    bot.llm.add(fake.tool_calls(many), call("goal_done", summary="wrote what was allowed"))
    assert bot.tick(GOAL) == EXIT_OK
    assert sorted(p.name for p in bot.work.glob("f*.txt")) == ["f0.txt", "f1.txt", "f2.txt", "f3.txt"]
    with bot.db() as db:
        assert len(db.tool_calls(1)) == 5  # four writes and goal_done
    answers = [m for m in bot.sent(-1) if m["role"] == "tool"]
    assert len(answers) == 6  # every call is answered, run or not
    assert all("at most 4 tool calls" in m["content"] for m in answers[4:])


def test_nothing_runs_after_goal_done_in_the_same_reply(bot):
    bot.llm.add(fake.tool_calls([("goal_done", {"summary": "finished"}),
                                 ("write_file", {"path": "late.txt", "content": "x"})]))
    assert bot.tick(GOAL) == EXIT_OK
    assert bot.goal()["state"] == "done" and not (bot.work / "late.txt").exists()


# upstream errors: deferral, never failure --------------------------------


def test_429_on_the_first_call_defers_and_the_next_tick_completes(bot):
    bot.llm.add(fake.error(429, retry_after=120))
    before = time.time()
    assert bot.tick(GOAL) == EXIT_TEMPFAIL
    goal = bot.goal()
    assert goal["state"] == "active" and goal["failures"] == 0 and goal["deferrals"] == 1
    assert before + 119 <= goal["not_before"] <= time.time() + 121  # Retry-After honoured
    assert bot.attempts()[0]["outcome"] == "deferred"

    assert bot.tick() == EXIT_OK  # before the backoff: no model call at all
    assert len(bot.llm.requests) == 1 and "waiting" in bot.log("action.log")

    bot.due()
    bot.llm.add(*happy_path())
    assert bot.tick() == EXIT_OK
    goal = bot.goal()
    assert goal["state"] == "done" and goal["failures"] == 0
    assert [a["outcome"] for a in bot.attempts()] == ["deferred", "completed"]


def test_429_mid_loop_keeps_the_journal_and_summarises_it_to_the_next_attempt(bot):
    bot.llm.add(call("write_file", path="hello.txt", content="hello\n"),
                call("note", text="file written; still to verify"),
                fake.error(429))
    assert bot.tick(GOAL) == EXIT_TEMPFAIL
    (attempt,) = bot.attempts()
    with bot.db() as db:
        journal = db.tool_calls(attempt["id"])
    assert [(c["name"], c["ok"]) for c in journal] == [("write_file", 1), ("note", 1)]
    assert bot.goal()["failures"] == 0

    bot.due()
    bot.llm.add(call("read_file", path="hello.txt"), call("goal_done", summary="verified"))
    assert bot.tick() == EXIT_OK
    user = bot.sent(3)[1]["content"]  # first request of the second attempt
    assert user.startswith(f"Goal: {GOAL}")
    assert "- file written; still to verify" in user
    assert "Attempt 1 ended as 'deferred'" in user and "HTTP 429" in user
    assert 'write_file {"path": "hello.txt"' in user and "-> wrote 6 characters" in user
    assert len(bot.sent(3)) == 2  # a fresh start: the old transcript is not replayed


@pytest.mark.parametrize("step", [fake.error(500), fake.error(503), fake.error(408), fake.drop()])
def test_every_retryable_error_is_a_deferral(bot, step):
    bot.llm.add(step)
    assert bot.tick(GOAL) == EXIT_TEMPFAIL
    goal = bot.goal()
    assert (goal["state"], goal["failures"], goal["deferrals"]) == ("active", 0, 1)


def test_deferrals_alone_never_make_a_goal_stuck_and_back_off(bot):
    waits = []
    for _ in range(8):
        bot.llm.add(fake.error(503))
        assert bot.tick(*([GOAL] if not waits else [])) == EXIT_TEMPFAIL
        waits.append(bot.goal()["not_before"] - time.time())
        bot.due()
    goal = bot.goal()
    assert goal["state"] == "active" and goal["failures"] == 0 and goal["deferrals"] == 8
    assert [round(w / 300) for w in waits] == [1, 2, 4, 8, 12, 12, 12, 12]  # 5 min doubling, 1 h cap
    assert "stuck" not in bot.log("report.log")


def test_a_fatal_upstream_error_defers_long_and_reports_without_burning_the_goal(bot):
    bot.llm.add(fake.error(401, "bad key"))
    assert bot.tick(GOAL) == EXIT_TEMPFAIL
    goal = bot.goal()
    assert goal["state"] == "active" and goal["failures"] == 0
    assert goal["not_before"] - time.time() > 3500
    report = bot.log("report.log")
    assert "goal 1 deferred" in report and "bad key" in report and "check the key" in report


def test_a_deferral_does_not_erase_earlier_failures_and_success_resets_backoff(bot):
    bot.llm.add(*[fake.text("hm")] * 3)
    assert bot.tick(GOAL) == EXIT_FAILED
    bot.llm.add(fake.error(503))
    assert bot.tick() == EXIT_TEMPFAIL
    goal = bot.goal()
    assert (goal["failures"], goal["deferrals"]) == (1, 1)
    bot.due()
    bot.llm.add(*[fake.text("hm")] * 3)
    assert bot.tick() == EXIT_FAILED
    assert (bot.goal()["failures"], bot.goal()["deferrals"]) == (2, 0)


# counted failures --------------------------------------------------------


def test_three_budget_outcomes_make_the_goal_stuck(bot):
    for number in (1, 2, 3):
        bot.llm.add(*[call("run_shell", command="ls")] * 4)  # max_turns is 4
        assert bot.tick(*([GOAL] if number == 1 else [])) == EXIT_FAILED
        assert bot.goal()["failures"] == number
    goal = bot.goal()
    assert goal["state"] == "stuck"
    assert [a["outcome"] for a in bot.attempts()] == ["budget"] * 3
    report = bot.log("report.log")
    assert "attempt 1: budget, turn limit of 4 reached (failure 1 of 3)" in report
    assert "goal 1 is stuck after 3 failed attempts" in report and "needs an operator" in report

    requests = len(bot.llm.requests)
    assert bot.tick() == EXIT_OK  # a stuck goal is left alone
    assert bot.tick() == EXIT_OK
    assert len(bot.llm.requests) == requests == 12 and bot.llm.unscripted == 0


def test_malformed_tool_calls_end_the_attempt_as_invalid(bot):
    bot.llm.add(fake.tool_call("run_shell", "{not json"),
                fake.tool_call("launch_missiles", {}),
                fake.tool_call("read_file", {}))
    assert bot.tick(GOAL) == EXIT_FAILED
    (attempt,) = bot.attempts()
    assert attempt["outcome"] == "invalid" and bot.goal()["failures"] == 1
    tool_messages = [m["content"] for m in bot.sent(-1) if m["role"] == "tool"]
    assert "not valid JSON" in tool_messages[0] and "no tool named" in tool_messages[1]


def test_a_tool_error_is_an_answer_not_a_failure(bot):
    bot.llm.add(call("run_shell", command="python3 -V"),
                call("read_file", path="missing.txt"),
                call("goal_done", summary="nothing to do"))
    assert bot.tick(GOAL) == EXIT_OK
    assert bot.goal()["failures"] == 0
    with bot.db() as db:
        assert [c["ok"] for c in db.tool_calls(1)] == [0, 0, 1]


# a tick that dies --------------------------------------------------------


def test_killed_mid_tool_is_recorded_as_crashed_and_the_next_attempt_is_told(bot, monkeypatch):
    def die(ctx, command):
        raise KeyboardInterrupt  # stands in for SIGKILL: nothing after this runs

    real = tools.TOOLS["run_shell"]
    monkeypatch.setitem(tools.TOOLS, "run_shell",
                        tools.Tool(real.name, real.description, real.arguments, die))
    bot.llm.add(call("note", text="about to restart the service"),
                call("run_shell", command="kill -HUP 4242"))
    with pytest.raises(KeyboardInterrupt):
        bot.tick(GOAL)
    (attempt,) = bot.attempts()
    assert attempt["ended_at"] is None and attempt["outcome"] is None
    with bot.db() as db:
        journal = db.tool_calls(attempt["id"])
    assert journal[-1]["name"] == "run_shell" and journal[-1]["ended_at"] is None

    monkeypatch.setitem(tools.TOOLS, "run_shell", real)
    bot.llm.add(call("run_shell", command="ps aux"), call("goal_done", summary="it had restarted"))
    assert bot.tick() == EXIT_OK  # the lock was released, and work continues
    first, second = bot.attempts()
    assert first["outcome"] == "crashed" and second["outcome"] == "completed"
    assert bot.goal()["failures"] == 1 and bot.goal()["state"] == "done"
    user = bot.sent(2)[1]["content"]
    assert "Attempt 1 ended as 'crashed'" in user
    assert 'run_shell {"command": "kill -HUP 4242"} -> STARTED, OUTCOME UNKNOWN' in user
    assert "Inspect before repeating" in user and "- about to restart the service" in user


def test_three_crashes_make_the_goal_stuck_without_calling_the_model(bot):
    with bot.db() as db:  # two failures so far, and a third attempt that died
        goal_id, _ = db.add_goal(GOAL, "cli")
        db.update_goal(goal_id, state="active", failures=2)
        db.start_attempt(goal_id)
    assert bot.tick() == EXIT_OK
    assert bot.goal()["state"] == "stuck" and bot.llm.requests == []


# memory, notes, input ----------------------------------------------------


def test_memory_written_in_one_tick_is_in_the_next_ticks_prompt(bot):
    bot.llm.add(call("remember", text="the app logs to /srv/app/logs"),
                call("goal_done", summary="learned where logs are"))
    assert bot.tick("Find where the app logs.") == EXIT_OK
    assert "What you have learned" not in bot.sent(0)[0]["content"]
    bot.llm.add(call("goal_done", summary="ok"))
    assert bot.tick("A different goal.") == EXIT_OK
    system = bot.sent(-1)[0]["content"]
    assert "What you have learned about this node" in system
    assert "- the app logs to /srv/app/logs" in system
    assert "Find where the app logs" not in bot.sent(-1)[1]["content"]  # notes are per goal


def test_incoming_files_become_goals_and_move_to_done(bot):
    incoming = bot.base / "incoming"
    incoming.mkdir(parents=True)
    (incoming / "b-second.txt").write_text("Second task.\n")
    (incoming / "a-first.txt").write_text("First task.\n")
    (incoming / ".hidden").write_text("not a goal")
    (incoming / "empty.txt").write_text("  \n")
    bot.llm.add(call("goal_done", summary="one"))
    assert bot.tick() == EXIT_OK
    with bot.db() as db:
        goals = [(g["prompt"], g["source"], g["state"]) for g in db.goals()]
    assert goals == [("First task.", "incoming:a-first.txt", "done"),
                     ("Second task.", "incoming:b-second.txt", "queued")]
    assert sorted(p.name for p in (incoming / "done").iterdir()) == [
        "a-first.txt", "b-second.txt", "empty.txt"]
    assert sorted(p.name for p in incoming.iterdir()) == [".hidden", "done"]


def test_the_same_incoming_file_delivered_twice_produces_one_goal(bot):
    incoming = bot.base / "incoming"
    incoming.mkdir(parents=True)
    for _ in range(2):
        (incoming / "task.txt").write_text("Rotate the logs.\n")
        bot.llm.add(fake.error(503))
        assert bot.tick() == EXIT_TEMPFAIL
        bot.due()
    with bot.db() as db:
        assert [g["prompt"] for g in db.goals()] == ["Rotate the logs."]
    assert len(list((incoming / "done").iterdir())) == 2  # both deliveries kept


def test_a_crash_between_queueing_and_moving_an_incoming_file_is_harmless(bot):
    incoming = bot.base / "incoming"
    incoming.mkdir(parents=True)
    with bot.db() as db:  # the earlier tick queued it, then died before the move
        db.add_goal("Rotate the logs.", "incoming:task.txt")
    (incoming / "task.txt").write_text("Rotate the logs.\n")
    bot.llm.add(call("goal_done", summary="rotated"))
    assert bot.tick() == EXIT_OK
    with bot.db() as db:
        assert len(db.goals()) == 1
    assert not (incoming / "task.txt").exists()


# startup problems leave the goal untouched --------------------------------


def test_missing_system_txt_is_a_config_error_and_costs_the_goal_nothing(bot):
    (bot.venv / "system.txt").unlink()
    assert bot.tick(GOAL) == EXIT_CONFIG
    goal = bot.goal()
    assert goal["state"] == "queued" and goal["failures"] == 0 and bot.attempts() == []
    assert bot.llm.requests == []
    (bot.venv / "system.txt").write_text("Use tools.")
    bot.llm.add(call("goal_done", summary="ok"))
    assert bot.tick() == EXIT_OK


def test_the_shipped_system_prompt_and_schemas_fit_the_budget():
    from pathlib import Path

    text = (Path(__file__).parent.parent / "system.txt").read_text()
    for needle in ("later runs", "note", "goal_done", "check"):
        assert needle in text
    # About 1,500 tokens for prompt plus schemas; four characters per token.
    assert len(text) + len(json.dumps(tools.schemas())) < 6000
    assert tick.NOTES_CHARS + 1500 < 6000  # notes and memory stay small too


def test_a_blank_prompt_on_the_command_line_is_ignored(bot):
    assert bot.tick("", "  ") == EXIT_OK
    with bot.db() as db:
        assert db.goals() == []
