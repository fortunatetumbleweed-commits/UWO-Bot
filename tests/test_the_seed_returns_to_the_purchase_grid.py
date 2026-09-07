"""Reading the hold is a TICK, not a side trip inside one.

The hold is only legible on the Sell grid, so the buy round has to go there — and it used to
go and come back INSIDE a single tick: switch tabs, scroll a whole grid, switch back. That is
the sub-loop shape the market refactor removes, and it did real damage twice.

    the live screen no longer matched the frame the tick was reasoning about, so
    `refresh_market`'s own capture found the Sell page and refused: "no restock control
    (market fresh or not on Purchase grid)" — with the ↻ sitting there at 00:15:07 and 11
    blue gems (Faro, frame 69 of trace_barter_cmd_2026-09-06T20-48-41)

    and every failed port-name read rebuilt the state, so it ran AGAIN — twelve times for
    eleven purchases at Madeira, each one a full grid scroll

As ticks it is three plain steps, and nothing acts on a screen the dispatcher has not seen:

    purchase page, hold unread  ->  ask for the Sell tab, hand back
    sell page                   ->  read the hold HERE, seed, ask for Purchase, hand back
    purchase page, hold known   ->  buy
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

import brain.market_context as ctx
from brain.activities.market import Hold, MarketActivity
from brain.dispatcher import UNRECOGNISED, WORKING
from brain.market_ledger import MarketLedger


def _act(context, *, shown=None, sell_tab=True):
    act = MarketActivity(context_fn=lambda _f: context,
                         capture_fn=lambda: object(), tap_fn=lambda *a: None,
                         omni_fn=lambda _f: [],
                         show_grid_fn=(lambda: shown.append("purchase")) if shown is not None
                         else None)
    act._sell_tab_ok = sell_tab
    return act


def _state():
    return types.SimpleNamespace(state="building:market", port="Faro", frame=object())


GOAL = Hold(orders={"Pig": 900})


class TheBuyTickAsksForTheSellTab(unittest.TestCase):
    """It does not go and fetch the hold itself."""

    def test_an_unread_hold_sends_us_to_the_sell_tab(self):
        act = _act(ctx.PURCHASE_PAGE)
        with mock.patch("actions.buy_materials.ensure_sell_tab", return_value=True) as tab, \
             mock.patch.object(MarketActivity, "_port_name", return_value="Faro"):
            res = act.work(GOAL, _state())
        tab.assert_called_once()
        self.assertEqual(res.observed["did"], "went to read the hold")

    def test_a_sell_tab_that_will_not_open_hands_back(self):
        """Never buy against a count we do not have."""
        act = _act(ctx.PURCHASE_PAGE)
        with mock.patch("actions.buy_materials.ensure_sell_tab", return_value=False), \
             mock.patch.object(MarketActivity, "_port_name", return_value="Faro"):
            res = act.work(GOAL, _state())
        self.assertEqual(res.status, UNRECOGNISED)

    def test_once_the_hold_is_known_it_just_buys(self):
        act = _act(ctx.PURCHASE_PAGE)
        with mock.patch("actions.buy_materials.ensure_sell_tab", return_value=True), \
             mock.patch.object(MarketActivity, "_port_name", return_value="Faro"):
            act.work(GOAL, _state())          # the tick that goes to read the hold
        act._state.ledger = MarketLedger()    # ... which the sell page would then seed

        with mock.patch("actions.buy_materials.ensure_sell_tab") as tab, \
             mock.patch("brain.activities.market_buy.on_purchase_page",
                        return_value={"do": "waited"}), \
             mock.patch.object(MarketActivity, "_port_name", return_value="Faro"):
            act.work(GOAL, _state())
        tab.assert_not_called()


class TheSellPageReadsTheHoldItIsLookingAt(unittest.TestCase):

    def _run(self, goods):
        shown = []
        act = _act(ctx.SELL_PAGE, shown=shown)
        with mock.patch("actions.sell_goods._sell_page", return_value=goods), \
             mock.patch.object(MarketActivity, "_port_name", return_value="Faro"):
            res = act.work(GOAL, _state())
        return act, shown, res

    def test_it_seeds_from_the_grid_in_front_of_it(self):
        good = types.SimpleNamespace(name="Pig", owned_qty=457)
        act, _shown, _res = self._run([good])
        self.assertEqual(act._state.ledger.believed("Pig"), 457)

    def test_and_then_goes_back_to_the_purchase_grid(self):
        good = types.SimpleNamespace(name="Pig", owned_qty=457)
        _act_, shown, res = self._run([good])
        self.assertEqual(shown, ["purchase"])
        self.assertEqual(res.observed["did"], "switched to the purchase tab")

    def test_an_unreadable_grid_still_goes_back(self):
        """A poorer seed, not a stall — being stuck on the Sell page helps nobody."""
        act, shown, res = self._run([])
        self.assertIsNotNone(act._state.ledger)
        self.assertEqual(shown, ["purchase"])
        self.assertEqual(res.status, WORKING)


if __name__ == "__main__":
    unittest.main()
