"""Facts the bot has remembered, as text for the system prompt."""

from __future__ import annotations

from persistbot.store import Store

PROMPT_CHARS = 1500  # cap on remembered text injected into one prompt


def prompt_block(db: Store, max_chars: int = PROMPT_CHARS) -> str:
    """Remembered facts, newest first, up to the cap. Empty when there are none."""
    rows = db.memories(kind="memory", max_chars=max_chars)
    if not rows:
        return ""
    lines = "\n".join(f"- {row['text']}" for row in rows)
    return f"What you have learned about this node, newest first:\n{lines}"
