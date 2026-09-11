"""The San Village barter tiles the bot tapped 40 times to no effect (2026-09-04).

The panel offered TWO goods. The bot found one, aimed at (424,509), and looped:

    [mission.barter] 1 tile(s) never answered on the first pass
    [ui] tap CALIBRATED (424,509) — barter → select the Luxuries good
    Barter panel: amity=Friendly(98849,100000) good=None out=None materials=[]

User, watching: "it indeed tapped but there was no response" and "only tapping at the icon
works, not on the text".

MEASURED ON THE FRAME. A tile is three stacked controls, and only the top one selects:

    thumbnail (icon + stock qty)   y 361-486     <- the only part that selects
    stock chip  'Abundant'         y 491-527
    category chip 'Luxuries'       y 532-570

y=509 is the STOCK CHIP. Two independent faults put it there:

  1. the thumbnail came back from OmniParser as a `button` carrying the stock quantity
     ('91' — a misread of 11), not as an `icon`, so the icon-typed search found nothing and
     the anchor fell through to the chip below;
  2. 'Recommended' was missing from the status vocabulary, so the Medicine tile — which had
     no icon element either — was dropped entirely rather than merely mis-aimed.
"""
from __future__ import annotations

import types
import unittest

from actions.barter_panel import _tradable_tiles

THUMBNAIL = (361, 486)
STOCK_CHIP = (491, 527)
CATEGORY_CHIP = (532, 570)


def _el(kind, label, x1, y1, x2, y2):
    return types.SimpleNamespace(element_type=kind, label=label, x1=x1, y1=y1, x2=x2, y2=y2,
                                 cx=(x1 + x2) // 2, cy=(y1 + y2) // 2)


def _san_village():
    """OmniParser's actual output for the two-tile San panel. Note both thumbnails are
    BUTTONS labelled with their stock quantity, and neither is an `icon`."""
    return [
        _el("text", "Tradable Trade Goods", 365, 303, 683, 339),
        _el("button", "Loot", 0, 334, 359, 457),             # left menu, same band
        _el("button", "Recruit Crew", 0, 447, 358, 567),
        _el("button", "91", 357, 356, 490, 495),             # tile 1 thumbnail
        _el("button", "10", 490, 355, 626, 495),             # tile 2 thumbnail
        _el("button", "Abundant", 359, 485, 487, 534),
        _el("icon", "icon", 493, 488, 623, 533),             # tile 2's chip, unlabelled
        _el("button", "Luxuries", 358, 529, 490, 576),
        _el("button", "Medicine", 493, 528, 626, 576),
    ]


class BothTilesAreFound(unittest.TestCase):

    def test_the_panel_offers_two_goods_and_both_are_seen(self):
        """It reported one. A tile the strip never returns is a good the mission cannot
        try, and San offered exactly two."""
        tiles = _tradable_tiles(_san_village())
        self.assertEqual([t["category"] for t in tiles], ["Luxuries", "Medicine"])

    def test_a_thumbnail_that_is_not_an_icon_element_still_counts(self):
        """The whole failure in one assertion: both thumbnails are `button`s here."""
        tiles = _tradable_tiles(_san_village())
        self.assertEqual(len(tiles), 2)

    def test_the_left_menu_is_not_mistaken_for_a_tile(self):
        """Widening the search from `icon` to any thumbnail-sized box lets 'Loot' (y 334-457,
        squarely in the band) apply. It is rejected on COLUMN, not on type."""
        cats = [t["category"] for t in _tradable_tiles(_san_village())]
        self.assertNotIn("Loot", cats)
        self.assertNotIn("Recruit Crew", cats)


class TheTapLandsOnTheThumbnail(unittest.TestCase):

    def _aim(self):
        return {t["category"]: t["tap_y"] for t in _tradable_tiles(_san_village())}

    def test_it_does_not_aim_at_the_stock_chip(self):
        """(424,509) — the exact dead tap, forty times over."""
        for cat, y in self._aim().items():
            with self.subTest(cat):
                self.assertFalse(STOCK_CHIP[0] <= y <= STOCK_CHIP[1],
                                 f"{cat} aims at the stock chip again")

    def test_it_does_not_aim_at_the_category_chip(self):
        for cat, y in self._aim().items():
            with self.subTest(cat):
                self.assertFalse(CATEGORY_CHIP[0] <= y <= CATEGORY_CHIP[1])

    def test_it_aims_INSIDE_the_thumbnail(self):
        for cat, y in self._aim().items():
            with self.subTest(cat):
                self.assertTrue(THUMBNAIL[0] <= y <= THUMBNAIL[1],
                                f"{cat} aims at {y}, outside the thumbnail {THUMBNAIL}")

    def test_it_still_clears_the_LOCK_BANNER(self):
        """The other bound, from Svear 2026-08-26: a locked good draws a red banner across
        the MIDDLE of its thumbnail — y 420-460 of one spanning 366-514, i.e. 36%-63% of the
        height. Tapping it selects nothing and raises an info tip, which then sits over the
        strip and swallows the next tap. That is how Birch Tree was missed twice.

        Aiming higher to dodge the chips below must not walk back into it, so the bound is
        checked proportionally and therefore holds whatever size the tile is drawn at."""
        for t in _tradable_tiles(_san_village()):
            with self.subTest(t["category"]):
                top, bottom = t["y1"], t["y2"]
                banner_ends = top + (bottom - top) * 0.63
                self.assertGreater(t["tap_y"], banner_ends, "back inside the lock banner")
                self.assertLess(t["tap_y"], bottom, "below the thumbnail entirely")


class TheStatusVocabulary(unittest.TestCase):

    def test_recommended_is_a_status(self):
        """'Recommended' was absent, and a tile carrying it with no icon element was dropped
        rather than mis-aimed — which is how a two-good panel read as one."""
        els = [_el("button", "10", 490, 355, 626, 495),
               _el("button", "Recommended", 493, 488, 623, 533),
               _el("button", "Medicine", 493, 528, 626, 576)]
        tiles = _tradable_tiles(els)
        self.assertEqual(len(tiles), 1)
        self.assertEqual(tiles[0]["status"], "Recommended")


if __name__ == "__main__":
    unittest.main()
