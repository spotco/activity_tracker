"""Shared sampling helpers for tray and CLI trackers."""

from __future__ import annotations

import threading
from typing import Any

from tracker_browser import BrowserStateStore, handle_browser_state_command
from tracker_config import TrackerConfig
from tracker_core import ActivitySample, ActivityTracker
from windows_api import take_sample, windows_monotonic_seconds


def make_observation(
    tracker: ActivityTracker,
    browser_store: BrowserStateStore,
    config: TrackerConfig,
) -> ActivitySample:
    raw = take_sample()
    browser_snapshot = None
    if config.browser_integration.enabled and raw.executable:
        executable = raw.executable
        browser_id = config.browser_integration.executable_to_id.get(executable.casefold())
        if browser_id is not None:
            browser_snapshot = browser_store.snapshot(browser_id, raw.monotonic)
    return ActivitySample(
        raw.monotonic,
        raw.wall_time,
        raw.executable,
        raw.last_input_monotonic,
        raw.desktop_locked,
        browser_snapshot,
    )


def observe_with_browser_context(
    tracker: ActivityTracker,
    browser_store: BrowserStateStore,
    config: TrackerConfig,
    lock: threading.RLock,
) -> None:
    with lock:
        tracker.observe(make_observation(tracker, browser_store, config))


def browser_plugin_status(
    browser_store: BrowserStateStore,
    config: TrackerConfig,
) -> dict[str, Any]:
    integration = config.browser_integration
    if not integration.enabled:
        return {"enabled": False, "connected": False, "browsers": {}}
    report = browser_store.plugin_status_report(
        frozenset(integration.browsers),
        windows_monotonic_seconds(),
    )
    return {"enabled": True, **report}


def build_status_response(
    tracker: ActivityTracker,
    browser_store: BrowserStateStore,
    config: TrackerConfig,
) -> dict[str, Any]:
    snapshot = tracker.export_data()
    programs = [program for day in snapshot["days"] for program in day["programs"]]
    return {
        "ok": True,
        "command": "status",
        "running": True,
        "tracking_started_at": snapshot["tracking_started_at"],
        "poll_interval_seconds": snapshot["poll_interval_seconds"],
        "idle_threshold_seconds": snapshot["idle_threshold_seconds"],
        "day_count": len(snapshot["days"]),
        "program_count": len(programs),
        "tracked_seconds": round(sum(item["seconds"] for item in programs), 3),
        "browser_plugin": browser_plugin_status(browser_store, config),
    }


def ingest_browser_state_request(
    request: dict,
    browser_store: BrowserStateStore,
    config: TrackerConfig,
    lock: threading.RLock,
) -> dict:
    reference = windows_monotonic_seconds()
    with lock:
        return handle_browser_state_command(
            request,
            store=browser_store,
            allowed_browsers=frozenset(config.browser_integration.browsers),
            integration_enabled=config.browser_integration.enabled,
            received_monotonic=reference,
            reference_monotonic=reference,
        )


def reset_tracker_state(
    tracker: ActivityTracker,
    browser_store: BrowserStateStore,
    lock: threading.RLock,
    *,
    reset_at=None,
) -> None:
    with lock:
        tracker.reset(reset_at=reset_at)
        browser_store.clear()
