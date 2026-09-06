"""Buying, one action per tick.

The cart's state is READ, never remembered. A price appears beside `Purchase` only when
something is staged — measured on trace_barter_cmd_2026-09-05T21-02-24:

    frame 64 (empty cart)   ['Purchase']
    frame 68 (110 staged)   ['63,360', 'Purchase']

That matters because A SECOND TAP ON A STAGED TILE UN-STAGES IT. "No commit button" is
ambiguous — a swallowed tap, or a detector that missed one — and `purchase_goods` answered
that in August by refusing to retry at all, which is safe but turns a swallowed tap into a
failed leg. A cost of ZERO is unambiguous, so a tick can do better than the refusal.
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

from brain.activities.market_buy import (credit_the_shelf_drop, on_purchase_page,
                                         shelf_signature)
from brain.market_state import MarketState
from brain.market_ledger import MarketLedger


def _tile(name, qty=500):
    return types.SimpleNamespace(name=name, available_qty=qty, sold_out=(qty == 0),
                                 conditional=False)


class _Goal:
    def __init__(self, orders):
        self.orders = orders


def _run(state, goal, rows, *, cost=0, taps=None, refresh=None):
    taps = [] if taps is None else taps
    commit = types.SimpleNamespace(cx=1959, cy=997, verb="purchase", cost=str(cost))
    with mock.patch("vision.market_reader.read_market_page_omni", return_value=rows), \
         mock.patch("actions.buy_materials._find_purchase_commit", return_value=commit), \
         mock.patch("actions.buy_materials._find_material_tile",
                    side_effect=lambda els, m: (500, 400)), \
         mock.patch("actions.buy_materials.refresh_market",
                    return_value=refresh or {"ok": True}):
        out = on_purchase_page(state, goal, "Madeira", frame=object(),
                               capture_fn=lambda: object(),
                               tap_fn=lambda x, y: taps.append((x, y)),
                               omni_fn=lambda f: [])
    return out, taps


def _state():
    s = MarketState()
    s.ledger = MarketLedger()
    return s


class TheCartIsReadNotRemembered(unittest.TestCase):

    def test_a_price_means_the_cart_is_loaded(self):
        out, taps = _run(_state(), _Goal({"Raisin": 1000}), [_tile("Raisin")], cost=63360)
        self.assertEqual(out["do"], "committed")
        self.assertEqual(taps, [(1959, 997)], "the Purchase button, not a tile")

    def test_no_price_means_stage(self):
        out, taps = _run(_state(), _Goal({"Raisin": 1000}), [_tile("Raisin")], cost=0)
        self.assertEqual(out["do"], "staged")
        self.assertEqual(taps, [(500, 400)])

    def test_there_is_still_no_staged_flag(self):
        self.assertNotIn("staged", set(MarketState().__dataclass_fields__))


class ASwallowedStageIsRetriedBecauseZeroIsUnambiguous(unittest.TestCase):

    def test_an_unchanged_shelf_and_an_empty_cart_is_a_swallowed_tap(self):
        s, goal, rows = _state(), _Goal({"Raisin": 1000}), [_tile("Raisin")]
        _run(s, goal, rows, cost=0)
        out, taps = _run(s, goal, rows, cost=0)
        self.assertEqual(out["do"], "staged", "the tap never landed — stage again")

    def test_a_tile_that_never_takes_a_tap_is_reported(self):
        s, goal, rows = _state(), _Goal({"Raisin": 1000}), [_tile("Raisin")]
        for _ in range(3):
            out, _ = _run(s, goal, rows, cost=0)
        self.assertEqual(out["do"], "blocked")
        self.assertIn("not taking taps", out["why"])


class TheShelfSaysWhatWasBought(unittest.TestCase):
    """The result card reports MONEY. Units come from the shelf's drop."""

    def test_the_drop_is_credited(self):
        s = _state()
        before = shelf_signature({"raisin": _tile("raisin", 500)})
        credited = credit_the_shelf_drop(s, before, {"raisin": _tile("raisin", 390)})
        self.assertEqual(credited, [("raisin", 110)])
        self.assertEqual(s.ledger.believed("raisin"), 110)

    def test_an_unread_shelf_makes_no_claim(self):
        s = _state()
        before = shelf_signature({"raisin": _tile("raisin", None)})
        self.assertEqual(credit_the_shelf_drop(s, before, {"raisin": _tile("raisin", 390)}), [])
        self.assertEqual(s.ledger.believed("raisin"), 0)

    def test_a_restock_is_not_a_negative_purchase(self):
        s = _state()
        before = shelf_signature({"raisin": _tile("raisin", 100)})
        self.assertEqual(credit_the_shelf_drop(s, before, {"raisin": _tile("raisin", 500)}), [])


class AGemOnlyBuysWhatThisPortStillOwes(unittest.TestCase):

    def test_faro_does_not_refresh_for_a_good_it_does_not_sell(self):
        """Pig met at this port, Raisin short but not stocked here."""
        s = _state()
        s.ledger.seed({"pig": 1828})
        out, _ = _run(s, _Goal({"Pig": 1719, "Raisin": 1719}), [_tile("Pig", 0)], cost=0)
        self.assertEqual(out["do"], "finished")
        self.assertIn("nothing here is still wanted", out["why"])

    def test_barcelona_refreshes_while_a_stocked_good_is_short(self):
        """Iron met, Matchlock short — and BOTH are sold here, so the market-wide restock
        is exactly right."""
        s = _state()
        s.ledger.seed({"iron": 800})
        out, _ = _run(s, _Goal({"Iron": 709, "Matchlock Gun": 355}),
                      [_tile("Iron", 0), _tile("Matchlock Gun", 0)], cost=0)
        self.assertEqual(out["do"], "refreshed")


class AnUnreadableGridIsNotAnEmptyShop(unittest.TestCase):
    def test_it_waits_rather_than_concluding(self):
        out, taps = _run(_state(), _Goal({"Raisin": 1000}), [], cost=0)
        self.assertEqual(out["do"], "waited")
        self.assertEqual(taps, [])
