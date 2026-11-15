"""read_file and write_file.

Reads go anywhere the account can read. Writes stay under the bot's base
directory, and never touch the harness's own state there.
"""

from __future__ import annotations

from pathlib import Path

from persistbot.tools import Context, Result

READ_CAP = 8000        # characters returned to the model
WRITE_CAP = 100_000    # characters accepted in one write
PROTECTED = ("state.db", "state.db-wal", "state.db-shm", "bot.lock")


def _resolve(ctx: Context, path: str) -> Path:
    if not path.strip() or "\x00" in path:
        raise ValueError("the path is empty or not usable")
    target = Path(path.strip()).expanduser()
    if not target.is_absolute():
        target = ctx.base / target
    return target.resolve()  # follows symlinks, so a link cannot lead a write outside


def read_file(ctx: Context, path: str) -> Result:
    try:
        target = _resolve(ctx, path)
        if target.is_dir():
            return Result(False, f"error: {target} is a directory; use run_shell with ls")
        if not target.is_file():
            # Also refuses devices and pipes, which could block or never end.
            return Result(False, f"error: no such file: {target}")
        with open(target, "rb") as handle:
            data = handle.read(READ_CAP * 4 + 1)
    except (OSError, ValueError) as exc:
        return Result(False, f"error: cannot read {path}: {getattr(exc, 'strerror', None) or exc}")
    if b"\x00" in data:
        return Result(False, f"error: {target} is not a text file")
    text = data.decode("utf-8", errors="replace")
    if len(text) > READ_CAP or len(data) > READ_CAP * 4:
        text = text[:READ_CAP] + f"\n[cut off after {READ_CAP} characters; the file is longer]"
    return Result(True, text if text else "(the file is empty)")


def write_file(ctx: Context, path: str, content: str) -> Result:
    if len(content) > WRITE_CAP:
        return Result(False, f"error: content is over {WRITE_CAP} characters; write a smaller file")
    try:
        base = ctx.base.resolve()
        target = _resolve(ctx, path)
        if target != base and base not in target.parents:
            return Result(
                False,
                f"error: files can be written only under {base}. Use a relative path.",
            )
        if target == base or target.is_dir():
            return Result(False, f"error: {target} is a directory")
        if target.parent == base and target.name in PROTECTED:
            return Result(False, f"error: {target.name} belongs to the harness and cannot be written")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except (OSError, ValueError) as exc:
        return Result(False, f"error: cannot write {path}: {getattr(exc, 'strerror', None) or exc}")
    return Result(True, f"wrote {len(content)} characters to {target}")
