"""A gather leg trims BEFORE it buys, so the purchase has room to land in.

Live 2026-09-04 (session trace_barter_cmd_2026-09-04T17-35-01):

    Faro     ordered Pig 1,260  ->  bought 2,736   (whole 684-unit shelves)
    Madeira  ordered Raisin 1,260 -> bought   869   "no further progress"

Between them the hold went to 4,847/4,952 — 105 slots free — because Faro's 1,476-unit
surplus sailed to Madeira. The Raisin shortfall capped the barter at four rounds, and four
blue-gem refreshes were spent on a Raisin shelf that was never empty.

The graph already trims for exactly this reason ("buy_to_goal buys whole shelves, so the
hold arrives over-stocked"), but `sell_surplus` depends on ALL the gathers:

    trim_before_gather -> {gather:Faro, gather:Madeira} -> sell_surplus -> ... -> village

so the trim that would have released those 1,476 slots was scheduled to run after Madeira.
Frame 171 is the moment it could have been fixed: the Sell grid open at Madeira, the hold in
plain view, before a single Raisin had been bought (user, 2026-09-04 — "at 171 it saw there
are too many pigs, trim there"). Space is what a purchase needs, so a trim that runs after
the buying is a trim that could not help it.

Both goals are MARKET goals, so this is one visit and one walk: TrimHold enters the market
and Hold finds itself already there.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from brain.mission import build_barter_graph


class _Plan:
    needs = {"Pig": 1260, "Raisin": 1260}
    purchases = {"Faro": {"Pig": 1260}, "Madeira": {"Raisin": 1260}}


def _graph():
    opp = SimpleNamespace(good="Bambara Groundnut", village="San Village",
                          sell_port="London", rounds=5)
    return {t.id: t for t in build_barter_graph(opp, _Plan())}


class EveryGatherKnowsTheTargets(unittest.TestCase):

    def test_each_gather_carries_keep_qty(self):
        """Without it the leg has nothing to trim against and the surplus sails on."""
        g = _graph()
        for port in ("Faro", "Madeira"):
            with self.subTest(port):
                self.assertEqual(g[f"gather:{port}"].params.get("keep_qty"),
                                 {"Pig": 1260, "Raisin": 1260})

    def test_the_orders_are_still_per_port(self):
        """keep_qty is the whole plan; the ORDER stays this port's own."""
        g = _graph()
        self.assertEqual(g["gather:Faro"].params["orders"], {"Pig": 1260})
        self.assertEqual(g["gather:Madeira"].params["orders"], {"Raisin": 1260})

    def test_the_end_of_gathering_trim_is_still_there(self):
        """Per-leg trimming bounds what each leg hands the next; this catches whatever the
        LAST leg overshot, which no later gather would."""
        g = _graph()
        self.assertIn("sell_surplus", g)
        self.assertEqual(g["sell_surplus"].params.get("keep_qty"),
                         {"Pig": 1260, "Raisin": 1260})

    def test_the_gathers_stay_mutually_unordered(self):
        """Trimming inside the leg rather than as a node between legs is what preserves
        this — the scheduler still picks the cheapest next port."""
        g = _graph()
        for port in ("Faro", "Madeira"):
            with self.subTest(port):
                self.assertEqual(g[f"gather:{port}"].deps, ("trim_before_gather",))

    def test_the_pre_gather_clear_is_untouched(self):
        g = _graph()
        self.assertTrue(g["trim_before_gather"].params.get("clear"))
        self.assertTrue(g["trim_before_gather"].optional)


if __name__ == "__main__":
    unittest.main()
