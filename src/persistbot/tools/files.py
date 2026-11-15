"""read_file and write_file.

Reads go anywhere the account can read. Writes stay under the bot's base
directory, and never touch the harness's own state there.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from persistbot.tools import Context, Result

READ_CAP = 8000        # characters returned to the model
WRITE_CAP = 100_000    # characters accepted in one write
_UMASK = os.umask(0)
os.umask(_UMASK)
PROTECTED = ("state.db", "state.db-wal", "state.db-shm", "bot.lock")


def _resolve(ctx: Context, path: str) -> Path:
    if not path.strip() or "\x00" in path:
        raise ValueError("the path is empty or not usable")
    target = Path(path.strip()).expanduser()
    if not target.is_absolute():
        target = ctx.base / target
    return target.resolve()  # follows symlinks, so a link cannot lead a write outside


def _replace(target: Path, content: str) -> None:
    """Write a new file and move it into place.

    Writing through the existing name would also change every hard link to
    it: `ln state.db alias` would make `alias` a way to truncate the
    database, or a link to a file outside the base a way to write there.
    Replacing the name leaves the other links and their content alone.
    """
    mode = target.stat().st_mode & 0o777 if target.exists() else None
    handle, name = tempfile.mkstemp(dir=target.parent, prefix=".write-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(content)
            if mode is not None:
                os.fchmod(out.fileno(), mode)  # a script stays executable
            else:
                os.fchmod(out.fileno(), 0o666 & ~_UMASK)
        os.replace(name, target)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


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


def writable(ctx: Context, path: str) -> Path:
    """The resolved path if a file may be written there, else ValueError for the model."""
    base = ctx.base.resolve()
    target = _resolve(ctx, path)
    if target != base and base not in target.parents:
        raise ValueError(f"files can be written only under {base}. Use a relative path.")
    if target == base or target.is_dir():
        raise ValueError(f"{target} is a directory")
    if target.parent == base and target.name in PROTECTED:
        raise ValueError(f"{target.name} belongs to the harness and cannot be written")
    return target


def write_file(ctx: Context, path: str, content: str) -> Result:
    if len(content) > WRITE_CAP:
        return Result(False, f"error: content is over {WRITE_CAP} characters; write a smaller file")
    try:
        target = writable(ctx, path)
    except ValueError as exc:
        return Result(False, f"error: {exc}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _replace(target, content)
    except OSError as exc:
        return Result(False, f"error: cannot write {path}: {exc.strerror or exc}")
    return Result(True, f"wrote {len(content)} characters to {target}")
