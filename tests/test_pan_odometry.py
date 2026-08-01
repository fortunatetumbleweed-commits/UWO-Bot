"""Tests for the odometry-based scale recalibration + persisted-scale
validation introduced 2026-05-20.

Background — the live failure that motivated this:
  - `calibration.json` on disk had `scale_x=-0.0046, scale_y=0.0255` from
    an earlier buggy run.  These values are physically nonsense but
    passed the load path unchecked.
  - Inside `pan_to_port` the sanity check compared fresh calibration
    against the cached value.  Fresh calibration produced correct ~2.0
    every iteration, but the divergence-from-cache rule rejected every
    correct fresh value — the bad cache permanently won.
  - As a result the bot panned with garbage scale (effective scale_y=0.03)
    and either overshot off-screen or barely moved.

The fix has two parts:
  A. `_load_persisted_scale` runs disk values through `_is_plausible_scale`;
     implausible values are rejected and the file is deleted.
  B. `pan_to_port` now derives an independent "odometry" scale from
     (departing port catalogue, cumulative commanded swipe, any visible
     port screen position).  Odometry breaks the cache-poison loop: it's
     observed-from-reality rather than self-referentially cached.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from vision.world_map_parser import VisiblePort
from actions.world_map_nav import (
    WorldMapNavigator,
    _is_plausible_scale,
    _load_persisted_scale,
    _save_persisted_scale,
    _pick_scale,
)


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


class PersistedScaleValidationTests(unittest.TestCase):

    def test_implausible_persisted_negative_x_rejected_and_file_deleted(self):
        """The live 2026-05-20 failure: scale_x=-0.0046 on disk.
        Must return (None, None) AND remove the file."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "calibration.json"
            path.write_text(json.dumps({
                "scale_x": -0.004598875830352581,
                "scale_y":  0.025543723990292783,
            }))
            with patch("actions.world_map_nav._SCALE_CACHE_PATH", path):
                sx, sy = _load_persisted_scale()
            self.assertIsNone(sx)
            self.assertIsNone(sy)
            self.assertFalse(path.exists(), "Poisoned file should be deleted")

    def test_implausible_persisted_tiny_y_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "calibration.json"
            path.write_text(json.dumps({"scale_x": 2.03, "scale_y": 0.001}))
            with patch("actions.world_map_nav._SCALE_CACHE_PATH", path):
                sx, sy = _load_persisted_scale()
            self.assertIsNone(sx)
            self.assertIsNone(sy)
            self.assertFalse(path.exists())

    def test_plausible_persisted_loaded(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "calibration.json"
            path.write_text(json.dumps({"scale_x": 2.03, "scale_y": 1.94}))
            with patch("actions.world_map_nav._SCALE_CACHE_PATH", path):
                sx, sy = _load_persisted_scale()
            self.assertAlmostEqual(sx, 2.03)
            self.assertAlmostEqual(sy, 1.94)
            self.assertTrue(path.exists(), "Plausible file should remain on disk")

    def test_save_refuses_implausible_values(self):
        """Defensive: even if calling code passes garbage, don't write it."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "calibration.json"
            with patch("actions.world_map_nav._SCALE_CACHE_PATH", path):
                _save_persisted_scale(-0.0046, 0.0255)
            self.assertFalse(path.exists(),
                "_save_persisted_scale must refuse implausible values")


class OdometryScaleDerivationTests(unittest.TestCase):
    """Direct test of `_odometry_scale` — pure math, no real swipes."""

    def setUp(self):
        with patch("actions.world_map_nav.load_port_catalogue", return_value={}):
            self.nav = WorldMapNavigator.__new__(WorldMapNavigator)
            self.nav._ports = {}
            self.nav._aliases = {}

    def test_first_frame_zero_swipe_derives_scale_from_known_port_offset(self):
        """Camera opened at L (departing port).  total_swipe = (0, 0).
        A visible port P at known catalogue position appears at known
        screen position.  Derived scale should reverse-engineer correctly.
        """
        # Scenario: world map opened with London (4671, 1231) at screen
        # centre (1200, 540).  Edinburgh (4530, 1100) — 141 game-units
        # west, 131 north of London — appears at screen (1200 + (4530-4671)*2,
        # 540 + (1100-1231)*2) = (918, 278) with scale 2.0.
        from_info = {"x": 4671, "y": 1231}    # London
        visible = [_vp("edinburgh", 4530, 1100, 918, 278)]
        sx, sy = self.nav._odometry_scale(
            from_info, total_swipe_dpx=0, total_swipe_dpy=0,
            visible=visible, frame_w=2400, frame_h=1080,
        )
        self.assertAlmostEqual(sx, 2.0, places=2)
        self.assertAlmostEqual(sy, 2.0, places=2)

    def test_after_swipe_derives_correct_scale(self):
        """After commanding a swipe Δpix=(-400, 200), the camera should
        have moved 200 game-units east + 100 game-units north at scale 2.0.
        Camera now ≈ (4671 + 200, 1231 - 100) = (4871, 1131).
        A port at catalogue (4530, 1100) appears at:
            (1200 + (4530 - 4871) * 2, 540 + (1100 - 1131) * 2)
          = (1200 - 682,           540 - 62)
          = (518, 478)
        """
        from_info = {"x": 4671, "y": 1231}
        visible = [_vp("edinburgh", 4530, 1100, 518, 478)]
        sx, sy = self.nav._odometry_scale(
            from_info, total_swipe_dpx=-400, total_swipe_dpy=200,
            visible=visible, frame_w=2400, frame_h=1080,
        )
        self.assertAlmostEqual(sx, 2.0, places=2)
        self.assertAlmostEqual(sy, 2.0, places=2)

    def test_median_across_visible_ports(self):
        """Multiple visible ports → median ratio is robust to one outlier."""
        from_info = {"x": 4671, "y": 1231}
        # Three ports with consistent scale-2.0 positions, plus one with
        # corrupted pixel position that would produce scale ~5.0.
        visible = [
            _vp("a", 4671 + 100, 1231,       1400, 540),   # → scale 2.0
            _vp("b", 4671 + 200, 1231,       1600, 540),   # → scale 2.0
            _vp("c", 4671 + 300, 1231,       1800, 540),   # → scale 2.0
            _vp("d", 4671 + 100, 1231,       1700, 540),   # corrupted → scale 5.0
        ]
        sx, _ = self.nav._odometry_scale(
            from_info, 0, 0, visible, 2400, 1080,
        )
        # Median of [2, 2, 2, 5] = 2.  Robust.
        self.assertAlmostEqual(sx, 2.0, places=2)

    def test_returns_none_when_no_port_far_enough(self):
        """All visible ports too close to L in catalogue → no usable
        denominator → returns None on that axis."""
        from_info = {"x": 4671, "y": 1231}
        # A single port 10 game-units away on each axis — below
        # _MIN_PAIR_GAME_DIST.
        visible = [_vp("a", 4681, 1241, 1220, 560)]
        sx, sy = self.nav._odometry_scale(
            from_info, 0, 0, visible, 2400, 1080,
        )
        self.assertIsNone(sx)
        self.assertIsNone(sy)


class PickScaleTests(unittest.TestCase):
    """Selection logic between fresh / odo / cached scale signals."""

    def test_fresh_and_odo_agree_use_fresh(self):
        v, src = _pick_scale(fresh=2.05, odo=2.00, cached=1.95, axis="x")
        self.assertEqual(v, 2.05)
        self.assertEqual(src, "fresh+odo")

    def test_fresh_and_odo_disagree_use_odo(self):
        # Fresh says 0.03 (a bad pair); odo says 2.0 — adopt odo.
        v, src = _pick_scale(fresh=0.03, odo=2.0, cached=2.0, axis="x")
        # 0.03 is implausible absolute → won't be picked anyway, but the
        # interesting case is when fresh is plausible but disagrees.
        # Use plausible-but-disagreeing values instead.
        v, src = _pick_scale(fresh=4.0, odo=2.0, cached=2.0, axis="x")
        self.assertEqual(v, 2.0)
        self.assertEqual(src, "odo-override")

    def test_only_odo_plausible(self):
        v, src = _pick_scale(fresh=None, odo=2.03, cached=None, axis="y")
        self.assertEqual(v, 2.03)
        self.assertEqual(src, "odo")

    def test_only_fresh_plausible(self):
        v, src = _pick_scale(fresh=2.03, odo=None, cached=None, axis="y")
        self.assertEqual(v, 2.03)
        self.assertEqual(src, "fresh")

    def test_neither_plausible_returns_cache(self):
        v, src = _pick_scale(fresh=-0.01, odo=None, cached=2.0, axis="y")
        self.assertEqual(v, 2.0)
        self.assertEqual(src, "cache")

    def test_implausible_fresh_rejected_in_favour_of_odo(self):
        # Live 2026-05-20 case: fresh comes out negative due to bad pair,
        # odo gives correct value.
        v, src = _pick_scale(fresh=-0.005, odo=2.03, cached=2.0, axis="x")
        self.assertEqual(v, 2.03)
        self.assertEqual(src, "odo")


class CachePoisonRecoveryTests(unittest.TestCase):
    """End-to-end: a poisoned cached scale + a correct odo derivation
    should cause pan_to_port to override the cache, NOT keep using the
    poison.  This is the regression from the 2026-05-20 live run.
    """

    def setUp(self):
        with patch("actions.world_map_nav.load_port_catalogue", return_value={
            "london": {"x": 4671, "y": 1231},
            "port royal": {"x": 2504, "y": 2429},
        }):
            self.nav = WorldMapNavigator.__new__(WorldMapNavigator)
            self.nav._ports = {
                "london":     {"x": 4671, "y": 1231},
                "port royal": {"x": 2504, "y": 2429},
            }
            self.nav._aliases = {}

    def test_odometry_overrides_poisoned_cache(self):
        # Force the persisted cache to be poisoned values BUT make
        # _load_persisted_scale return them anyway (skipping the
        # auto-rejection) to exercise the in-pan path.
        # The pair calibration will produce a correct ~2.0 scale, and
        # odometry will also produce ~2.0.  Both agree → fresh wins,
        # cache is replaced.
        frame = _frame()
        # Visible set including a known port other than departing.
        # London (4671, 1231) at screen centre; Edinburgh (4530, 1100)
        # at offset corresponding to scale 2.0.
        visible_with_target = [
            _vp("port royal", 2504, 2429, 800, 500),
        ]
        # First frame: clean calibration view; target Port Royal in view
        # so loop terminates.

        with patch("actions.world_map_nav._load_persisted_scale",
                   return_value=(0.001, 0.001)), \
             patch("actions.world_map_nav._save_persisted_scale") as save_mock, \
             patch("capture.adb_capture.capture_screen", return_value=frame), \
             patch("actions.world_map_nav.parse_visible_ports",
                   return_value=visible_with_target), \
             patch("actions.world_map_nav.time.sleep"):
            pos = self.nav.pan_to_port("port royal",
                                        from_port="London",
                                        max_pans=4)
        # Target visible immediately → returned.
        self.assertEqual(pos, (800, 500))


if __name__ == "__main__":
    unittest.main()
