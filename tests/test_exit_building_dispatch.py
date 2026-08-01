"""Test _action_exit_building's dispatch order.

Origin: 2026-05-22 18:26-18:47 — bot looped 17 times trying to exit
the shipyard via system Back (KEYCODE_BACK), each cycle triggering
the in-game 'Exit Game?' confirmation dialog instead of advancing to
port_overworld.  Wasted ~22 minutes.

Fix: prefer the in-game back-arrow at (110, 40) over system Back.
"""
import unittest
from unittest.mock import MagicMock, patch

from brain.goals.sail_to import SailToGoal


def _chrome(home=False, back=False):
    c = MagicMock()
    c.has_home = home
    c.has_back_arrow = back
    return c


class ExitBuildingDispatchTests(unittest.TestCase):

    def setUp(self):
        self.goal = SailToGoal(destination="Berber")

    def _run(self, chrome):
        detector = MagicMock()
        detector.detect.return_value = chrome
        tap_calls = []
        back_calls = []

        with patch("actions.adb_actions.tap", side_effect=lambda x, y: tap_calls.append((x, y))), \
             patch("actions.adb_actions.press_back", side_effect=lambda: back_calls.append(True)), \
             patch("vision.chrome_detector.get_chrome_detector", return_value=detector), \
             patch("capture.adb_capture.capture_screen", return_value=MagicMock()):
            self.goal._action_exit_building()
        return tap_calls, back_calls

    def test_home_button_preferred_when_visible(self):
        taps, backs = self._run(_chrome(home=True, back=True))
        self.assertEqual(taps, [(2300, 45)])
        self.assertEqual(backs, [])

    def test_back_arrow_used_when_home_absent(self):
        """Regression: when only back-arrow is visible, tap the on-screen
        arrow at (110, 40) — NOT system Back (which triggers Exit Game)."""
        taps, backs = self._run(_chrome(home=False, back=True))
        self.assertEqual(taps, [(110, 40)])
        self.assertEqual(backs, [])

    def test_system_back_only_as_last_resort(self):
        """When neither home nor back-arrow are detected, fall back to
        system Back.  Acceptable but suboptimal."""
        taps, backs = self._run(_chrome(home=False, back=False))
        self.assertEqual(taps, [])
        self.assertEqual(backs, [True])


if __name__ == "__main__":
    unittest.main()
