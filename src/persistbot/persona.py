"""Personas: three lookups joined by two keys. See docs/persona-pattern.md.

The agent name is a label. The persona names a recipe in conf/persona*.json.
The persona's provider, lowercased, keys conf/provider.json and conf/keys.json.
Everything is read once by `load`; `Registry.resolve` then builds a
self-contained `ResolvedModel` and nothing downstream reads the files again.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from persistbot.config import NAME_RE

PERSONA_FIELDS = {
    "model", "provider", "driver", "url", "vendor", "character", "parameters", "price",
}
PROVIDER_FIELDS = {"driver", "url", "parameters"}
# Persona-level names that must never appear inside `parameters`.
RESERVED_PARAMETERS = {"character", "vendor", "model", "driver", "provider", "url", "price"}


class PersonaError(Exception):
    """Base for every failure to turn a persona name into a usable model."""


class PersonaNotFoundError(PersonaError):
    """No persona file defines this name."""


class ConfigurationError(PersonaError):
    """A persona or provider file is missing, malformed or inconsistent."""


class AuthenticationError(PersonaError):
    """The keys file is missing or has no entry for the provider."""


class UnknownDriverError(ConfigurationError, ValueError):
    """The resolved driver is not one this build can call."""


@dataclass(frozen=True)
class ResolvedModel:
    name: str
    model: str
    provider: str
    driver: str
    url: str | None = None
    vendor: str | None = None
    character: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    auth: dict[str, Any] = field(default_factory=dict, repr=False)

    def identity(self) -> str:
        """The line the system prompt ends with."""
        line = f"Your name for this session is {self.name}."
        return f"{line} {self.character}" if self.character else line


def _read_json(path: Path, error: type[PersonaError]) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise error(f"cannot read {path}: {exc.strerror or exc}") from exc
    except ValueError as exc:
        raise error(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise error(f"{path}: expected a JSON object")
    return doc


def _string(where: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{where}: expected a non-empty string")
    return value.strip()


def _is_number_text(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def check_parameters(where: str, value: Any) -> dict[str, Any]:
    """Validate one layer of model parameters and return a copy."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigurationError(f"{where}: expected a map")
    for key, val in value.items():
        if key in RESERVED_PARAMETERS:
            raise ConfigurationError(
                f"{where}: '{key}' belongs on the persona, not inside parameters"
            )
        if isinstance(val, str) and _is_number_text(val):
            raise ConfigurationError(
                f"{where}.{key}: write the number {val} without quotes"
            )
    timeout = value.get("timeout")
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0
    ):
        raise ConfigurationError(f"{where}.timeout: expected a positive number of seconds")
    return dict(value)


def discover_persona_files(conf_dir: Path) -> list[Path]:
    """`persona.json` first, then every other `persona*.json` by filename."""
    found = sorted(p for p in conf_dir.glob("persona*.json") if p.is_file())
    first = [p for p in found if p.name == "persona.json"]
    return first + [p for p in found if p.name != "persona.json"]


def _persona_map(path: Path) -> dict:
    doc = _read_json(path, ConfigurationError)
    inner = doc.get("persona")
    if set(doc) == {"persona"} and isinstance(inner, dict) and "model" not in inner:
        return inner
    return doc


def _check_persona(where: str, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{where}: expected a map")
    extra = sorted(k for k in raw if k not in PERSONA_FIELDS)
    if extra:
        raise ConfigurationError(f"{where}: unknown field(s): {', '.join(extra)}")
    for key in ("model", "provider"):
        if key not in raw:
            raise ConfigurationError(f"{where}: '{key}' is required")
    definition = dict(raw)
    for key in ("model", "provider", "driver", "url"):
        if definition.get(key) is not None:
            definition[key] = _string(f"{where}.{key}", definition[key])
    for key in ("character", "vendor"):
        if definition.get(key) is not None and not isinstance(definition[key], str):
            raise ConfigurationError(f"{where}.{key}: expected text")
    definition["provider"] = definition["provider"].lower()
    definition["parameters"] = check_parameters(f"{where}.parameters", raw.get("parameters"))
    return definition


def _lower_keys(path: Path, doc: dict) -> dict:
    lowered: dict = {}
    for key, value in doc.items():
        if key.lower() in lowered:
            raise ConfigurationError(f"{path}: provider '{key.lower()}' is given twice")
        lowered[key.lower()] = value
    return lowered


def _example(conf_dir: Path, name: str) -> str:
    return f"copy {conf_dir / 'examples' / name} to {conf_dir / name} and edit it"


class Registry:
    """The persona, provider and keys files of one conf directory, loaded."""

    def __init__(self, conf_dir: Path) -> None:
        self.conf_dir = conf_dir
        self.personas: dict[str, dict[str, Any]] = {}
        self.sources: dict[str, Path] = {}
        self.providers: dict[str, dict[str, Any]] = {}
        self.keys: dict[str, dict[str, Any]] = {}

    def resolve(
        self,
        name: str,
        persona: str,
        character: str | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> ResolvedModel:
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise ConfigurationError(
                f"agent name '{name}' may contain only letters, digits, '_' and '-'"
            )
        definition = self.personas.get(persona)
        if definition is None:
            known = ", ".join(sorted(self.personas)) or "none"
            raise PersonaNotFoundError(
                f"persona '{persona}' is not defined in {self.conf_dir} (defined: {known})"
            )
        where = f"persona '{persona}'"
        provider = definition["provider"]
        entry = self.providers.get(provider)
        if entry is None:
            raise ConfigurationError(
                f"{where}: provider '{provider}' has no entry in "
                f"{self.conf_dir / 'provider.json'}"
            )
        auth = self.keys.get(provider)
        if auth is None:
            raise AuthenticationError(
                f"{where}: provider '{provider}' has no entry in {self.conf_dir / 'keys.json'}"
            )
        driver = definition.get("driver") or entry.get("driver")
        if not driver:
            raise ConfigurationError(
                f"{where}: neither the persona nor provider '{provider}' names a driver"
            )
        from persistbot.driver import DRIVERS  # late: driver imports this module's types

        if driver not in DRIVERS:
            raise UnknownDriverError(
                f"{where}: unknown driver '{driver}' (available: {', '.join(sorted(DRIVERS))})"
            )
        merged = dict(entry["parameters"])
        merged.update(definition["parameters"])
        caller = check_parameters(f"{where}: caller parameters", parameters)
        if character is not None and not isinstance(character, str):
            raise ConfigurationError(f"{where}: caller character: expected text")
        merged.update({k: v for k, v in caller.items() if v is not None})
        return ResolvedModel(
            name=name,
            model=definition["model"],
            provider=provider,
            driver=driver,
            url=definition.get("url") or entry.get("url"),
            vendor=definition.get("vendor"),
            character=(character or "").strip() or (definition.get("character") or "").strip(),
            parameters=merged,
            auth=dict(auth),
        )


def load(conf_dir: str | Path) -> Registry:
    """Read every persona, provider and keys file under `conf_dir`."""
    conf_dir = Path(conf_dir)
    registry = Registry(conf_dir)

    files = discover_persona_files(conf_dir)
    if not files:
        raise ConfigurationError(
            f"no persona file in {conf_dir}: {_example(conf_dir, 'persona.json')}"
        )
    for path in files:
        for name, raw in _persona_map(path).items():
            if name in registry.personas:
                raise ConfigurationError(
                    f"persona '{name}' is defined in both "
                    f"{registry.sources[name].name} and {path.name}"
                )
            registry.personas[name] = _check_persona(f"{path.name}: persona '{name}'", raw)
            registry.sources[name] = path

    path = conf_dir / "provider.json"
    if not path.is_file():
        raise ConfigurationError(f"no {path}: {_example(conf_dir, 'provider.json')}")
    for name, raw in _lower_keys(path, _read_json(path, ConfigurationError)).items():
        where = f"{path.name}: provider '{name}'"
        if not isinstance(raw, dict):
            raise ConfigurationError(f"{where}: expected a map")
        extra = sorted(k for k in raw if k not in PROVIDER_FIELDS)
        if extra:
            raise ConfigurationError(f"{where}: unknown field(s): {', '.join(extra)}")
        entry = dict(raw)
        for key in ("driver", "url"):
            if entry.get(key) is not None:
                entry[key] = _string(f"{where}.{key}", entry[key])
        entry["parameters"] = check_parameters(f"{where}.parameters", raw.get("parameters"))
        registry.providers[name] = entry

    path = conf_dir / "keys.json"
    if not path.is_file():
        raise AuthenticationError(f"no {path}: {_example(conf_dir, 'keys.json')}")
    for name, raw in _lower_keys(path, _read_json(path, AuthenticationError)).items():
        if not isinstance(raw, dict):
            raise AuthenticationError(f"{path.name}: entry '{name}' must be a map")
        registry.keys[name] = raw
    return registry
