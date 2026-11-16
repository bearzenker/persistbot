"""read_file and write_file.

Both are done by the runner, on the bot's own account. Reads go anywhere
that account can read. Writes stay under the bot's working directory.
"""

from __future__ import annotations

from persistbot import link, log
from persistbot.runner import READ_CAP, WRITE_CAP
from persistbot.tools import Context, Result, answer

LOST = "error: the file runner stopped during this call. Check the file before repeating it."


def read_file(ctx: Context, path: str) -> Result:
    try:
        reply = ctx.link.call({"op": "read", "path": path}, link.FILE_WAIT)
    except link.Lost as exc:
        log.action(f"read_file: runner lost: {exc}", level="warning")
        return Result(False, LOST)
    return answer(reply, READ_CAP + 200)


def write_file(ctx: Context, path: str, content: str) -> Result:
    if len(content) > WRITE_CAP:
        return Result(False, f"error: content is over {WRITE_CAP} characters; write a smaller file")
    try:
        reply = ctx.link.call({"op": "write", "path": path, "content": content}, link.FILE_WAIT)
    except link.Lost as exc:
        log.action(f"write_file: runner lost: {exc}", level="warning")
        return Result(False, LOST)
    return answer(reply, 1000)
