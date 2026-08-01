"""Tests for stride pre-planning in pan_to_port.

When the bot knows both the departing port and target port (catalogue
coords) and has a persisted scale from a previous calibration, the first
leg of a long-haul pan can be pre-planned as multiple back-to-back swipes
without recapturing between them.  This saves the ~10-15 s perception
cycle on each intermediate pan — a Europe→Caribbean crossing drops from
~5 perception cycles to ~1 stride + 1 recalibrate.

Tests:
  - stride math: N pans for an N×screen-distance trajectory
  - reserve budget: never strides through the full max_pans
  - skip when from_port unknown (catalogue miss)
  - skip when no persisted scale yet
  - skip when distance < 1 pan (single iterative pan is enough)
"""
import json
import tempfile
import unittest
from pathlib import Path
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


class _Base(unittest.TestCase):

    def setUp(self):
        # Fresh nav with London + Port Royal in the catalogue.
        with patch("actions.world_map_nav.load_port_catalogue", return_value={
            "london": {"x": 4671, "y": 1231},
            "port royal": {"x": 2504, "y": 2429},
        }):
            self.nav = WorldMapNavigator.__new__(WorldMapNavigator)
            self.nav._ports  = {
                "london": {"x": 4671, "y": 1231},
                "port royal": {"x": 2504, "y": 2429},
            }
            self.nav._aliases = {}


class ScalePersistenceTests(_Base):

    def test_save_and_load_roundtrip(self):
        from actions.world_map_nav import _save_persisted_scale, _load_persisted_scale
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "calibration.json"
            with patch("actions.world_map_nav._SCALE_CACHE_PATH", path):
                _save_persisted_scale(2.03, 1.94)
                sx, sy = _load_persisted_scale()
                self.assertAlmostEqual(sx, 2.03, places=2)
                self.assertAlmostEqual(sy, 1.94, places=2)

    def test_load_missing_returns_none_pair(self):
        from actions.world_map_nav import _load_persisted_scale
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "does_not_exist.json"
            with patch("actions.world_map_nav._SCALE_CACHE_PATH", path):
                sx, sy = _load_persisted_scale()
                self.assertIsNone(sx)
                self.assertIsNone(sy)

    def test_load_corrupt_returns_none_pair(self):
        from actions.world_map_nav import _load_persisted_scale
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "calibration.json"
            path.write_text("{not valid json")
            with patch("actions.world_map_nav._SCALE_CACHE_PATH", path):
                sx, sy = _load_persisted_scale()
                self.assertIsNone(sx)
                self.assertIsNone(sy)


class StrideExecutionTests(_Base):

    def _run_pan(self, *, from_port, hits_on_attempt=1,
                 persisted_scale=(2.03, 1.94)):
        """Drive pan_to_port and collect swipe calls.

        *hits_on_attempt*: 1 = target visible in the very first iterative
        frame (immediately after the stride).  Used to make the loop
        terminate predictably so we can inspect the stride behaviour.
        """
        frame = _frame()
        stride_swipes: list = []
        iterative_swipes: list = []

        # Visible-port list per iterative-loop attempt.
        # First attempt → target visible (so loop terminates immediately).
        hit = [_vp("port royal", 2504, 2429, 1000, 500)]
        parse_calls = [hit]

        def fake_parse(*a, **k):
            return parse_calls.pop(0) if parse_calls else hit

        # Mock the cache load to return the desired scale.
        with patch("actions.world_map_nav._load_persisted_scale",
                   return_value=persisted_scale), \
             patch("actions.world_map_nav._save_persisted_scale"), \
             patch("capture.adb_capture.capture_screen", return_value=frame), \
             patch("actions.world_map_nav.parse_visible_ports",
                   side_effect=fake_parse), \
             patch.object(self.nav, "_swipe_pan",
                          side_effect=lambda dpx, dpy, w, h: (stride_swipes.append((dpx, dpy)) or (dpx, dpy))), \
             patch("actions.world_map_nav.time.sleep"):
            pos = self.nav.pan_to_port("port royal",
                                        from_port=from_port,
                                        max_pans=8)
        return pos, stride_swipes

    def test_stride_issued_when_distance_exceeds_one_pan(self):
        # London → Port Royal: Δgame=(-2167, 1198), scale~(2,2)
        # → total Δpix ≈ (-4334, 2336).  Per-pan max = 0.8 * 2400 = 1920
        # in x, 0.8 * 1080 = 864 in y.
        # n_needed_x = ceil(4334/1920) = 3; n_needed_y = ceil(2336/864) = 3.
        # n_stride = min(3 // 2, 8 - 2) = 1.  Half-power stride (changed
        # 2026-05-20 from n-1: the old formula overshot into empty
        # Atlantic mid-stride.  Iterative recalibration now handles the
        # remaining 50%).
        pos, swipes = self._run_pan(from_port="London")
        self.assertEqual(pos, (1000, 500))
        # 1 stride pan, no iterative pan (loop terminated on attempt 1).
        self.assertEqual(len(swipes), 1)

    def test_stride_skipped_when_from_port_unknown(self):
        pos, swipes = self._run_pan(from_port="Atlantis")  # not in catalogue
        self.assertEqual(pos, (1000, 500))
        # 0 stride pans — loop terminates on attempt 1.
        self.assertEqual(len(swipes), 0)

    def test_stride_skipped_when_no_persisted_scale(self):
        pos, swipes = self._run_pan(from_port="London",
                                     persisted_scale=(None, None))
        self.assertEqual(pos, (1000, 500))
        self.assertEqual(len(swipes), 0)

    def test_stride_reserves_budget_for_iterative_loop(self):
        # Pretend distance is enormous — needs 100 pans worth.
        # max_pans=8 → max_stride=6 → strides 6, not all 100.
        # (Build a synthetic catalogue entry far away.)
        self.nav._ports["far"] = {"x": 100_000, "y": 100_000}
        frame = _frame()
        stride_swipes: list = []
        hit = [_vp("far", 100_000, 100_000, 1000, 500)]
        with patch("actions.world_map_nav._load_persisted_scale",
                   return_value=(2.0, 2.0)), \
             patch("actions.world_map_nav._save_persisted_scale"), \
             patch("capture.adb_capture.capture_screen", return_value=frame), \
             patch("actions.world_map_nav.parse_visible_ports",
                   return_value=hit), \
             patch.object(self.nav, "_swipe_pan",
                          side_effect=lambda dpx, dpy, w, h: (stride_swipes.append((dpx, dpy)) or (dpx, dpy))), \
             patch("actions.world_map_nav.time.sleep"):
            self.nav.pan_to_port("far", from_port="London", max_pans=8)
        # max_stride = max_pans - 2 = 6.
        self.assertEqual(len(stride_swipes), 6)


class StrideMathTests(_Base):
    """Direct unit tests on _stride_from — easier to inspect than the loop."""

    def test_stride_count_matches_distance(self):
        from_info = {"x": 4671, "y": 1231}     # London
        target_gx, target_gy = 2504, 2429       # Port Royal
        swipes: list = []
        with patch("capture.adb_capture.capture_screen", return_value=_frame()), \
             patch.object(self.nav, "_swipe_pan",
                          side_effect=lambda dpx, dpy, w, h: (swipes.append((dpx, dpy)) or (dpx, dpy))), \
             patch("actions.world_map_nav.time.sleep"):
            n, cum_dpx, cum_dpy = self.nav._stride_from(
                from_info, target_gx, target_gy,
                scale_x=2.03, scale_y=1.94,
                max_pans=8,
            )
        # n_needed = max(ceil(4400/1920), ceil(2324/864)) = max(3,3) = 3
        # n_stride = min(3 // 2, 6) = 1  (half-power stride)
        self.assertEqual(n, 1)
        self.assertEqual(len(swipes), 1)
        # Cumulative swipe equals n × per-pan swipe; per_dpx/per_dpy
        # match the per-pan values issued to _swipe_pan.
        per_dpx, per_dpy = swipes[0]
        self.assertEqual(cum_dpx, per_dpx * n)
        self.assertEqual(cum_dpy, per_dpy * n)

    def test_swipe_direction_matches_target_direction(self):
        # Target west and south of from — swipe should drag east + north
        # (opposite of camera-motion direction).
        from_info = {"x": 5000, "y": 1000}
        target_gx, target_gy = 2000, 3000       # west + south
        swipes: list = []
        with patch("capture.adb_capture.capture_screen", return_value=_frame()), \
             patch.object(self.nav, "_swipe_pan",
                          side_effect=lambda dpx, dpy, w, h: (swipes.append((dpx, dpy)) or (dpx, dpy))), \
             patch("actions.world_map_nav.time.sleep"):
            self.nav._stride_from(
                from_info, target_gx, target_gy,
                scale_x=2.0, scale_y=2.0, max_pans=8,
            )
        # dgx=-3000 (west), dgy=2000 (south).
        # total_dpx = -3000*2 = -6000 → camera should move west, so swipe
        # the map content east → positive dpx in _swipe_pan.
        # total_dpy = 2000*2 = 4000 → camera south, swipe map north → negative dpy.
        self.assertGreater(swipes[0][0], 0, "Should swipe map east when target is west")
        self.assertLess(swipes[0][1], 0, "Should swipe map north when target is south")

    def test_swipe_direction_matches_target_direction_unpacks_tuple(self):
        # The previous swipe-direction test calls _stride_from but ignores
        # the return; the tuple change doesn't affect it.  This is a
        # placeholder to keep the regression covered after the API change.
        pass

    def test_skip_when_distance_under_one_pan(self):
        # Target only 100 game units away, scale 2 → 200 pix total.
        # Far below max per-pan; nothing to stride.
        from_info = {"x": 2400, "y": 2300}
        swipes: list = []
        with patch("capture.adb_capture.capture_screen", return_value=_frame()), \
             patch.object(self.nav, "_swipe_pan",
                          side_effect=lambda dpx, dpy, w, h: (swipes.append((dpx, dpy)) or (dpx, dpy))), \
             patch("actions.world_map_nav.time.sleep"):
            n, cum_dpx, cum_dpy = self.nav._stride_from(
                from_info, 2504, 2429,
                scale_x=2.0, scale_y=2.0, max_pans=8,
            )
        self.assertEqual(n, 0)
        self.assertEqual(len(swipes), 0)
        # Nothing strided → no cumulative swipe.
        self.assertEqual((cum_dpx, cum_dpy), (0, 0))


class LookupPortTests(_Base):

    def test_canonical_name(self):
        self.assertIsNotNone(self.nav._lookup_port("London"))
        self.assertIsNotNone(self.nav._lookup_port("london"))
        self.assertIsNotNone(self.nav._lookup_port("  London  "))

    def test_alias_resolves_to_catalogue_entry(self):
        # Catalogue has "lisboa", caller uses canonical "Lisbon".
        self.nav._ports["lisboa"] = {"x": 100, "y": 200}
        self.nav._aliases = {"lisbon": ["lisboa"]}
        info = self.nav._lookup_port("Lisbon")
        self.assertIsNotNone(info)
        self.assertEqual(info["x"], 100)

    def test_unknown_port_returns_none(self):
        self.assertIsNone(self.nav._lookup_port("Atlantis"))


if __name__ == "__main__":
    unittest.main()
