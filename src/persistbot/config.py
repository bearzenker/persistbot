"""Load and validate a bot's YAML configuration.

Relative paths in the file resolve against `venv`, so `base: raven` under
`venv: /opt/persistbot` is `/opt/persistbot/raven`.

Two directories belong to a bot. `base` is the harness's: state, logs and
the incoming queue. `work` is the model's: the only place its tools write.
With `user` set the tools run as that Linux account, which must not be able
to reach `base`; `work` is then `/opt/bot-<user>` beside the install
directory unless the file says otherwise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
USER_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
RUNNER_DIR = Path("/run/persistbot")  # where the runner socket units listen
LOG_MODES = ("local",)

BOT_KEYS = {
    "venv", "base", "system", "name", "persona", "friends", "logging",
    "reporting", "skills", "incoming", "limits", "user", "work", "runner",
}
SINK_KEYS = {"mode", "dir"}
LIMIT_KEYS = {"max_turns", "max_failures"}


class ConfigError(Exception):
    """The configuration is missing, malformed or inconsistent."""


@dataclass(frozen=True)
class Sink:
    mode: str
    dir: Path


@dataclass(frozen=True)
class Limits:
    max_turns: int = 12
    max_failures: int = 3


@dataclass(frozen=True)
class BotConfig:
    path: Path
    venv: Path
    base: Path
    system: Path
    name: str
    persona: str
    friends: dict[str, str]
    logging: Sink
    reporting: Sink
    skills: tuple[str, ...]
    incoming: Path
    work: Path                     # the model's working directory
    user: str | None = None        # Linux account the tools run as; None: the harness's own
    runner: Path | None = None     # the runner's socket; None: a child process
    limits: Limits = field(default_factory=Limits)

    @property
    def db_path(self) -> Path:
        return self.base / "state.db"

    @property
    def lock_path(self) -> Path:
        return self.base / "bot.lock"


def _unknown(where: str, got: dict, allowed: set[str]) -> None:
    extra = sorted(str(k) for k in got if k not in allowed)
    if extra:
        raise ConfigError(f"{where}: unknown key(s): {', '.join(extra)}")


def _as_map(where: str, value: Any) -> dict:
    """Accept a map, or the design doc's list of single-key maps."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list) and all(isinstance(i, dict) for i in value):
        merged: dict = {}
        for item in value:
            for key, val in item.items():
                if key in merged:
                    raise ConfigError(f"{where}: key given twice: {key}")
                merged[key] = val
        return merged
    raise ConfigError(f"{where}: expected a map")


def _text(where: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: expected a non-empty string")
    return value.strip()


def _path(venv: Path, where: str, value: Any) -> Path:
    path = Path(_text(where, value)).expanduser()
    return path if path.is_absolute() else (venv / path).resolve()


def _sink(venv: Path, where: str, value: Any, default_dir: Path) -> Sink:
    raw = _as_map(where, value)
    _unknown(where, raw, SINK_KEYS)
    mode = _text(f"{where}.mode", raw.get("mode", "local"))
    if mode not in LOG_MODES:
        raise ConfigError(
            f"{where}.mode: '{mode}' is not supported (supported: {', '.join(LOG_MODES)})"
        )
    directory = _path(venv, f"{where}.dir", raw["dir"]) if "dir" in raw else default_dir
    return Sink(mode=mode, dir=directory)


def _limits(value: Any) -> Limits:
    raw = _as_map("bot.limits", value)
    _unknown("bot.limits", raw, LIMIT_KEYS)
    for key, val in raw.items():
        if isinstance(val, bool) or not isinstance(val, int) or val < 1:
            raise ConfigError(f"bot.limits.{key}: expected a positive integer")
    return Limits(**raw)


def _skill(value: Any) -> str:
    """A skill is a name under the bot's base directory. Phase 9 gives it content."""
    name = _text("bot.skills", value)
    if not NAME_RE.fullmatch(name):
        raise ConfigError(f"bot.skills: '{name}' may contain only letters, digits, '_' and '-'")
    return name


def load(path: str | Path) -> BotConfig:
    path = Path(path)
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

    if not isinstance(doc, dict) or not isinstance(doc.get("bot"), dict):
        raise ConfigError(f"{path}: expected a top-level 'bot' map")
    _unknown(str(path), doc, {"bot"})
    bot = doc["bot"]
    _unknown("bot", bot, BOT_KEYS)

    for key in ("venv", "name", "persona"):
        if key not in bot:
            raise ConfigError(f"bot.{key} is required")

    venv = Path(_text("bot.venv", bot["venv"]))
    if not venv.is_absolute():
        raise ConfigError("bot.venv must be an absolute path")

    name = _text("bot.name", bot["name"])
    if not NAME_RE.fullmatch(name):
        raise ConfigError("bot.name may contain only letters, digits, '_' and '-'")

    base = _path(venv, "bot.base", bot["base"]) if "base" in bot else venv
    if base.exists() and not base.is_dir():
        raise ConfigError(f"bot.base is not a directory: {base}")

    system_name = _text("bot.system", bot.get("system", "system"))
    system = base / system_name
    if not system.suffix:
        system = system.with_suffix(".txt")

    friends = {}
    for persona, topic in _as_map("bot.friends", bot.get("friends")).items():
        friends[_text("bot.friends", persona)] = _text(f"bot.friends.{persona}", topic)

    skills = bot.get("skills") or []
    if not isinstance(skills, list):
        raise ConfigError("bot.skills: expected a list")

    user = None
    if bot.get("user") is not None:
        user = _text("bot.user", bot["user"])
        if not USER_RE.fullmatch(user):
            raise ConfigError(f"bot.user: '{user}' is not a Linux account name")
    if user is None and "runner" in bot:
        raise ConfigError("bot.runner needs bot.user: the account the runner must run as")
    if "work" in bot:
        work = _path(venv, "bot.work", bot["work"])
    elif user is not None:
        work = venv.parent / f"bot-{user}"
    else:
        work = base / "work"
    if user is not None and (work == base or base in work.parents):
        raise ConfigError(
            f"bot.work ({work}) is inside bot.base ({base}), which the tools account must not reach"
        )
    runner = None
    if user is not None:
        runner = (
            _path(venv, "bot.runner", bot["runner"]) if "runner" in bot
            else RUNNER_DIR / f"{user}.sock"
        )

    logging = _sink(venv, "bot.logging", bot.get("logging"), base / "logs")
    return BotConfig(
        path=path.resolve(),
        venv=venv,
        base=base,
        system=system,
        name=name,
        persona=_text("bot.persona", bot["persona"]),
        friends=friends,
        logging=logging,
        reporting=_sink(venv, "bot.reporting", bot.get("reporting"), logging.dir),
        skills=tuple(_skill(s) for s in skills),
        incoming=(
            _path(venv, "bot.incoming", bot["incoming"])
            if "incoming" in bot
            else base / "incoming"
        ),
        work=work,
        user=user,
        runner=runner,
        limits=_limits(bot.get("limits")),
    )
