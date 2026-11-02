from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tracker_config import ConfigError, TrackerConfig, ensure_installation_config


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

    def test_duplicate_browser_executable_mappings_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "browser_integration": {
                            "enabled": True,
                            "browsers": {"brave": "brave.exe", "chrome": "Brave.EXE"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                TrackerConfig.load(Path(temporary))

    def test_tracker_config_accepts_original_four_positional_arguments(self) -> None:
        config = TrackerConfig(30, 300, frozenset(), {})
        self.assertFalse(config.browser_integration.enabled)
        self.assertEqual(config.browser_integration.browsers["brave"], "brave.exe")

    def test_ensure_installation_config_writes_utf8_without_bom(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            ensure_installation_config(directory)
            raw = (directory / "config.json").read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))


if __name__ == "__main__":
    unittest.main()
