from __future__ import annotations

import os
import threading
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from tracker_browser import DOMAIN_UNKNOWN, BrowserStateStore
from tracker_config import BrowserIntegrationConfig, TrackerConfig
from tracker_core import UTC, ActivitySample, ActivityTracker, _iter_local_segments
from tracker_sampling import reset_tracker_state


NY = ZoneInfo("America/New_York")


def browser_state(**overrides) -> dict:
    payload = {
        "command": "browser_state",
        "browser": "brave",
        "focused": True,
        "hostname": "a.com",
        "generation": 1,
        "session_epoch": 100,
        "sequence": 1,
    }
    payload.update(overrides)
    return payload


class LocalSegmentDstTests(unittest.TestCase):
    def test_fall_back_does_not_merge_repeated_one_am_hours(self) -> None:
        utc_start = datetime(2026, 11, 1, 5, 50, tzinfo=UTC)
        utc_end = datetime(2026, 11, 1, 6, 10, tzinfo=UTC)
        segments = _iter_local_segments(utc_start, utc_end, NY)
        hour_keys = [hour_key for _, _, _, hour_key in segments]
        self.assertEqual(len(hour_keys), 2)
        self.assertNotEqual(hour_keys[0], hour_keys[1])

    def test_spring_forward_splits_missing_local_hour(self) -> None:
        utc_start = datetime(2026, 3, 8, 6, 50, tzinfo=UTC)
        utc_end = datetime(2026, 3, 8, 7, 10, tzinfo=UTC)
        segments = _iter_local_segments(utc_start, utc_end, NY)
        hour_keys = [hour_key for _, _, _, hour_key in segments]
        self.assertEqual(len(hour_keys), 2)
        self.assertEqual(hour_keys[0], "2026-03-08T01:00:00-05:00")
        self.assertEqual(hour_keys[1], "2026-03-08T03:00:00-04:00")

    @unittest.skipUnless(os.name == "nt", "Windows timezone registry is required.")
    def test_runtime_local_timezone_splits_fall_back(self) -> None:
        import windows_api as wa

        wa._LOCAL_TIMEZONE = None
        tz = wa.local_timezone()
        utc_start = datetime(2026, 11, 1, 5, 50, tzinfo=UTC)
        utc_end = datetime(2026, 11, 1, 6, 10, tzinfo=UTC)
        hour_keys = [hour for _, _, _, hour in _iter_local_segments(utc_start, utc_end, tz)]
        if "New_York" not in str(tz):
            self.skipTest(f"Fall-back case requires America/New_York, got {tz!r}.")
        self.assertEqual(len(hour_keys), 2)
        self.assertNotEqual(hour_keys[0], hour_keys[1])


class BrowserPartitionRegressionTests(unittest.TestCase):
    def test_enabled_browser_without_extension_is_all_domain_unknown(self) -> None:
        config = TrackerConfig(
            5,
            300,
            frozenset(),
            {},
            BrowserIntegrationConfig(True, 15, {"brave": "brave.exe"}),
        )
        tracker = ActivityTracker(config)
        start = datetime(2026, 11, 1, 12, 0, tzinfo=NY)
        tracker.observe(ActivitySample(0, start, "brave.exe", 0, False, None))
        tracker.observe(ActivitySample(5, start + timedelta(seconds=5), "brave.exe", 5, False, None))
        program = tracker.export_data(exported_at=start + timedelta(seconds=5))["days"][0]["programs"][0]
        domains = {entry["hostname"]: entry["seconds"] for entry in program["browser_domains"]}
        self.assertEqual(domains, {DOMAIN_UNKNOWN: 5.0})

    def test_tracker_reset_clears_browser_session_state(self) -> None:
        store = BrowserStateStore()
        config = TrackerConfig(
            5,
            300,
            frozenset(),
            {},
            BrowserIntegrationConfig(True, 15, {"brave": "brave.exe"}),
        )
        tracker = ActivityTracker(config)
        lock = threading.RLock()
        store.ingest(browser_state(session_epoch=10, sequence=1), received_monotonic=1.0, reference_monotonic=1.0)
        reset_tracker_state(tracker, store, lock)
        store.ingest(browser_state(session_epoch=99, sequence=1), received_monotonic=2.0, reference_monotonic=2.0)
        self.assertIsNotNone(store.snapshot("brave", 2.0))

    def test_service_worker_restart_accepts_new_session_epoch(self) -> None:
        store = BrowserStateStore()
        store.ingest(browser_state(session_epoch=10, sequence=1), received_monotonic=1.0, reference_monotonic=1.0)
        store.ingest(browser_state(session_epoch=11, sequence=1), received_monotonic=2.0, reference_monotonic=2.0)
        with self.assertRaises(ValueError):
            store.ingest(browser_state(session_epoch=10, sequence=2), received_monotonic=3.0, reference_monotonic=3.0)


if __name__ == "__main__":
    unittest.main()
