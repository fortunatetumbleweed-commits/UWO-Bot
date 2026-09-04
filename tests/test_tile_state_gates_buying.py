"""A shelf is EMPTY as a STATE, not as a transition.

The buy loop used to notice an empty shelf by watching a tile go grey ACROSS a buy. That
only catches a shelf emptying while you stand there. Arrive at an already-empty shelf — or
exit the building and come back — and the transition is long gone while the shelf is still
empty (user, 2026-08-24).

Live at Bordeaux, 2026-08-24: Raisin had been drained by an earlier run. Its tile was greyed,
showed 0, and carried a "Sold Out" stamp — three independent indications — yet the buy gate
tested the `sold_out` FLAG alone, which read False. The loop tapped the tile, nothing staged,
the Purchase button stayed greyed at 0, and no refresh fired. A stocked good is never 0
unless it has been bought.

`tile_in_stock` is the same test the blue-gem refresh already verifies with; the gate asks it
too, so an empty shelf routes to the refresh path instead of being tapped fruitlessly.
"""

from __future__ import annotations

import types
import unittest

from actions.buy_materials import tile_in_stock


def _good(name, avail, sold_out=False):
    return types.SimpleNamespace(name=name, available_qty=avail, sold_out=sold_out)


class TileStateDecidesBuyability(unittest.TestCase):

    def test_the_bordeaux_raisin_tile_is_not_buyable(self):
        """The live tile: quantity unreadable, artwork GREYED.

        `vision.market_reader` sets `sold_out` from the greyed art, so by the time the gate
        sees this good the state is already carried — which is why the flag is True here even
        though the quantity never parsed. See tests/test_greyed_tile_is_sold_out.py.
        """
        self.assertFalse(tile_in_stock(_good("Raisin", None, sold_out=True)))

    def test_a_zero_quantity_is_empty_whatever_the_art_looks_like(self):
        """A good on sale at a port is never 0 unless it has been bought."""
        self.assertFalse(tile_in_stock(_good("Raisin", 0, sold_out=False)))

    def test_an_unreadable_quantity_on_an_active_tile_is_NOT_treated_as_empty(self):
        """UNREADABLE IS NOT ZERO — skipping a stocked shelf wastes a blue gem on it."""
        self.assertTrue(tile_in_stock(_good("Raisin", None, sold_out=False)))

    def test_a_stocked_tile_is_buyable(self):
        self.assertTrue(tile_in_stock(_good("Wine", 322)))
        self.assertTrue(tile_in_stock(_good("Grape", 666)))

    def test_the_gate_and_the_refresh_verifier_ask_the_SAME_question(self):
        """One canonical test: the refresh confirms restock with it, the gate admits with it.
        Two different notions of "in stock" is how a shelf gets refreshed and then skipped,
        or skipped and never refreshed."""
        from actions.buy_materials import _good_in_stock
        import inspect
        self.assertIn("tile_in_stock", inspect.getsource(_good_in_stock))


if __name__ == "__main__":
    unittest.main()
