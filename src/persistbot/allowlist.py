"""Which commands `run_shell` may run, and how a command line is read.

A guardrail against a confused model, not a sandbox: the security boundary
is the unprivileged account the bot runs as.

No shell is ever involved. `parse` reads the command line itself and
understands only this much:

  cmd args | cmd args ...     a pipeline; every command must be allowed
  2>/dev/null   2>&1          per command
  >/dev/null  > file  >> file on the last command; the file must be under
                              the bot's base directory (run_shell checks)
  'single' "double" \\x       quoting, as a shell would read it

Everything else that only a shell could do is refused with a message that
says so (`;`, `&&`, `||`, `&`, `<`, backticks, `$(...)`, `$VAR`), because
running it as plain words would do something other than what was meant.
Wildcards and `~` are not expanded either; they reach the command as text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

COMMANDS = (
    "awk", "cat", "chmod", "cp", "diff", "file", "find", "free", "grep", "head",
    "host", "hostname", "kill", "killall", "ln", "logger", "ls", "mkdir", "mv",
    "ps", "pwd", "rm", "rmdir", "sort", "tail", "tar", "umask", "uniq", "unzip",
    "which", "zcat", "zgrep", "zip",
)
# `*` is any run of name characters, `?` exactly one: lsblk, gzip, gunzip.
WILDCARDS = ("ls*", "?zip", "?unzip")
MAX_COMMANDS = 6  # commands in one pipeline

_NAME_CHAR = "[A-Za-z0-9_-]"


def _pattern(name: str) -> str:
    return "".join(
        _NAME_CHAR + "*" if ch == "*" else _NAME_CHAR if ch == "?" else re.escape(ch)
        for ch in name
    )


_ALLOWED_NAME = re.compile("|".join(_pattern(name) for name in COMMANDS + WILDCARDS))
_VARIABLE = re.compile(r"[A-Za-z0-9_{?]")

ONE_PER_CALL = "run one command or one pipeline per call"


class Rejected(Exception):
    """The command may not run. The message is written for the model."""


@dataclass
class Command:
    argv: list[str]
    stderr: str = "show"  # "show" to the model, "null" (2>/dev/null) or "stdout" (2>&1)


@dataclass
class Pipeline:
    commands: list[Command] = field(default_factory=list)
    stdout: str | None = None  # None: shown to the model; else "/dev/null" or a file path
    append: bool = False


def _tokens(text: str) -> list[tuple]:
    """Split into ("word", str), ("pipe",), ("redirect", fd, append) and ("dup",)."""
    out: list[tuple] = []
    word: list[str] = []
    quoted = False  # the current word has a quoted part, so "" is still a word
    i, n = 0, len(text)

    def flush() -> None:
        nonlocal quoted
        if word or quoted:
            out.append(("word", "".join(word)))
        word.clear()
        quoted = False

    def dollar(at: int) -> None:
        rest = text[at + 1:at + 2]
        if rest == "(":
            raise Rejected("$(...) is not available: run that command first, then use its output")
        if rest and _VARIABLE.match(rest):
            raise Rejected(
                "variables such as $HOME are not expanded: write the value itself. "
                "Inside single quotes $ is passed through, as awk needs."
            )

    while i < n:
        ch = text[i]
        if ch == "'":
            end = text.find("'", i + 1)
            if end < 0:
                raise Rejected("cannot parse the command: a quote is not closed")
            word.append(text[i + 1:end])
            quoted = True
            i = end + 1
        elif ch == '"':
            quoted = True
            i += 1
            while True:
                if i >= n:
                    raise Rejected("cannot parse the command: a quote is not closed")
                ch = text[i]
                if ch == '"':
                    break
                if ch == "\\" and text[i + 1:i + 2] in ('"', "\\", "$", "`"):
                    word.append(text[i + 1])
                    i += 2
                    continue
                if ch == "`":
                    raise Rejected("backticks are not available: run that command first")
                if ch == "$":
                    dollar(i)
                word.append(ch)
                i += 1
            i += 1
        elif ch == "\\":
            if i + 1 >= n:
                raise Rejected("cannot parse the command: it ends with a backslash")
            word.append(text[i + 1])
            quoted = True
            i += 2
        elif ch == " ":
            flush()
            i += 1
        elif ch == "|":
            if text[i + 1:i + 2] == "|":
                raise Rejected(f"|| is not available: {ONE_PER_CALL}")
            flush()
            out.append(("pipe",))
            i += 1
        elif ch == ">":
            fd = 1
            if not quoted and "".join(word) in ("1", "2"):
                fd = int(word[0])
                word.clear()
            flush()
            i += 1
            append = text[i:i + 1] == ">"
            i += append
            out.append(("redirect", fd, append))
            if text[i:i + 1] == "&":
                if fd != 2 or append or text[i + 1:i + 2] != "1" or text[i + 2:i + 3] not in ("", " ", "|"):
                    raise Rejected("the only redirection between streams is 2>&1")
                out.append(("dup",))
                i += 2
        elif ch == "&":
            what = "&&" if text[i + 1:i + 2] == "&" else "&"
            raise Rejected(f"{what} is not available: {ONE_PER_CALL}")
        elif ch == ";":
            raise Rejected(f"; is not available: {ONE_PER_CALL}")
        elif ch == "<":
            raise Rejected("< is not available: give the file name to the command instead")
        elif ch == "`":
            raise Rejected("backticks are not available: run that command first")
        else:
            if ch == "$":
                dollar(i)
            word.append(ch)
            i += 1
    flush()
    return out


def _check_name(name: str) -> None:
    if name == "sudo":
        raise Rejected("sudo is not available")
    if not _ALLOWED_NAME.fullmatch(name):
        hint = " (give the bare command name, not a path)" if "/" in name else ""
        raise Rejected(
            f"'{name}' is not an allowed command{hint}. Allowed: "
            + ", ".join(COMMANDS + WILDCARDS)
        )


def parse(command: str) -> Pipeline:
    """Read a command line into a pipeline of allowed commands, or raise Rejected."""
    if not isinstance(command, str) or not command.strip(" "):
        raise Rejected("empty command")
    for ch in command:
        if ord(ch) < 32 or ord(ch) == 127:
            raise Rejected(f"control characters and line breaks are not allowed: {ONE_PER_CALL}")

    pipeline = Pipeline(commands=[Command(argv=[])])
    tokens = _tokens(command)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        current = pipeline.commands[-1]
        index += 1
        if token[0] == "word":
            current.argv.append(token[1])
        elif token[0] == "pipe":
            if not current.argv:
                raise Rejected("cannot parse the command: nothing before |")
            if pipeline.stdout is not None:
                raise Rejected("> can only be used on the last command of a pipeline")
            pipeline.commands.append(Command(argv=[]))
        else:  # redirect
            _, fd, append = token
            following = tokens[index] if index < len(tokens) else None
            index += 1
            if following == ("dup",):
                current.stderr = "stdout"
            elif following is None or following[0] != "word" or not following[1]:
                raise Rejected("cannot parse the command: > needs a file name after it")
            elif fd == 2:
                if following[1] != "/dev/null" or append:
                    raise Rejected("errors can only be redirected with 2>/dev/null or 2>&1")
                current.stderr = "null"
            else:
                if pipeline.stdout is not None:
                    raise Rejected("only one > is allowed in a command")
                pipeline.stdout, pipeline.append = following[1], append

    if not pipeline.commands[-1].argv:
        if len(pipeline.commands) > 1:
            raise Rejected("cannot parse the command: nothing after |")
        raise Rejected("cannot parse the command: there is no command name")
    if len(pipeline.commands) > MAX_COMMANDS:
        raise Rejected(f"at most {MAX_COMMANDS} commands in one pipeline")
    for item in pipeline.commands:
        _check_name(item.argv[0])
    return pipeline
