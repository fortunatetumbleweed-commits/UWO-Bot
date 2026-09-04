"""Leaving a building uses the GAME's controls, not the Android back key.

`exit_to_overworld` pressed Android back in a loop, which can overshoot past the port
overworld and open the system "exit game?" dialog — so the function carried a mechanism to
notice and dismiss that dialog, and escalated into `recover_to_port_overworld` when back
presses stopped helping. Those two could re-enter each other, a cycle with no shared budget.

All of it existed to survive a control the game never intended us to use. The in-game HOME
button leaves any building in one tap and cannot leave the app; the chromed title's BACK ARROW
steps up exactly one level. Both are already located by template match, with their centres
recorded — measured on a real Market frame: home @ (2219,44), and absent on the overworld.
"""

from __future__ import annotations

import unittest
from unittest import mock

from actions import sail_actions


class _Chrome:
    def __init__(self, positions):
        self.positions = positions
        self.has_home = "home" in positions
        self.has_back_arrow = "back_arrow" in positions


class OneTapOut(unittest.TestCase):

    def _tap_exit(self, positions):
        taps = []
        detector = mock.Mock()
        detector.detect.return_value = _Chrome(positions)
        with mock.patch("vision.chrome_detector.get_chrome_detector", lambda: detector), \
             mock.patch.object(sail_actions, "tap", lambda x, y: taps.append((x, y))), \
             mock.patch.object(sail_actions, "capture_screen", lambda: object()):
            res = sail_actions.tap_exit_to_overworld()
        return res, taps

    def test_home_is_preferred(self):
        """One tap out of any depth, measured at (2219,44) on a real Market frame."""
        res, taps = self._tap_exit({"home": (2219, 44), "back_arrow": (40, 40)})
        self.assertEqual(res["control"], "home")
        self.assertEqual(taps, [(2219, 44)])

    def test_the_title_arrow_is_the_fallback(self):
        res, taps = self._tap_exit({"back_arrow": (40, 40)})
        self.assertEqual(res["control"], "back_arrow")
        self.assertEqual(taps, [(40, 40)])

    def test_no_control_means_nothing_is_tapped(self):
        """Tapping blind is how the Android back key got us into this."""
        res, taps = self._tap_exit({})
        self.assertFalse(res["tapped"])
        self.assertEqual(taps, [])

    def test_it_never_presses_android_back(self):
        """The whole point: the game's controls cannot leave the app."""
        with mock.patch.object(sail_actions, "press_back") as back:
            self._tap_exit({"home": (2219, 44)})
        back.assert_not_called()

    def test_tapped_does_not_claim_the_world_changed(self):
        """Per the task-runner contract, the caller perceives and decides."""
        res, _taps = self._tap_exit({"home": (2219, 44)})
        self.assertTrue(res["tapped"])
        self.assertNotIn("port_overworld", str(res))
