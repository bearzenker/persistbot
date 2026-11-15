"""Which commands `run_shell` may run.

A guardrail against a confused model, not a sandbox: the security boundary
is the unprivileged account the bot runs as. Commands run without a shell,
so nothing here is expanded, piped or substituted.
"""

from __future__ import annotations

import re
import shlex

COMMANDS = (
    "awk", "cat", "chmod", "cp", "diff", "file", "find", "free", "grep", "head",
    "host", "hostname", "kill", "killall", "ln", "logger", "ls", "mkdir", "mv",
    "ps", "pwd", "rm", "rmdir", "sort", "tail", "tar", "umask", "uniq", "unzip",
    "which", "zcat", "zgrep", "zip",
)
# `*` is any run of name characters, `?` exactly one: lsblk, gzip, gunzip.
WILDCARDS = ("ls*", "?zip", "?unzip")

FORBIDDEN = ";&|$<>`"
_NAME_CHAR = "[A-Za-z0-9_-]"


def _pattern(name: str) -> str:
    return "".join(
        _NAME_CHAR + "*" if ch == "*" else _NAME_CHAR if ch == "?" else re.escape(ch)
        for ch in name
    )


_NAMES = "|".join(_pattern(name) for name in COMMANDS + WILDCARDS)
_ALLOWED = re.compile(rf"^(?:{_NAMES})(?:\s|$)")
_ALLOWED_NAME = re.compile(rf"(?:{_NAMES})")


class Rejected(Exception):
    """The command may not run. The message is written for the model."""


def check(command: str) -> list[str]:
    """Return the argument list for an allowed command, or raise Rejected."""
    if not isinstance(command, str) or not command.strip(" "):
        raise Rejected("empty command")
    for ch in command:
        if ch in FORBIDDEN:
            raise Rejected(
                f"the character {ch!r} is not allowed: run one plain command per call, "
                "with no pipes, redirection, variables or substitution"
            )
        if ord(ch) < 32 or ord(ch) == 127:
            raise Rejected("control characters and line breaks are not allowed: one command per call")
    command = command.strip(" ")
    if re.match(r"sudo(\s|$)", command):
        raise Rejected("sudo is not available")
    if not _ALLOWED.match(command):
        word = command.split(" ", 1)[0]
        hint = " (give the bare command name, not a path)" if "/" in word else ""
        raise Rejected(
            f"'{word}' is not an allowed command{hint}. Allowed: "
            + ", ".join(COMMANDS + WILDCARDS)
        )
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise Rejected(f"cannot parse the command: {exc}") from exc
    if not argv or not _ALLOWED_NAME.fullmatch(argv[0]):
        raise Rejected("cannot parse the command name")
    return argv
