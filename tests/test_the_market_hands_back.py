"""The market as contexts: classify, do ONE thing, hand back.

The property that matters is not what each handler does — it is that a screen the activity
has no move for reaches the DISPATCHER instead of being acted on. FC-1 and FC-3 are both
"a positive button pressed by code that never knew which card it was on".
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

import brain.market_context as ctx
from brain.activities.market import MarketActivity, SellHold
from brain.dispatcher import BLOCKED, FINISHED, UNRECOGNISED, WORKING

GOAL = SellHold(exclude=("Water", "Food"))


def _state(where="building:market", port="London"):
    return types.SimpleNamespace(state=where, port=port, frame=object())


def _act(context, *, sell_out=None, taps=None):
    taps = [] if taps is None else taps
    return MarketActivity(context_fn=lambda f: context,
                          capture_fn=lambda: object(),
                          tap_fn=lambda x, y: taps.append((x, y)),
                          omni_fn=lambda f: []), taps


class AnUnrecognisedScreenGoesBackToTheDispatcher(unittest.TestCase):
    """The whole point. Every context without a handler must hand back, not guess."""

    def test_a_MISS_hands_back(self):
        act, _ = _act(ctx.MISS)
        res = act.work(GOAL, _state())
        self.assertEqual(res.status, UNRECOGNISED)

    def test_contexts_this_activity_does_not_drive_hand_back(self):
        """Buying's cards and the barter's overflow are not this goal's business. Answering
        them here would be the activity acting on a screen it cannot reason about."""
        for where in (ctx.QUANTITY_DIALOG, ctx.TRADE_GOODS_INFO, ctx.RESTOCK_PROMPT,
                      ctx.OVERFLOW_PROMPT, ctx.DISCARD_NOTICE):
            act, taps = _act(where)
            res = act.work(GOAL, _state())
            self.assertEqual(res.status, UNRECOGNISED, where)
            self.assertEqual(taps, [], f"{where}: handed back, and nothing tapped")


class TheWrongGridIsSwitched_NotHandedBack(unittest.TestCase):
    """Handing back would be honest and useless — the dispatcher routes here again on the
    same screen. The two grids look alike and mean opposite things, so switching IS the one
    action worth taking."""

    def test_a_sell_goal_on_the_purchase_grid_switches(self):
        act, _ = _act(ctx.PURCHASE_PAGE)
        with mock.patch("actions.buy_materials.ensure_sell_tab", return_value=True) as sw:
            res = act.work(GOAL, _state())
        self.assertEqual(res.status, WORKING)
        self.assertEqual(res.observed["did"], "switched to the sell tab")
        sw.assert_called_once()

    def test_a_buy_goal_on_the_sell_grid_switches(self):
        from brain.activities.market import Hold

        act, _ = _act(ctx.SELL_PAGE)
        shown = []
        act._show_grid = lambda: shown.append(True)
        res = act.work(Hold({"Iron": 100}), _state())
        self.assertEqual(res.observed["did"], "switched to the purchase tab")
        self.assertEqual(len(shown), 1)


class OurOwnCardsAreAnsweredOnceEach(unittest.TestCase):

    def test_a_confirm_is_one_tap_then_hand_back(self):
        act, _ = _act(ctx.CONFIRM_DIALOG)
        with mock.patch("brain.commit_actions.commit_via_positive_taps",
                        return_value=[("ok", 0.5, 0.9)]) as loop:
            res = act.work(GOAL, _state())
        self.assertEqual(res.status, WORKING)
        self.assertEqual(loop.call_args.kwargs.get("max_taps"), 1,
                         "one tap per tick — the looping form is what caused FC-3")

    def test_a_negotiation_is_answered_the_same_way(self):
        act, _ = _act(ctx.NEGOTIATION)
        with mock.patch("brain.commit_actions.commit_via_positive_taps",
                        return_value=[("ok", 0.5, 0.9)]) as loop:
            act.work(GOAL, _state())
        self.assertEqual(loop.call_args.kwargs.get("max_taps"), 1)


class OnlyTheResultCardWritesTheLedger(unittest.TestCase):
    """FC-2: a purchase was recorded that never happened — no result card, no goods, an
    entry anyway. Writing only from here makes that unreachable."""

    def test_the_result_records_what_it_names(self):
        act, _ = _act(ctx.RESULT_DIALOG)
        sold = [types.SimpleNamespace(name="Bambara Groundnut")]
        with mock.patch("brain.commit_actions.commit_via_positive_taps",
                        return_value=[("ok", 0.5, 0.9)]), \
             mock.patch("actions.sell_goods._sell_page", return_value=sold):
            res = act.work(GOAL, _state())
        self.assertIn("Bambara Groundnut", res.observed["sold"])

    def test_staging_records_nothing(self):
        """The defect this replaces: `sold` reporting what was STAGED, never revisited."""
        act, taps = _act(ctx.SELL_PAGE)
        good = types.SimpleNamespace(name="Pig", owned_qty=10, profit_per_unit=5,
                                     tap_x=1450, tap_y=314)
        with mock.patch("actions.sell_goods._sell_page", return_value=[good]), \
             mock.patch("actions.sell_goods._find_sell_commit", return_value=None):
            res = act.work(GOAL, _state())
        self.assertEqual(res.observed["sold"], [], "staging is not selling")
        self.assertEqual(res.observed["did"], "staged")


class TheStateBelongsToTheVisit(unittest.TestCase):

    def test_a_new_port_does_not_inherit_the_last_one_s_basket(self):
        act, _ = _act(ctx.SELL_PAGE)
        good = types.SimpleNamespace(name="Pig", owned_qty=10, profit_per_unit=5,
                                     tap_x=1450, tap_y=314)
        with mock.patch("actions.sell_goods._sell_page", return_value=[good]), \
             mock.patch("actions.sell_goods._find_sell_commit", return_value=None):
            act.work(GOAL, _state(port="London"))
            act._state.sold.append("Pig")
            res = act.work(GOAL, _state(port="Lisboa"))
        self.assertEqual(res.observed["sold"], [], "another port's sales are not ours")


class ItStillRefusesToBeSomewhereElse(unittest.TestCase):
    def test_not_in_a_market_hands_back(self):
        act, _ = _act(ctx.SELL_PAGE)
        res = act.work(GOAL, _state(where="sea"))
        self.assertEqual(res.status, UNRECOGNISED)
