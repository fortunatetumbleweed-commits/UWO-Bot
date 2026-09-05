"""A goods tile is proved by its CATEGORY LABEL, not by an icon box.

LIVE 2026-08-30 at Hutu Village. Three goods were on the Barter panel — Luxuries, Medicine,
Perfume, all Abundant — with six barter rounds unspent and both materials aboard (Raisin
1,580/219, Pig 1,792/219). `_tradable_tiles` required an `icon` element per tile and dropped
any tile without one. OmniParser emitted only TWO icons for those three tiles on a good
capture, and NONE on the capture that mattered, so the row read as empty.

The caller then reported "'Bambara Groundnut' is not on offer today" — a claim about the
village made on the strength of a parse that came back blank — and the mission ended.

`_aim_below_the_banner` already distrusts individual icon boxes ("the row decides, not each
icon... an individual box can come back short when something overlaps it"). A box that is
missing entirely is that same problem one step further on.
"""
import unittest


class _El:
    def __init__(self, label, cx, cy, kind="text", y1=None, y2=None):
        self.label, self.cx, self.cy, self.element_type = label, cx, cy, kind
        self.y1 = y1 if y1 is not None else cy - 70
        self.y2 = y2 if y2 is not None else cy + 70


def _panel(icon_columns):
    """The Hutu row as measured: icons cy≈425, status cy≈510, category cy≈552."""
    els = []
    for cx, cat in ((424, "Luxuries"), (558, "Medicine"), (694, "Perfume")):
        els.append(_El(cat, cx, 552))
        els.append(_El("Abundant", cx, 510))
        if cx in icon_columns:
            els.append(_El("icon", cx, 425, kind="icon", y1=356, y2=497))
    return els


class TheRowDecides(unittest.TestCase):
    def _cats(self, icon_columns):
        from actions.barter_panel import _tradable_tiles
        return [t["category"] for t in _tradable_tiles(_panel(icon_columns))]

    def test_all_three_when_every_icon_is_detected(self):
        self.assertEqual(self._cats({424, 558, 694}), ["Luxuries", "Medicine", "Perfume"])

    def test_the_tile_whose_icon_was_missed_is_still_a_tile(self):
        # Exactly what was measured live: Perfume's icon never came back.
        self.assertEqual(self._cats({424, 558}), ["Luxuries", "Medicine", "Perfume"])

    def test_the_row_still_places_a_tap_for_it(self):
        from actions.barter_panel import _tradable_tiles
        tiles = _tradable_tiles(_panel({424, 558}))
        taps = {t["category"]: t["tap_y"] for t in tiles}
        self.assertEqual(taps["Perfume"], taps["Luxuries"],
                         "the row's median extent places every tile, icon or not")

    def test_no_icons_at_all_still_reads_the_row(self):
        self.assertEqual(self._cats(set()), ["Luxuries", "Medicine", "Perfume"],
                         "the capture that killed the run produced no icons whatsoever")

    def test_a_label_with_nothing_under_it_is_not_a_tile(self):
        from actions.barter_panel import _tradable_tiles
        stray = [_El("Negotiate", 1700, 552)]      # the detail panel, same height
        self.assertEqual(_tradable_tiles(stray), [])


class AnUnreadPanelIsNotAVerdict(unittest.TestCase):
    def test_no_tiles_returns_None_not_False(self):
        from unittest.mock import patch
        from actions import barter_panel
        with patch.object(barter_panel, "_tradable_tiles", return_value=[]), \
             patch("capture.adb_capture.capture_screen", return_value=object()), \
             patch("vision.omniparser.parse_fast_cached", return_value=[]):
            self.assertIsNone(barter_panel._select_trade_good("Bambara Groundnut"),
                              "None means ask again; False would mean the village lacks it")


if __name__ == "__main__":
    unittest.main()


class NoSINGLEPartIsRequired(unittest.TestCase):
    """user, 2026-09-05: "the exact string is not that important, it is an indicator of the
    existence of the tile. so it is better to not fail just because a word is not found".

    The icon, the chip and the label all say the same thing — a good is offered in this
    column — and none of them reads reliably. Requiring any ONE of them specifically is how
    tiles kept disappearing:

        the ICON   — the failure this file was written for (Hutu, 2026-08-30)
        the CHIP's WORD — 'Recommended' 09-04 lost San's Medicine tile, 'Excessive' 09-05
                          lost ARGAN OIL and cost a three-port gather
        the LABEL  — tested here, and the mirror of both

    What is still required is CORROBORATION, because one word at the right height is not a
    tile: the detail panel carries text at the category's height too.
    """

    def _cats(self, els):
        from actions.barter_panel import _tradable_tiles
        return [t["category"] for t in _tradable_tiles(els)]

    def test_a_tile_whose_LABEL_failed_to_read_is_still_a_tile(self):
        els = [_El("icon", 424, 425, kind="icon", y1=356, y2=497), _El("Abundant", 424, 510),
               _El("Medicine", 558, 552), _El("Abundant", 558, 510)]
        self.assertEqual(len(self._cats(els)), 2, "a missing category word removed a good")

    def test_a_thumbnail_alone_is_enough(self):
        """`_is_tile_body` has already checked its band, size and shape."""
        els = [_El("icon", 424, 425, kind="icon", y1=356, y2=497),
               _El("icon", 558, 425, kind="icon", y1=356, y2=497)]
        self.assertEqual(len(self._cats(els)), 2)

    def test_chip_plus_label_is_enough(self):
        """The Hutu capture: three labels, three chips, not one icon."""
        els = [_El("Luxuries", 424, 552), _El("Abundant", 424, 510),
               _El("Medicine", 558, 552), _El("Anything At All", 558, 510)]
        self.assertEqual(self._cats(els), ["Luxuries", "Medicine"])

    def test_a_lone_word_is_still_not_a_tile(self):
        for stray in ("Negotiate", "Check Barter Effect", "Stock Info"):
            with self.subTest(stray):
                self.assertEqual(self._cats([_El(stray, 1700, 552)]), [])

    def test_a_lone_chip_is_not_a_tile_either(self):
        self.assertEqual(self._cats([_El("Abundant", 1700, 510)]), [])
