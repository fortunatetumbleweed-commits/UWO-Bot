"""Tests for the world-map pan-swipe primitives.

Three issues live-observed in 2026-05-19 and 2026-05-20 runs:

  1. _swipe_pan started at screen centre; for any Δ greater than half the
     screen on either axis, the end point landed off-screen.  Android
     clipped the gesture, so the *effective* swipe was much smaller than
     the math intended.

  2. The per-axis Δ could come out small (e.g. 344 px) when the target
     was close on that axis but far on the other.  Below ~1 cm (~200 px)
     the game registers the gesture as a tap and does nothing.

  3. Anchoring at the *screen* corner opposite the motion put the touch-
     down inside the world-map toolbar (mode tabs, Search) when the
     direction required starting top-right or bottom-left.  UWO
     interprets the touch as a tab click and discards the drag.

The current fix anchors inside _MAP_SAFE_RECT — the central area of the
world map that contains only map content, no UI controls.  _swipe_pan
now returns the actual (dpx, dpy) achieved so dead-reckoning stays
honest after any safe-rect clamping.
"""
import unittest
from unittest.mock import patch

from actions.world_map_nav import (
    WorldMapNavigator,
    _enforce_min_pan,
    _MIN_PAN_PX,
    _MAP_SAFE_LEFT,
    _MAP_SAFE_TOP,
    _MAP_SAFE_RIGHT,
    _MAP_SAFE_BOTTOM,
)


class EnforceMinPanTests(unittest.TestCase):

    def test_zero_stays_zero(self):
        self.assertEqual(_enforce_min_pan(0), 0)

    def test_positive_below_floor_bumped_up(self):
        self.assertEqual(_enforce_min_pan(50), _MIN_PAN_PX)

    def test_negative_below_floor_bumped_down(self):
        self.assertEqual(_enforce_min_pan(-50), -_MIN_PAN_PX)

    def test_value_above_floor_unchanged(self):
        self.assertEqual(_enforce_min_pan(344), 344)
        self.assertEqual(_enforce_min_pan(-864), -864)

    def test_exactly_at_floor_unchanged(self):
        self.assertEqual(_enforce_min_pan(_MIN_PAN_PX), _MIN_PAN_PX)
        self.assertEqual(_enforce_min_pan(-_MIN_PAN_PX), -_MIN_PAN_PX)


class SwipePanSafeRectTests(unittest.TestCase):
    """_swipe_pan must anchor inside _MAP_SAFE_RECT.  No swipe endpoint
    may land on the top/bottom toolbars or edge icon columns.

    Regression target: 2026-05-20 — swipe (-1920, +864) anchored at
    (2320, 80) — top-right toolbar zone.  UWO interpreted the touch-down
    as a Port-tab click; the drag was discarded; the map didn't pan.
    """

    SL = _MAP_SAFE_LEFT
    ST = _MAP_SAFE_TOP
    SR = _MAP_SAFE_RIGHT
    SB = _MAP_SAFE_BOTTOM

    def setUp(self):
        with patch("actions.world_map_nav.load_port_catalogue", return_value={}):
            self.nav = WorldMapNavigator.__new__(WorldMapNavigator)
            self.nav._ports = {}
            self.nav._aliases = {}

    def _captured_swipe(self, dpx, dpy, w=2400, h=1080):
        with patch("actions.adb_actions.swipe") as mock:
            actual = self.nav._swipe_pan(dpx, dpy, w, h)
            self.assertTrue(mock.called)
            args, _ = mock.call_args
            x0, y0, x1, y1 = args[0], args[1], args[2], args[3]
            return (x0, y0, x1, y1), actual

    def _assert_inside_safe(self, x, y):
        self.assertGreaterEqual(x, self.SL,
            f"x={x} must be ≥ safe-rect-left {self.SL} (would touch left icon column)")
        self.assertLessEqual(x, self.SR,
            f"x={x} must be ≤ safe-rect-right {self.SR} (would touch right edge buttons)")
        self.assertGreaterEqual(y, self.ST,
            f"y={y} must be ≥ safe-rect-top {self.ST} (would touch top toolbar)")
        self.assertLessEqual(y, self.SB,
            f"y={y} must be ≤ safe-rect-bottom {self.SB} (would touch bottom toolbar)")

    def test_leftward_swipe_anchors_at_safe_rect_right(self):
        """Δpx < 0 → start at safe-rect right (NOT screen right=2320)."""
        (x0, y0, x1, y1), _ = self._captured_swipe(-1000, 0)
        self.assertEqual(x0, self.SR)
        self._assert_inside_safe(x0, y0)
        self._assert_inside_safe(x1, y1)

    def test_rightward_swipe_anchors_at_safe_rect_left(self):
        (x0, y0, x1, y1), _ = self._captured_swipe(1000, 0)
        self.assertEqual(x0, self.SL)
        self._assert_inside_safe(x0, y0)
        self._assert_inside_safe(x1, y1)

    def test_downward_swipe_anchors_at_safe_rect_top(self):
        (x0, y0, x1, y1), _ = self._captured_swipe(0, 500)
        self.assertEqual(y0, self.ST)
        self._assert_inside_safe(x0, y0)
        self._assert_inside_safe(x1, y1)

    def test_upward_swipe_anchors_at_safe_rect_bottom(self):
        (x0, y0, x1, y1), _ = self._captured_swipe(0, -500)
        self.assertEqual(y0, self.SB)
        self._assert_inside_safe(x0, y0)
        self._assert_inside_safe(x1, y1)

    def test_live_2026_05_20_case_avoids_top_toolbar(self):
        """The exact case that broke: _swipe_pan(-1920, +864).
        Old code: anchored at (2320, 80) — top-right toolbar.
        New code: must NOT touch y < 200 or x > 2150.
        """
        (x0, y0, x1, y1), _ = self._captured_swipe(-1920, 864)
        # The most important guard: the touch-down (x0, y0) is the part
        # the game's gesture handler classifies as a button click.
        self._assert_inside_safe(x0, y0)
        self._assert_inside_safe(x1, y1)

    def test_returns_actual_dpx_dpy(self):
        """_swipe_pan returns the actual achieved Δ — for in-bounds
        magnitudes this equals the request."""
        (_, _, _, _), actual = self._captured_swipe(800, 400)
        self.assertEqual(actual, (800, 400))

    def test_returns_clamped_dpx_dpy_when_oversized(self):
        """When request exceeds safe-rect dimensions, returned Δ is the
        actual clamped value, not the request."""
        # Safe rect height = SB-ST = 700.  Request 864 in y exceeds that.
        (_, y0, _, y1), actual = self._captured_swipe(0, 864)
        achieved_dy = y1 - y0
        self.assertEqual(actual[1], achieved_dy)
        self.assertLessEqual(abs(actual[1]), self.SB - self.ST)
        self.assertLess(abs(actual[1]), 864)   # was clamped

    def test_zero_input_returns_zero(self):
        (x0, y0, x1, y1), actual = self._captured_swipe(0, 0)
        self.assertEqual(actual, (0, 0))
        self.assertEqual((x0, y0), (x1, y1))


if __name__ == "__main__":
    unittest.main()
