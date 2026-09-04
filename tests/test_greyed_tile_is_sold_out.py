"""A sold-out shelf is GREY — read that, not a stamp, and not a transition.

Three things change when a Purchase shelf empties: the artwork desaturates, the quantity
shows 0, and a "Sold Out" stamp appears. The stamp is the least reliable of the three; the
GREYED ART is visually unmistakable (user, 2026-08-24).

It is also a STATE, readable from a single frame. The buy loop previously noticed emptiness
by watching a tile go grey ACROSS a buy, which only catches a shelf emptying while you stand
there — arrive at an already-empty shelf, or leave the building and come back, and there is
no transition left to see. Bordeaux 2026-08-24: Raisin drained by an earlier run, tile grey
with 0 on it, and the loop tapped it anyway because `sold_out` was only ever set from a
restock-TIMER token.

Measured on that frame (thumbnail saturation): Raisin 0.004 against 0.233-0.622 for every
active tile — a ~60x gap, so the threshold is not delicate.
"""

from __future__ import annotations

import types
import unittest

import numpy as np
from PIL import Image

from actions.buy_materials import tile_in_stock
from vision.market_reader import _tile_looks_sold_out


class _Cell:
    def __init__(self, x1, y1):
        self.x1, self.y1 = x1, y1


def _frame_with_tile(rgb):
    """A frame whose tile artwork is painted `rgb`."""
    img = Image.new("RGB", (2400, 1080), (20, 20, 20))
    img.paste(Image.new("RGB", (200, 200), rgb), (100, 100))
    return img


class GreyArtMeansSoldOut(unittest.TestCase):

    def test_a_grey_dark_tile_reads_sold_out(self):
        """Raisin measured sat=0.004, bright=18.5."""
        self.assertTrue(_tile_looks_sold_out(_frame_with_tile((18, 18, 19)), _Cell(100, 100)))

    def test_a_colourful_tile_does_not(self):
        """Wine measured sat=0.245, bright=73.1; Gobelin 0.622 / 114."""
        self.assertFalse(_tile_looks_sold_out(_frame_with_tile((120, 70, 40)), _Cell(100, 100)))
        self.assertFalse(_tile_looks_sold_out(_frame_with_tile((30, 90, 60)), _Cell(100, 100)))

    def test_a_dark_but_colourful_tile_survives(self):
        """Brightness alone must not condemn a tile — Azurite is dark (75.6) and blue."""
        self.assertFalse(_tile_looks_sold_out(_frame_with_tile((10, 20, 70)), _Cell(100, 100)))


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
