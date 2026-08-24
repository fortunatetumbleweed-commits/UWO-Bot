"""Tests for SailToGoal's position-confirmed stall detection.

Regression for the 2026-08-19 hamburger loop: `_action_sailing` used to re-navigate on a bare
`speed==0` read, so a single OCR misread of a MOVING ship's speed discarded the committed route
and re-opened the world map mid-voyage.  The fix requires BOTH speed==0 AND the lat/lon position
held (not advanced) before concluding a stall.
"""
from __future__ import annotations

import unittest
from unittest import mock

from brain.goals.sail_to import SailToGoal, SailPhase
from brain.perceive import PerceiveResult


def _sea() -> PerceiveResult:
    return PerceiveResult(state="sea", port=None, detail="sea")


def _tick(goal, speed, pos):
    """Run one _action_sailing tick with mocked speed + lat/lon reads."""
    with mock.patch("actions.adb_actions.tap"), \
         mock.patch("capture.adb_capture.capture_screen", return_value="FRAME"), \
         mock.patch("actions.sail_actions._read_sea_speed", return_value=speed), \
         mock.patch("vision.sea_hud.read_latlon", return_value=pos):
        return goal._action_sailing(_sea())


class StallDetectionTests(unittest.TestCase):
    def _goal(self) -> SailToGoal:
        g = SailToGoal(destination="Jakarta")
        g.phase = SailPhase.SAILING
        g._destination_selected = True
        g._last_pos = (10.0, 20.0)          # seed a prior position
        return g

    def test_speed_zero_but_position_advanced_is_not_a_stall(self):
        """THE BUG FIX: speed misreads 0 while the ship is moving → route must NOT be discarded."""
        g = self._goal()
        _tick(g, 0.0, (10.5, 20.0))         # moved 0.5° despite speed=0
        self.assertEqual(g._zero_speed_count, 0)
        self.assertTrue(g._destination_selected)
        _tick(g, 0.0, (11.0, 20.0))
        self.assertEqual(g._zero_speed_count, 0)
        self.assertTrue(g._destination_selected)   # still committed after two misreads

    def test_speed_zero_and_position_held_stalls_after_two_ticks(self):
        g = self._goal()
        r1 = _tick(g, 0.0, (10.0, 20.0))    # held
        self.assertEqual(g._zero_speed_count, 1)
        self.assertTrue(g._destination_selected)
        self.assertEqual(r1.action, "sailing")
        r2 = _tick(g, 0.0, (10.0, 20.0))    # held again → confirmed stall
        self.assertEqual(r2.action, "stall_renavigate")
        self.assertFalse(g._destination_selected)
        self.assertEqual(g.phase, SailPhase.SEA_NAVIGATE)

    def test_positive_speed_resets_the_counter(self):
        g = self._goal()
        _tick(g, 0.0, (10.0, 20.0))         # held → count 1
        self.assertEqual(g._zero_speed_count, 1)
        _tick(g, 12.5, (10.0, 20.0))        # moving → reset
        self.assertEqual(g._zero_speed_count, 0)

    def test_unreadable_position_does_not_stall(self):
        """Can't confirm the ship is stopped → don't discard the route."""
        g = self._goal()
        _tick(g, 0.0, None)
        _tick(g, 0.0, None)
        self.assertEqual(g._zero_speed_count, 0)
        self.assertTrue(g._destination_selected)


if __name__ == "__main__":
    unittest.main()
