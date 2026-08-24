"""Tests for the learning flywheel (#32)."""
import tempfile
import types
import unittest
from pathlib import Path

from brain import flywheel
from brain.flywheel import record_mission_outcome, load_outcomes, realized_stats


def _result(good="Camas", est=250_000_000, realized=305_000_000, ok=True,
            village="Apache Village", sell_port="Edinburgh"):
    play = types.SimpleNamespace(good=good, village=village, sell_port=sell_port,
                                 est_net_profit=est)
    return types.SimpleNamespace(ok=ok, play=play, profit=realized)


class FlywheelTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = flywheel._OUTCOMES_PATH
        flywheel._OUTCOMES_PATH = Path(self._tmp.name) / "outcomes.jsonl"

    def tearDown(self):
        flywheel._OUTCOMES_PATH = self._orig
        self._tmp.cleanup()

    def test_record_and_load(self):
        record_mission_outcome(_result(), season="7", xp_before=68.0, xp_after=70.5,
                               timestamp="2026-08-15T00:00:00Z")
        rows = load_outcomes()
        self.assertEqual(len(rows), 1)
        o = rows[0]
        self.assertEqual(o["good"], "Camas")
        self.assertEqual(o["realized_profit"], 305_000_000)
        self.assertAlmostEqual(o["xp_gain"], 2.5)
        self.assertEqual(o["season"], "7")

    def test_calibration_realized_over_estimated(self):
        # est 200M, realized 300M → calibration 1.5.
        record_mission_outcome(_result(est=200_000_000, realized=300_000_000), timestamp="t")
        stats = realized_stats("Camas")
        self.assertEqual(stats["count"], 1)
        self.assertAlmostEqual(stats["calibration"], 1.5)

    def test_stats_filter_by_good_and_best(self):
        record_mission_outcome(_result(good="Camas", realized=305_000_000), timestamp="t1")
        record_mission_outcome(_result(good="Sofrito", realized=120_000_000), timestamp="t2")
        record_mission_outcome(_result(good="Camas", realized=290_000_000), timestamp="t3")
        camas = realized_stats("Camas")
        self.assertEqual(camas["count"], 2)
        self.assertEqual(camas["best"]["realized_profit"], 305_000_000)
        allg = realized_stats()
        self.assertEqual(allg["count"], 3)

    def test_failed_missions_excluded_from_stats(self):
        record_mission_outcome(_result(ok=False, realized=None), timestamp="t")
        self.assertEqual(realized_stats()["count"], 0)

    def test_empty_stats(self):
        self.assertEqual(realized_stats()["count"], 0)
        self.assertIsNone(realized_stats()["calibration"])


if __name__ == "__main__":
    unittest.main()
