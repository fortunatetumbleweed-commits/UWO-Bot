"""Knowing WHICH TAB the right panel is on, instead of inferring it from the list.

Live 2026-08-24 at Bordeaux the fleet arrived, an event popup covered the right panel, and
the panel underneath was the TASKS tab. Three things then went wrong in sequence:

  * the popup's own close-X sat inside the tab band and was offered as a "tab" candidate;
  * tapping popup furniture never produced a building list, so the search gave up;
  * the code fell through anyway and fuzzy-matched the target against whatever was on the
    panel — hitting the word "market" inside the quest objective "move to market in ...".

Tapping that handed the fleet to a quest voyage to Jakarta and the gather never happened.

The two lists are visually very distinct (user, 2026-08-24), so the tab state is READ:
a strip is a ROW of similar icons, and the selected tab is painted warm cream against
neutral grey — the same "gold means active" idiom as the game's commit buttons.
"""

from __future__ import annotations

import types
import unittest

from PIL import Image

from actions.port_panel import _row_only, _on_buildings_tab
from actions.sail_actions import selected_tab_index


def _el(cx, cy, label="icon"):
    return types.SimpleNamespace(cx=cx, cy=cy, label=label)


def _strip(selected=None, n=4, x0=1912, gap=100, cy=165):
    """A frame with `n` tabs; `selected` (index) painted warm cream, the rest grey."""
    img = Image.new("RGB", (2400, 1080), (30, 30, 30))
    tabs = []
    for i in range(n):
        cx = x0 + i * gap
        colour = (206, 186, 162) if i == selected else (163, 158, 151)
        img.paste(Image.new("RGB", (70, 56), colour), (cx - 35, cy - 28))
        tabs.append((cx, cy))
    return img, tabs


class ReadingWhichTabIsSelected(unittest.TestCase):

    def test_it_finds_the_warm_highlighted_tab(self):
        """Only the mutually exclusive group (Tasks / Buildings / Players) can BE selected;
        the trailing location pin is an independent toggle, not a tab."""
        for want in range(3):
            with self.subTest(selected=want):
                img, tabs = _strip(selected=want)
                self.assertEqual(selected_tab_index(img, tabs), want)

    def test_the_location_toggle_does_not_masquerade_as_the_selected_tab(self):
        """In port the strip is Tasks | Buildings | Players | location-pin. The first three
        are mutually exclusive; the PIN is independent — it toggles the in-port minimap and
        is lit whenever that toggle is on (user, 2026-08-24). On a live frame the pin was the
        WARMEST icon there (49.4 against the selected house's 46.5), so "warmest wins" picks
        the toggle. Reading only the mutually exclusive group keeps the answer right."""
        img, tabs = _strip(selected=1, n=4)
        cx, cy = tabs[3]                                   # light the location toggle, brighter
        img.paste(Image.new("RGB", (70, 56), (232, 205, 170)), (cx - 35, cy - 28))
        self.assertEqual(selected_tab_index(img, tabs), 1)

    def test_a_lit_toggle_alone_still_names_no_tab(self):
        """If none of the mutually exclusive tabs reads as lit, the answer is "unknown" —
        the independent toggle must never stand in for a tab selection."""
        img, tabs = _strip(selected=None, n=4)
        cx, cy = tabs[3]
        img.paste(Image.new("RGB", (70, 56), (232, 205, 170)), (cx - 35, cy - 28))
        self.assertIsNone(selected_tab_index(img, tabs))

    def test_no_highlight_reads_as_unknown(self):
        """All-grey means the read cannot tell — better than naming one at random."""
        img, tabs = _strip(selected=None)
        self.assertIsNone(selected_tab_index(img, tabs))

    def test_no_tabs_is_unknown_not_a_crash(self):
        img, _tabs = _strip(selected=1)
        self.assertIsNone(selected_tab_index(img, []))


class ATabStripIsARow(unittest.TestCase):

    def test_the_popup_close_x_is_not_a_tab_strip(self):
        """The exact Bordeaux candidates: a close-X at 1915 and one icon 300px away."""
        self.assertEqual(_row_only([_el(1915, 165), _el(2212, 166)]), [])

    def test_a_real_strip_survives(self):
        hits = [_el(1912, 165), _el(2012, 165), _el(2112, 166), _el(2212, 165)]
        self.assertEqual(len(_row_only(hits)), 4)

    def test_uneven_spacing_is_rejected(self):
        self.assertEqual(_row_only([_el(1900, 165), _el(2000, 165), _el(2300, 165)]), [])

    def test_icons_at_different_heights_are_rejected(self):
        """Stacked panel furniture is not a horizontal tab strip."""
        self.assertEqual(_row_only([_el(1912, 165), _el(2012, 300), _el(2112, 440)]), [])


class TheQuestListIsNotABuildingList(unittest.TestCase):

    def test_the_bordeaux_tasks_panel_is_not_a_building_list(self):
        """Verbatim from the live log — note it contains the word 'market'."""
        quest_panel = [(lbl, 2062, 600 + i * 40) for i, lbl in enumerate([
            "4th anniversar", "2admiral chronicle", "complete anonymous",
            "threatening letter request", "from istanbul union", "0/1)",
            "move to market in", "2w left", "proceed with another",
            "relationship chronicle", "new royal order", "palace:",
        ])]
        self.assertFalse(_on_buildings_tab(quest_panel))

    def test_a_real_building_list_passes(self):
        real = [(lbl, 2062, 600 + i * 40)
                for i, lbl in enumerate(["Harbor", "Market", "Shipyard", "Bank", "Inn"])]
        self.assertTrue(_on_buildings_tab(real))


if __name__ == "__main__":
    unittest.main()
