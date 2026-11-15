import importlib.util
import json
from pathlib import Path

import pytest

import fakellm as fake
from conftest import write_conf_dir
from persistbot import persona, tools

REPO = Path(__file__).parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("evals_run", REPO / "evals" / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


evals = _load()

TASKS = [
    {"id": "a", "goal": "list files", "expect": ["run_shell"],
     "match": {"run_shell": {"command": "^ls"}}},
    {"id": "b", "goal": "make hello.txt", "expect": ["read_file"], "forbid": ["goal_done"],
     "history": [{"tool": "write_file", "arguments": {"path": "hello.txt", "content": "hi"},
                  "result": "wrote 2 characters"}]},
]


@pytest.fixture
def model(tmp_path, fakellm):
    registry = persona.load(write_conf_dir(tmp_path / "conf", url=fakellm.url))
    return registry.resolve("Raven", "Bichon")


def test_shipped_tasks_load_and_name_only_real_tools():
    tasks = evals.load_tasks(evals.TASKS)
    assert len(tasks) >= 20
    assert any(task.get("forbid") for task in tasks)
    assert any(task.get("history") for task in tasks)
    assert {name for task in tasks for name in task["expect"]} == set(tools.TOOLS)


@pytest.mark.parametrize(
    "task",
    [
        {"id": "x", "goal": "g"},
        {"id": "x", "goal": "g", "expect": []},
        {"id": "x", "goal": "g", "expect": ["no_such_tool"]},
    ],
)
def test_bad_task_files_are_refused(tmp_path, task):
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps([task]))
    with pytest.raises(ValueError):
        evals.load_tasks(path)


def test_each_verdict(model, fakellm):
    fakellm.add(
        fake.tool_call("run_shell", {"command": "ls /tmp"}),       # acted
        fake.text("I would list the files."),                      # silent
        fake.tool_call("run_shell", {"command": "rm -rf /tmp"}),   # wrong: argument pattern
        fake.tool_call("note", {"text": "hmm"}),                   # wrong: not expected
        fake.tool_call("run_shell", "{broken"),                    # invalid
        fake.tool_call("make_coffee", {}),                         # invalid
        fake.tool_call("run_shell", {"command": "  "}),            # invalid: empty argument
    )
    verdicts = [evals.ask(model, TASKS[0])[0] for _ in range(7)]
    assert verdicts == ["acted", "silent", "wrong", "wrong", "invalid", "invalid", "invalid"]
    assert fakellm.unscripted == 0


def test_forbidden_tool_is_wrong_and_history_is_sent(model, fakellm):
    fakellm.add(fake.tool_call("goal_done", {"summary": "done"}))
    assert evals.ask(model, TASKS[1])[0] == "wrong"
    body = fakellm.requests[0]["body"]
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "tool"]
    assert "Your name for this session is Raven." in body["messages"][0]["content"]
    assert body["messages"][2]["tool_calls"][0]["function"]["name"] == "write_file"
    assert body["tools"] == tools.schemas()


def test_retryable_errors_are_retried_then_left_out_of_the_rate(model, fakellm):
    waits = []
    fakellm.add(
        fake.error(429, retry_after=7),
        fake.tool_call("run_shell", {"command": "ls"}),
        fake.error(503), fake.error(503), fake.error(503),
    )
    counts = evals.run(model, [TASKS[0], TASKS[0]], sleep=waits.append)
    assert counts == {"acted": 1, "silent": 0, "wrong": 0, "invalid": 0, "error": 1}
    assert waits[0] == 7
    assert "acted when it should 100% of 1" in evals.summary("Bichon", model, counts)


def test_main_prints_a_rate_per_persona(tmp_path, fakellm, capsys):
    conf = write_conf_dir(tmp_path / "conf", url=fakellm.url)
    (conf / "bot.yaml").write_text(f"bot:\n  venv: {tmp_path}\n  name: Raven\n  persona: Bichon\n")
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps(TASKS))
    fakellm.add(
        fake.tool_call("run_shell", {"command": "ls"}), fake.text("no"),
        fake.tool_call("run_shell", {"command": "ls"}), fake.tool_call("read_file", {"path": "hello.txt"}),
    )
    code = evals.main([str(conf / "bot.yaml"), "-t", str(tasks), "-p", "Bichon", "-p", "Antelope", "-v"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Bichon (edge-3b): acted when it should 50% of 2" in out
    assert "Antelope (big-model): acted when it should 100% of 2" in out
    assert "silent  b" in out


def test_main_stops_a_persona_on_a_fatal_error_and_reports_bad_config(tmp_path, fakellm, capsys):
    conf = write_conf_dir(tmp_path / "conf", url=fakellm.url)
    (conf / "bot.yaml").write_text(f"bot:\n  venv: {tmp_path}\n  name: Raven\n  persona: Bichon\n")
    fakellm.add(fake.error(401, "bad key"))
    assert evals.main([str(conf / "bot.yaml")]) == 1
    assert "provider refused" in capsys.readouterr().out
    assert evals.main([str(conf / "bot.yaml"), "-p", "Nobody"]) == 2
