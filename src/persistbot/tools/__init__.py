"""The tools the model may call, and the one way to call them.

`schemas()` is what goes in the request. `call()` runs one tool call and
always returns a `Result`: a tool error is an answer for the model, never an
exception for the tick.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from persistbot.store import Store

MAX_TOOLS = 6


@dataclass
class Context:
    """What a tool needs to know about the tick it runs in."""

    db: Store
    base: Path                 # the bot's base directory
    goal_id: int
    attempt_id: int | None = None
    shell_timeout: float = 60.0


@dataclass(frozen=True)
class Result:
    ok: bool
    text: str                  # what the model is told
    done: str | None = None    # the summary, when the model called goal_done
    malformed: bool = False    # the call itself was unusable; counts toward `invalid`


@dataclass(frozen=True)
class Tool:
    name: str
    description: str           # one or two lines: the target model is small
    arguments: dict[str, str]  # argument name -> description; all are required text
    handler: Callable[..., Result]

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        name: {"type": "string", "description": text}
                        for name, text in self.arguments.items()
                    },
                    "required": list(self.arguments),
                },
            },
        }


def _registry() -> dict[str, Tool]:
    from persistbot.tools import files, shell, state

    tools = (
        Tool(
            "run_shell",
            "Run an allowed command on this node, or several joined with |, and return "
            "the output. No ; or && and no $VARIABLES.",
            {"command": "The command line, for example: ps aux | grep nginx"},
            shell.run_shell,
        ),
        Tool(
            "read_file",
            "Read a text file. Long files are cut off.",
            {"path": "Path of the file to read"},
            files.read_file,
        ),
        Tool(
            "write_file",
            "Create or replace a file in your working directory.",
            {"path": "Path relative to your working directory", "content": "The whole file content"},
            files.write_file,
        ),
        Tool(
            "remember",
            "Save a lasting fact about this node. It is shown to you in every later run.",
            {"text": "One short fact"},
            state.remember,
        ),
        Tool(
            "note",
            "Save a work note on the current goal: what you tried, found, or plan next.",
            {"text": "The note"},
            state.note,
        ),
        Tool(
            "goal_done",
            "Declare the goal complete. Call this only after checking the result.",
            {"summary": "What was done and how you checked it"},
            state.goal_done,
        ),
    )
    if len(tools) > MAX_TOOLS:
        raise RuntimeError(f"at most {MAX_TOOLS} tools may be offered in one request")
    return {tool.name: tool for tool in tools}


TOOLS: dict[str, Tool] = _registry()


def schemas() -> list[dict]:
    """The tool list for a chat completion request."""
    return [tool.schema() for tool in TOOLS.values()]


def parse(name: str, arguments: str) -> tuple[Tool, dict[str, str]] | Result:
    """Check a tool call as the model wrote it, without running it.

    Returns the tool and its argument values, or the malformed `Result`.
    """
    tool = TOOLS.get(name)
    if tool is None:
        return Result(
            False,
            f"error: there is no tool named '{name}'. The tools are: {', '.join(TOOLS)}.",
            malformed=True,
        )
    try:
        args = json.loads(arguments) if arguments and arguments.strip() else {}
    except ValueError as exc:
        return Result(False, f"error: the arguments are not valid JSON: {exc}", malformed=True)
    if not isinstance(args, dict):
        return Result(False, "error: the arguments must be a JSON object", malformed=True)
    values = {}
    for arg in tool.arguments:
        value = args.get(arg)
        if not isinstance(value, str):
            problem = "is missing" if arg not in args else "must be text"
            return Result(False, f"error: {name} argument '{arg}' {problem}", malformed=True)
        values[arg] = value
    return tool, values


def call(ctx: Context, name: str, arguments: str) -> Result:
    """Run the tool call the model wrote. `arguments` is its JSON text, unparsed."""
    parsed = parse(name, arguments)
    if isinstance(parsed, Result):
        return parsed
    tool, values = parsed
    try:
        return tool.handler(ctx, **values)
    except Exception as exc:  # a tool must never end the tick
        return Result(False, f"error: {name} failed: {type(exc).__name__}: {exc}")
