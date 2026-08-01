"""Tests for brain.moondream_family_cache."""
import os
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch


class MoondreamFamilyCacheTests(unittest.TestCase):

    def setUp(self):
        from brain import moondream_family_cache as mfc
        mfc.reset()

    def test_set_and_get(self):
        from brain import moondream_family_cache as mfc
        mfc.set_family("in_town")
        self.assertEqual(mfc.get_family(), "in_town")

    def test_get_returns_none_when_unset(self):
        from brain import moondream_family_cache as mfc
        self.assertIsNone(mfc.get_family())

    def test_invalidate_clears_get(self):
        from brain import moondream_family_cache as mfc
        mfc.set_family("at_sea")
        mfc.invalidate("set_sail")
        self.assertIsNone(mfc.get_family())

    def test_invalidate_records_reason(self):
        from brain import moondream_family_cache as mfc
        mfc.set_family("in_town")
        mfc.invalidate("task_start")
        snap = mfc.snapshot()
        self.assertTrue(snap["invalidated"])
        self.assertEqual(snap["invalid_reason"], "task_start")

    def test_ttl_expires_after_window(self):
        from brain import moondream_family_cache as mfc
        with patch.dict(os.environ, {"MOONDREAM_FAMILY_TTL_SECONDS": "60"}):
            mfc.set_family("in_town")
            self.assertEqual(mfc.get_family(), "in_town")
            # Backdate the cache entry to ~70s ago
            mfc._state.set_at = datetime.now() - timedelta(seconds=70)
            self.assertIsNone(mfc.get_family())

    def test_set_after_invalidate_clears_invalidated_flag(self):
        from brain import moondream_family_cache as mfc
        mfc.set_family("in_town")
        mfc.invalidate("set_sail")
        mfc.set_family("at_sea")
        self.assertEqual(mfc.get_family(), "at_sea")
        snap = mfc.snapshot()
        self.assertFalse(snap["invalidated"])

    def test_is_fresh_matches_get_family(self):
        from brain import moondream_family_cache as mfc
        self.assertFalse(mfc.is_fresh())
        mfc.set_family("in_town")
        self.assertTrue(mfc.is_fresh())
        mfc.invalidate("test")
        self.assertFalse(mfc.is_fresh())

    def test_snapshot_returns_full_state(self):
        from brain import moondream_family_cache as mfc
        mfc.set_family("at_sea", source="moondream")
        snap = mfc.snapshot()
        self.assertEqual(snap["verdict"], "at_sea")
        self.assertEqual(snap["source"], "moondream")
        self.assertIsNotNone(snap["set_at"])
        self.assertGreaterEqual(snap["age_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
