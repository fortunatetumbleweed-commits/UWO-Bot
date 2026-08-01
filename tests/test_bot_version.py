"""Bot versioning + version-aware retry policy.

Pin both layers:
  - brain.version parses dotted-version strings and orders them
  - explore_actions._load_prior_failed_buildings honours the policy
    "old versions' tried_but_failed flags are expired, current
    version's flags are honoured"
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import brain.version as version
from actions import explore_actions


class ParseVersionTests(unittest.TestCase):

    def test_parses_semver(self):
        self.assertEqual(version.parse_version("0.5.0"), (0, 5, 0))
        self.assertEqual(version.parse_version("1.2.3"), (1, 2, 3))

    def test_parses_partial(self):
        self.assertEqual(version.parse_version("2"), (2,))
        self.assertEqual(version.parse_version("0.5"), (0, 5))

    def test_strips_non_numeric_suffix(self):
        self.assertEqual(version.parse_version("0.5.0-dev"), (0, 5, 0))
        self.assertEqual(version.parse_version("1.2.3+commit-abc"), (1, 2, 3))

    def test_empty_or_garbage_returns_zero(self):
        self.assertEqual(version.parse_version(""), (0,))
        self.assertEqual(version.parse_version("abc"), (0,))

    def test_tuple_ordering(self):
        self.assertLess(version.parse_version("0.4.0"),
                         version.parse_version("0.5.0"))
        self.assertLess(version.parse_version("0.5.0"),
                         version.parse_version("0.5.1"))
        self.assertLess(version.parse_version("0.5"),
                         version.parse_version("0.5.0"))


class IsOlderThanCurrentTests(unittest.TestCase):

    def test_strictly_older_is_true(self):
        with patch.object(version, "BOT_VERSION", "0.5.0"):
            self.assertTrue(version.is_older_than_current("0.4.0"))
            self.assertTrue(version.is_older_than_current("0.4.9"))

    def test_equal_is_false(self):
        with patch.object(version, "BOT_VERSION", "0.5.0"):
            self.assertFalse(version.is_older_than_current("0.5.0"))

    def test_newer_is_false(self):
        with patch.object(version, "BOT_VERSION", "0.5.0"):
            self.assertFalse(version.is_older_than_current("0.5.1"))
            self.assertFalse(version.is_older_than_current("1.0.0"))

    def test_missing_stamp_treated_as_older(self):
        """Pre-versioning records have no stamp.  Treat them as older
        so their flags auto-expire on the first version-aware run."""
        with patch.object(version, "BOT_VERSION", "0.5.0"):
            self.assertTrue(version.is_older_than_current(""))


class LoadPriorFailedBuildingsTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._port_patch = patch.object(
            explore_actions, "_PORT_KB",
            Path(self._tmp.name) / "ports",
        )
        self._port_patch.start()

    def tearDown(self):
        self._port_patch.stop()
        self._tmp.cleanup()

    def _seed_record(self, content):
        port_dir = Path(self._tmp.name) / "ports"
        port_dir.mkdir(parents=True, exist_ok=True)
        (port_dir / "london.json").write_text(json.dumps(content))

    def test_flag_from_current_version_honoured(self):
        with patch.object(version, "BOT_VERSION", "0.5.0"):
            self._seed_record({
                "port": "London",
                "buildings": [
                    {"name": "palace", "locked": False,
                     "tried_but_failed": True,
                     "failed_on_version": "0.5.0"},
                ],
            })
            failed = explore_actions._load_prior_failed_buildings("London")
        self.assertEqual(failed, {"palace"})

    def test_flag_from_older_version_expired(self):
        with patch.object(version, "BOT_VERSION", "0.5.0"):
            self._seed_record({
                "port": "London",
                "buildings": [
                    {"name": "palace", "locked": False,
                     "tried_but_failed": True,
                     "failed_on_version": "0.4.0"},
                ],
            })
            failed = explore_actions._load_prior_failed_buildings("London")
        self.assertEqual(failed, set(),
                         "older-version flag must be expired")

    def test_flag_without_version_stamp_expired(self):
        """Pre-versioning records (no failed_on_version field)
        treated as expired so the bot retries on the first
        version-aware run."""
        with patch.object(version, "BOT_VERSION", "0.5.0"):
            self._seed_record({
                "port": "London",
                "buildings": [
                    {"name": "palace", "locked": False,
                     "tried_but_failed": True},
                    # no failed_on_version
                ],
            })
            failed = explore_actions._load_prior_failed_buildings("London")
        self.assertEqual(failed, set())

    def test_mixed_flags_partially_expired(self):
        with patch.object(version, "BOT_VERSION", "0.5.0"):
            self._seed_record({
                "port": "London",
                "buildings": [
                    {"name": "palace", "locked": False,
                     "tried_but_failed": True,
                     "failed_on_version": "0.4.0"},     # expired
                    {"name": "inn", "locked": False,
                     "tried_but_failed": True,
                     "failed_on_version": "0.5.0"},     # honoured
                    {"name": "harbor", "locked": False},  # not failed
                ],
            })
            failed = explore_actions._load_prior_failed_buildings("London")
        self.assertEqual(failed, {"inn"})


class WritePortRecordStampsVersionTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._port_patch = patch.object(
            explore_actions, "_PORT_KB",
            Path(self._tmp.name) / "ports",
        )
        self._port_patch.start()

    def tearDown(self):
        self._port_patch.stop()
        self._tmp.cleanup()

    def test_tried_but_failed_records_version_and_timestamp(self):
        with patch.object(version, "BOT_VERSION", "0.5.0"):
            explore_actions._update_port_record(
                "London",
                [
                    {"name": "palace", "locked": False,
                     "tried_but_failed": True},
                    {"name": "harbor", "locked": False},
                ],
            )
            rec = json.loads(
                (Path(self._tmp.name) / "ports" / "london.json").read_text()
            )
        by_name = {b["name"]: b for b in rec["buildings"]}
        self.assertEqual(by_name["palace"]["failed_on_version"], "0.5.0")
        self.assertIn("failed_at", by_name["palace"])
        self.assertNotIn("failed_on_version", by_name["harbor"])
        self.assertEqual(rec["last_explored_by_version"], "0.5.0")


if __name__ == "__main__":
    unittest.main()
