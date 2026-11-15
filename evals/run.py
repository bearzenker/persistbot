#!/usr/bin/env python3
"""Does a model act when it should? Run before trusting a small model.

    python evals/run.py [conf.yaml] [-p PERSONA ...] [-t tasks.json] [-v]

Each task is one request: the system prompt, the six real tool schemas, a
goal and sometimes the tool calls made so far. The reply is scored, and no
tool is ever executed, so this is safe to run anywhere.

  acted    called an expected tool with usable arguments
  silent   answered in text, or with nothing: the harness has no one to
           read that, so it is always wrong here
  wrong    called a tool the task does not expect, or one it forbids
  invalid  called a tool that does not exist, or with unusable arguments
  error    the provider failed; the task is left out of the rates

Without -p the bot's own persona is tested. Every persona named must be in
conf/persona*.json. Keep the model with the highest `acted` rate.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
if (HOME / "src").is_dir():
    sys.path.insert(0, str(HOME / "src"))

from persistbot import config, driver, log, persona, tools  # noqa: E402

TASKS = Path(__file__).resolve().parent / "tasks.json"
VERDICTS = ("acted", "silent", "wrong", "invalid", "error")
RETRIES = 2
MAX_WAIT = 60.0
MAY_BE_EMPTY = {("write_file", "content")}  # an empty file is a valid thing to write

# Phase 4 ships the real system.txt. Until then this stands in for it.
SYSTEM = (
    "You are an unattended agent on a Linux node. Nobody reads your text replies: "
    "you make progress only by calling a tool, one step at a time. "
    "Work continues across runs, so leave notes. "
    "Call goal_done only after you have checked the result."
)


def load_tasks(path: Path) -> list[dict]:
    tasks = json.loads(path.read_text(encoding="utf-8"))
    seen = set()
    for task in tasks:
        missing = {"id", "goal", "expect"} - set(task)
        if missing:
            raise ValueError(f"task {task.get('id', '?')}: missing {', '.join(sorted(missing))}")
        if task["id"] in seen:
            raise ValueError(f"task id used twice: {task['id']}")
        seen.add(task["id"])
        names = list(task["expect"]) + list(task.get("forbid", []))
        names += [step["tool"] for step in task.get("history", [])]
        unknown = sorted(set(names) - set(tools.TOOLS))
        if unknown or not task["expect"]:
            raise ValueError(f"task {task['id']}: unknown or missing tool name(s) {unknown}")
    return tasks


def messages(task: dict, model: persona.ResolvedModel) -> list[dict]:
    system = f"{SYSTEM}\nYour name for this session is {model.name}. {model.character}".strip()
    out = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Goal: {task['goal']}"},
    ]
    for number, step in enumerate(task.get("history", []), start=1):
        call_id = f"call_{number}"
        out.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": step["tool"], "arguments": json.dumps(step["arguments"])},
            }],
        })
        out.append({"role": "tool", "tool_call_id": call_id, "content": step["result"]})
    return out


def score(task: dict, reply: driver.Reply) -> tuple[str, str]:
    """(verdict, detail) for one reply. Only the first tool call is judged."""
    if not reply.tool_calls:
        return "silent", reply.text.strip().replace("\n", " ")[:80]
    call = reply.tool_calls[0]
    detail = f"{call.name} {call.arguments}".replace("\n", " ")[:100]
    parsed = tools.parse(call.name, call.arguments)
    if isinstance(parsed, tools.Result):
        return "invalid", detail
    _, values = parsed
    if not all(value.strip() for arg, value in values.items() if (call.name, arg) not in MAY_BE_EMPTY):
        return "invalid", detail  # an empty argument: the real tool would refuse it
    if call.name in task.get("forbid", []) or call.name not in task["expect"]:
        return "wrong", detail
    for arg, pattern in task.get("match", {}).get(call.name, {}).items():
        if not re.search(pattern, values.get(arg, "")):
            return "wrong", detail
    return "acted", detail


def ask(model: persona.ResolvedModel, task: dict, sleep=time.sleep) -> tuple[str, str]:
    for attempt in range(RETRIES + 1):
        try:
            return score(task, driver.call(model, messages(task, model), tools.schemas()))
        except driver.UpstreamRetryable as exc:
            if attempt == RETRIES:
                return "error", str(exc)[:100]
            sleep(min(exc.retry_after or 5.0 * (attempt + 1), MAX_WAIT))
    raise AssertionError("unreachable")


def run(model: persona.ResolvedModel, tasks: list[dict], verbose: bool = False,
        delay: float = 0.0, sleep=time.sleep) -> dict[str, int]:
    """Run every task against one model. Raises UpstreamFatal: nothing else will work."""
    counts = dict.fromkeys(VERDICTS, 0)
    for number, task in enumerate(tasks):
        if delay and number:
            sleep(delay)
        verdict, detail = ask(model, task, sleep)
        counts[verdict] += 1
        if verbose or verdict == "error":
            log.out(f"  {verdict:7} {task['id']:24} {detail}")
    return counts


def summary(name: str, model: persona.ResolvedModel, counts: dict[str, int]) -> str:
    scored = sum(counts.values()) - counts["error"]
    rate = f"{100 * counts['acted'] / scored:.0f}%" if scored else "n/a"
    parts = "  ".join(f"{verdict} {counts[verdict]}" for verdict in VERDICTS)
    return f"{name} ({model.model}): acted when it should {rate} of {scored}  [{parts}]"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tool-use eval for Persist Bot personas.")
    parser.add_argument("conf", nargs="?", default=str(HOME / "conf" / "init.yaml"))
    parser.add_argument("-p", "--persona", action="append", help="persona to test; repeatable")
    parser.add_argument("-t", "--tasks", default=str(TASKS))
    parser.add_argument("-v", "--verbose", action="store_true", help="one line per task")
    parser.add_argument("--delay", type=float, default=0.0, help="seconds between requests")
    args = parser.parse_args(argv)

    try:
        cfg = config.load(args.conf)
        registry = persona.load(cfg.path.parent)
        names = args.persona or [cfg.persona]
        models = [(name, registry.resolve(cfg.name, name)) for name in names]
        tasks = load_tasks(Path(args.tasks))
    except (config.ConfigError, persona.PersonaError, OSError, ValueError) as exc:
        log.error(str(exc))
        return 2

    failed = False
    for name, model in models:
        log.out(f"{name}: {len(tasks)} tasks against {model.model} at {model.url}")
        try:
            counts = run(model, tasks, args.verbose, args.delay)
        except driver.UpstreamFatal as exc:
            log.out(f"{name}: stopped, the provider refused the request: {exc}")
            failed = True
            continue
        log.out(summary(name, model, counts))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
