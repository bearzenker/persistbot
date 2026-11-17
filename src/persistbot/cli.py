"""Command line: bot.py [conf.yaml] [-f prompt_file] [prompt words...]
               bot.py [conf.yaml] status
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

from persistbot import EXIT_CONFIG, EXIT_OK, __version__, link, log, persona, store, tick
from persistbot.config import BotConfig, ConfigError, load

DEFAULT_CONF = Path("conf") / "init.yaml"
CONF_SUFFIXES = (".yaml", ".yml")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bot.py",
        description="Run one tick of a Persist Bot, or show its state with 'status'.",
    )
    parser.add_argument("-f", "--file", metavar="PROMPT_FILE", help="queue the prompt in this file")
    parser.add_argument("--version", action="version", version=f"persistbot {__version__}")
    parser.add_argument(
        "words",
        nargs="*",
        metavar="ARG",
        help="optional config file (.yaml/.yml) first, then 'status' or prompt words",
    )
    return parser


def _read_prompt_file(config: BotConfig, name: str) -> tuple[str, str]:
    """A relative prompt path is looked up under the bot's base directory."""
    path = Path(name).expanduser()
    if not path.is_absolute():
        path = config.base / path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read prompt file {path}: {exc.strerror or exc}") from exc
    if not text.strip():
        raise ConfigError(f"prompt file {path} is empty")
    return text, f"file:{path}"


def _when(stamp: float | None) -> str:
    if not stamp:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))


def _resolve(config: BotConfig) -> persona.ResolvedModel:
    """The bot's own persona, from the conf directory its config file is in."""
    return persona.load(config.path.parent).resolve(config.name, config.persona)


def status(config: BotConfig) -> int:
    log.out(f"bot {config.name}  persona {config.persona}  state {config.db_path}")
    try:
        model = _resolve(config)
        log.out(f"model {model.model}  driver {model.driver}  url {model.url or '(default)'}")
    except persona.PersonaError as exc:
        log.out(f"persona does not resolve: {exc}")
    _status_tools(config)
    if not config.db_path.exists():
        log.out("no state yet: this bot has not run")
        return EXIT_OK
    with store.Store(config.db_path) as db:
        goals = db.goals()
        if not goals:
            log.out("no goals")
        now = time.time()
        for goal in goals:
            first_line = goal["prompt"].splitlines()[0][:70]
            log.out(
                f"goal {goal['id']} [{goal['state']}] failures={goal['failures']} "
                f"deferrals={goal['deferrals']} created={_when(goal['created_at'])} "
                f"source={goal['source']}"
            )
            log.out(f"  {first_line}")
            if goal["state"] in store.OPEN_STATES and goal["not_before"] > now:
                log.out(f"  waiting until {_when(goal['not_before'])}")
            if goal["summary"]:
                log.out(f"  summary: {goal['summary']}")
            for attempt in db.attempts(goal["id"]):
                outcome = attempt["outcome"] or "open"
                detail = f"  {attempt['detail']}" if attempt["detail"] else ""
                log.out(
                    f"  attempt {attempt['id']} {_when(attempt['started_at'])} "
                    f"{outcome}{detail}"
                )
    return EXIT_OK


def _status_tools(config: BotConfig) -> None:
    """Say whether the tools are separated from the harness, and prove it."""
    runner = link.Link.for_config(config)
    if not runner.separated:
        log.out(f"tools {runner.describe()} (no bot.user in the config)")
        return
    try:
        runner.open()
        log.out(f"tools {runner.describe()}, checked")
    except link.RunnerError as exc:
        log.out(f"tools cannot run: {exc}")
    finally:
        runner.close()


def main(argv: list[str] | None = None, home: Path | None = None) -> int:
    args = _parser().parse_intermixed_args(argv)
    words = list(args.words)
    home = Path(home) if home else Path.cwd()

    if words and words[0].lower().endswith(CONF_SUFFIXES):
        conf_path = Path(words.pop(0))
    else:
        conf_path = home / DEFAULT_CONF
    want_status = words == ["status"] and not args.file

    try:
        config = load(conf_path)
        if want_status:
            return status(config)
        config.base.mkdir(parents=True, exist_ok=True)
        log.setup(
            config.name,
            config.logging.mode,
            config.logging.dir,
            config.reporting.mode,
            config.reporting.dir,
        )
        prompts = []
        if args.file:
            prompts.append(_read_prompt_file(config, args.file))
        if " ".join(words).strip():
            prompts.append((" ".join(words), "cli"))
        try:
            model = _resolve(config)
        except persona.PersonaError as exc:
            # Queue the input before giving up, so a prompt is never dropped.
            tick.run(config, None, prompts)
            log.error(str(exc))
            return EXIT_CONFIG
        log.action(
            f"persona {config.persona}: model {model.model} via {model.driver} "
            f"at {model.url or 'the driver default'}"
        )
        return tick.run(config, model, prompts)
    except ConfigError as exc:
        log.error(str(exc))
        return EXIT_CONFIG
    except (OSError, sqlite3.Error, store.StoreError) as exc:
        log.error(f"cannot start: {exc}")
        return EXIT_CONFIG
    finally:
        log.close()
