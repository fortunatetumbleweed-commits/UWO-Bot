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

from PIL import Image, ImageDraw

from actions.ui import active_submenu, on_submenu
from vision.omniparser import DetectedElement

FRAME = Image.new("RGB", (2400, 1080))


def _el(label, cx, cy, w=140, h=40):
    return DetectedElement(label=label, element_type="text",
                           x1=cx - w // 2, y1=cy - h // 2,
                           x2=cx + w // 2, y2=cy + h // 2, confidence=0.9)


def _frame(elements, glyph=255):
    """A frame whose title glyphs are LIT, as a real screen's are.

    The fixture used to be pure black, which was harmless while the title was only READ.
    Now that `on_submenu` also asks whether the screen is DIMMED, black means "a dialog is
    covering this" — so the fixture has to say which case it stands for. `glyph` is the value
    the game itself produces: 255 for a screen you can act on, ~128 under one dialog.
    """
    img = Image.new("RGB", (2400, 1080))
    d = ImageDraw.Draw(img)
    for e in elements:
        d.rectangle([e.x1, e.y1, e.x2, e.y2], fill=(glyph, glyph, glyph))
    return img


def _with(elements, fn, *a, glyph=255):
    frame = _frame(elements, glyph)
    with patch("vision.omniparser.parse_fast_cached", return_value=elements):
        return fn(*a, frame) if a else fn(frame)


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


class ADimmedTitleIsNotAPlaceYouCanAct(unittest.TestCase):
    """THE TITLE SAYS WHICH SCREEN; ITS BRIGHTNESS SAYS WHETHER YOU CAN ACT ON IT (user,
    2026-09-03).

    At San Village a `Notice` was drawn over the Barter screen, whose chromed title still read
    'Barter'. `_open_barter_panel` opens with "if on_submenu('barter'): return True — already
    there, do not tap again", so it returned True without tapping and without logging, every
    tick, while the dispatcher logged 'opened the barter panel' three times for doing nothing.
    The stall guard ended the mission with the dialog still on screen.

    The game composites a flat 50% scrim over what a dialog covers, and the chromed title is
    behind it: measured 253-255 undimmed, 140-146 under one dialog, 32-64 when they stack."""

    def test_the_reading_stays_true_under_a_dialog(self):
        """`active_submenu` is not what was wrong. Barter really IS the open submenu, and a
        caller that wants to know which screen is open must still be told."""
        self.assertEqual(_with([TITLE], active_submenu, glyph=128), "Sell")

    def test_but_it_is_not_somewhere_you_can_act(self):
        self.assertFalse(_with([TITLE], on_submenu, "sell", glyph=128),
                         "answered 'yes, you are here' through a modal")

    def test_an_undimmed_title_still_says_yes(self):
        self.assertTrue(_with([TITLE], on_submenu, "sell", glyph=255))

    def test_stacked_dialogs_are_dimmer_still(self):
        from actions.ui import chrome_is_dimmed
        self.assertTrue(_with([TITLE], chrome_is_dimmed, glyph=64))

    def test_unreadable_is_None_and_not_False(self):
        """"Could not tell" must never be delivered as "nothing in the way" — the same rule
        `active_submenu` follows by returning None rather than ""."""
        from actions.ui import chrome_is_dimmed
        self.assertIsNone(_with([], chrome_is_dimmed))
