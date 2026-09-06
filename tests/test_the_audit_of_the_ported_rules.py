"""Rules the refactor nearly dropped, checked against the flow they came from.

Both were found by AUDITING the port rather than by a run failing — the negotiation stall at
Barcelona was the warning that `_react_after_purchase`'s chain held more than one rule, and
these are the two that had no counterpart on the new path.
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

import brain.market_context as ctx
from brain.activities.market import Hold, MarketActivity
from brain.activities.market_buy import _stocked_but_unmoved, on_purchase_page
from brain.dispatcher import UNRECOGNISED, WORKING
from brain.market_state import MarketState


class Tile:
    """A Purchase tile, as the grid reader hands one over."""

    def __init__(self, name, qty, sold_out=False, active=True):
        self.name, self.available_qty = name, qty
        self.sold_out, self.is_active = sold_out, active


def _goods(**kw):
    return {n.lower(): Tile(n, q) for n, q in kw.items()}


class TheOverloadNoticeIsAnswered(unittest.TestCase):
    """"...will be exceeded by N slots. Purchase the trade goods?" — OK.

    A PLAIN ACKNOWLEDGEMENT: the game hands back what fits, it spends no gems, and cancelling
    abandons a purchase the hold has room for. `_react_after_purchase` matched it on BOTH its
    phrases; the new table had no entry at all, so it would have stalled exactly as the
    negotiation did — hand back, dispatcher routes here again, same screen, forever.
    """

    def test_it_has_a_handler_at_all(self):
        self.assertIn(ctx.CARGO_FULL_NOTICE, MarketActivity._HANDLERS)

    def _answer(self, pressed: bool):
        act = MarketActivity(context_fn=lambda f: ctx.CARGO_FULL_NOTICE,
                             capture_fn=lambda: object(), tap_fn=lambda x, y: None,
                             omni_fn=lambda f: [])
        with mock.patch("brain.commit_actions.tap_one_positive",
                        return_value=pressed) as tap:
            res = act.work(Hold(orders={"Iron": 500}), types.SimpleNamespace(
                state="building:market", port="Barcelona", frame=object()))
        return res, tap

    def test_it_is_answered_rather_than_handed_back(self):
        res, tap = self._answer(True)
        self.assertEqual(res.status, WORKING)
        self.assertEqual(tap.call_args.kwargs["goal_keywords"], ["ok"])

    def test_it_acts_on_the_DISPATCHER_S_frame_and_captures_nothing_new(self):
        """Per-frame perception sharing, and more: a handler that re-captures acts on a
        screen the dispatcher never saw."""
        res, tap = self._answer(True)
        self.assertIsNotNone(tap.call_args.kwargs.get("capture_fn"))

    def test_an_unfindable_OK_hands_back_rather_than_tapping_blind(self):
        """A refusal is the safe answer — `game_rules` still gets its turn at the dialog."""
        res, _ = self._answer(False)
        self.assertEqual(res.status, UNRECOGNISED)


class BoughtNothingFromAStockedShelfMeansTheHoldIsFull(unittest.TestCase):
    """The room is the problem, not the shelf (user, 2026-09-04).

    Madeira frame 259: Raisin 217 on the tile, fully active, and the hold full of Pig. The
    old loop read the 0 as a possible sold-out and spent a blue gem at 205 and rising.
    """

    def test_a_shelf_that_did_not_move_while_stocked_names_the_good(self):
        state = MarketState(last_signature=(("raisin", 217),))
        self.assertEqual(_stocked_but_unmoved(state, {"Raisin": 900}, _goods(Raisin=217)),
                         "Raisin")

    def test_a_shelf_that_dropped_is_not_it(self):
        state = MarketState(last_signature=(("raisin", 217),))
        self.assertIsNone(_stocked_but_unmoved(state, {"Raisin": 900}, _goods(Raisin=100)))

    def test_UNREAD_IS_NOT_UNMOVED(self):
        """A missing reading yields no claim — the rule `credit_the_shelf_drop` follows, and
        the reason this cannot fire on a bad parse."""
        state = MarketState(last_signature=(("raisin", -1),))
        self.assertIsNone(_stocked_but_unmoved(state, {"Raisin": 900}, _goods(Raisin=217)))

    def test_a_sold_out_shelf_is_a_STOCK_problem_and_not_this_one(self):
        state = MarketState(last_signature=(("raisin", 0),))
        goods = {"raisin": Tile("Raisin", 0, sold_out=True, active=False)}
        self.assertIsNone(_stocked_but_unmoved(state, {"Raisin": 900}, goods))

    def test_the_buy_round_STOPS_rather_than_buying_or_refreshing_again(self):
        state = MarketState(last_intent="tapped Purchase", last_signature=(("raisin", 217),))
        with mock.patch("vision.market_reader.read_market_page_omni",
                        return_value=[Tile("Raisin", 217)]), \
             mock.patch("actions.buy_materials._find_purchase_commit", return_value=None):
            out = on_purchase_page(
                state, Hold(orders={"Raisin": 900}), "Madeira", frame=object(),
                capture_fn=lambda: object(), omni_fn=lambda f: [],
                tap_fn=lambda x, y: self.fail(f"tapped {x},{y} with a full hold"))
        self.assertEqual(out["do"], "finished")
        self.assertTrue(out.get("cargo_full"))


if __name__ == "__main__":
    unittest.main()
