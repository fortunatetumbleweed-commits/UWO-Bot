"""A blue gem restocks a shelf. It cannot make room in a full hold.

Live 2026-09-04 at Madeira. Raisin sells 217 at a time, so four refresh-and-buy rounds are
correct and were: each followed a purchase that emptied the shelf. Then the hold filled with
Faro's Pig surplus and the fifth buy raised

    "The Cargo Hold's Trade Goods slot will be exceeded by 52 slots. Purchase the Trade Goods?"

The loop read the resulting 0 as a shelf the reader might have missed and spent a gem — at
205 and rising — on a tile that frame 259 shows plainly: Raisin 217, fully active, 103%.
One round later it worked out the hold was full and stopped anyway.

    18:05:04  load Raisin        <- buy attempt, no purchase
    18:05:56  tap refresh        <- the gem this test is about
    18:06:27  load Raisin        <- fails again
    18:07:20  "still 0 after a refresh -> can't load (cargo full)"

The rule (user, 2026-09-04): "blue gem should only be used when the tile is greyed out and
stock is 0."
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from actions.buy_materials import tile_in_stock


def _tile(qty, *, sold_out=False):
    # GREYING IS REPORTED THROUGH `sold_out` — vision.market_reader sets it FROM the greyed
    # tile, so there is no separate flag to set here. "Greyed out and stock is 0" is one
    # state with three tells, and `tile_in_stock` takes any of them.
    return SimpleNamespace(available_qty=qty, sold_out=sold_out, conditional=False)


class WhatCountsAsWorthAGem(unittest.TestCase):
    """`tile_in_stock` is the test the refresh guard now asks. These pin the two answers the
    Madeira round turned on."""

    def test_a_stocked_shelf_is_in_stock(self):
        """Frame 259: Raisin 217, active. Buying 0 here is about room, not stock."""
        self.assertTrue(tile_in_stock(_tile(217)))

    def test_a_zero_shelf_is_not(self):
        """The four legitimate refreshes: 217 bought, tile to 0, restock."""
        self.assertFalse(tile_in_stock(_tile(0)))

    def test_a_greyed_or_sold_out_tile_is_not(self):
        """The greyed tile is what `sold_out` means — one state, three tells."""
        self.assertFalse(tile_in_stock(_tile(None, sold_out=True)))
        self.assertFalse(tile_in_stock(_tile(0, sold_out=True)))

    def test_an_UNREADABLE_quantity_defers_to_the_tile(self):
        """Requiring a positive number would burn a gem on a stocked shelf whose badge
        failed to OCR — which is the same waste from the other direction."""
        self.assertTrue(tile_in_stock(_tile(None)))


class TheGuardIsWiredIntoTheZeroBuyBranch(unittest.TestCase):
    """The branch that spent the gem is `elif got == 0:` — a speculative refresh for a shelf
    the READER might have missed. It now asks first whether the reader can see the shelf and
    whether that shelf is stocked."""

    def test_the_source_checks_stock_before_the_speculative_refresh(self):
        import inspect
        from actions import buy_materials
        src = inspect.getsource(buy_materials.buy_to_goal)
        guard = src.index("stocked = [m for m in buyable")
        speculative = src.index("Maybe the reader just missed a sold-out shelf")
        self.assertLess(guard, speculative,
                        "the stock check must come BEFORE the speculative refresh")

    def test_a_stocked_shelf_reports_cargo_full_rather_than_refreshing(self):
        import inspect
        from actions import buy_materials
        src = inspect.getsource(buy_materials.buy_to_goal)
        tail = src[src.index("stocked = [m for m in buyable"):]
        self.assertIn("cargo_full", tail.split("Maybe the reader")[0],
                      "a stocked shelf with a zero buy is a room problem, and must say so")


if __name__ == "__main__":
    unittest.main()
