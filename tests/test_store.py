import sqlite3

import pytest

from persistbot import store
from persistbot.store import Store, StoreError


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def db(tmp_path, clock):
    with Store(tmp_path / "state.db", clock=clock) as s:
        yield s


def test_creates_schema_in_wal_mode(db):
    assert db.version == len(store.MIGRATIONS)
    assert db.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {r[0] for r in db.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"goal", "attempt", "tool_call", "note", "memory", "meta"} <= tables


def test_reopen_keeps_data_and_does_not_rerun_migrations(tmp_path):
    path = tmp_path / "state.db"
    with Store(path) as s:
        s.add_goal("check the logs", "cli")
    with Store(path) as s:
        assert len(s.goals()) == 1


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "state.db"
    Store(path).close()
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA user_version=99")
    raw.commit()
    raw.close()
    with pytest.raises(StoreError, match="schema version 99"):
        Store(path)


def test_add_goal_dedupes_only_while_open(db):
    first, created = db.add_goal("check the logs\n", "cli")
    assert created
    assert db.add_goal("  check the logs", "file:x") == (first, False)
    db.update_goal(first, state=store.DONE, summary="fine")
    second, created = db.add_goal("check the logs", "cli")
    assert created and second != first
    with pytest.raises(ValueError):
        db.add_goal("   ", "cli")


def test_next_goal_prefers_active_then_oldest_and_honours_not_before(db, clock):
    a, _ = db.add_goal("a", "cli")
    b, _ = db.add_goal("b", "cli")
    assert db.next_goal()["id"] == a
    db.update_goal(b, state=store.ACTIVE)
    assert db.next_goal()["id"] == b
    db.update_goal(b, not_before=clock.now + 300)
    assert db.next_goal()["id"] == a
    db.update_goal(a, state=store.STUCK)
    assert db.next_goal() is None
    clock.now += 301
    assert db.next_goal()["id"] == b


def test_update_goal_validates(db):
    g, _ = db.add_goal("a", "cli")
    with pytest.raises(ValueError):
        db.update_goal(g, state="finished")
    with pytest.raises(ValueError):
        db.update_goal(g, prompt="other")


def test_attempt_and_tool_journal_survive_an_abrupt_close(tmp_path):
    path = tmp_path / "state.db"
    s = Store(path)
    g, _ = s.add_goal("a", "cli")
    attempt = s.start_attempt(g)
    done = s.begin_tool_call(attempt, "run_shell", '{"command": "ls"}')
    s.finish_tool_call(done, True, "file")
    s.begin_tool_call(attempt, "run_shell", '{"command": "rm x"}')
    # No close(): the process is gone. A fresh connection must see it all.
    with Store(path) as again:
        (left_open,) = again.open_attempts()
        assert left_open["id"] == attempt and left_open["outcome"] is None
        first, second = again.tool_calls(attempt)
        assert (first["ok"], first["result"]) == (1, "file")
        assert second["ended_at"] is None and second["ok"] is None
        again.end_attempt(attempt, store.CRASHED, "tick died")
        assert again.open_attempts() == []
        assert again.attempts(g)[0]["outcome"] == "crashed"
        with pytest.raises(ValueError):
            again.end_attempt(attempt, "exploded")
    s.close()


def test_notes_memory_and_meta(db):
    g, _ = db.add_goal("a", "cli")
    db.add_note(g, "disk is /dev/vdb ")
    db.add_note(g, "try fsck", kind="advice")
    assert [(n["kind"], n["text"]) for n in db.notes(g)] == [
        ("note", "disk is /dev/vdb"),
        ("advice", "try fsck"),
    ]
    db.remember("old fact")
    db.remember("new fact")
    db.remember("Antelope is terse", kind="gossip")
    assert [m["text"] for m in db.memories()] == ["new fact", "old fact"]
    assert [m["text"] for m in db.memories(max_chars=10)] == ["new fact"]
    assert [m["text"] for m in db.memories("gossip")] == ["Antelope is terse"]
    assert db.get_meta("k") is None
    db.set_meta("k", "1")
    db.set_meta("k", "2")
    assert db.get_meta("k") == "2"


def test_close_attempt_ends_the_attempt_and_updates_the_goal_in_one_commit(tmp_path):
    with Store(tmp_path / "s.db") as db:
        goal_id, _ = db.add_goal("fix it", "cli")
        attempt = db.start_attempt(goal_id)
        seen = []
        db.db.set_trace_callback(seen.append)
        db.close_attempt(attempt, "completed", "all good", state="done", summary="all good")
        db.db.set_trace_callback(None)
        writes = [line.split()[0] for line in seen if line.split()[0] in ("BEGIN", "UPDATE", "COMMIT")]
        assert writes == ["BEGIN", "UPDATE", "UPDATE", "COMMIT"]
        assert db.attempts(goal_id)[0]["outcome"] == "completed"
        assert db.goal(goal_id)["state"] == "done" and db.open_attempts() == []


def test_close_attempt_writes_nothing_when_the_goal_update_is_refused(tmp_path):
    import pytest

    with Store(tmp_path / "s.db") as db:
        goal_id, _ = db.add_goal("fix it", "cli")
        attempt = db.start_attempt(goal_id)
        for bad in ({"state": "finished"}, {"prompt": "x"}):
            with pytest.raises(ValueError):
                db.close_attempt(attempt, "completed", "x", **bad)
        with pytest.raises(ValueError):
            db.close_attempt(attempt, "exploded")
        with pytest.raises(ValueError):
            db.close_attempt(999, "completed")
        assert db.attempts(goal_id)[0]["ended_at"] is None
        assert db.goal(goal_id)["state"] == "queued"
