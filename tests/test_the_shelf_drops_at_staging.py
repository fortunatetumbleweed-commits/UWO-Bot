"""The tile loses its units when the cart is LOADED, not when Purchase is tapped.

So a baseline read on the tick that taps Purchase is one tick too late: the units it exists
to count have already left the tile, and the drop measured across the purchase is zero.

Live 2026-09-10 at Gijón. Pig read 308, the tile was tapped, the next tick read 205 and took
THAT as the baseline; the confirm card said "103 Pig" and the result card confirmed the buy;
the drop came out 205 -> 205 = 0. The cargo-total fallback was blind at the same moment for
an unrelated reason — the hold was already at 4,952/4,952, which is what the overflow notice
("exceed capacity by 103 slots") means, and a counter at its cap cannot rise. Two readings,
both legitimately zero, so the ledger learned nothing: Pig read `have: 0` against a want of
1,254 and the mission chose its route on that zero.

The baseline must therefore span STAGE -> PURCHASE, not just the purchase.
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

from brain.activities.market import Hold
from brain.activities.market_buy import on_purchase_page
from brain.market_ledger import MarketLedger
from brain.market_state import MarketState


class Tile:
    def __init__(self, name, qty, active=True):
        self.name, self.available_qty = name, qty
        self.sold_out, self.is_active = (qty == 0), active
        self.season = None


def _tick(state, tiles, *, cart_cost, taps):
    """One turn of the purchase grid. `cart_cost` 0 means the cart is empty."""
    commit = types.SimpleNamespace(cx=100, cy=200)
    goal = Hold(orders={"Pig": 1254})
    with mock.patch("vision.market_reader.read_market_page_omni", return_value=tiles), \
         mock.patch("actions.buy_materials._find_purchase_commit",
                    return_value=commit if cart_cost else None), \
         mock.patch("actions.buy_materials._cost_of", return_value=cart_cost), \
         mock.patch("actions.buy_materials._find_material_tile", return_value=(1008, 314)), \
         mock.patch("actions.market_actions._is_bulk_mode_on", return_value=True), \
         mock.patch("memory.market_kb.note_season"), \
         mock.patch("vision.region_detectors.market_restock.find_restock_button",
                    return_value=None):
        return on_purchase_page(state, goal, "Gijón", frame=object(),
                                capture_fn=lambda: object(),
                                tap_fn=lambda *a: taps.append(a),
                                omni_fn=lambda _f: [],
                                set_bulk_fn=lambda *a: None)


class TheBaselineSpansStagingAndPurchase(unittest.TestCase):

    def _state(self):
        st = MarketState(ledger=MarketLedger())
        st.ledger.seed({"pig": 0})
        return st

    def test_GIJON_the_103_pig_are_credited(self):
        """The live sequence, tick for tick. Nothing else changed: the hold is full, so the
        cargo-total fallback cannot help and only the shelf can answer."""
        st, taps = self._state(), []

        # 09:05:08 — the tile still holds all 308, and the cart is empty.
        self._assert_did(_tick(st, [Tile("Pig", 308)], cart_cost=0, taps=taps), "staged")
        self.assertEqual(taps, [(1008, 314)])

        # 09:05:27 — staging took 103 off the tile. THIS is the reading that used to become
        # the baseline, and it is already too late.
        self._assert_did(_tick(st, [Tile("Pig", 205)], cart_cost=26471, taps=taps),
                         "committed")

        # 09:07:14 — back on the grid after the confirm, overflow and result cards.
        _tick(st, [Tile("Pig", 205)], cart_cost=0, taps=taps)
        self.assertEqual(st.ledger.believed("Pig"), 103)

    def test_the_baseline_is_taken_before_the_tile_is_tapped(self):
        st, taps = self._state(), []
        _tick(st, [Tile("Pig", 308)], cart_cost=0, taps=taps)
        self.assertEqual(st.shelf_before_cart, (("pig", 308),))

    def test_A_RESTAGE_DOES_NOT_OVERWRITE_IT(self):
        """A tap that did not land makes the activity stage again. That second look is at a
        shelf which may already have moved, and it must not replace the real baseline."""
        st, taps = self._state(), []
        _tick(st, [Tile("Pig", 308)], cart_cost=0, taps=taps)
        _tick(st, [Tile("Pig", 205)], cart_cost=0, taps=taps)
        self.assertEqual(st.shelf_before_cart, (("pig", 308),))

    def test_the_slot_is_spent_by_the_purchase(self):
        """One cart, one baseline — it must not leak into the next purchase at this port."""
        st, taps = self._state(), []
        _tick(st, [Tile("Pig", 308)], cart_cost=0, taps=taps)
        _tick(st, [Tile("Pig", 205)], cart_cost=26471, taps=taps)
        self.assertIsNone(st.shelf_before_cart)
        self.assertEqual(st.awaiting_credit[0], (("pig", 308),))

    def _assert_did(self, result, expected):
        self.assertEqual(result.get("do"), expected, result)


if __name__ == "__main__":
    unittest.main()
