"""Tests for water-tap localization wired into pan_to_port.

Covers:
  • When 0 visible ports + affine available, water-tap is tried first
    and its result is used as the camera position for the next swipe.
  • Stuck detector is bypassed when camera_source == "water-tap".
  • Water-tap returning None falls back to dead-reckon.
"""
import unittest
from unittest.mock import MagicMock, patch

from vision.world_map_parser import VisiblePort
from actions.world_map_nav import WorldMapNavigator


def _vp(key, gx, gy, px, py):
    return VisiblePort(
        key=key, name=key.title(),
        game_x=gx, game_y=gy,
        pix_cx=px, pix_cy=py,
        label_text=key.title(),
    )


def _frame(w=2400, h=1080):
    f = MagicMock(); f.width = w; f.height = h
    return f


class WaterTapInPanTests(unittest.TestCase):

    def setUp(self):
        with patch("actions.world_map_nav.load_port_catalogue",
                   return_value={"berber": {"x": 4289, "y": 2212}}):
            self.nav = WorldMapNavigator.__new__(WorldMapNavigator)
            self.nav._ports = {"berber": {"x": 4289, "y": 2212}}
            self.nav._aliases = {}

    def test_water_tap_used_when_no_anchors(self):
        """0 visible ports + affine available → water-tap localize fires
        and its catalogue result drives the next swipe."""
        # Two attempts: first triggers water-tap, second finds the target.
        captures = [_frame(), _frame(), _frame()]

        # MagicMock identity isn't enough for list.index() — auto-eq is
        # truthy.  Use a call counter to sequence results.
        parse_calls = [0]
        def fake_parse(frame, ports, aliases):
            idx = parse_calls[0]
            parse_calls[0] += 1
            if idx < 2:
                return []
            return [_vp("berber", 4289, 2212, 800, 500)]

        # Fake localize result: tap pixel (1200, 540), catalogue (4500, 2000).
        # With scale (2.0, 2.0), screen-centre catalogue =
        #   tap_cat - (tap_px - centre_px)/scale
        # = (4500, 2000) - ((1200-1200)/2.0, (540-540)/2.0)
        # = (4500, 2000) — i.e. tap is at screen centre, so camera = tap_cat.
        fake_loc = {
            "tap_pixel": (1200, 540),
            "latlon": (32.0, 14.0),
            "catalogue": (4500.0, 2000.0),
        }

        swipes = []
        def fake_swipe(dpx, dpy, w, h):
            swipes.append((dpx, dpy))
            return dpx, dpy

        with patch("actions.world_map_nav._load_persisted_scale",
                   return_value=(2.0, 2.0)), \
             patch("actions.world_map_nav._save_persisted_scale"), \
             patch("capture.adb_capture.capture_screen",
                   side_effect=captures), \
             patch("actions.world_map_nav.parse_visible_ports",
                   side_effect=fake_parse), \
             patch.object(self.nav, "_swipe_pan", side_effect=fake_swipe), \
             patch("actions.world_map_nav.time.sleep"), \
             patch("actions.latlon_localize.affine_available",
                   return_value=True), \
             patch("actions.latlon_localize.localize_screen_center",
                   return_value=fake_loc) as mock_localize:
            pos = self.nav.pan_to_port("berber", max_pans=8)

        self.assertEqual(pos, (800, 500))
        # Water-tap was invoked at least once.
        self.assertGreaterEqual(mock_localize.call_count, 1)
        # First swipe used the water-tap-derived camera position.
        # Δgame = (4289-4500, 2212-2000) = (-211, 212).
        # Δpix = (-211*2.0, 212*2.0) = (-422, 424).
        # swipe = -(int(Δpix)) clamped to min-pan floor (200).
        # Direction sign: dgx<0 → swipe_dpx > 0.  dgy>0 → swipe_dpy < 0.
        first_dpx, first_dpy = swipes[0]
        self.assertGreater(first_dpx, 0)
        self.assertLess(first_dpy, 0)

    def test_falls_back_to_dead_reckon_when_localize_returns_none(self):
        """Water-tap returns None → dead-reckon takes over (the existing path)."""
        # 3 captures: stride pre-plan + attempt 1 (empty) + attempt 2 (hit).
        captures = [_frame(), _frame(), _frame()]
        parse_calls = [0]
        def fake_parse(frame, ports, aliases):
            idx = parse_calls[0]
            parse_calls[0] += 1
            if idx == 0:
                return []
            return [_vp("berber", 4289, 2212, 800, 500)]

        swipes = []
        with patch("actions.world_map_nav._load_persisted_scale",
                   return_value=(2.0, 2.0)), \
             patch("actions.world_map_nav._save_persisted_scale"), \
             patch("capture.adb_capture.capture_screen",
                   side_effect=captures), \
             patch("actions.world_map_nav.parse_visible_ports",
                   side_effect=fake_parse), \
             patch.object(self.nav, "_swipe_pan",
                          side_effect=lambda dpx, dpy, w, h: (swipes.append((dpx, dpy)) or (dpx, dpy))), \
             patch("actions.world_map_nav.time.sleep"), \
             patch("actions.latlon_localize.affine_available",
                   return_value=True), \
             patch("actions.latlon_localize.localize_screen_center",
                   return_value=None):
            pos = self.nav.pan_to_port(
                "berber", max_pans=4, from_port="tripoli",
            )
        # Got home via dead-reckon — swipe was issued.
        self.assertEqual(pos, (800, 500))
        self.assertEqual(len(swipes), 1)

    def test_water_tap_rejected_when_disagrees_with_dead_reckon(self):
        """An open-ocean water-tap misread that disagrees with dead-reckon by
        more than one screen-width is rejected; dead-reckon drives the swipe.

        Repro of the 2026-08-13 London→Port Royal failure: the stride lands in
        port-less ocean, water-tap misreads a camera far to the east, and the
        next hop would go the wrong way if it were trusted."""
        # berber close to tripoli → stride skipped, total_swipe=0, so
        # dead-reckon camera == tripoli catalogue.
        self.nav._ports = {
            "berber": {"x": 4289, "y": 2212},
            "tripoli": {"x": 4000, "y": 2200},
        }
        captures = [_frame(), _frame(), _frame(), _frame()]
        parse_calls = [0]
        def fake_parse(frame, ports, aliases):
            idx = parse_calls[0]
            parse_calls[0] += 1
            if idx == 0:
                return []          # attempt 1: no anchors → reconcile fires
            return [_vp("berber", 4289, 2212, 800, 500)]

        # Water-tap claims camera x=7000 — ~3000 catalogue (6000px @ scale 2.0)
        # east of dead-reckon (tripoli x=4000).  That's >1 screen-width → reject.
        bad_loc = {
            "tap_pixel": (1200, 540),
            "latlon": (30.0, 40.0),
            "catalogue": (7000.0, 2200.0),
        }
        swipes = []
        def fake_swipe(dpx, dpy, w, h):
            swipes.append((dpx, dpy))
            return dpx, dpy

        with patch("actions.world_map_nav._load_persisted_scale",
                   return_value=(2.0, 2.0)), \
             patch("actions.world_map_nav._save_persisted_scale"), \
             patch("capture.adb_capture.capture_screen",
                   side_effect=captures), \
             patch("actions.world_map_nav.parse_visible_ports",
                   side_effect=fake_parse), \
             patch.object(self.nav, "_swipe_pan", side_effect=fake_swipe), \
             patch("actions.world_map_nav.time.sleep"), \
             patch("actions.latlon_localize.affine_available",
                   return_value=True), \
             patch("actions.latlon_localize.localize_screen_center",
                   return_value=bad_loc):
            pos = self.nav.pan_to_port("berber", max_pans=6, from_port="tripoli")

        self.assertEqual(pos, (800, 500))
        # Dead-reckon (camera x=4000) targets berber (x=4289) to the EAST →
        # dgx>0 → swipe_dpx < 0.  The rejected water-tap (x=7000) would have put
        # berber to the WEST → swipe_dpx > 0.  Assert dead-reckon won.
        self.assertLess(swipes[0][0], 0)

    def test_no_affine_skips_water_tap(self):
        """Without an affine on disk, water-tap should not even be attempted."""
        # 3 captures: stride pre-plan + attempt 1 (empty) + attempt 2 (hit).
        captures = [_frame(), _frame(), _frame()]
        parse_calls = [0]
        def fake_parse(frame, ports, aliases):
            idx = parse_calls[0]
            parse_calls[0] += 1
            if idx == 0:
                return []
            return [_vp("berber", 4289, 2212, 800, 500)]

        with patch("actions.world_map_nav._load_persisted_scale",
                   return_value=(2.0, 2.0)), \
             patch("actions.world_map_nav._save_persisted_scale"), \
             patch("capture.adb_capture.capture_screen",
                   side_effect=captures), \
             patch("actions.world_map_nav.parse_visible_ports",
                   side_effect=fake_parse), \
             patch.object(self.nav, "_swipe_pan",
                          side_effect=lambda dpx, dpy, w, h: (dpx, dpy)), \
             patch("actions.world_map_nav.time.sleep"), \
             patch("actions.latlon_localize.affine_available",
                   return_value=False), \
             patch("actions.latlon_localize.localize_screen_center") as mock_localize:
            self.nav.pan_to_port(
                "berber", max_pans=4, from_port="tripoli",
            )
        mock_localize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
