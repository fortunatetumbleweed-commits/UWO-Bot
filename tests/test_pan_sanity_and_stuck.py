"""Tests for the 2026-05-20 pan_to_port fixes:

  1. Scale sanity check — reject implausible fresh calibration values
     (negative, near-zero, or wildly different from cached) so a single
     bad pair (e.g. a chrome label slipping through) can't overwrite a
     good cached scale.

  2. Stuck detection — if the same swipe vector is emitted against the
     same visible-port set as the previous attempt, the map isn't
     moving (panning boundary or gesture rejected).  Abort fast instead
     of burning the rest of the budget on the same wrong vector.

  3. Chrome filter — "My Company Location", "Target Location" and
     "Undiscovered Area" must NOT be matched as ports.
"""
import unittest
from unittest.mock import patch, MagicMock

from vision.world_map_parser import VisiblePort, _match_token_to_port
from actions.world_map_nav import (
    WorldMapNavigator,
    _is_plausible_scale,
    _MIN_PLAUSIBLE_SCALE,
    _MAX_PLAUSIBLE_SCALE,
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


class ScalePlausibilityTests(unittest.TestCase):

    def test_typical_scale_is_plausible(self):
        self.assertTrue(_is_plausible_scale(2.03))
        self.assertTrue(_is_plausible_scale(0.5))
        self.assertTrue(_is_plausible_scale(10.0))

    def test_near_zero_rejected(self):
        self.assertFalse(_is_plausible_scale(0.0))
        self.assertFalse(_is_plausible_scale(0.03))     # the live failure
        self.assertFalse(_is_plausible_scale(_MIN_PLAUSIBLE_SCALE - 0.01))

    def test_negative_rejected(self):
        # _calibrate can return negative when two ports happen to be
        # ordered the wrong way in pixel coords vs game coords.
        self.assertFalse(_is_plausible_scale(-0.001))   # the live failure
        self.assertFalse(_is_plausible_scale(-2.0))

    def test_huge_rejected(self):
        self.assertFalse(_is_plausible_scale(_MAX_PLAUSIBLE_SCALE + 1))
        self.assertFalse(_is_plausible_scale(1000.0))

    def test_none_rejected(self):
        self.assertFalse(_is_plausible_scale(None))

    def test_divergent_from_cache_rejected(self):
        # Cached 2.0; fresh 0.5 — too far apart, zoom doesn't change.
        self.assertFalse(_is_plausible_scale(0.5, cached=2.0))
        self.assertFalse(_is_plausible_scale(4.0, cached=2.0))

    def test_close_to_cache_accepted(self):
        # 10% drift from cached — still well within sanity.
        self.assertTrue(_is_plausible_scale(2.2, cached=2.0))
        self.assertTrue(_is_plausible_scale(1.8, cached=2.0))


class CalibrationRejectionTests(unittest.TestCase):
    """End-to-end: an implausible fresh calibration must NOT overwrite
    a good cached scale, and the loop must still produce a sensible swipe."""

    def setUp(self):
        with patch("actions.world_map_nav.load_port_catalogue", return_value={}):
            self.nav = WorldMapNavigator.__new__(WorldMapNavigator)
            self.nav._ports = {
                "port royal": {"name": "Port Royal", "x": 2504, "y": 2429},
                "tumbes":     {"name": "Tumbes",     "x": 2375, "y": 2276},
            }
            self.nav._aliases = {}

    def test_implausible_fresh_does_not_overwrite_cache(self):
        # Frame 1: clean calibration → scale ~2.0 cached.
        # Frame 2: 3 visible ports but their geometry yields scale=(-0.001, 0.03)
        #          (the live 2026-05-20 failure mode).  Sanity check must reject
        #          and keep using the 2.0 cache so the swipe is sensible.
        # Frame 3: target visible → loop terminates.
        clean = [
            _vp("a", 1000, 1000, 100, 100),
            _vp("b", 1200, 1200, 500, 500),
        ]
        # Construct 3 ports whose pair ratios would yield bad scale:
        # game-coords differ by lot but pixels barely move → tiny scale.
        bad = [
            _vp("a", 1000, 1000, 100, 100),
            _vp("b", 6000, 1010, 102, 102),
            _vp("c", 3500, 1005, 101, 101),
        ]
        hit = [_vp("port royal", 2504, 2429, 800, 500)]
        frames = [_frame(), _frame(), _frame()]
        parses = [clean, bad, hit]
        i = [0]

        def fake_parse(*a, **k):
            r = parses[i[0]]; i[0] += 1
            return r

        swipes: list = []
        with patch("actions.world_map_nav._load_persisted_scale",
                   return_value=(None, None)), \
             patch("actions.world_map_nav._save_persisted_scale"), \
             patch("capture.adb_capture.capture_screen",
                   side_effect=frames), \
             patch("actions.world_map_nav.parse_visible_ports",
                   side_effect=fake_parse), \
             patch.object(self.nav, "_swipe_pan",
                          side_effect=lambda dpx, dpy, w, h: (swipes.append((dpx, dpy)) or (dpx, dpy))), \
             patch("actions.world_map_nav.time.sleep"):
            pos = self.nav.pan_to_port("port royal", max_pans=5)

        self.assertEqual(pos, (800, 500))
        # 2 swipes — frame 1 and frame 2.  Frame 2 with cached scale 2.0
        # produces a meaningful Δpix; the implausible fresh (-0.001, 0.03)
        # is rejected.  Frame 3 finds the target.
        self.assertEqual(len(swipes), 2)
        # The second swipe must not be a tiny noise vector — abs ≥ _MIN_PAN_PX.
        from actions.world_map_nav import _MIN_PAN_PX
        sx, sy = swipes[1]
        self.assertTrue(abs(sx) >= _MIN_PAN_PX or abs(sy) >= _MIN_PAN_PX,
            f"Second swipe ({sx},{sy}) is sub-floor — implausible scale was used")


class ChromeFilterTests(unittest.TestCase):
    """Player-location chrome labels must NOT match against catalogue ports."""

    def _ports(self):
        # A small catalogue with names that could potentially fuzzy-match
        # the chrome strings if filtering weren't applied.
        return {
            "lisbon":     {"name": "Lisbon",     "x": 100, "y": 100},
            "london":     {"name": "London",     "x": 200, "y": 200},
            "port royal": {"name": "Port Royal", "x": 300, "y": 300},
        }

    def test_my_company_location_not_matched(self):
        # Pre-fix this slipped past the chrome filter and fuzzy-matched
        # something in the catalogue — corrupted calibration.
        self.assertIsNone(
            _match_token_to_port("My Company Location", self._ports(), {})
        )

    def test_target_location_not_matched(self):
        self.assertIsNone(
            _match_token_to_port("Target Location", self._ports(), {})
        )

    def test_undiscovered_area_not_matched(self):
        self.assertIsNone(
            _match_token_to_port("222 Undiscovered Area", self._ports(), {})
        )
        self.assertIsNone(
            _match_token_to_port("Undiscovered Area", self._ports(), {})
        )

    def test_real_port_still_matched(self):
        # Regression check: the chrome additions don't accidentally
        # filter out real ports.
        self.assertEqual(
            _match_token_to_port("Lisbon", self._ports(), {}),
            "lisbon",
        )


if __name__ == "__main__":
    unittest.main()
