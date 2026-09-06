"""The market is one activity with three goals.

`gather`, `sell_surplus` and `sell` are three task nodes today, and each navigates to the
market before doing its one market thing. They differ only in WHICH goods and HOW MANY — which
is CLAUDE.md's "ONE sell flow, several goals" reached from the other direction, and why
`_enter_market_at` had to be patched into four call sites at once on 2026-08-26.

Two boundaries these tests exist to hold:

  * the activity does NOT get to the market. Entering a building is a transition — it ends one
    activity and expects another — so it is an intent the dispatcher dispatches, and this
    begins once perception already says the bot is in a market.
  * the goals carry NO SCREENS. `Hold({"Iron": 242})` says what the hold should contain and
    mentions no tab, tile or checkbox.
"""

from __future__ import annotations

import unittest

from brain.activities.market import FreeHold, Hold, MarketActivity, SellHold
from brain.dispatcher import BLOCKED, FINISHED, UNRECOGNISED


class _State:
    def __init__(self, where="building:market", port="Amsterdam"):
        self.state, self.port = where, port


def _activity(**kw):
    kw.setdefault("show_grid_fn", lambda: None)
    kw.setdefault("port_fn", lambda: "Amsterdam")
    return MarketActivity(**kw)


class TheGoalsAreTaskVocabulary(unittest.TestCase):

    def test_they_read_as_intentions_not_procedures(self):
        self.assertEqual(str(Hold({"Iron": 242})), "hold Iron (~242)")
        self.assertEqual(str(SellHold(("Water",))), "sell the hold (excluding Water)")

    def test_no_goal_mentions_a_screen(self):
        """If a goal can name a tab or a tile, the boundary has already leaked."""
        for g in (Hold({"Iron": 242}), FreeHold(("Water",)), SellHold(("Food",))):
            text = str(g).lower()
            for word in ("tab", "tile", "button", "tap", "grid", "checkbox", "bulk"):
                self.assertNotIn(word, text)

    def test_the_quantity_is_an_estimate_not_a_contract(self):
        """The plan wanted 242 Iron on 2026-08-26 and a bulk tap bought 445. That is not a
        failure, and the result must not call it one."""
        act = _activity(buy_fn=lambda port, goal, **k: {"met": True, "bought_total": 445})
        res = act.work(Hold({"Iron": 242}), _State())
        self.assertTrue(res.ok)
        self.assertEqual(res.observed["bought_total"], 445)
        self.assertEqual(res.observed["ordered"], {"Iron": 242},
                         "what was ASKED for is reported beside what arrived — and no "
                         "per-good breakdown is invented, because there is none to give")


class ItOnlyWorksWhereItIs(unittest.TestCase):

    def test_it_hands_back_when_it_is_not_in_a_market(self):
        """Getting there is the dispatcher's business. An activity that navigates is the
        inversion this whole design removes."""
        res = _activity().work(Hold({"Iron": 242}), _State(where="sea"))
        self.assertEqual(res.status, UNRECOGNISED)

    def test_it_does_not_sail_or_tap_its_way_anywhere(self):
        called = []
        act = _activity(buy_fn=lambda *a, **k: called.append("buy") or {"met": True,
                                                                        "bought_total": 1})
        act.work(Hold({"Iron": 1}), _State(where="world_map"))
        self.assertEqual(called, [], "it must do nothing at all from the wrong screen")


class TheThreeGoalsShareOneActivity(unittest.TestCase):

    def test_buying_reports_what_it_bought_and_why_it_stopped(self):
        act = _activity(buy_fn=lambda port, goal, **k: {"met": False, "bought_total": 0})
        res = act.work(Hold({"Iron": 242}), _State())
        self.assertEqual(res.observed["stopped_because"], "shelf empty or hold full")

    # THE SELL GOALS NOW GO THROUGH CONTEXTS, one action per tick, so there is no injected
    # flow to inspect — the question these asked ("is the goal's protection honoured?") is
    # asked of `market_sell.selection_for`, which is the piece that still decides it.
    #
    # Converted 2026-09-06. The old versions asserted that `sell_goods` was CALLED with
    # goal="clear" / exclude=[...]; that call no longer happens, and asserting on it would
    # pin the market to the flow this work removes.

    def test_freeing_the_hold_keeps_only_what_the_goal_protects(self):
        import types

        from brain.activities.market_sell import selection_for

        goods = [types.SimpleNamespace(name=n, owned_qty=10, profit_per_unit=5,
                                       tap_x=0, tap_y=0)
                 for n in ("Pig", "Raisin", "Water", "Food", "Iron")]
        chosen = {getattr(g, "name") for g in
                  selection_for(FreeHold(("Water", "Food", "Iron")), goods)}
        self.assertEqual(chosen, {"Pig", "Raisin"})

    def test_selling_for_profit_protects_the_exclusions(self):
        import types

        from brain.activities.market_sell import selection_for

        goods = [types.SimpleNamespace(name=n, owned_qty=10, profit_per_unit=5,
                                       tap_x=0, tap_y=0)
                 for n in ("Birch Tree", "Water", "Food")]
        chosen = {getattr(g, "name") for g in
                  selection_for(SellHold(("Water", "Food")), goods)}
        self.assertNotIn("Water", chosen)
        self.assertNotIn("Food", chosen)
        self.assertIn("Birch Tree", chosen)

    def test_an_unknown_goal_is_refused_rather_than_guessed_at(self):
        res = _activity().work("do something clever", _State())
        self.assertEqual(res.status, BLOCKED)


class WhatCrossesTheBoundary(unittest.TestCase):
    """Up: what changed and why it stopped. Never tabs, tiles or coordinates."""

    def test_the_result_carries_no_ui_detail(self):
        act = _activity(buy_fn=lambda port, goal, **k: {"met": True, "bought_total": 445})
        res = act.work(Hold({"Iron": 242}), _State())
        blob = f"{res.observed}".lower()
        for word in ("tab", "tile", "coord", "tap", "checkbox", "grid"):
            self.assertNotIn(word, blob)

    def test_it_says_why_it_stopped(self):
        """'Goal met' and 'the shelf ran out' are different situations for the task."""
        act = _activity(buy_fn=lambda port, goal, **k: {"met": True, "bought_total": 445})
        self.assertEqual(act.work(Hold({"Iron": 242}), _State()).observed["stopped_because"],
                         "goal met")
