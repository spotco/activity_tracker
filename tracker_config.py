"""Configuration loading and validation for the foreground activity tracker."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "poll_interval_seconds": 30,
    "idle_threshold_seconds": 300,
    "excluded_programs": [],
    "program_aliases": {
        "GenshinImpact.exe": "Genshin Impact",
        "Cursor.exe": "Cursor",
    },
}


class ConfigError(ValueError):
    """Raised when config.json is malformed or has invalid values."""


@dataclass(frozen=True)
class TrackerConfig:
    poll_interval_seconds: int
    idle_threshold_seconds: int
    excluded_programs: frozenset[str]
    program_aliases: dict[str, str]

    @classmethod
    def load(cls, script_dir: Path) -> "TrackerConfig":
        path = script_dir / "config.json"
        if not path.exists():
            try:
                path.write_text(
                    json.dumps(DEFAULT_CONFIG, indent=2) + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                raise ConfigError(f"Could not create {path}: {exc}") from exc

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Could not read valid JSON from {path}: {exc}") from exc

        if not isinstance(raw, dict):
            raise ConfigError("The top level of config.json must be a JSON object.")

        poll = _integer(raw, "poll_interval_seconds", 30, minimum=1, maximum=3600)
        idle = _integer(raw, "idle_threshold_seconds", 300, minimum=1, maximum=86400)

        excluded_value = raw.get("excluded_programs", [])
        if not isinstance(excluded_value, list) or any(
            not isinstance(item, str) or not item.strip() for item in excluded_value
        ):
            raise ConfigError("excluded_programs must be an array of non-empty executable names.")
        excluded = frozenset(_executable_key(item) for item in excluded_value)

        aliases_value = raw.get("program_aliases", {})
        if not isinstance(aliases_value, dict):
            raise ConfigError("program_aliases must be a JSON object of executable names to display names.")
        aliases: dict[str, str] = {}
        for executable, name in aliases_value.items():
            if not isinstance(executable, str) or not executable.strip():
                raise ConfigError("Every program_aliases key must be a non-empty executable name.")
            if not isinstance(name, str) or not name.strip():
                raise ConfigError(f"Alias for {executable!r} must be a non-empty display name.")
            aliases[_executable_key(executable)] = name.strip()

        return cls(poll, idle, excluded, aliases)


def _integer(raw: dict[str, Any], key: str, default: int, *, minimum: int, maximum: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigError(f"{key} must be an integer from {minimum} through {maximum}.")
    return value


def _executable_key(value: str) -> str:
    # Config entries are executable names, not paths. Basename handling is
    # forgiving while matching remains case-insensitive.
    return os.path.basename(value.strip()).casefold()
