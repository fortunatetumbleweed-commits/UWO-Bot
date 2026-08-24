"""A village screen must not be classified as a port overworld.

A chromed screen normally carries a top-right icon bar with a Home button; a VILLAGE does
not (user, 2026-08-23), so its chrome reads exactly like an overworld's. Measured:

    port overworld   home=False  hamburger=False
    market (chromed) home=True   hamburger=False
    village landing  home=False  hamburger=False
    village barter   home=False  hamburger=False

With that discriminator gone the cascade fell through to counting right-edge panels — and on
a chromed screen the right panel appears only in RESPONSE to selecting an item, so the count
is a behaviour, not a state. The same village barter screen was classified `village`,
`building`, `port_overworld`, `sea` and `unknown` inside one run, and when it landed on
`port_overworld`, `open_world_map` tapped the calibrated port globe at (2227,361) — a
Check-Barter-Effect control there — and the mission hung.

The village's left menu (barter + gifting) appears on no port screen, so it is the exact
disproof.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from PIL import Image

from brain import perceive

FRAME = Image.new("RGB", (2400, 1080))
VILLAGE = ["Explore", "Gifting", "Loot", "Recruit Crew", "Barter"]
PORT = ["Purchase", "Sell"]
WORLD_MAP_NOISE = ["gold", "Trade Event", "Schedule"]      # what the detector really returns


def _menu(labels):
    return types.SimpleNamespace(labels=lambda: list(labels), items=[l for l in labels],
                                 find=lambda l: None)


def _has_village_menu(labels):
    with patch("vision.omniparser.parse_fast_cached", return_value=[]), \
         patch("vision.region_detectors.left_menu.detect_left_menu",
               return_value=_menu(labels)):
        return perceive._has_village_menu(FRAME)


class TheVillageMenuIsTheDisproof(unittest.TestCase):

    def test_a_village_menu_is_recognised(self):
        self.assertTrue(_has_village_menu(VILLAGE))

    def test_a_port_menu_is_not(self):
        self.assertFalse(_has_village_menu(PORT))

    def test_barter_alone_is_not_enough(self):
        """The word appears elsewhere; barter+gifting is what pins a village."""
        self.assertFalse(_has_village_menu(["Barter"]))

    def test_the_world_map_is_not_a_village(self):
        """detect_left_menu really returns this on the world map — it must not match."""
        self.assertFalse(_has_village_menu(WORLD_MAP_NOISE))

    def test_no_menu_is_not_a_village(self):
        with patch("vision.omniparser.parse_fast_cached", return_value=[]), \
             patch("vision.region_detectors.left_menu.detect_left_menu", return_value=None):
            self.assertFalse(perceive._has_village_menu(FRAME))

    def test_a_detector_failure_is_not_a_village(self):
        with patch("vision.omniparser.parse_fast_cached", side_effect=RuntimeError("boom")):
            self.assertFalse(perceive._has_village_menu(FRAME))


if __name__ == "__main__":
    unittest.main()
