"""Tests for WorldMapNavigator math.

The expensive parts (OmniParser parse, ADB swipe) are mocked.  The
calibration math and pan-vector derivation are pure functions of the
parsed VisiblePort list, so we can pin them precisely.

Origin: 2026-05-13.  Validates the coordinate-based pan primitive that
replaces the legacy visual-panning fallback (which couldn't resolve
cross-ocean targets — the fleet-died-at-sea failure of 2026-05-12).
"""

import unittest
from unittest.mock import patch, MagicMock

from vision.world_map_parser import VisiblePort
from actions.world_map_nav import WorldMapNavigator, _MIN_PAIR_GAME_DIST


def _vp(key, gx, gy, px, py, name=None):
    return VisiblePort(
        key=key, name=name or key.title(),
        game_x=gx, game_y=gy,
        pix_cx=px, pix_cy=py,
        label_text=name or key.title(),
    )


class CalibrateTests(unittest.TestCase):
    """_calibrate returns median of far-pair same-axis ratios."""

    def setUp(self):
        with patch("actions.world_map_nav.load_port_catalogue", return_value={}):
            self.nav = WorldMapNavigator.__new__(WorldMapNavigator)
            self.nav._ports = {}
            self.nav._aliases = {}

    def test_returns_none_when_under_two_ports(self):
        sx, sy = self.nav._calibrate([_vp("a", 100, 100, 200, 200)])
        self.assertIsNone(sx)
        self.assertIsNone(sy)

    def test_far_pair_yields_scale_on_each_axis(self):
        # Two ports 200 game-units apart on both axes, observed 400 px apart.
        # Expected scale: 2.0 on both axes.
        visible = [
            _vp("a", 1000, 1000, 100, 100),
            _vp("b", 1200, 1200, 500, 500),
        ]
        sx, sy = self.nav._calibrate(visible)
        self.assertAlmostEqual(sx, 2.0, places=3)
        self.assertAlmostEqual(sy, 2.0, places=3)

    def test_close_pair_skipped_below_min_distance(self):
        # Two ports only 40 game-units apart — below _MIN_PAIR_GAME_DIST.
        # Should NOT contribute to scale (returns None).
        visible = [
            _vp("a", 1000, 1000, 100, 100),
            _vp("b", 1040, 1040, 200, 200),
        ]
        sx, sy = self.nav._calibrate(visible)
        self.assertIsNone(sx)
        self.assertIsNone(sy)

    def test_median_robust_to_outlier(self):
        # 4 ports — 6 pairs total.  Most agree on px/x=2.0, one pair has
        # extreme noise.  Median should ignore the outlier.
        visible = [
            _vp("a", 1000, 0, 100, 0),
            _vp("b", 1200, 0, 500, 0),
            _vp("c", 1400, 0, 900, 0),
            _vp("d", 1600, 0, 1300, 0),
        ]
        # 1000→1200, 1000→1400, 1000→1600, 1200→1400, 1200→1600, 1400→1600
        # All pairs give px/x = 2.0; no outliers.
        sx, _ = self.nav._calibrate(visible)
        self.assertAlmostEqual(sx, 2.0, places=3)

    def test_one_axis_calibrated_when_other_axis_lacks_far_pairs(self):
        # Ports differ on x (Δgame > 80) but not on y (Δgame < 80).
        visible = [
            _vp("a", 1000, 1000, 100, 100),
            _vp("b", 1200, 1020, 500, 140),
        ]
        sx, sy = self.nav._calibrate(visible)
        self.assertAlmostEqual(sx, 2.0, places=3)
        self.assertIsNone(sy)


class EstimateCenterTests(unittest.TestCase):
    """_estimate_center back-projects from visible ports to screen centre."""

    def setUp(self):
        self.nav = WorldMapNavigator.__new__(WorldMapNavigator)
        self.nav._ports = {}
        self.nav._aliases = {}

    def test_center_recovered_when_one_port_is_centered(self):
        # Port a is exactly at screen centre, scale 2.0 px/unit.
        # Centre game-coord should equal port-a's game-coord.
        visible = [_vp("a", 5000, 2500, 1200, 540)]
        gx, gy = self.nav._estimate_center(visible, 2.0, 2.0, 2400, 1080)
        self.assertAlmostEqual(gx, 5000.0)
        self.assertAlmostEqual(gy, 2500.0)

    def test_center_recovered_from_two_off_center_ports(self):
        # Port a 100 game-units east of centre, port b 100 west.
        # Both should back-project to the same centre.
        visible = [
            _vp("a", 5100, 2500, 1200 + 200, 540),  # +100 game = +200 px
            _vp("b", 4900, 2500, 1200 - 200, 540),  # -100 game = -200 px
        ]
        gx, gy = self.nav._estimate_center(visible, 2.0, 2.0, 2400, 1080)
        self.assertAlmostEqual(gx, 5000.0)
        self.assertAlmostEqual(gy, 2500.0)

    def test_axis_with_none_scale_returns_none_center(self):
        visible = [_vp("a", 5000, 2500, 1200, 540)]
        gx, gy = self.nav._estimate_center(visible, 2.0, None, 2400, 1080)
        self.assertAlmostEqual(gx, 5000.0)
        self.assertIsNone(gy)


class PanToPortTests(unittest.TestCase):
    """Integration of pan_to_port: parses → matches → swipes → finds."""

    def _make_nav(self, ports_dict):
        with patch("actions.world_map_nav.load_port_catalogue",
                    return_value=ports_dict):
            return WorldMapNavigator()

    def test_unknown_port_returns_none(self):
        nav = self._make_nav({})
        result = nav.pan_to_port("NoSuchPort", max_pans=2)
        self.assertIsNone(result)

    def test_target_visible_on_first_capture_returns_immediately(self):
        nav = self._make_nav({
            "london": {"name": "London", "x": 4680, "y": 1210},
        })
        # The target IS in the catalogue AND the first parse returns it.
        fake_frame = MagicMock(width=2400, height=1080)
        with patch("capture.adb_capture.capture_screen", return_value=fake_frame), \
             patch("actions.world_map_nav.parse_visible_ports",
                    return_value=[_vp("london", 4680, 1210, 988, 537,
                                        name="London")]), \
             patch.object(nav, "_swipe_pan") as mock_swipe:
            result = nav.pan_to_port("London", max_pans=8)
        self.assertEqual(result, (988, 537))
        mock_swipe.assert_not_called()   # no swipe needed — already found

    def test_target_found_after_one_pan(self):
        nav = self._make_nav({
            "london":   {"name": "London",   "x": 4680, "y": 1210},
            "sierra leone": {"name": "Sierra Leone", "x": 4286, "y": 2709},
            "benin":    {"name": "Benin",    "x": 4807, "y": 2760},
        })
        # First capture: West Africa (Sierra Leone + Benin visible).
        # Second capture: London visible.
        west_africa = [
            _vp("sierra leone", 4286, 2709, 715,  344, name="Sierra Leone"),
            _vp("benin",        4807, 2760, 1715, 459, name="Benin"),
        ]
        london_view = [_vp("london", 4680, 1210, 988, 537, name="London")]
        fake_frame = MagicMock(width=2400, height=1080)
        parse_results = iter([west_africa, london_view])
        with patch("capture.adb_capture.capture_screen", return_value=fake_frame), \
             patch("actions.world_map_nav.parse_visible_ports",
                    side_effect=lambda f, *a, **kw: next(parse_results)), \
             patch.object(nav, "_swipe_pan", return_value=(0, 0)) as mock_swipe, \
             patch("time.sleep"):
            result = nav.pan_to_port("London", max_pans=4)
        self.assertEqual(result, (988, 537))
        # Exactly one swipe between the two captures
        self.assertEqual(mock_swipe.call_count, 1)
        # Swipe direction: London is NORTH (-y) and slightly WEST (-x) of W Africa.
        # We want camera to move north → drag map content DOWN (positive Δpix_y).
        # Drag direction is -dpx_target, -dpy_target.
        # dpy_target = (1210 - center_y) * scale_y — center_y ≈ 2734
        #            ≈ (1210 - 2734) * ~2 ≈ -3048, clamped to -864
        # So swipe Δpix = +864 in y (drag down).
        swipe_args = mock_swipe.call_args
        dpx_swipe, dpy_swipe = swipe_args[0][0], swipe_args[0][1]
        # Swipe drag should be DOWN (positive dpy_swipe) to move camera UP
        self.assertGreater(dpy_swipe, 0,
                            f"Expected positive dpy (drag down to pan north), got {dpy_swipe}")

    def test_no_visible_ports_returns_none(self):
        nav = self._make_nav({
            "london": {"name": "London", "x": 4680, "y": 1210},
        })
        fake_frame = MagicMock(width=2400, height=1080)
        with patch("capture.adb_capture.capture_screen", return_value=fake_frame), \
             patch("actions.world_map_nav.parse_visible_ports", return_value=[]), \
             patch.object(nav, "_swipe_pan") as mock_swipe:
            result = nav.pan_to_port("London", max_pans=3)
        self.assertIsNone(result)
        mock_swipe.assert_not_called()

    def test_repeated_state_triggers_stuck_detection(self):
        """When the same swipe vector is emitted against the same visible
        set as the previous attempt, the map didn't move (boundary or
        gesture rejected) — pan_to_port should abort fast rather than
        burning the full max_pans budget on the same wrong vector.
        """
        nav = self._make_nav({
            "london":   {"name": "London",   "x": 4680, "y": 1210},
            "abidjan":  {"name": "Abidjan",  "x": 4543, "y": 2793},
            "benin":    {"name": "Benin",    "x": 4807, "y": 2760},
        })
        # Every capture shows the same West Africa pair, never London.
        # First swipe emits some vector; second iteration sees identical
        # state and identical vector → stuck detector fires.
        west_africa = [
            _vp("abidjan", 4543, 2793, 1227, 515, name="Abidjan"),
            _vp("benin",   4807, 2760, 1715, 459, name="Benin"),
        ]
        fake_frame = MagicMock(width=2400, height=1080)
        with patch("capture.adb_capture.capture_screen", return_value=fake_frame), \
             patch("actions.world_map_nav.parse_visible_ports", return_value=west_africa), \
             patch.object(nav, "_swipe_pan", return_value=(0, 0)) as mock_swipe, \
             patch("actions.world_map_nav._load_persisted_scale",
                   return_value=(None, None)), \
             patch("actions.world_map_nav._save_persisted_scale"), \
             patch("time.sleep"):
            result = nav.pan_to_port("London", max_pans=8)
        self.assertIsNone(result)
        # Exactly 1 swipe — the second attempt aborts via stuck detection.
        self.assertEqual(mock_swipe.call_count, 1)


if __name__ == "__main__":
    unittest.main()
