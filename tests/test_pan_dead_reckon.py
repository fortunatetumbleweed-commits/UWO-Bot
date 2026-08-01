"""Tests for dead-reckoning when 0 visible ports.

Regression: 2026-05-20 live run.  After stride pans the bot ended up in
an empty Atlantic region with no port labels visible.  The iterative
loop's `if not visible: return None` killed the call, falling back to
the port-search list (which doesn't contain cross-region targets).

Fix: when from_info + cached_scale are both known, estimate camera
position from cumulative commanded swipe + departing port, compute a
corrective swipe toward target, and continue.  Stuck detector still
fires if we issue the same dead-reckon swipe twice in a row.
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


class DeadReckonTests(unittest.TestCase):

    def setUp(self):
        with patch("actions.world_map_nav.load_port_catalogue", return_value={}):
            self.nav = WorldMapNavigator.__new__(WorldMapNavigator)
            self.nav._ports = {
                "port royal": {"x": 2504, "y": 2429},
                "london":     {"x": 4680, "y": 1210},
            }
            self.nav._aliases = {}

    def test_no_visible_with_scale_dead_reckons(self):
        """0 visible ports + known from_info + persisted scale →
        dead-reckon swipe instead of returning None."""
        # Sequence: attempt 1 has 0 visible (post-stride empty Atlantic).
        # Dead-reckoning fires.  Attempt 2 has the target visible — done.
        empty: list = []
        hit = [_vp("london", 4680, 1210, 800, 500)]
        parses = [empty, hit]
        i = [0]

        def fake_parse(*a, **k):
            r = parses[i[0] % len(parses)]
            i[0] += 1
            return r

        swipes: list = []
        with patch("actions.world_map_nav._load_persisted_scale",
                   return_value=(2.0, 2.0)), \
             patch("actions.world_map_nav._save_persisted_scale"), \
             patch("capture.adb_capture.capture_screen", return_value=_frame()), \
             patch("actions.world_map_nav.parse_visible_ports",
                   side_effect=fake_parse), \
             patch.object(self.nav, "_swipe_pan",
                          side_effect=lambda dpx, dpy, w, h: (swipes.append((dpx, dpy)) or (dpx, dpy))), \
             patch("actions.world_map_nav.time.sleep"):
            pos = self.nav.pan_to_port(
                "london", from_port="Port Royal", max_pans=5,
            )
        # Must NOT have returned None on the empty frame.  Found target
        # on the second attempt → returns target tap position.
        self.assertEqual(pos, (800, 500))
        # At least one swipe was issued via dead-reckoning (loop didn't
        # bail out on attempt 1).
        self.assertGreaterEqual(len(swipes), 1)

    def test_no_visible_no_from_info_still_gives_up(self):
        """Without from_port we cannot dead-reckon; behaviour unchanged."""
        with patch("actions.world_map_nav._load_persisted_scale",
                   return_value=(2.0, 2.0)), \
             patch("actions.world_map_nav._save_persisted_scale"), \
             patch("capture.adb_capture.capture_screen", return_value=_frame()), \
             patch("actions.world_map_nav.parse_visible_ports",
                   return_value=[]), \
             patch.object(self.nav, "_swipe_pan",
                          return_value=(0, 0)) as mock_swipe, \
             patch("actions.world_map_nav.time.sleep"):
            pos = self.nav.pan_to_port("london", max_pans=5)   # no from_port
        self.assertIsNone(pos)
        mock_swipe.assert_not_called()

    def test_no_visible_no_cached_scale_still_gives_up(self):
        """Without cached scale we cannot dead-reckon either."""
        with patch("actions.world_map_nav._load_persisted_scale",
                   return_value=(None, None)), \
             patch("actions.world_map_nav._save_persisted_scale"), \
             patch("capture.adb_capture.capture_screen", return_value=_frame()), \
             patch("actions.world_map_nav.parse_visible_ports",
                   return_value=[]), \
             patch.object(self.nav, "_swipe_pan",
                          return_value=(0, 0)) as mock_swipe, \
             patch("actions.world_map_nav.time.sleep"):
            pos = self.nav.pan_to_port(
                "london", from_port="Port Royal", max_pans=5,
            )
        self.assertIsNone(pos)
        mock_swipe.assert_not_called()

    def test_dead_reckon_stuck_detector(self):
        """If we issue the same dead-reckon swipe twice with no anchors
        both frames, abort fast — the map clearly isn't responding."""
        # Both attempts return empty visible.  Dead-reckon should fire
        # once, then the next attempt with same (empty, same swipe)
        # triggers stuck detection.
        empty: list = []
        with patch("actions.world_map_nav._load_persisted_scale",
                   return_value=(2.0, 2.0)), \
             patch("actions.world_map_nav._save_persisted_scale"), \
             patch("capture.adb_capture.capture_screen", return_value=_frame()), \
             patch("actions.world_map_nav.parse_visible_ports",
                   return_value=empty), \
             patch.object(self.nav, "_swipe_pan",
                          return_value=(100, 100)) as mock_swipe, \
             patch("actions.world_map_nav.time.sleep"):
            pos = self.nav.pan_to_port(
                "london", from_port="Port Royal", max_pans=8,
            )
        self.assertIsNone(pos)
        # 2 swipes total: 1 stride pan + 1 dead-reckon swipe on attempt 1.
        # Attempt 2 fires the stuck detector (same empty visible set +
        # same dead-reckon swipe) and aborts before issuing.
        self.assertEqual(mock_swipe.call_count, 2)


if __name__ == "__main__":
    unittest.main()
