"""The chromed title names the sub-menu that is open.

Paradigm (user, 2026-08-22): selecting a sub-menu item highlights it AND makes it the
screen's title — "if Purchase is highlighted, the title is Purchase, when Supply is
highlighted, the title is Supply". True of every chromed screen.

Verified against the corpus: all 8 frames labelled `sub_menu` read TITLE='Recruit Crew'
against a menu of Supply / Repair / Recruit Crew, and run 24's market pages read 'Sell'.

This is the answer to "which sub-screen am I on?" — a question the bot had been answering
by tapping, which is how it tapped the title (a BACK control) on a page it had reached.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from PIL import Image

from actions.ui import active_submenu, on_submenu
from vision.omniparser import DetectedElement

FRAME = Image.new("RGB", (2400, 1080))


def _el(label, cx, cy, w=140, h=40):
    return DetectedElement(label=label, element_type="text",
                           x1=cx - w // 2, y1=cy - h // 2,
                           x2=cx + w // 2, y2=cy + h // 2, confidence=0.9)


def _with(elements, fn, *a):
    with patch("vision.omniparser.parse_fast_cached", return_value=elements):
        return fn(*a, FRAME) if a else fn(FRAME)


# Measured on frame_0017 of trace_barter_cmd_2026-08-22T21-21-44.
TITLE = _el("Sell", 107, 53)
MENU_PURCHASE = _el("Purchase", 96, 160)
MENU_SELL = _el("Sell", 65, 275)


class TitleNamesTheOpenSubmenu(unittest.TestCase):

    def test_it_reads_the_title(self):
        self.assertEqual(_with([TITLE, MENU_PURCHASE, MENU_SELL], active_submenu), "Sell")

    def test_menu_items_below_the_title_are_not_mistaken_for_it(self):
        """Purchase is on screen too — it is just not the one that is open."""
        self.assertNotEqual(_with([TITLE, MENU_PURCHASE, MENU_SELL], active_submenu),
                            "Purchase")

    def test_a_multi_word_title_survives(self):
        """The sub_menu corpus reads 'Recruit Crew', not a stray 'Crew'."""
        els = [_el("Recruit Crew", 150, 55), _el("Crew", 90, 60, w=60)]
        self.assertEqual(_with(els, active_submenu), "Recruit Crew")

    def test_unreadable_is_None_not_a_guess(self):
        """None must mean "could not tell" — treating it as "not here" would navigate away
        from a screen already reached."""
        self.assertIsNone(_with([], active_submenu))

    def test_on_submenu_matches_case_insensitively(self):
        self.assertTrue(_with([TITLE], on_submenu, "sell"))

    def test_on_submenu_is_false_for_another_screen(self):
        self.assertFalse(_with([_el("Supply", 120, 55)], on_submenu, "sell"))

    def test_an_unreadable_title_is_not_a_confirmation(self):
        self.assertFalse(_with([], on_submenu, "sell"))


if __name__ == "__main__":
    unittest.main()
