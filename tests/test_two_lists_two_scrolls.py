"""The world map's list and the village panel's trade list are DIFFERENT lists.

LIVE 2026-08-30 at Hutu Village. Both scrolls went through one helper hardcoded to (300,700)
— the map's own destination rail, down the left. That is right for the destination list and
wrong for the trade list, which lives in the Village Info panel on the RIGHT. So the bot sat
swiping a rail holding one already-selected result while the list it was reading never moved:
sixteen ticks, the same screen re-read every time, and the mission stopped holding a partial
recipe for a good it could see the whole time.

The `why=` string travelled into the wrong context too, logging "the search did not surface
it" about a search that was not running — the tell that one helper was serving two callers.
"""
import unittest
from unittest.mock import patch

from brain.activities.world_map import WorldMapActivity


class _Frame:
    width, height = 2400, 1080


class _Panel:
    def __init__(self, bbox): self.bbox = bbox


class TheTradeListScrollsItsOwnPanel(unittest.TestCase):
    def _act(self):
        return WorldMapActivity(capture_fn=lambda *a, **k: _Frame())

    def test_it_swipes_inside_the_panel_not_the_map_rail(self):
        act = self._act()
        # The Village Info panel as it sat on screen: right side, x 1704-2232.
        panel = _Panel((1704, 100, 2232, 980))
        with patch("vision.omniparser.parse_fast_cached", return_value=[]), \
             patch("vision.region_detectors.panels.detect_right_panel", return_value=panel), \
             patch("actions.ui.scroll") as scroll:
            self.assertTrue(act._scroll_trade_list())

        (x, y, dy), kw = scroll.call_args[0], scroll.call_args[1]
        self.assertGreater(x, 1200, "the trade list is on the RIGHT, not the map's left rail")
        self.assertTrue(1704 <= x <= 2232, "and inside the panel that was detected")
        self.assertLess(dy, 0, "scrolling down the list")
        self.assertNotIn("search", (kw.get("why") or "").lower(),
                         "no search is running; that reason belongs to the map's list")

    def test_the_map_rail_still_scrolls_where_it_always_did(self):
        act = self._act()
        with patch("actions.ui.scroll") as scroll:
            act._scroll_list()
        x, y, dy = scroll.call_args[0]
        self.assertEqual((x, y), (300, 700), "the destination rail is unchanged")

    def test_no_panel_means_no_guess(self):
        act = self._act()
        with patch("vision.omniparser.parse_fast_cached", return_value=[]), \
             patch("vision.region_detectors.panels.detect_right_panel", return_value=None), \
             patch("actions.ui.scroll") as scroll:
            self.assertFalse(act._scroll_trade_list(), "says so rather than swiping somewhere")
            scroll.assert_not_called()


if __name__ == "__main__":
    unittest.main()
