"""Panning into cloud means the CAMERA is lost, not that the target is missing.

User 2026-08-21: "a known location is never under cloud, so if it swiped to cloud, then
the location trying to find can not be there. It is a clear indication that the bot has
swept to the wrong place. The bot should ... click the My Location button to recenter to
where the bot is. And start fresh."

Before this, `pan_to_port` read "0 visible ports" as "not here yet", kept swiping on an
already-wrong estimate until its attempts ran out, and reported a misleading "not found"."""
import types
import unittest
from unittest import mock

from actions.world_map_nav import (WorldMapNavigator, _BLANK_FRAMES_MEAN_UNEXPLORED,
                                   _MAX_RECENTERS)


class RecenterPrimitiveTests(unittest.TestCase):
    def _nav(self):
        return WorldMapNavigator.__new__(WorldMapNavigator)

    def test_it_finds_my_location_by_label_not_by_coordinates(self):
        taps = []
        with mock.patch("actions.sail_actions._find_button", return_value=(133, 1012)) as f, \
             mock.patch("actions.adb_actions.tap", side_effect=lambda x, y: taps.append((x, y))), \
             mock.patch("time.sleep"):
            ok = self._nav()._recenter_on_fleet(object())
        self.assertTrue(ok)
        self.assertEqual(taps, [(133, 1012)])
        self.assertIn("my location", [a.lower() for a in f.call_args[0][1:]])

    def test_a_missing_control_reports_rather_than_tapping_anything(self):
        taps = []
        with mock.patch("actions.sail_actions._find_button", return_value=None), \
             mock.patch("actions.adb_actions.tap", side_effect=lambda x, y: taps.append((x, y))), \
             mock.patch("time.sleep"):
            ok = self._nav()._recenter_on_fleet(object())
        self.assertFalse(ok)
        self.assertEqual(taps, [])


class BlanknessIsNotProofTests(unittest.TestCase):
    """Blank screen has several possible causes and they do not exclude one another
    (user 2026-08-21): still en route across unexplored ocean; an area never visited, so
    fogged; a camera that went wrong; or simply not in view yet. Only blank WHILE
    dead-reckoning says we have arrived is contradictory — a catalogued port has been
    visited, so its surroundings are cleared and something would render."""

    def test_the_near_threshold_is_much_smaller_than_a_voyage(self):
        from actions.world_map_nav import _NEAR_TARGET_UNITS
        # "Arrived" has to mean arrived. Diu→Masulipatnam is 294 units, Diu→Atuona 5,948.
        self.assertLessEqual(_NEAR_TARGET_UNITS, 600)
        self.assertGreater(_NEAR_TARGET_UNITS, 50)

    def test_the_recovery_is_gated_on_arrival_not_on_blankness_alone(self):
        import inspect
        from actions.world_map_nav import WorldMapNavigator
        src = inspect.getsource(WorldMapNavigator.pan_to_port)
        # The recentre must require BOTH signals, never blankness by itself.
        self.assertIn("and arrived", src)
        self.assertIn("_NEAR_TARGET_UNITS", src)
        self.assertIn("keep panning", src)


class CloudPolicyTests(unittest.TestCase):
    """The thresholds encode the rule; pin them so they can't drift silently."""

    def test_a_few_label_free_frames_are_treated_as_lost_not_absent(self):
        # One or two blank frames are normal mid-swipe; a run of them is not.
        self.assertGreaterEqual(_BLANK_FRAMES_MEAN_UNEXPLORED, 3)
        self.assertLessEqual(_BLANK_FRAMES_MEAN_UNEXPLORED, 6)

    def test_recentering_is_bounded(self):
        # Recovery must not loop forever — it recentres a couple of times, then reports.
        self.assertGreaterEqual(_MAX_RECENTERS, 1)
        self.assertLessEqual(_MAX_RECENTERS, 3)


if __name__ == "__main__":
    unittest.main()


class GameLabelsItsOwnFogTests(unittest.TestCase):
    """The map prints 'Undiscovered Area' over never-visited regions — seen live
    2026-08-21 while hunting Melanesian Village. That is positive evidence of fog, far
    stronger than counting label-free frames (which can just mean open ocean en route)."""

    def _says(self, text):
        from actions.world_map_nav import _frame_says_undiscovered
        tokens = [(w, 0.9, 10, 10) for w in text.split()]
        with mock.patch("actions.sail_actions._ocr_frame", return_value=tokens):
            return _frame_says_undiscovered(object())

    def test_it_recognises_the_games_fog_label(self):
        self.assertTrue(self._says("world port explore 222 undiscovered area target"))

    def test_an_ordinary_map_frame_is_not_fog(self):
        self.assertFalse(self._says("world port explore route goa kochi ceylon male"))

    def test_an_ocr_failure_does_not_claim_fog(self):
        from actions.world_map_nav import _frame_says_undiscovered
        with mock.patch("actions.sail_actions._ocr_frame", side_effect=RuntimeError("boom")):
            self.assertFalse(_frame_says_undiscovered(object()))
