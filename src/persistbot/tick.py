"""One wake-up of the bot: find work, run one bounded attempt, record it.

The order matters for persistence. Everything a later tick needs is written
before the step that could fail: the attempt row before the first model
call, each tool call before it runs. A tick that dies anywhere leaves an
open attempt, which the next tick closes as `crashed` and carries on from.

A goal is closed only by an explicit `goal_done` call.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from persistbot import EXIT_CONFIG, EXIT_FAILED, EXIT_OK, EXIT_TEMPFAIL, driver, log, memory, store, tools
from persistbot.config import BotConfig, ConfigError
from persistbot.lock import TickLock
from persistbot.persona import ResolvedModel

INVALID_LIMIT = 3            # unusable replies in one attempt before it ends as `invalid`
BACKOFF_FIRST = 300.0        # seconds after the first deferral; doubles each time
BACKOFF_CAP = 3600.0
FATAL_BACKOFF = 3600.0       # a bad key or model will not fix itself quickly
NOTES_CHARS = 2000           # work notes shown to an attempt, newest kept
SUMMARY_ATTEMPTS = 3         # earlier attempts summarised
SUMMARY_CALLS = 4            # last tool calls shown per attempt
CLIP = 160                   # characters of a call's arguments or result in a summary
NUDGE = (
    "That reply did not call a tool, so nothing happened. Call one tool now. "
    "If the goal is met and checked, call goal_done."
)


@dataclass
class Outcome:
    kind: str                       # one of store.OUTCOMES
    detail: str = ""
    retry_after: float | None = None
    fatal: bool = False             # an upstream error an operator has to fix


def _clip(text: str, limit: int = CLIP) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


# input -------------------------------------------------------------------


def _ingest(config: BotConfig, db: store.Store, prompts: list[tuple[str, str]]) -> None:
    for text, source in prompts:
        _queue(db, text, source)
    if not config.incoming.is_dir():
        return
    done = config.incoming / "done"
    for path in sorted(config.incoming.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.action(f"cannot read incoming file {path.name}: {exc.strerror or exc}", "warning")
            continue
        if text.strip():
            # add_goal returns the open goal when this text is already queued,
            # so a crash between here and the move cannot duplicate it.
            _queue(db, text, f"incoming:{path.name}")
        else:
            log.action(f"incoming file {path.name} is empty; set aside", "warning")
        done.mkdir(exist_ok=True)
        target = done / path.name
        if target.exists():
            target = done / f"{path.name}.{int(db.clock())}"
        os.replace(path, target)


def _queue(db: store.Store, text: str, source: str) -> None:
    goal_id, created = db.add_goal(text, source)
    if created:
        log.action(f"goal {goal_id} queued from {source}")
    else:
        log.action(f"goal {goal_id} is already open with this prompt; not queued again")


# failure accounting ------------------------------------------------------


def _count_failure(config: BotConfig, db: store.Store, goal_id: int, attempt_id: int,
                   kind: str, detail: str) -> None:
    goal = db.goal(goal_id)
    failures = goal["failures"] + 1
    limit = config.limits.max_failures
    if failures >= limit:
        db.update_goal(goal_id, failures=failures, deferrals=0, state=store.STUCK)
        log.report(
            f"goal {goal_id} is stuck after {failures} failed attempts "
            f"(last: {kind}, {detail}); it needs an operator"
        )
    else:
        db.update_goal(goal_id, failures=failures, deferrals=0)
        log.report(f"goal {goal_id} attempt {attempt_id}: {kind}, {detail} (failure {failures} of {limit})")


def _close_crashed(config: BotConfig, db: store.Store) -> None:
    """An attempt with a start and no end belongs to a tick that died."""
    for attempt in db.open_attempts():
        detail = "the run ended without finishing"
        db.end_attempt(attempt["id"], store.CRASHED, detail)
        goal = db.goal(attempt["goal_id"])
        if goal is not None and goal["state"] in store.OPEN_STATES:
            _count_failure(config, db, goal["id"], attempt["id"], store.CRASHED, detail)


# prompt ------------------------------------------------------------------


def _system_prompt(config: BotConfig, model: ResolvedModel, db: store.Store) -> str:
    common = config.venv / "system.txt"
    try:
        parts = [common.read_text(encoding="utf-8").strip()]
    except OSError as exc:
        raise ConfigError(f"cannot read {common}: {exc.strerror or exc}") from exc
    if config.system.is_file():
        parts.append(config.system.read_text(encoding="utf-8").strip())
    parts.append(model.identity())
    remembered = memory.prompt_block(db)
    if remembered:
        parts.append(remembered)
    return "\n\n".join(part for part in parts if part)


def _attempt_summary(db: store.Store, attempt: sqlite3.Row, number: int) -> str:
    calls = db.tool_calls(attempt["id"])
    head = f"Attempt {number} ended as '{attempt['outcome']}'"
    if attempt["detail"]:
        head += f" ({_clip(attempt['detail'])})"
    if not calls:
        return head + ". It made no tool calls."
    lines = [head + f". It made {len(calls)} tool call(s); the last ones:"]
    for call in calls[-SUMMARY_CALLS:]:
        text = f"  {call['name']} {_clip(call['arguments'])}"
        if call["ended_at"] is None:
            text += " -> STARTED, OUTCOME UNKNOWN: the run died here. Inspect before repeating it."
        else:
            text += f" -> {_clip(call['result'])}"
        lines.append(text)
    return "\n".join(lines)


def _user_prompt(db: store.Store, goal: sqlite3.Row, attempt_id: int) -> str:
    parts = [f"Goal: {goal['prompt']}"]
    notes, used = [], 0
    for row in reversed(db.notes(goal["id"])):
        used += len(row["text"])
        if used > NOTES_CHARS:
            break
        notes.append(f"- {row['text']}")
    if notes:
        parts.append("Your notes from earlier runs, oldest first:\n" + "\n".join(reversed(notes)))
    earlier = [a for a in db.attempts(goal["id"]) if a["id"] != attempt_id]
    if earlier:
        first = len(earlier) - len(earlier[-SUMMARY_ATTEMPTS:]) + 1
        summaries = [
            _attempt_summary(db, attempt, first + offset)
            for offset, attempt in enumerate(earlier[-SUMMARY_ATTEMPTS:])
        ]
        parts.append(
            "This goal has been worked on before. You do not remember those runs:\n"
            + "\n".join(summaries)
        )
    return "\n\n".join(parts)


# the tool loop -----------------------------------------------------------


def _attempt(config: BotConfig, model: ResolvedModel, db: store.Store,
             goal: sqlite3.Row, attempt_id: int) -> Outcome:
    messages = [
        {"role": "system", "content": _system_prompt(config, model, db)},
        {"role": "user", "content": _user_prompt(db, goal, attempt_id)},
    ]
    for message in messages:
        log.chat(message["role"], message["content"])
    ctx = tools.Context(db=db, base=config.base, goal_id=goal["id"], attempt_id=attempt_id)
    schemas = tools.schemas()
    invalid = 0

    for _ in range(config.limits.max_turns):
        try:
            reply = driver.call(model, messages, schemas)
        except driver.UpstreamRetryable as exc:
            return Outcome(store.DEFERRED, str(exc), retry_after=exc.retry_after)
        except driver.UpstreamFatal as exc:
            return Outcome(store.DEFERRED, str(exc), fatal=True)
        message = reply.message()
        messages.append(message)
        log.chat("assistant", message)

        if not reply.tool_calls:
            invalid += 1
            if invalid >= INVALID_LIMIT:
                return Outcome(store.INVALID, f"{invalid} replies without a usable tool call")
            messages.append({"role": "user", "content": NUDGE})
            log.chat("user", NUDGE)
            continue

        done = None
        for call in reply.tool_calls:
            # Two journal writes: before the tool runs and after. A run that
            # dies between them leaves the first, and the next attempt is
            # told the outcome is unknown.
            journal = db.begin_tool_call(attempt_id, call.name, call.arguments)
            result = tools.call(ctx, call.name, call.arguments)
            db.finish_tool_call(journal, result.ok, result.text)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result.text})
            log.chat("tool", {"name": call.name, "ok": result.ok, "result": result.text})
            if result.malformed:
                invalid += 1
            if result.done is not None and done is None:
                done = result.done
        if done is not None:
            return Outcome(store.COMPLETED, done)
        if invalid >= INVALID_LIMIT:
            return Outcome(store.INVALID, f"{invalid} replies without a usable tool call")

    return Outcome(store.BUDGET, f"turn limit of {config.limits.max_turns} reached")


def _record(config: BotConfig, db: store.Store, goal: sqlite3.Row, attempt_id: int,
            outcome: Outcome) -> int:
    goal_id = goal["id"]
    db.end_attempt(attempt_id, outcome.kind, outcome.detail)
    if outcome.kind == store.COMPLETED:
        db.update_goal(goal_id, state=store.DONE, summary=outcome.detail, deferrals=0)
        log.report(f"goal {goal_id} done: {_clip(outcome.detail, 300)}")
        return EXIT_OK
    if outcome.kind == store.DEFERRED:
        # Not the model's fault: never counted toward the failure limit.
        deferrals = goal["deferrals"] + 1
        if outcome.fatal:
            wait = FATAL_BACKOFF
        elif outcome.retry_after is not None:
            wait = outcome.retry_after
        else:
            wait = min(BACKOFF_FIRST * 2 ** (deferrals - 1), BACKOFF_CAP)
        db.update_goal(goal_id, deferrals=deferrals, not_before=db.clock() + wait)
        line = f"goal {goal_id} deferred for {wait:.0f}s: {_clip(outcome.detail, 300)}"
        if outcome.fatal:
            log.report(line + " (will not fix itself: check the key, model and provider)")
        else:
            log.action(line, "warning")
        return EXIT_TEMPFAIL
    _count_failure(config, db, goal_id, attempt_id, outcome.kind, outcome.detail)
    return EXIT_FAILED


# entry point -------------------------------------------------------------


def run(config: BotConfig, model: ResolvedModel | None, prompts: list[tuple[str, str]]) -> int:
    """One tick. `model` is None when the persona did not resolve: input is
    still queued, and nothing is run."""
    lock = TickLock(config.lock_path)
    if not lock.acquire():
        log.action("another tick holds the lock; exiting")
        return EXIT_OK
    try:
        with store.Store(config.db_path) as db:
            _close_crashed(config, db)
            _ingest(config, db, prompts)
            if model is None:
                return EXIT_CONFIG
            goal = db.next_goal()
            if goal is None:
                waiting = db.goals(store.OPEN_STATES)
                if waiting:
                    soonest = min(row["not_before"] for row in waiting)
                    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(soonest))
                    log.action(f"{len(waiting)} open goal(s) waiting; next is due at {when}")
                else:
                    log.action("tick: nothing to do")
                return EXIT_OK
            # Read the prompt files before anything is recorded, so a missing
            # system.txt costs the goal nothing.
            _system_prompt(config, model, db)
            if goal["state"] == store.QUEUED:
                db.update_goal(goal["id"], state=store.ACTIVE)
            attempt_id = db.start_attempt(goal["id"])
            log.action(f"goal {goal['id']} attempt {attempt_id} started")
            outcome = _attempt(config, model, db, goal, attempt_id)
            code = _record(config, db, db.goal(goal["id"]), attempt_id, outcome)
            log.action(f"goal {goal['id']} attempt {attempt_id}: {outcome.kind}")
            return code
    finally:
        lock.release()
