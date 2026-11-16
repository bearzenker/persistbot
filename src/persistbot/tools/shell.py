"""run_shell: allowlisted commands, alone or in a pipeline, with no shell.

The harness reads the command line and checks every command against the
allowlist. The runner, on the bot's own account, starts the processes and
connects them. Time and output are bounded there.
"""

from __future__ import annotations

from persistbot import allowlist, link, log
from persistbot.runner import FILE_CAP, OUTPUT_CAP  # noqa: F401  (the limits, for callers)
from persistbot.tools import Context, Result, answer

LOST = (
    "error: the command runner stopped during this call, so the outcome is unknown. "
    "Inspect before repeating it."
)


def run_shell(ctx: Context, command: str) -> Result:
    try:
        pipeline = allowlist.parse(command)
    except allowlist.Rejected as exc:
        log.action(f"run_shell rejected: {command!r}", level="warning")
        return Result(False, f"error: command not run: {exc}")

    names = " | ".join(item.argv[0] for item in pipeline.commands)
    request = {
        "op": "run",
        "commands": [{"argv": item.argv, "stderr": item.stderr} for item in pipeline.commands],
        "stdout": pipeline.stdout,
        "append": pipeline.append,
        "timeout": ctx.shell_timeout,
    }
    try:
        reply = ctx.link.call(request, ctx.shell_timeout + link.RUN_GRACE)
    except link.Lost as exc:
        log.action(f"run_shell {names}: runner lost: {exc}", level="warning")
        return Result(False, LOST)
    result = answer(reply, OUTPUT_CAP + 400)
    log.action(f"run_shell {names}: {str(reply.get('status') or result.text.splitlines()[0])[:80]}")
    return result
