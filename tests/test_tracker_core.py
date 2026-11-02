from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import unittest

from tracker_config import BrowserIntegrationConfig, TrackerConfig
from tracker_core import ActivitySample, ActivityTracker


LOCAL = timezone(timedelta(hours=-4))
BASE_WALL = datetime(2026, 10, 25, 12, 0, tzinfo=LOCAL)


def make_config(
    *,
    poll: int = 30,
    idle: int = 300,
    excluded: tuple[str, ...] = (),
    aliases: dict[str, str] | None = None,
    browser_enabled: bool = False,
) -> TrackerConfig:
    return TrackerConfig(
        poll_interval_seconds=poll,
        idle_threshold_seconds=idle,
        excluded_programs=frozenset(name.casefold() for name in excluded),
        program_aliases={key.casefold(): value for key, value in (aliases or {}).items()},
        browser_integration=BrowserIntegrationConfig(
            enabled=browser_enabled,
            stale_observation_seconds=15,
            browsers={"brave": "brave.exe"},
        ),
    )


def sample(
    at: float,
    *,
    executable: str | None = "Game.exe",
    last_input: float | None = -10.0,
    wall: datetime | None = None,
    locked: bool | None = False,
) -> ActivitySample:
    return ActivitySample(
        monotonic=at,
        wall_time=wall or (BASE_WALL + timedelta(seconds=at)),
        executable=executable,
        last_input_monotonic=last_input,
        desktop_locked=locked,
    )


def only_program(data: dict) -> dict:
    return data["days"][0]["programs"][0]


class TrackerAccountingTests(unittest.TestCase):
    def test_30_second_intervals_and_fractional_seconds_are_preserved(self) -> None:
        tracker = ActivityTracker(make_config())
        tracker.observe(sample(0, last_input=-10))
        tracker.observe(sample(30.25, last_input=-10))
        tracker.observe(sample(60.5, last_input=-10))

        program = only_program(tracker.export_data(exported_at=BASE_WALL))
        self.assertEqual(program["seconds"], 60.5)
        self.assertEqual(program["minutes"], 1.008)

    def test_idle_cutoff_stops_at_300_seconds(self) -> None:
        tracker = ActivityTracker(make_config())
        tracker.observe(sample(0, last_input=0))
        for at in range(30, 331, 30):
            tracker.observe(sample(at, last_input=0))

        program = only_program(tracker.export_data(exported_at=BASE_WALL))
        self.assertEqual(program["seconds"], 300.0)

    def test_long_afk_is_not_backfilled_and_only_grace_is_counted(self) -> None:
        tracker = ActivityTracker(make_config())
        tracker.observe(sample(0, last_input=-1))

        # Twenty minutes of ordinary use: a recent last input is observed each
        # poll. Then the last input remains fixed for an hour of AFK.
        for at in range(30, 1201, 30):
            tracker.observe(sample(at, last_input=at - 1))
        last_use_input = 1199.0
        for at in range(1230, 4801, 30):
            tracker.observe(sample(at, last_input=last_use_input))

        before_return = only_program(tracker.export_data(exported_at=BASE_WALL))
        self.assertEqual(before_return["seconds"], 1499.0)

        # Input resumes at 4800. That sample does not backfill the absent hour;
        # the following stable 30-second interval is credited normally.
        tracker.observe(sample(4800, last_input=4800))
        tracker.observe(sample(4830, last_input=4800))
        after_return = only_program(tracker.export_data(exported_at=BASE_WALL))
        self.assertEqual(after_return["seconds"], 1529.0)

    def test_switch_interval_is_untracked_and_stable_program_is_counted(self) -> None:
        tracker = ActivityTracker(make_config())
        tracker.observe(sample(0, executable="Game.exe", last_input=-10))
        tracker.observe(sample(30, executable="Editor.exe", last_input=-10))
        tracker.observe(sample(60, executable="Editor.exe", last_input=-10))

        data = tracker.export_data(exported_at=BASE_WALL)
        self.assertEqual(len(data["days"][0]["programs"]), 1)
        self.assertEqual(data["days"][0]["programs"][0]["executable"], "Editor.exe")
        self.assertEqual(data["days"][0]["programs"][0]["seconds"], 30.0)

    def test_long_sleep_gap_is_discarded_and_new_sample_is_baseline(self) -> None:
        tracker = ActivityTracker(make_config())
        tracker.observe(sample(0, last_input=-10))
        tracker.observe(sample(3600, last_input=3590))
        tracker.observe(sample(3630, last_input=3590))

        program = only_program(tracker.export_data(exported_at=BASE_WALL))
        self.assertEqual(program["seconds"], 30.0)

    def test_suspend_wall_gap_is_discarded_even_if_monotonic_gap_is_short(self) -> None:
        tracker = ActivityTracker(make_config())
        tracker.observe(sample(0, last_input=-10))
        tracker.observe(
            sample(
                30,
                last_input=-10,
                wall=BASE_WALL + timedelta(seconds=90),
            )
        )
        self.assertEqual(tracker.export_data(exported_at=BASE_WALL)["days"], [])

    def test_reset_clears_totals_and_resets_sampling_baseline(self) -> None:
        tracker = ActivityTracker(make_config())
        tracker.observe(sample(0, last_input=-10))
        tracker.observe(sample(30, last_input=-10))
        tracker.reset()
        tracker.observe(sample(60, last_input=-10))
        self.assertEqual(tracker.export_data(exported_at=BASE_WALL)["days"], [])
        tracker.observe(sample(90, last_input=-10))
        self.assertEqual(only_program(tracker.export_data(exported_at=BASE_WALL))["seconds"], 30.0)

    def test_reset_updates_export_start_timestamp(self) -> None:
        tracker = ActivityTracker(make_config(), started_at=BASE_WALL)
        tracker.observe(sample(0, last_input=-10))
        tracker.observe(sample(30, last_input=-10))

        reset_time = BASE_WALL + timedelta(minutes=10)
        tracker.reset(reset_at=reset_time)
        snapshot = tracker.export_data(exported_at=reset_time)
        self.assertEqual(snapshot["tracking_started_at"], reset_time.isoformat(timespec="seconds"))
        self.assertEqual(snapshot["days"], [])

    def test_five_second_sampling_limits_switch_interval_loss(self) -> None:
        tracker = ActivityTracker(make_config(poll=5), started_at=BASE_WALL)
        tracker.observe(sample(0, executable="Brave.exe", last_input=0))
        tracker.observe(sample(5, executable="Cursor.exe", last_input=5))
        tracker.observe(sample(10, executable="Cursor.exe", last_input=10))
        data = tracker.export_data(exported_at=BASE_WALL)
        self.assertEqual(data["days"][0]["programs"][0]["executable"], "Cursor.exe")
        self.assertEqual(data["days"][0]["programs"][0]["seconds"], 5.0)

    def test_midnight_interval_is_split_between_local_dates(self) -> None:
        tracker = ActivityTracker(make_config())
        before_midnight = datetime(2026, 10, 25, 23, 59, 45, tzinfo=LOCAL)
        after_midnight = datetime(2026, 10, 26, 0, 0, 15, tzinfo=LOCAL)
        tracker.observe(sample(0, last_input=-10, wall=before_midnight))
        tracker.observe(sample(30, last_input=-10, wall=after_midnight))

        data = tracker.export_data(exported_at=after_midnight)
        self.assertEqual([day["date"] for day in data["days"]], ["2026-10-25", "2026-10-26"])
        self.assertEqual([day["programs"][0]["seconds"] for day in data["days"]], [15.0, 15.0])

    def test_exclusions_aliases_and_case_insensitive_identity(self) -> None:
        tracker = ActivityTracker(
            make_config(
                excluded=("BRAVE.EXE",),
                aliases={"gAmE.ExE": "Friendly Game"},
            )
        )
        tracker.observe(sample(0, executable="Game.EXE", last_input=-10))
        tracker.observe(sample(30, executable="game.exe", last_input=-10))
        tracker.observe(sample(30, executable="Brave.exe", last_input=20))
        tracker.observe(sample(60, executable="brave.EXE", last_input=20))

        data = tracker.export_data(exported_at=BASE_WALL)
        self.assertEqual(len(data["days"][0]["programs"]), 1)
        game = data["days"][0]["programs"][0]
        self.assertEqual(game["executable"], "Game.EXE")
        self.assertEqual(game["name"], "Friendly Game")
        self.assertEqual(game["seconds"], 30.0)

    def test_json_schema_serializes_empty_and_nonempty_snapshots(self) -> None:
        tracker = ActivityTracker(make_config(), started_at=BASE_WALL)
        empty = json.loads(json.dumps(tracker.export_data(exported_at=BASE_WALL)))
        self.assertEqual(empty["schema_version"], 2)
        self.assertEqual(empty["source"], "windows_foreground_tracker")
        self.assertEqual(empty["days"], [])
        self.assertIn("-04:00", empty["tracking_started_at"])

        tracker.observe(sample(0, last_input=-10))
        tracker.observe(sample(30, last_input=-10))
        payload = json.loads(json.dumps(tracker.export_data(exported_at=BASE_WALL)))
        self.assertEqual(payload["days"][0]["programs"][0]["minutes"], 0.5)
        self.assertEqual(payload["days"][0]["programs"][0]["seconds"], 30.0)
        repeated = tracker.export_data(exported_at=BASE_WALL + timedelta(minutes=1))
        self.assertEqual(repeated["days"], payload["days"])

    def test_unknown_process_or_unavailable_lock_state_is_not_attributed(self) -> None:
        tracker = ActivityTracker(make_config())
        tracker.observe(sample(0, last_input=-10))
        tracker.observe(sample(30, executable=None, last_input=-10))
        tracker.observe(sample(60, executable="Game.exe", last_input=-10, locked=None))
        tracker.observe(sample(90, executable="Game.exe", last_input=-10, locked=False))
        self.assertEqual(tracker.export_data(exported_at=BASE_WALL)["days"], [])


if __name__ == "__main__":
    unittest.main()
