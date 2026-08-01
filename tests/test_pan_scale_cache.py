"""Tests for the in-call scale cache in pan_to_port.

Regression: 2026-05-19 22:32 — bot was sailing to Port Royal.  First pan
landed in a sparse region with only 1 visible port (Tumbes at game-coord
(2375, 2276), target (2504, 2429) — only 153 game-units away on the y
axis).  Calibration needs ≥2 ports for far-pair scale, so it returned
None; the code then fell into the blind-pan branch which used a fixed
0.6×screen swipe (1440 px x, 648 px y) — a massive overshoot for a
target that was actually ~300 px away.  Worse, the centroid (the single
visible port's catalogue coord) is invariant across attempts, so the
same enormous-and-wrong vector was issued 7 times in a row.

Fix: cache the scale from the most recent successful calibration within
a single pan_to_port call (zoom doesn't change), so _estimate_center can
still produce a real centre estimate from a single visible port.  Then
the normal Δgame * scale swipe vector applies — small Δgame yields a
small swipe instead of the blunt blind-pan fraction.
"""
import unittest
from unittest.mock import patch, MagicMock

from vision.world_map_parser import VisiblePort
from actions.world_map_nav import WorldMapNavigator


def _vp(key, gx, gy, px, py, name=None):
    return VisiblePort(
        key=key, name=name or key.title(),
        game_x=gx, game_y=gy,
        pix_cx=px, pix_cy=py,
        label_text=name or key.title(),
    )


def _frame(w=2400, h=1080):
    f = MagicMock(); f.width = w; f.height = h
    return f


class ScaleCacheTests(unittest.TestCase):

    def setUp(self):
        with patch("actions.world_map_nav.load_port_catalogue", return_value={
            "port royal": {"x": 2504, "y": 2429},
        }):
            self.nav = WorldMapNavigator.__new__(WorldMapNavigator)
            self.nav._ports  = {"port royal": {"x": 2504, "y": 2429}}
            self.nav._aliases = {}

    def test_first_attempt_calibrates_then_caches(self):
        """First frame: multi-port → calibration succeeds.
        Second frame: single port → no fresh calibration, but cached
        scale carries over and produces a proper Δgame * scale swipe.
        """
        # Two attempts then "found" so the loop terminates.
        frame_multi = _frame()
        frame_sparse = _frame()
        frame_hit = _frame()

        # Multi-port frame: 3 ports for clean calibration.
        # Make ports far apart in game coords for far-pair scale.
        multi = [
            _vp("a", 1000, 1000, 100, 100),
            _vp("b", 1200, 1200, 500, 500),
            _vp("c", 1100, 1300, 300, 700),
        ]
        # Sparse frame: just one anchor port (e.g. Tumbes).
        sparse = [_vp("tumbes", 2375, 2276, 1300, 600)]
        # Hit frame: target appears in view.
        hit = [_vp("port royal", 2504, 2429, 800, 500)]

        captures = [frame_multi, frame_sparse, frame_hit]

        def fake_parse(frame, ports, aliases):
            return [multi, sparse, hit][captures.index(frame)]

        swipes: list = []

        def fake_swipe_pan(dpx, dpy, w, h):
            swipes.append((dpx, dpy))
            return dpx, dpy   # _swipe_pan returns actual (dpx, dpy) achieved

        # Force "no persisted scale" so this test exercises the
        # within-call cache path (not the new pre-stride path which
        # would pre-pan based on a disk-persisted scale).
        with patch("actions.world_map_nav._load_persisted_scale",
                   return_value=(None, None)), \
             patch("actions.world_map_nav._save_persisted_scale"), \
             patch("capture.adb_capture.capture_screen",
                   side_effect=captures), \
             patch("actions.world_map_nav.parse_visible_ports",
                   side_effect=fake_parse), \
             patch.object(self.nav, "_swipe_pan",
                          side_effect=fake_swipe_pan), \
             patch("actions.world_map_nav.time.sleep"):
            pos = self.nav.pan_to_port("port royal", max_pans=8)

        self.assertEqual(pos, (800, 500))
        # Exactly 2 swipes (after attempts 1 and 2; attempt 3 was the hit).
        self.assertEqual(len(swipes), 2)

        # First swipe is the standard calibrated one — Δgame is large
        # so swipe is sizable.
        # Second swipe is the cached-scale one — we now have just 1
        # visible port BUT cached scale enables a real Δgame*scale swipe.
        # In the OLD code, this would have been the blunt blind-pan
        # vector ±(0.6 * 2400, 0.6 * 1080) = ±(1440, 648).
        # In the NEW code, it should be the proper Δgame*scale.
        second_dpx, second_dpy = swipes[1]
        # 0.6 * 2400 = 1440 — the blind-pan x magnitude that was
        # previously emitted.  The cached-scale path must NOT match
        # this exact magnitude (it'd be much smaller).
        self.assertNotEqual(abs(second_dpx), 1440,
            "Second swipe still using blind-pan fraction — scale cache not used")
        self.assertNotEqual(abs(second_dpy), 648,
            "Second swipe still using blind-pan fraction — scale cache not used")

    def test_falls_back_to_blind_pan_when_no_prior_calibration(self):
        """If the very first frame already has only 1 visible port, we
        have no cached scale — must still pan via the blind-pan branch
        (not crash, not return None inappropriately).
        """
        sparse_frame_1 = _frame()
        sparse_frame_2 = _frame()
        hit_frame = _frame()
        captures = [sparse_frame_1, sparse_frame_2, hit_frame]

        sparse_1 = [_vp("tumbes", 2375, 2276, 1300, 600)]
        sparse_2 = [_vp("tumbes", 2375, 2276, 1100, 500)]   # slightly moved
        hit = [_vp("port royal", 2504, 2429, 800, 500)]

        def fake_parse(frame, ports, aliases):
            return [sparse_1, sparse_2, hit][captures.index(frame)]

        swipes: list = []
        # Force "no persisted scale" — without this, a calibration.json
        # left over from prior test runs would seed cached_scale and
        # the test wouldn't exercise the no-prior-calibration code path.
        with patch("actions.world_map_nav._load_persisted_scale",
                   return_value=(None, None)), \
             patch("actions.world_map_nav._save_persisted_scale"), \
             patch("capture.adb_capture.capture_screen",
                   side_effect=captures), \
             patch("actions.world_map_nav.parse_visible_ports",
                   side_effect=fake_parse), \
             patch.object(self.nav, "_swipe_pan",
                          side_effect=lambda dpx, dpy, w, h: (swipes.append((dpx, dpy)) or (dpx, dpy))), \
             patch("actions.world_map_nav.time.sleep"):
            pos = self.nav.pan_to_port("port royal", max_pans=8)
        # Either finds it or gives up — both fine, just must not crash.
        self.assertTrue(pos is None or pos == (800, 500))
        # First attempt MUST use the blind-pan path (no cached scale yet).
        # That path emits ±0.6*screen on each axis.
        first_dpx, first_dpy = swipes[0]
        self.assertEqual(abs(first_dpx), int(0.6 * 2400))
        self.assertEqual(abs(first_dpy), int(0.6 * 1080))


if __name__ == "__main__":
    unittest.main()
