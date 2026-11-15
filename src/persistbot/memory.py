"""Facts the bot has remembered, as text for the system prompt."""

from __future__ import annotations

from persistbot.store import Store

PROMPT_CHARS = 1500  # cap on remembered text injected into one prompt


HEADING = "What you have learned about this node, newest first:"


def prompt_block(db: Store, max_chars: int = PROMPT_CHARS) -> str:
    """Remembered facts, newest first. The whole block stays within `max_chars`."""
    lines, used = [], len(HEADING)
    for row in db.memories(kind="memory"):
        line = f"- {row['text']}"
        used += len(line) + 1
        if used > max_chars:
            break
        lines.append(line)
    return "\n".join([HEADING, *lines]) if lines else ""
