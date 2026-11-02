"""Browser hostname observations from the Chromium extension (labels only, no timing)."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
import threading
from typing import Any

DOMAIN_UNKNOWN = "domain_unknown"
_FUTURE_MONOTONIC_TOLERANCE_SECONDS = 2.0

_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


@dataclass(frozen=True)
class BrowserSnapshot:
    """Immutable browser label state captured at a Windows poll."""

    browser_id: str
    focused: bool
    hostname: str | None
    generation: int
    session_epoch: int
    received_monotonic: float


class BrowserStateStore:
    """Latest validated observation per browser identity."""

    def __init__(self, *, stale_observation_seconds: float = 15.0) -> None:
        self.stale_observation_seconds = stale_observation_seconds
        self._lock = threading.RLock()
        self._latest: dict[str, BrowserSnapshot] = {}
        self._session_epoch: dict[str, int] = {}
        self._last_sequence: dict[str, int] = {}
        self._last_generation: dict[str, int] = {}
        self._last_ingest: dict[str, dict[str, Any]] = {}

    def clear(self) -> None:
        with self._lock:
            self._latest.clear()
            self._session_epoch.clear()
            self._last_sequence.clear()
            self._last_generation.clear()
            self._last_ingest.clear()

    def note_ingest_attempt(
        self,
        browser_id: str,
        *,
        at_monotonic: float,
        ok: bool,
        error: str | None = None,
        session_epoch: int | None = None,
    ) -> None:
        with self._lock:
            self._last_ingest[browser_id] = {
                "ok": ok,
                "error": error,
                "session_epoch": session_epoch,
                "seconds_ago": 0.0,
                "at_monotonic": at_monotonic,
            }

    def plugin_status_report(
        self,
        browser_ids: frozenset[str],
        at_monotonic: float,
    ) -> dict[str, Any]:
        """Summarize recent extension messages for agent status commands."""
        with self._lock:
            browsers: dict[str, Any] = {}
            any_connected = False
            for browser_id in sorted(browser_ids):
                latest = self._latest.get(browser_id)
                ingest = self._last_ingest.get(browser_id)
                ingest_info: dict[str, Any] | None = None
                if ingest is not None:
                    ingest_age = max(0.0, at_monotonic - float(ingest["at_monotonic"]))
                    ingest_info = {
                        "ok": ingest["ok"],
                        "error": ingest["error"],
                        "session_epoch": ingest["session_epoch"],
                        "seconds_ago": round(ingest_age, 3),
                    }
                if latest is None:
                    browsers[browser_id] = {
                        "connected": False,
                        "last_message_seconds_ago": None,
                        "last_ingest": ingest_info,
                    }
                    continue
                age = max(0.0, at_monotonic - latest.received_monotonic)
                connected = age <= self.stale_observation_seconds
                if connected:
                    any_connected = True
                browsers[browser_id] = {
                    "connected": connected,
                    "last_message_seconds_ago": round(age, 3),
                    "session_epoch": latest.session_epoch,
                    "generation": latest.generation,
                    "focused": latest.focused,
                    "hostname": latest.hostname,
                    "last_ingest": ingest_info,
                }
            return {
                "connected": any_connected,
                "stale_after_seconds": self.stale_observation_seconds,
                "browsers": browsers,
            }

    def snapshot(self, browser_id: str, at_monotonic: float) -> BrowserSnapshot | None:
        """Return a copy of the latest observation if still fresh at poll time."""
        with self._lock:
            latest = self._latest.get(browser_id)
            if latest is None:
                return None
            if at_monotonic - latest.received_monotonic > self.stale_observation_seconds:
                return None
            if latest.received_monotonic > at_monotonic + _FUTURE_MONOTONIC_TOLERANCE_SECONDS:
                return None
            return latest

    def ingest(
        self,
        payload: dict[str, Any],
        *,
        received_monotonic: float,
        reference_monotonic: float | None = None,
    ) -> None:
        """Accept one validated browser_state message."""
        if not math.isfinite(received_monotonic):
            raise ValueError("received_monotonic must be finite.")
        if reference_monotonic is not None:
            if not math.isfinite(reference_monotonic):
                raise ValueError("reference_monotonic must be finite.")
            if received_monotonic > reference_monotonic + _FUTURE_MONOTONIC_TOLERANCE_SECONDS:
                raise ValueError("Browser observation timestamp is in the future.")
            if received_monotonic + self.stale_observation_seconds + _FUTURE_MONOTONIC_TOLERANCE_SECONDS < reference_monotonic:
                raise ValueError("Browser observation timestamp is too stale to accept.")

        browser_id = _require_string(payload, "browser", maximum=32).casefold()
        focused = payload.get("focused")
        if not isinstance(focused, bool):
            raise ValueError("focused must be a boolean.")
        generation = payload.get("generation")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError("generation must be a non-negative integer.")
        session_epoch = payload.get("session_epoch")
        if isinstance(session_epoch, bool) or not isinstance(session_epoch, int) or session_epoch < 0:
            raise ValueError("session_epoch must be a non-negative integer.")
        sequence = payload.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("sequence must be a non-negative integer.")

        hostname_raw = payload.get("hostname")
        hostname: str | None
        if hostname_raw is None:
            hostname = None
        elif not isinstance(hostname_raw, str) or not hostname_raw.strip():
            raise ValueError("hostname must be a string or null.")
        else:
            hostname = normalize_hostname(hostname_raw.strip())

        with self._lock:
            current_epoch = self._session_epoch.get(browser_id)
            if current_epoch is not None and session_epoch < current_epoch:
                raise ValueError("Browser session epoch is from a previous extension session.")
            if current_epoch is not None and session_epoch == current_epoch:
                last_sequence = self._last_sequence.get(browser_id, -1)
                if sequence <= last_sequence:
                    raise ValueError("Out-of-order browser sequence rejected.")
                last_generation = self._last_generation.get(browser_id)
                if last_generation is not None and generation < last_generation:
                    raise ValueError("Out-of-order browser generation rejected.")
            else:
                self._session_epoch[browser_id] = session_epoch
                self._last_sequence[browser_id] = -1
                self._last_generation[browser_id] = -1

            self._last_sequence[browser_id] = sequence
            self._last_generation[browser_id] = generation
            self._latest[browser_id] = BrowserSnapshot(
                browser_id=browser_id,
                focused=focused,
                hostname=hostname,
                generation=generation,
                session_epoch=session_epoch,
                received_monotonic=received_monotonic,
            )


def normalize_hostname(value: str) -> str:
    """Lowercase, drop a trailing dot, and punycode-encode non-ASCII labels."""
    host = value.strip().lower().rstrip(".")
    if not host or len(host) > 253:
        raise ValueError("hostname is invalid.")
    if not all(label and len(label) <= 63 for label in host.split(".")):
        raise ValueError("hostname is invalid.")
    try:
        host.encode("ascii")
    except UnicodeError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("hostname is invalid.") from exc
    if _HOSTNAME_RE.fullmatch(host) is None:
        raise ValueError("hostname is invalid.")
    return host


def resolve_browser_domain_label(
    *,
    browser_id: str | None,
    previous: BrowserSnapshot | None,
    current: BrowserSnapshot | None,
    previous_monotonic: float,
    current_monotonic: float,
    stale_observation_seconds: float,
) -> str | None:
    """Return a hostname, domain_unknown, or None when domain labeling does not apply."""
    if browser_id is None:
        return None

    def endpoint_valid(
        snap: BrowserSnapshot | None,
        at_monotonic: float,
    ) -> bool:
        if snap is None or snap.browser_id != browser_id:
            return False
        if at_monotonic - snap.received_monotonic > stale_observation_seconds:
            return False
        if snap.received_monotonic > at_monotonic + _FUTURE_MONOTONIC_TOLERANCE_SECONDS:
            return False
        if not snap.focused or snap.hostname is None:
            return False
        return True

    if (
        endpoint_valid(previous, previous_monotonic)
        and endpoint_valid(current, current_monotonic)
        and previous.hostname == current.hostname
        and previous.generation == current.generation
        and previous.session_epoch == current.session_epoch
    ):
        return previous.hostname
    return DOMAIN_UNKNOWN


def handle_browser_state_command(
    request: dict[str, Any],
    store: BrowserStateStore,
    *,
    allowed_browsers: frozenset[str],
    integration_enabled: bool = True,
    received_monotonic: float,
    reference_monotonic: float | None = None,
) -> dict[str, Any]:
    if str(request.get("command", "")).casefold() != "browser_state":
        raise ValueError("Unexpected command.")
    if not integration_enabled:
        return {
            "ok": False,
            "command": "browser_state",
            "error": "Browser integration is disabled.",
        }
    browser = _require_string(request, "browser", maximum=32).casefold()
    session_epoch_raw = request.get("session_epoch")
    session_epoch = session_epoch_raw if isinstance(session_epoch_raw, int) else None
    if browser not in allowed_browsers:
        response = {"ok": False, "command": "browser_state", "error": "Browser identity is not allowed."}
        store.note_ingest_attempt(
            browser,
            at_monotonic=received_monotonic,
            ok=False,
            error=str(response["error"]),
            session_epoch=session_epoch,
        )
        return response
    try:
        store.ingest(
            request,
            received_monotonic=received_monotonic,
            reference_monotonic=reference_monotonic,
        )
    except ValueError as exc:
        response = {"ok": False, "command": "browser_state", "error": str(exc)}
        store.note_ingest_attempt(
            browser,
            at_monotonic=received_monotonic,
            ok=False,
            error=str(exc),
            session_epoch=session_epoch,
        )
        return response
    store.note_ingest_attempt(
        browser,
        at_monotonic=received_monotonic,
        ok=True,
        error=None,
        session_epoch=session_epoch,
    )
    return {"ok": True, "command": "browser_state"}


def _require_string(payload: dict[str, Any], key: str, *, maximum: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{key} must be a non-empty string up to {maximum} characters.")
    return value.strip()
