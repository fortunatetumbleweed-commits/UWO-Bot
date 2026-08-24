"""The chromed title is a BACK control, so a label search must not return it.

On every chromed screen the title bar navigates back (the Android paradigm, user
2026-08-22). The title word is usually the same word the caller wants — the Sell page is
titled "Sell" — so the collision happens exactly when the bot has ALREADY reached the
screen, and the tap leaves it.

Live 2026-08-22, run 24: `_find_button(frame, "sell")` returned the title at (107,53) on a
Sell page already showing Ebony 700 / Coral 797. The bot left the market, the hold came
back unknown, and the mission planned zero rounds.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from PIL import Image

from vision.omniparser import DetectedElement

FRAME = Image.new("RGB", (2400, 1080))

# Measured on frame_0017 of trace_barter_cmd_2026-08-22T21-21-44.
TITLE_SELL = (107, 53)          # "< Sell" — the back control
MENU_SELL = (65, 275)           # the left sub-menu's Sell item — the real tab switch


def _el(label, cx, cy, w=120, h=40):
    return DetectedElement(label=label, element_type="text",
                           x1=cx - w // 2, y1=cy - h // 2,
                           x2=cx + w // 2, y2=cy + h // 2, confidence=0.9)


def _find(elements, *labels, **kw):
    from actions import sail_actions
    class _Parser:
        def yolo_available(self): return True
    with patch("vision.omniparser.get_omniparser", return_value=_Parser()), \
         patch("vision.omniparser.parse_fast_cached", return_value=elements):
        return sail_actions._find_button(FRAME, *labels, **kw)


class TitleIsNotAButton(unittest.TestCase):

    def test_the_title_is_not_returned(self):
        self.assertIsNone(_find([_el("Sell", *TITLE_SELL)], "sell"))

    def test_the_menu_item_wins_over_the_title(self):
        """Both are labelled "Sell"; only one switches tabs."""
        found = _find([_el("Sell", *TITLE_SELL), _el("Sell", *MENU_SELL)], "sell")
        self.assertEqual(found, MENU_SELL)

    def test_the_title_is_skipped_whatever_the_word(self):
        """Purchase pages are titled "Purchase", Market screens "Market"."""
        self.assertIsNone(_find([_el("Purchase", 120, 55)], "purchase"))

    def test_allow_title_opts_back_in(self):
        """A caller that genuinely wants the BACK control can ask for it."""
        self.assertEqual(_find([_el("Sell", *TITLE_SELL)], "sell", allow_title=True),
                         TITLE_SELL)

    def test_a_dialog_close_x_is_still_findable(self):
        """Dialog close buttons sit at the dialog's top-RIGHT, not the screen corner —
        excluding the whole top strip would have broken them."""
        self.assertEqual(_find([_el("X", 1668, 121)], "x"), (1668, 121))

    def test_buttons_below_the_corner_are_unaffected(self):
        self.assertEqual(_find([_el("Confirm", 1958, 997)], "confirm"), (1958, 997))


if __name__ == "__main__":
    unittest.main()
