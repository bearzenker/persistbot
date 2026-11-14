"""SQLite state for one bot: goals, attempts, tool journal, notes, memory.

Times are Unix seconds. Every helper commits before returning, so a tick
that dies leaves whatever it had already recorded.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Callable

# Goal states
QUEUED, ACTIVE, DONE, STUCK, ABANDONED = "queued", "active", "done", "stuck", "abandoned"
OPEN_STATES = (QUEUED, ACTIVE)
GOAL_STATES = (QUEUED, ACTIVE, DONE, STUCK, ABANDONED)

# Attempt outcomes
COMPLETED, DEFERRED, BUDGET, INVALID, CRASHED = (
    "completed", "deferred", "budget", "invalid", "crashed",
)
OUTCOMES = (COMPLETED, DEFERRED, BUDGET, INVALID, CRASHED)
FAILURES = (BUDGET, INVALID, CRASHED)


class StoreError(Exception):
    """The state database cannot be used as it is."""


def _v1(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE goal (
            id         INTEGER PRIMARY KEY,
            digest     TEXT NOT NULL,
            source     TEXT NOT NULL,
            prompt     TEXT NOT NULL,
            state      TEXT NOT NULL DEFAULT 'queued',
            failures   INTEGER NOT NULL DEFAULT 0,
            deferrals  INTEGER NOT NULL DEFAULT 0,
            not_before REAL NOT NULL DEFAULT 0,
            summary    TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX goal_state ON goal (state, id);
        CREATE INDEX goal_digest ON goal (digest);
        CREATE TABLE attempt (
            id         INTEGER PRIMARY KEY,
            goal_id    INTEGER NOT NULL REFERENCES goal (id),
            started_at REAL NOT NULL,
            ended_at   REAL,
            outcome    TEXT,
            detail     TEXT
        );
        CREATE INDEX attempt_goal ON attempt (goal_id, id);
        CREATE TABLE tool_call (
            id         INTEGER PRIMARY KEY,
            attempt_id INTEGER NOT NULL REFERENCES attempt (id),
            name       TEXT NOT NULL,
            arguments  TEXT NOT NULL,
            started_at REAL NOT NULL,
            ended_at   REAL,
            ok         INTEGER,
            result     TEXT
        );
        CREATE INDEX tool_call_attempt ON tool_call (attempt_id, id);
        CREATE TABLE note (
            id         INTEGER PRIMARY KEY,
            goal_id    INTEGER NOT NULL REFERENCES goal (id),
            attempt_id INTEGER REFERENCES attempt (id),
            kind       TEXT NOT NULL DEFAULT 'note',
            text       TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX note_goal ON note (goal_id, id);
        CREATE TABLE memory (
            id         INTEGER PRIMARY KEY,
            kind       TEXT NOT NULL DEFAULT 'memory',
            text       TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX memory_kind ON memory (kind, id);
        """
    )


# Append only. Index + 1 is the schema version.
MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [_v1]


def digest(prompt: str) -> str:
    return hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = path
        self.clock = clock
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def version(self) -> int:
        return self.db.execute("PRAGMA user_version").fetchone()[0]

    def _migrate(self) -> None:
        current = self.version
        if current > len(MIGRATIONS):
            raise StoreError(
                f"{self.path} has schema version {current}; this build knows up to "
                f"{len(MIGRATIONS)}. Upgrade persistbot before running this bot."
            )
        for number in range(current, len(MIGRATIONS)):
            # executescript commits on its own; keep each migration a single
            # step followed immediately by its version bump.
            MIGRATIONS[number](self.db)
            self.db.execute(f"PRAGMA user_version={number + 1}")
            self.db.commit()

    def _run(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        cursor = self.db.execute(sql, args)
        self.db.commit()
        return cursor

    # meta ---------------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self._run(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # goals --------------------------------------------------------------

    def add_goal(self, prompt: str, source: str) -> tuple[int, bool]:
        """Queue a goal. Returns (goal id, created).

        If a goal with the same text is still open, that goal is returned
        and nothing is added. This makes ingest safe to repeat after a
        crash, and stops a recurring prompt piling up behind itself.
        """
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("a goal needs a prompt")
        key = digest(prompt)
        row = self.db.execute(
            "SELECT id FROM goal WHERE digest=? AND state IN (?, ?) ORDER BY id LIMIT 1",
            (key, *OPEN_STATES),
        ).fetchone()
        if row:
            return row["id"], False
        now = self.clock()
        cursor = self._run(
            "INSERT INTO goal (digest, source, prompt, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, source, prompt, now, now),
        )
        return cursor.lastrowid, True

    def goal(self, goal_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM goal WHERE id=?", (goal_id,)).fetchone()

    def goals(self, states: tuple[str, ...] | None = None) -> list[sqlite3.Row]:
        if states:
            marks = ",".join("?" * len(states))
            return self.db.execute(
                f"SELECT * FROM goal WHERE state IN ({marks}) ORDER BY id", states
            ).fetchall()
        return self.db.execute("SELECT * FROM goal ORDER BY id").fetchall()

    def next_goal(self) -> sqlite3.Row | None:
        """The active goal, else the oldest queued one, whose `not_before` has passed."""
        return self.db.execute(
            "SELECT * FROM goal WHERE state IN (?, ?) AND not_before<=? "
            "ORDER BY CASE state WHEN 'active' THEN 0 ELSE 1 END, id LIMIT 1",
            (*OPEN_STATES, self.clock()),
        ).fetchone()

    def update_goal(self, goal_id: int, **fields) -> None:
        allowed = {"state", "failures", "deferrals", "not_before", "summary"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update goal field(s): {', '.join(sorted(bad))}")
        if "state" in fields and fields["state"] not in GOAL_STATES:
            raise ValueError(f"unknown goal state: {fields['state']}")
        fields["updated_at"] = self.clock()
        sets = ", ".join(f"{name}=?" for name in fields)
        self._run(f"UPDATE goal SET {sets} WHERE id=?", (*fields.values(), goal_id))

    # attempts -----------------------------------------------------------

    def start_attempt(self, goal_id: int) -> int:
        return self._run(
            "INSERT INTO attempt (goal_id, started_at) VALUES (?, ?)",
            (goal_id, self.clock()),
        ).lastrowid

    def end_attempt(self, attempt_id: int, outcome: str, detail: str = "") -> None:
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown attempt outcome: {outcome}")
        self._run(
            "UPDATE attempt SET ended_at=?, outcome=?, detail=? WHERE id=?",
            (self.clock(), outcome, detail, attempt_id),
        )

    def open_attempts(self) -> list[sqlite3.Row]:
        """Attempts with a start and no end: left behind by a tick that died."""
        return self.db.execute(
            "SELECT * FROM attempt WHERE ended_at IS NULL ORDER BY id"
        ).fetchall()

    def attempts(self, goal_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM attempt WHERE goal_id=? ORDER BY id", (goal_id,)
        ).fetchall()

    # tool journal -------------------------------------------------------

    def begin_tool_call(self, attempt_id: int, name: str, arguments: str) -> int:
        """First journal write, made before the tool runs."""
        return self._run(
            "INSERT INTO tool_call (attempt_id, name, arguments, started_at) "
            "VALUES (?, ?, ?, ?)",
            (attempt_id, name, arguments, self.clock()),
        ).lastrowid

    def finish_tool_call(self, call_id: int, ok: bool, result: str) -> None:
        """Second journal write, made after the tool returns."""
        self._run(
            "UPDATE tool_call SET ended_at=?, ok=?, result=? WHERE id=?",
            (self.clock(), int(ok), result, call_id),
        )

    def tool_calls(self, attempt_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM tool_call WHERE attempt_id=? ORDER BY id", (attempt_id,)
        ).fetchall()

    # notes --------------------------------------------------------------

    def add_note(
        self, goal_id: int, text: str, attempt_id: int | None = None, kind: str = "note"
    ) -> int:
        return self._run(
            "INSERT INTO note (goal_id, attempt_id, kind, text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (goal_id, attempt_id, kind, text.strip(), self.clock()),
        ).lastrowid

    def notes(self, goal_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM note WHERE goal_id=? ORDER BY id", (goal_id,)
        ).fetchall()

    # memory -------------------------------------------------------------

    def remember(self, text: str, kind: str = "memory") -> int:
        return self._run(
            "INSERT INTO memory (kind, text, created_at) VALUES (?, ?, ?)",
            (kind, text.strip(), self.clock()),
        ).lastrowid

    def memories(self, kind: str = "memory", max_chars: int | None = None) -> list[sqlite3.Row]:
        """Newest first. With `max_chars`, stops before the total text exceeds it."""
        rows = self.db.execute(
            "SELECT * FROM memory WHERE kind=? ORDER BY id DESC", (kind,)
        ).fetchall()
        if max_chars is None:
            return rows
        kept, used = [], 0
        for row in rows:
            used += len(row["text"])
            if used > max_chars:
                break
            kept.append(row)
        return kept
