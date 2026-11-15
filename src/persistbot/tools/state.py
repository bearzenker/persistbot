"""remember, note and goal_done: the tools that write the bot's own state."""

from __future__ import annotations

from persistbot.tools import Context, Result

FACT_CAP = 300    # characters in one remembered fact
NOTE_CAP = 1000   # characters in one work note


def _clean(text: str) -> str:
    return " ".join(text.split())


def remember(ctx: Context, text: str) -> Result:
    fact = _clean(text)
    if not fact:
        return Result(False, "error: nothing to remember: the text is empty")
    if len(fact) > FACT_CAP:
        return Result(False, f"error: a fact must be under {FACT_CAP} characters; shorten it")
    if any(row["text"] == fact for row in ctx.db.memories()):
        return Result(True, "already remembered")
    ctx.db.remember(fact)
    return Result(True, "remembered")


def note(ctx: Context, text: str) -> Result:
    body = text.strip()
    if not body:
        return Result(False, "error: the note is empty")
    cut = len(body) > NOTE_CAP
    ctx.db.add_note(ctx.goal_id, body[:NOTE_CAP], ctx.attempt_id)
    return Result(True, f"note saved (cut to {NOTE_CAP} characters)" if cut else "note saved")


def goal_done(ctx: Context, summary: str) -> Result:
    """Does not close the goal itself: the tick does, when it sees `done`."""
    text = summary.strip()
    if not text:
        return Result(False, "error: goal_done needs a summary of what was done and checked")
    return Result(True, "goal marked done", done=text)
