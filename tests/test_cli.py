import os
import subprocess
import sys
from pathlib import Path

from persistbot import EXIT_CONFIG, EXIT_OK, store
from persistbot.cli import main
from persistbot.lock import TickLock

from conftest import write_conf_dir

REPO = Path(__file__).parent.parent


def base(tmp_path):
    return tmp_path / "opt" / "raven"


def test_empty_tick_creates_state_and_logs(make_conf, tmp_path):
    assert main([str(make_conf())]) == EXIT_OK
    b = base(tmp_path)
    assert (b / "state.db").exists()
    for name in ("action.log", "chat.log", "report.log"):
        assert (b / "logs" / name).exists()
    assert "nothing to do" in (b / "logs" / "action.log").read_text()
    assert " Raven INFO " in (b / "logs" / "action.log").read_text()


def test_second_start_exits_quietly_without_touching_state(make_conf, tmp_path):
    conf = make_conf()
    b = base(tmp_path)
    b.mkdir(parents=True)
    held = TickLock(b / "bot.lock")
    assert held.acquire()
    try:
        assert main([str(conf), "do", "something"]) == EXIT_OK
        assert not (b / "state.db").exists()
    finally:
        held.release()
    assert main([str(conf)]) == EXIT_OK
    assert (b / "state.db").exists()


def test_lock_is_released_after_a_tick(make_conf, tmp_path):
    main([str(make_conf())])
    lock = TickLock(base(tmp_path) / "bot.lock")
    assert lock.acquire()
    lock.release()


def test_prompt_words_and_file_are_queued_once(make_conf, tmp_path):
    conf = make_conf()
    b = base(tmp_path)
    b.mkdir(parents=True)
    (b / "log_check.txt").write_text("Check /var/log for errors.\n")
    args = [str(conf), "-f", "log_check.txt", "report", "disk", "use"]
    assert main(args) == EXIT_OK
    assert main(args) == EXIT_OK  # same input again: still two goals
    with store.Store(b / "state.db") as db:
        goals = db.goals()
    assert [(g["prompt"], g["state"]) for g in goals] == [
        ("Check /var/log for errors.", "queued"),
        ("report disk use", "queued"),
    ]
    assert goals[0]["source"] == f"file:{b / 'log_check.txt'}"
    assert goals[1]["source"] == "cli"


def test_status(make_conf, tmp_path, capsys):
    conf = make_conf()
    assert main([str(conf), "status"]) == EXIT_OK
    assert "has not run" in capsys.readouterr().out
    assert not base(tmp_path).exists()  # status creates nothing
    main([str(conf), "report disk use"])
    with store.Store(base(tmp_path) / "state.db") as db:
        attempt = db.start_attempt(1)
        db.end_attempt(attempt, store.BUDGET, "12 turns")
    assert main([str(conf), "status"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "goal 1 [queued]" in out and "report disk use" in out
    assert "budget  12 turns" in out


def test_config_errors_exit_2(make_conf, tmp_path, capsys):
    assert main([str(tmp_path / "missing.yaml")]) == EXIT_CONFIG
    assert "cannot read" in capsys.readouterr().err
    assert main([str(make_conf("colour: blue"))]) == EXIT_CONFIG
    assert main([str(make_conf()), "-f", "absent.txt"]) == EXIT_CONFIG
    assert "prompt file" in capsys.readouterr().err


def test_unwritable_base_exits_2(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("")
    conf = tmp_path / "b.yaml"
    conf.write_text(f"bot:\n  venv: {blocker}\n  base: sub\n  name: A\n  persona: P\n")
    assert main([str(conf)]) == EXIT_CONFIG
    assert "cannot start" in capsys.readouterr().err


def test_default_config_is_home_conf_init_yaml(make_conf, tmp_path):
    home = tmp_path / "home"
    (home / "conf").mkdir(parents=True)
    (home / "conf" / "init.yaml").write_text(make_conf().read_text())
    write_conf_dir(home / "conf")
    assert main(["tidy", "up"], home=home) == EXIT_OK
    with store.Store(base(tmp_path) / "state.db") as db:
        assert db.goals()[0]["prompt"] == "tidy up"


def test_launcher_script_runs(make_conf, tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    done = subprocess.run(
        [sys.executable, str(REPO / "bot.py"), str(make_conf())],
        capture_output=True, text=True, cwd=tmp_path, env=env,
    )
    assert done.returncode == 0, done.stderr
    assert (base(tmp_path) / "state.db").exists()


def test_persona_is_resolved_at_startup(make_conf, tmp_path):
    assert main([str(make_conf())]) == EXIT_OK
    text = (base(tmp_path) / "logs" / "action.log").read_text()
    assert "persona Bichon: model edge-3b via openai at http://127.0.0.1:9/v1" in text


def test_unresolvable_persona_exits_2_but_still_queues_the_prompt(make_conf, tmp_path, capsys):
    conf = make_conf()
    (tmp_path / "keys.json").unlink()
    assert main([str(conf), "check", "the", "disk"]) == EXIT_CONFIG
    err = capsys.readouterr().err
    assert "keys.json" in err and str(tmp_path / "examples" / "keys.json") in err
    with store.Store(base(tmp_path) / "state.db") as db:
        assert [g["prompt"] for g in db.goals()] == ["check the disk"]
    lock = TickLock(base(tmp_path) / "bot.lock")
    assert lock.acquire()  # the failed start released it
    lock.release()

    write_conf_dir(tmp_path)  # the operator fixes it; the goal is still there
    assert main([str(conf)]) == EXIT_OK
    with store.Store(base(tmp_path) / "state.db") as db:
        assert len(db.goals()) == 1


def test_unknown_persona_and_unknown_driver_exit_2(make_conf, tmp_path, capsys):
    conf = make_conf()
    write_conf_dir(tmp_path, personas={"Heron": {"model": "m", "provider": "bichon"}})
    assert main([str(conf)]) == EXIT_CONFIG
    assert "persona 'Bichon' is not defined" in capsys.readouterr().err
    write_conf_dir(tmp_path, providers={"bichon": {"driver": "telepathy"}})
    assert main([str(conf)]) == EXIT_CONFIG
    assert "unknown driver 'telepathy'" in capsys.readouterr().err


def test_status_reports_the_resolved_model_or_why_not(make_conf, tmp_path, capsys):
    conf = make_conf()
    assert main([str(conf), "status"]) == EXIT_OK
    assert "model edge-3b  driver openai  url http://127.0.0.1:9/v1" in capsys.readouterr().out
    (tmp_path / "provider.json").unlink()
    assert main([str(conf), "status"]) == EXIT_OK  # status still works for the operator
    assert "persona does not resolve" in capsys.readouterr().out
