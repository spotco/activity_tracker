"""Configuration loading and validation for the foreground activity tracker."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import sys
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "poll_interval_seconds": 5,
    "idle_threshold_seconds": 300,
    "excluded_programs": [],
    "program_aliases": {
        "GenshinImpact.exe": "Genshin Impact",
        "Cursor.exe": "Cursor",
    },
    "browser_integration": {
        "enabled": True,
        "stale_observation_seconds": 15,
        "browsers": {
            "brave": "brave.exe",
        },
    },
}


@dataclass(frozen=True)
class BrowserIntegrationConfig:
    enabled: bool
    stale_observation_seconds: int
    browsers: dict[str, str]

    @property
    def executable_to_id(self) -> dict[str, str]:
        return {
            _executable_key(executable): browser_id
            for browser_id, executable in self.browsers.items()
        }


class ConfigError(ValueError):
    """Raised when config.json is malformed or has invalid values."""


def resolve_config_directory(anchor: Path | None = None) -> Path:
    """Return the directory that holds config.json for this installation."""
    override = os.environ.get("ACTIVITY_TRACKER_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if getattr(sys, "frozen", False):
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            return Path(local_app_data) / "ActivityTracker"
    if anchor is not None:
        return anchor.resolve().parent
    return Path.cwd()


def ensure_installation_config(config_dir: Path) -> None:
    """Create %LOCALAPPDATA%\\ActivityTracker\\config.json for packaged hosts when missing."""
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "config.json"
    if path.exists():
        return
    path.write_bytes((json.dumps(DEFAULT_CONFIG, indent=2) + "\n").encode("utf-8"))


@dataclass(frozen=True)
class TrackerConfig:
    poll_interval_seconds: int
    idle_threshold_seconds: int
    excluded_programs: frozenset[str]
    program_aliases: dict[str, str]
    browser_integration: BrowserIntegrationConfig = field(
        default_factory=lambda: BrowserIntegrationConfig(
            False,
            DEFAULT_CONFIG["browser_integration"]["stale_observation_seconds"],
            dict(DEFAULT_CONFIG["browser_integration"]["browsers"]),
        )
    )

    @classmethod
    def load(cls, script_dir: Path | None = None, *, anchor: Path | None = None) -> "TrackerConfig":
        directory = script_dir if script_dir is not None else resolve_config_directory(anchor)
        if getattr(sys, "frozen", False):
            ensure_installation_config(directory)
        path = directory / "config.json"
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

        poll = _integer(raw, "poll_interval_seconds", 5, minimum=1, maximum=3600)
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

        browser_integration = _browser_integration(raw.get("browser_integration", DEFAULT_CONFIG["browser_integration"]))

        return cls(poll, idle, excluded, aliases, browser_integration)


def _browser_integration(raw: Any) -> BrowserIntegrationConfig:
    if raw is None:
        raw = DEFAULT_CONFIG["browser_integration"]
    if not isinstance(raw, dict):
        raise ConfigError("browser_integration must be a JSON object.")

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("browser_integration.enabled must be true or false.")

    stale = _integer(raw, "stale_observation_seconds", 15, minimum=1, maximum=3600)

    browsers_value = raw.get("browsers", {"brave": "brave.exe"})
    if not isinstance(browsers_value, dict) or not browsers_value:
        raise ConfigError("browser_integration.browsers must be a non-empty JSON object.")
    browsers: dict[str, str] = {}
    executable_ids: dict[str, str] = {}
    for browser_id, executable in browsers_value.items():
        if not isinstance(browser_id, str) or not browser_id.strip():
            raise ConfigError("Every browser_integration.browsers key must be a non-empty browser id.")
        if not isinstance(executable, str) or not executable.strip():
            raise ConfigError(f"Browser {browser_id!r} must map to a non-empty executable name.")
        normalized_id = browser_id.strip().casefold()
        executable_key = _executable_key(executable)
        previous_id = executable_ids.get(executable_key)
        if previous_id is not None and previous_id != normalized_id:
            raise ConfigError(
                "browser_integration.browsers must map each executable to at most one browser id "
                f"(duplicate {executable_key!r})."
            )
        executable_ids[executable_key] = normalized_id
        browsers[normalized_id] = os.path.basename(executable.strip())

    return BrowserIntegrationConfig(enabled, stale, browsers)


def _integer(raw: dict[str, Any], key: str, default: int, *, minimum: int, maximum: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigError(f"{key} must be an integer from {minimum} through {maximum}.")
    return value


def _executable_key(value: str) -> str:
    # Config entries are executable names, not paths. Basename handling is
    # forgiving while matching remains case-insensitive.
    return os.path.basename(value.strip()).casefold()
