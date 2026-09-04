"""A screen is identified by its TITLE or its structure — never by a string somewhere on it.

Two live failures on 2026-08-25 came from matching text anywhere on the frame:

  * `_is_on_world_map` matched the phrase "world map", which the PORT MAP prints on its own
    globe button — so the bot believed the world map was open, looked for an Explore tab that
    was not there, and abandoned the run;
  * `_world_map_port_labels_visible` matched ONE known port name in the left of the frame, and
    the port map's title IS a port name sitting exactly there.

The loading screens are the same story waiting to happen: their whole-frame keyword check had
already lost "voyage" because NPC speech bubbles say "Bon voyage!".
"""

from __future__ import annotations

import os
import unittest

from PIL import Image

FRAMES = os.path.join(os.path.dirname(__file__), "..", "data", "test_frames", "transitions")
NEEDED = ("arrival_city_entering.png", "departure_sailing_preparing.png",
          "port_map_london.png", "at_sea_days_of_sailing_left.png")
HAVE_FRAMES = all(os.path.isfile(os.path.join(FRAMES, n)) for n in NEEDED)


def _frame(name):
    return Image.open(os.path.join(FRAMES, name)).convert("RGB")


@unittest.skipUnless(HAVE_FRAMES, "reference frames not present")
class TransitionScreensAreReadFromTheTitle(unittest.TestCase):
    """Both mean the same thing: the overworld is in transition, so wait and re-perceive."""

    def test_arrival_is_a_loading_screen(self):
        """Big "City" top-left, subtitle "Entering..." — the ship has reached a port."""
        from actions.sail_actions import _is_loading_screen
        self.assertTrue(_is_loading_screen(_frame("arrival_city_entering.png")))

    def test_departure_is_a_loading_screen(self):
        """Big "Sailing" top-left, "Preparing for Voyage..." — leaving the harbour."""
        from actions.sail_actions import _is_loading_screen
        self.assertTrue(_is_loading_screen(_frame("departure_sailing_preparing.png")))

    def test_open_sea_is_not_a_loading_screen(self):
        """The sea HUD prints "14 Days of Sailing Left" in the SAME top-left corner the
        transition title uses. Matching the big title word "Sailing" there made the bot read
        open sea as a transition, so it stopped knowing where it was, re-selected the port it
        had just reached and looped on departure (live 2026-08-25). Only the subtitle words —
        Entering / Preparing / Loading — name a transition."""
        from actions.sail_actions import _is_loading_screen
        self.assertFalse(_is_loading_screen(_frame("at_sea_days_of_sailing_left.png")))

    def test_a_speech_bubble_is_not_a_loading_screen(self):
        """The port map carries an NPC line ("You there. Come over here..."). Arbitrary
        sentences must never be able to halt the bot as a transition."""
        from actions.sail_actions import _is_loading_screen
        self.assertFalse(_is_loading_screen(_frame("port_map_london.png")))


@unittest.skipUnless(HAVE_FRAMES, "reference frames not present")
class ThePortMapIsNotTheWorldMap(unittest.TestCase):
    """It advertises the words "World Map" on its own globe button."""

    def setUp(self):
        self.port_map = _frame("port_map_london.png")

    def test_it_is_not_read_as_the_world_map(self):
        from actions.sail_actions import _is_on_world_map
        self.assertFalse(_is_on_world_map(self.port_map),
                         "the port map's globe button was mistaken for the world map")

    def test_it_is_recognised_as_the_port_map(self):
        from actions.sail_actions import _is_on_port_map
        self.assertTrue(_is_on_port_map(self.port_map))

    def test_its_globe_button_is_locatable(self):
        """The way OUT: tapping it opens the world map."""
        from actions.sail_actions import _world_map_button, _ocr_frame
        self.assertIsNotNone(_world_map_button(list(_ocr_frame(self.port_map))))

    def test_one_port_name_does_not_make_a_world_map(self):
        """The port map's title is a port name in the left of the frame — the exact thing
        the port-label check used to accept as proof of the world map."""
        from actions.sail_actions import _world_map_port_labels_visible
        self.assertFalse(_world_map_port_labels_visible(self.port_map))


if __name__ == "__main__":
    unittest.main()
