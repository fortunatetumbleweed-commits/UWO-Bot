"""The title crop must not reach the world map's mode tabs.

Live 2026-08-26, with the fleet bound for Lisboa:

    [classify] → world_map (family-classifier conf=0.99) — short-circuit
    World map check: has_sea_hud=False title_says_world_map=False port_map=True
    [open_world_map] loc='world_map' has persisted 3 re-perceives — not transient

`regions["title"]` ran to 0.40 of the frame, which reaches the Port/Explore/Route/Trade tab
row, and `title_text` returns ONE element from that cluster — it picked 'Port', the boxed
high-contrast button, over the two plain tokens of the real title. Everything downstream then
called the world map a port map, and the voyage never started.

Measured on this frame: the title is 'World' 96..232 and 'Map' 229..336, and the 'Port' tab
runs 762..955. The crop has to land between 336 and 762.
"""

from __future__ import annotations

import os
import unittest

from PIL import Image

FRAME = os.path.join(os.path.dirname(__file__), "..", "data", "test_frames",
                     "transitions", "world_map_with_mode_tabs.png")


@unittest.skipUnless(os.path.exists(FRAME), "world-map fixture not present")
class TheCropLandsBetweenTheTitleAndTheTabs(unittest.TestCase):

    def setUp(self):
        from vision.region_perception import REGIONS
        self.frame = Image.open(FRAME)
        l, t, r, b = REGIONS["title"]
        self.right = r * self.frame.width
        self.top = t * self.frame.height
        self.bottom = b * self.frame.height

    def test_it_clears_the_whole_title(self):
        """'World Map' ends at x=336 on this frame; a crop that cuts it is worse than one that
        overruns."""
        self.assertGreater(self.right, 336 + 40)

    def test_it_stops_short_of_the_first_mode_tab(self):
        """'Port' begins at x=762. Include it and `title_text` will choose it — it is a boxed
        button, and the title is two plain words."""
        self.assertLess(self.right, 762)

    def test_it_stops_above_the_icon_bar(self):
        """The icon bar below the title starts at y=115 on this frame. The old bottom edge,
        0.12H = 129, clipped 14px off it."""
        self.assertLess(self.bottom, 115)

    def test_it_starts_above_the_title_glyphs(self):
        """'Map' starts at y=20 and the old top edge sat at 0.02H = 21, shaving it."""
        self.assertLess(self.top, 20)

    def test_the_old_bound_would_have_included_the_tab(self):
        """Documents the regression rather than asserting a bare number."""
        self.assertGreater(0.40 * self.frame.width, 762)

    def test_the_region_holds_the_title_and_nothing_else(self):
        from vision.region_perception import perceive_regions
        got = {t.strip().lower() for t in
               (perceive_regions(self.frame).get("title").text or [])}
        self.assertEqual(got, {"world", "map"}, f"expected just the title, got {got}")

    def test_the_tab_is_no_longer_available_to_be_chosen(self):
        """The specific failure: `title_text` returns ONE element, and 'Port' — a boxed,
        high-contrast button — won against the title's two plain words."""
        from vision.region_perception import title_text
        self.assertNotEqual((title_text(self.frame) or "").strip().lower(), "port")

    def test_a_two_word_title_still_comes_back_as_one_token(self):
        """Recorded, not fixed. `title_text` returns a single element, so 'World Map' yields
        'Map'. That is a pre-existing limitation of the one-element contract — the same reason
        the KB carries building_name_variants ('shop' -> item_shop) — and it is harmless here
        because screen identity now comes from the classifier. Worth revisiting if a caller
        ever needs the whole title."""
        from vision.region_perception import title_text
        self.assertIn((title_text(self.frame) or "").strip().lower(), {"map", "world"})


@unittest.skipUnless(os.path.exists(FRAME), "world-map fixture not present")
class AndTheScreenIsIdentifiedCorrectly(unittest.TestCase):

    def test_it_is_the_world_map(self):
        from actions.sail_actions import _is_on_world_map
        self.assertTrue(_is_on_world_map(Image.open(FRAME)))

    def test_it_is_not_the_port_map(self):
        """The reading that stalled the voyage: port_map=True on the world map."""
        from actions.sail_actions import _is_on_port_map
        self.assertFalse(_is_on_port_map(Image.open(FRAME)))
