from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from activity_tracker_cli import TrackerCommands, _parse_command
from tracker_config import TrackerConfig
from tracker_core import ActivitySample, ActivityTracker


LOCAL = timezone(timedelta(hours=-4))
START = datetime(2026, 10, 25, 12, 0, tzinfo=LOCAL)


def config() -> TrackerConfig:
    return TrackerConfig(30, 300, frozenset({"brave.exe"}), {"game.exe": "Game"})


def populated_tracker() -> ActivityTracker:
    tracker = ActivityTracker(config(), started_at=START)
    tracker.observe(ActivitySample(0, START, "Game.exe", -10, False))
    tracker.observe(ActivitySample(30, START + timedelta(seconds=30), "Game.exe", -10, False))
    return tracker


class TrackerCliTests(unittest.TestCase):
    def test_command_parser_preserves_windows_paths_and_spaces(self) -> None:
        self.assertEqual(_parse_command('EXPORT "C:\\Users\\Me\\My Data\\file.json"'),
                         ("export", "C:\\Users\\Me\\My Data\\file.json"))
        self.assertEqual(_parse_command("view"), ("view", ""))

    def test_status_and_show_request_popup_without_printing_activity_json(self) -> None:
        tracker = populated_tracker()
        output: list[str] = []
        show_requests: list[bool] = []
        commands = TrackerCommands(
            tracker,
            output=output.append,
            show_callback=lambda: show_requests.append(True),
        )

        self.assertTrue(commands.execute("status"))
        status = json.loads(output[-1])
        self.assertEqual(status["tracked_seconds"], 30.0)
        self.assertEqual(status["program_count"], 1)

        commands.execute("show")
        show = json.loads(output[-1])
        self.assertEqual(show["command"], "show")
        self.assertTrue(show["popup_requested"])
        self.assertEqual(show_requests, [True])
        self.assertNotIn("Game.exe", output[-1])

    def test_reset_requires_exact_confirmation(self) -> None:
        tracker = populated_tracker()
        output: list[str] = []
        answers = iter(["no", "RESET"])
        commands = TrackerCommands(tracker, output=output.append, input_fn=lambda _prompt: next(answers))

        commands.execute("reset")
        self.assertTrue(json.loads(output[-1])["cancelled"])
        self.assertEqual(tracker.export_data()["days"][0]["programs"][0]["seconds"], 30.0)

        commands.execute("reset")
        self.assertTrue(json.loads(output[-1])["cleared"])
        self.assertEqual(tracker.export_data()["days"], [])

    def test_export_writes_valid_snapshot_without_clearing_memory(self) -> None:
        tracker = populated_tracker()
        output: list[str] = []
        commands = TrackerCommands(tracker, output=output.append)
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "activity.json"
            commands.execute(f'export "{target}"')
            reply = json.loads(output[-1])
            self.assertTrue(reply["ok"])
            saved = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(saved["days"][0]["programs"][0]["seconds"], 30.0)
            self.assertEqual(tracker.export_data()["days"][0]["programs"][0]["seconds"], 30.0)

    def test_exit_returns_false(self) -> None:
        commands = TrackerCommands(populated_tracker(), output=lambda _text: None)
        self.assertFalse(commands.execute("exit"))


if __name__ == "__main__":
    unittest.main()
