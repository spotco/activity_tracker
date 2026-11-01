from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tracker_config import ConfigError, TrackerConfig


class ConfigTests(unittest.TestCase):
    def test_missing_config_is_created_beside_script(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            config = TrackerConfig.load(directory)
            self.assertTrue((directory / "config.json").exists())
            self.assertEqual(config.poll_interval_seconds, 5)
            self.assertEqual(config.excluded_programs, frozenset())

    def test_names_and_aliases_are_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "poll_interval_seconds": 15,
                        "idle_threshold_seconds": 120,
                        "excluded_programs": ["C:/Apps/BrAvE.ExE"],
                        "program_aliases": {"CURSOR.EXE": "Cursor Editor"},
                    }
                ),
                encoding="utf-8",
            )
            config = TrackerConfig.load(Path(temporary))
            self.assertEqual(config.poll_interval_seconds, 15)
            self.assertIn("brave.exe", config.excluded_programs)
            self.assertEqual(config.program_aliases["cursor.exe"], "Cursor Editor")

    def test_invalid_configuration_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text('{"poll_interval_seconds": 0}', encoding="utf-8")
            with self.assertRaises(ConfigError):
                TrackerConfig.load(Path(temporary))


if __name__ == "__main__":
    unittest.main()
