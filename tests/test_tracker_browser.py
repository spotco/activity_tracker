from __future__ import annotations

import io
import json
import struct
import unittest

from browser_native_host import configure_binary_stdio, read_framed_message, validate_extension_message, write_framed_message
from tracker_browser import (
    DOMAIN_UNKNOWN,
    BrowserSnapshot,
    BrowserStateStore,
    handle_browser_state_command,
    normalize_hostname,
    resolve_browser_domain_label,
)
from tracker_config import BrowserIntegrationConfig, TrackerConfig
from tracker_core import ActivitySample, ActivityTracker


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


class BrowserNormalizationTests(unittest.TestCase):
    def test_normalize_hostname_lowercases_and_strips_trailing_dot(self) -> None:
        self.assertEqual(normalize_hostname("GitHub.COM."), "github.com")
        self.assertEqual(normalize_hostname("docs.google.com"), "docs.google.com")


class BrowserStoreTests(unittest.TestCase):
    def test_out_of_order_sequence_is_rejected(self) -> None:
        store = BrowserStateStore()
        store.ingest(browser_state(sequence=1), received_monotonic=10.0, reference_monotonic=10.0)
        with self.assertRaises(ValueError):
            store.ingest(browser_state(sequence=1), received_monotonic=11.0, reference_monotonic=11.0)

    def test_old_session_epoch_is_rejected(self) -> None:
        store = BrowserStateStore()
        store.ingest(browser_state(session_epoch=200, sequence=1), received_monotonic=1.0, reference_monotonic=1.0)
        with self.assertRaises(ValueError):
            store.ingest(
                browser_state(session_epoch=100, sequence=2),
                received_monotonic=2.0,
                reference_monotonic=2.0,
            )

    def test_heartbeat_refreshes_receipt_for_unchanged_hostname(self) -> None:
        store = BrowserStateStore(stale_observation_seconds=15.0)
        store.ingest(
            browser_state(hostname="stable.com", generation=3, sequence=1),
            received_monotonic=0.0,
            reference_monotonic=0.0,
        )
        self.assertIsNone(store.snapshot("brave", 65.0))
        store.ingest(
            browser_state(hostname="stable.com", generation=3, sequence=2),
            received_monotonic=50.0,
            reference_monotonic=50.0,
        )
        snapshot = store.snapshot("brave", 65.0)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.hostname, "stable.com")

    def test_plugin_status_report_marks_recent_messages_connected(self) -> None:
        store = BrowserStateStore(stale_observation_seconds=15.0)
        store.ingest(browser_state(), received_monotonic=100.0, reference_monotonic=100.0)
        report = store.plugin_status_report(frozenset({"brave"}), 105.0)
        self.assertTrue(report["connected"])
        self.assertTrue(report["browsers"]["brave"]["connected"])
        stale = store.plugin_status_report(frozenset({"brave"}), 200.0)
        self.assertFalse(stale["connected"])

    def test_note_ingest_attempt_surfaces_in_status_report(self) -> None:
        store = BrowserStateStore()
        store.note_ingest_attempt(
            "brave",
            at_monotonic=10.0,
            ok=False,
            error="Out-of-order browser sequence rejected.",
            session_epoch=5,
        )
        report = store.plugin_status_report(frozenset({"brave"}), 12.0)
        ingest = report["browsers"]["brave"]["last_ingest"]
        self.assertIsNotNone(ingest)
        self.assertFalse(ingest["ok"])
        self.assertIn("sequence", ingest["error"])

    def test_future_receipt_is_rejected(self) -> None:
        store = BrowserStateStore()
        with self.assertRaises(ValueError):
            store.ingest(
                browser_state(),
                received_monotonic=100.0,
                reference_monotonic=10.0,
            )

    def test_stale_snapshot_is_not_returned(self) -> None:
        store = BrowserStateStore(stale_observation_seconds=15.0)
        store.ingest(browser_state(), received_monotonic=0.0, reference_monotonic=0.0)
        self.assertIsNone(store.snapshot("brave", 20.0))


class BrowserAccountingTests(unittest.TestCase):
    def _browser_config(self) -> TrackerConfig:
        return TrackerConfig(
            poll_interval_seconds=5,
            idle_threshold_seconds=300,
            excluded_programs=frozenset(),
            program_aliases={},
            browser_integration=BrowserIntegrationConfig(
                enabled=True,
                stale_observation_seconds=15,
                browsers={"brave": "brave.exe"},
            ),
        )

    def test_stable_hostname_is_credited_to_domain_bucket(self) -> None:
        tracker = ActivityTracker(self._browser_config())
        snap = BrowserSnapshot("brave", True, "github.com", 4, 100, 0.0)
        tracker.observe(ActivitySample(0, _wall(0), "brave.exe", 0, False, snap))
        tracker.observe(
            ActivitySample(
                5,
                _wall(5),
                "brave.exe",
                5,
                False,
                BrowserSnapshot("brave", True, "github.com", 4, 100, 5.0),
            )
        )
        program = tracker.export_data(exported_at=_wall(5))["days"][0]["programs"][0]
        domains = {entry["hostname"]: entry["seconds"] for entry in program["browser_domains"]}
        self.assertEqual(domains["github.com"], 5.0)

    def test_long_lived_labeling_with_refreshed_receipt(self) -> None:
        store = BrowserStateStore(stale_observation_seconds=15.0)
        tracker = ActivityTracker(self._browser_config())
        sequence = 0
        for tick in range(0, 155, 5):
            sequence += 1
            store.ingest(
                browser_state(hostname="stable.com", generation=1, sequence=sequence, session_epoch=1),
                received_monotonic=float(tick),
                reference_monotonic=float(tick),
            )
            snapshot = store.snapshot("brave", float(tick))
            tracker.observe(
                ActivitySample(float(tick), _wall(tick), "brave.exe", float(tick), False, snapshot)
            )
        program = tracker.export_data(exported_at=_wall(150))["days"][0]["programs"][0]
        self.assertEqual(program["seconds"], 150.0)
        domains = {entry["hostname"]: entry["seconds"] for entry in program["browser_domains"]}
        self.assertEqual(domains["stable.com"], 150.0)

    def test_session_epoch_mismatch_is_domain_unknown_despite_same_generation(self) -> None:
        tracker = ActivityTracker(self._browser_config())
        tracker.observe(
            ActivitySample(
                0,
                _wall(0),
                "brave.exe",
                0,
                False,
                BrowserSnapshot("brave", True, "github.com", 5, 100, 0.0),
            )
        )
        tracker.observe(
            ActivitySample(
                5,
                _wall(5),
                "brave.exe",
                5,
                False,
                BrowserSnapshot("brave", True, "github.com", 5, 101, 5.0),
            )
        )
        domains = tracker.export_data(exported_at=_wall(5))["days"][0]["programs"][0]["browser_domains"]
        self.assertEqual(domains[0]["hostname"], DOMAIN_UNKNOWN)


class BrowserIntegrationGateTests(unittest.TestCase):
    def test_disabled_integration_rejects_ingest(self) -> None:
        store = BrowserStateStore()
        response = handle_browser_state_command(
            browser_state(),
            store,
            allowed_browsers=frozenset({"brave"}),
            integration_enabled=False,
            received_monotonic=1.0,
            reference_monotonic=1.0,
        )
        self.assertFalse(response["ok"])
        self.assertIsNone(store.snapshot("brave", 1.0))

    def test_resolve_label_requires_matching_session_epoch(self) -> None:
        previous = BrowserSnapshot("brave", True, "github.com", 5, 100, 0.0)
        current = BrowserSnapshot("brave", True, "github.com", 5, 101, 5.0)
        label = resolve_browser_domain_label(
            browser_id="brave",
            previous=previous,
            current=current,
            previous_monotonic=0.0,
            current_monotonic=5.0,
            stale_observation_seconds=15.0,
        )
        self.assertEqual(label, DOMAIN_UNKNOWN)


class NativeHostFramingTests(unittest.TestCase):
    def test_framed_message_preserves_embedded_newline_bytes(self) -> None:
        payload = {
            "type": "browser_state",
            "browser": "brave",
            "focused": True,
            "hostname": "a.com",
            "note": "line1\nline2\r\n",
            "generation": 0,
            "session_epoch": 1,
            "sequence": 1,
        }
        buffer = io.BytesIO()
        write_framed_message(payload, buffer)
        buffer.seek(0)
        self.assertEqual(read_framed_message(buffer), payload)
        framed = buffer.getvalue()
        self.assertEqual(int.from_bytes(framed[:4], "little"), len(framed) - 4)
        self.assertIn(b"line1\\nline2\\r\\n", framed)

    @unittest.skipUnless(__import__("sys").platform == "win32", "Windows only")
    def test_configure_binary_stdio_sets_binary_mode(self) -> None:
        configure_binary_stdio()

    def test_validate_extension_message_includes_session_fields(self) -> None:
        mapped = validate_extension_message(
            {
                "type": "browser_state",
                "browser": "brave",
                "focused": True,
                "hostname": "a.com",
                "generation": 1,
                "session_epoch": 9,
                "sequence": 2,
            }
        )
        self.assertEqual(mapped["session_epoch"], 9)
        self.assertEqual(mapped["sequence"], 2)

    def test_status_message_maps_to_ping(self) -> None:
        self.assertEqual(validate_extension_message({"type": "status"})["command"], "status")


def _wall(seconds: float):
    from datetime import datetime, timedelta, timezone as tz

    local = tz(timedelta(hours=-4))
    base = datetime(2026, 11, 1, 12, 0, tzinfo=local)
    return base + timedelta(seconds=seconds)


if __name__ == "__main__":
    unittest.main()
