"""A calibrated coordinate must not be tapped on a screen it does not belong to.

Live 2026-08-23, run 39. After bartering, the tail asked for the world map. The village
BARTER panel was classified 'port_overworld', so open_world_map tapped the calibrated port
globe:

    [open_world_map] PORT — tap globe @ (2227,361)

On that screen (2227,361) is a Check-Barter-Effect / Village-Influence control, which opened
an info popup ("This is the impact currently received in this village"). The loop then spun
re-perceiving a screen it had opened itself, and the run hung.

The family classifier has called that same barter panel 'sea' AND 'port_overworld' on
different frames, so the state alone cannot gate the tap. A village's left menu —
barter + gifting — appears on every village screen and on no port screen, so it is the
disproof that costs one detector call.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from PIL import Image

from actions import sail_actions

FRAME = Image.new("RGB", (2400, 1080))
VILLAGE_MENU = ["Explore", "Gifting", "Loot", "Recruit Crew", "Barter"]
PORT_MENU = ["Purchase", "Sell"]


def _menu(labels):
    return types.SimpleNamespace(labels=lambda: list(labels), items=[], find=lambda l: None)


class VillageIsDetectedByItsMenu(unittest.TestCase):

    def _looks(self, labels):
        with patch("vision.omniparser.parse_fast_cached", return_value=[]), \
             patch("vision.region_detectors.left_menu.detect_left_menu",
                   return_value=_menu(labels)):
            return sail_actions._looks_like_a_village(FRAME)

    def test_a_village_menu_is_recognised(self):
        self.assertTrue(self._looks(VILLAGE_MENU))

    def test_a_port_menu_is_not(self):
        self.assertFalse(self._looks(PORT_MENU))

    def test_barter_alone_is_not_enough(self):
        """The word turns up elsewhere; barter+gifting is what pins a village."""
        self.assertFalse(self._looks(["Barter"]))

    def test_no_menu_is_not_a_village(self):
        with patch("vision.omniparser.parse_fast_cached", return_value=[]), \
             patch("vision.region_detectors.left_menu.detect_left_menu", return_value=None):
            self.assertFalse(sail_actions._looks_like_a_village(FRAME))

    def test_a_detector_failure_is_not_a_village(self):
        with patch("vision.omniparser.parse_fast_cached", side_effect=RuntimeError("boom")):
            self.assertFalse(sail_actions._looks_like_a_village(FRAME))


class TheGlobeIsNotTappedOnAVillage(unittest.TestCase):

    def _open(self, *, menu):
        taps = []
        with patch.object(sail_actions, "where_am_i",
                          return_value={"location": "port_overworld", "port": "X",
                                        "detail": ""}), \
             patch.object(sail_actions, "_is_on_world_map", return_value=False), \
             patch.object(sail_actions, "capture_screen", return_value=FRAME), \
             patch.object(sail_actions, "tap", side_effect=lambda x, y: taps.append((x, y))), \
             patch("vision.omniparser.parse_fast_cached", return_value=[]), \
             patch("vision.region_detectors.left_menu.detect_left_menu",
                   return_value=_menu(menu)), \
             patch("time.sleep", lambda *_a, **_k: None):
            result = sail_actions.open_world_map()
        return result, taps

    def test_it_refuses_on_a_village_screen(self):
        result, taps = self._open(menu=VILLAGE_MENU)
        self.assertFalse(result)
        self.assertEqual(taps, [], "the calibrated globe must not be tapped here")

    def test_it_still_taps_the_globe_at_a_real_port(self):
        _result, taps = self._open(menu=PORT_MENU)
        self.assertIn(sail_actions._PORT_WORLD_MAP_GLOBE, taps)


if __name__ == "__main__":
    unittest.main()
