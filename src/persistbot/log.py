"""The only place that writes logs or console output.

Callers use `action`, `chat`, `report` and `out`. They never touch the
standard `logging` module, so a later mode (rsyslog) changes `setup` alone.

Sinks, in local mode:
  action.log  operational messages
  chat.log    append-only transcript: every request, response and tool call
  report.log  lines meant for upstream monitoring
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

_ACTION = logging.getLogger("persistbot.action")
_CHAT = logging.getLogger("persistbot.chat")
_REPORT = logging.getLogger("persistbot.report")
_SINKS = (_ACTION, _CHAT, _REPORT)

_STAMP = "%Y-%m-%dT%H:%M:%S%z"
_ready = False


def _attach(logger: logging.Logger, handler: logging.Handler, fmt: str) -> None:
    handler.setFormatter(logging.Formatter(fmt, datefmt=_STAMP))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False


def close() -> None:
    """Detach and close every handler. Safe to call repeatedly."""
    global _ready
    for logger in _SINKS:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
    _ready = False


def setup(name: str, mode: str, log_dir: Path, report_mode: str, report_dir: Path) -> None:
    """Open the sinks for bot `name`. Raises OSError if a directory is unusable."""
    global _ready
    close()
    for which in (mode, report_mode):
        if which != "local":
            raise ValueError(f"logging mode '{which}' is not supported")
    log_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    line = f"%(asctime)s {name} %(levelname)s %(message)s"
    plain = f"%(asctime)s {name} %(message)s"
    _attach(_ACTION, logging.FileHandler(log_dir / "action.log", encoding="utf-8"), line)
    _attach(_CHAT, logging.FileHandler(log_dir / "chat.log", encoding="utf-8"), plain)
    _attach(_REPORT, logging.FileHandler(report_dir / "report.log", encoding="utf-8"), plain)
    _ready = True


def action(message: str, level: str = "info") -> None:
    """Operational message. Before `setup`, falls back to stderr."""
    if not _ready:
        sys.stderr.write(f"persistbot {level}: {message}\n")
        return
    _ACTION.log(logging.getLevelName(level.upper()), message)


def error(message: str) -> None:
    """An error an operator must see: the action log, and always stderr."""
    if _ready:
        _ACTION.error(message)
    sys.stderr.write(f"persistbot error: {message}\n")


def chat(role: str, content: Any) -> None:
    """Append one transcript entry. `role` is system, user, assistant or tool."""
    if not _ready:
        return
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, default=str)
    _CHAT.info("[%s] %s", role, content)


def report(message: str) -> None:
    """A line for upstream monitoring; also copied to the action log."""
    if not _ready:
        sys.stderr.write(f"persistbot report: {message}\n")
        return
    _REPORT.info(message)
    _ACTION.info("report: %s", message)


def out(text: str) -> None:
    """Command output for an operator (the `status` command)."""
    sys.stdout.write(text + "\n")
