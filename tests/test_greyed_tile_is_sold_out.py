"""A sold-out shelf is GREY — and it is the whole CARD that greys, not just the artwork.

Three things change when a Purchase shelf empties: the tile greys, the quantity shows 0, and
a "Sold Out" stamp appears. The greying is the most prominent (user, 2026-08-24, and again
2026-09-07: "the whole tile is greyed out, that should be the most prominent determining
factor").

WHICH PART OF THE TILE decides it. This measured the ARTWORK, which is the good's own
picture, so no absolute threshold can hold across goods: dark brown Ebony reads
sat 0.28 / brightness 21 when SOLD OUT, while Rosewood reads 0.45 / 54 while perfectly in
stock. Live 2026-09-07 at Ambon the Ebony tile was greyed, stamped, and badged 0, and the
buy loop tapped it twice, found the cart empty, concluded "the tile is not taking taps" and
failed the mission.

The CARD BODY is chrome with a fixed palette. Over two frames and twelve tiles:

    live tiles          197.1 .. 203.8   (including both GATED tiles, which are not sold out)
    Palm Oil sold out    99.6
    Ebony sold out      107.8

It is also a STATE, readable from a single frame. The buy loop previously noticed emptiness
by watching a tile go grey ACROSS a buy, which only catches a shelf emptying while you stand
there — arrive at an already-empty shelf, or leave the building and come back, and there is
no transition left to see. Bordeaux 2026-08-24: Raisin drained by an earlier run, tile grey
with 0 on it, and the loop tapped it anyway because `sold_out` was only ever set from a
restock-TIMER token.

~90 points of clear air either side of the boundary, against 33 points of overlap on the
artwork.
"""

from __future__ import annotations

import types
import unittest

import numpy as np
from PIL import Image

from actions.buy_materials import tile_in_stock
from vision.market_reader import _tile_looks_sold_out


_W, _H = 436, 240                       # a real Purchase tile, measured on two live frames


class _Cell:
    def __init__(self, x1, y1, w=_W, h=_H):
        self.x1, self.y1 = x1, y1
        self.x2, self.y2 = x1 + w, y1 + h


def _frame_with_card(body_rgb, art_rgb=(120, 70, 40)):
    """A tile whose CARD BODY is `body_rgb` and whose artwork is `art_rgb`.

    The two are painted separately so a test can prove which one decides.
    """
    img = Image.new("RGB", (2400, 1080), (20, 20, 20))
    img.paste(Image.new("RGB", (_W, _H), body_rgb), (100, 100))
    img.paste(Image.new("RGB", (110, 100), art_rgb), (114, 118))
    return img


class TheCARDDecides(unittest.TestCase):

    def test_a_greyed_card_reads_sold_out(self):
        """Palm Oil measured 99.6, Ebony 107.8."""
        self.assertTrue(_tile_looks_sold_out(_frame_with_card((104, 104, 104)), _Cell(100, 100)))

    def test_a_cream_card_does_not(self):
        """Live tiles measured 197.1-203.8."""
        self.assertFalse(_tile_looks_sold_out(_frame_with_card((200, 199, 197)), _Cell(100, 100)))

    def test_DARK_ARTWORK_ON_A_LIVE_CARD_IS_STILL_IN_STOCK(self):
        """The whole point. Ebony in stock is near-black art on a cream card, and the old
        artwork test would have condemned it."""
        self.assertFalse(_tile_looks_sold_out(
            _frame_with_card((200, 199, 197), art_rgb=(18, 14, 10)), _Cell(100, 100)))

    def test_BRIGHT_ARTWORK_ON_A_GREY_CARD_IS_SOLD_OUT(self):
        """And the converse — the artwork must not rescue a dead card."""
        self.assertTrue(_tile_looks_sold_out(
            _frame_with_card((104, 104, 104), art_rgb=(240, 200, 60)), _Cell(100, 100)))

    def test_a_cell_with_no_size_is_refused_rather_than_guessed(self):
        class _Degenerate:
            x1 = y1 = x2 = y2 = 0
        self.assertFalse(_tile_looks_sold_out(_frame_with_card((104, 104, 104)), _Degenerate()))


class UnreadableQuantityDefersToTheVisualState(unittest.TestCase):

    def _g(self, avail, sold_out=False):
        return types.SimpleNamespace(name="X", available_qty=avail, sold_out=sold_out)

    def test_greyed_wins_even_with_no_quantity(self):
        """The live Bordeaux Raisin: quantity unreadable, art grey."""
        self.assertFalse(tile_in_stock(self._g(None, sold_out=True)))

    def test_unreadable_quantity_on_an_ACTIVE_tile_is_still_buyable(self):
        """UNREADABLE IS NOT ZERO. Skipping here would pass over a stocked shelf and burn a
        blue gem refreshing something that was never empty."""
        self.assertTrue(tile_in_stock(self._g(None, sold_out=False)))

    def test_a_readable_zero_still_settles_it(self):
        """A good on sale is never 0 unless it has been bought."""
        self.assertFalse(tile_in_stock(self._g(0, sold_out=False)))

    def test_a_stocked_tile_is_buyable(self):
        self.assertTrue(tile_in_stock(self._g(322)))


if __name__ == "__main__":
    unittest.main()
