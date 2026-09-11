"""A sell leg that still holds what it came to sell is not finished.

Live 2026-09-06 at Lisboa, at the end of an otherwise clean mission — Tripoli, Svear, six
barter rounds, sailed to Lisboa to sell:

    [sell] skipping 'Birch Tree' — we hold 3668 but its price is unreadable,
           and this pass sells on profit
    nothing sellable after scrolling to page 2 — the clear is finished
    sell done / every leg is done / status done

The mission reported SUCCESS holding the 3,668 units it had sailed there to sell. Skipping an
unpriced good is right on its own — a profit pass must not guess at a price — but calling the
leg finished turned one bad read into a lost cargo, and threw away the retry that would have
re-read it.

CLAUDE.md, flow completeness: "a flow is complete only when it (1) ends at a recognised state
AND (2) contains at least one positive transaction."
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

from brain.activities.market import FreeHold, SellHold
from brain.activities.market_sell import _MAX_PRICE_READS, on_sell_page
from brain.market_state import MarketState


def _good(name, owned):
    return types.SimpleNamespace(name=name, owned_qty=owned, tap_x=10, tap_y=20,
                                 profit_per_unit=None)


class _Page:
    def __init__(self, goods):
        self.goods = goods

    def run(self, goal, state):
        with mock.patch("actions.sell_goods._sell_page", return_value=self.goods), \
             mock.patch("actions.sell_goods._find_sell_commit", return_value=None), \
             mock.patch("brain.activities.market_sell.selection_for", return_value=[]):
            return on_sell_page(state, goal, frame=object(), capture_fn=lambda: object(),
                                tap_fn=lambda *a: None, omni_fn=lambda _f: [])


GOAL = SellHold(exclude=("Water", "Food"))


class CargoWeCouldNotPriceHoldsTheLegOpen(unittest.TestCase):

    def test_it_looks_again_rather_than_reporting_the_hold_empty(self):
        out = _Page([_good("Birch Tree", 3668)]).run(GOAL, MarketState())
        self.assertEqual(out["do"], "waited")
        self.assertIn("Birch Tree", out["why"])

    def test_it_reports_BLOCKED_rather_than_done_once_the_looks_are_spent(self):
        state, page = MarketState(), _Page([_good("Birch Tree", 3668)])
        for _ in range(_MAX_PRICE_READS + 1):
            out = page.run(GOAL, state)
        self.assertEqual(out["do"], "blocked")
        self.assertIn("nothing was sold", out["why"])

    def test_an_EMPTY_hold_still_finishes(self):
        """The ordinary case must not be held open — no owned tiles, nothing to sell."""
        state = MarketState(last_intent="scrolled")
        out = _Page([_good("Birch Tree", 0)]).run(GOAL, state)
        self.assertEqual(out["do"], "finished")

    def test_goods_the_goal_KEEPS_do_not_hold_it_open(self):
        """Water, Food and the barter's materials are kept, not declined."""
        state = MarketState(last_intent="scrolled")
        goal = FreeHold(keep=("Water", "Food", "Iron"))
        out = _Page([_good("Water", 200), _good("Iron", 822)]).run(goal, state)
        self.assertEqual(out["do"], "finished")

    def test_a_leg_that_SOLD_something_finishes_normally(self):
        """One positive transaction is what completeness asks for."""
        state = MarketState(last_intent="scrolled")
        state.sold.append("Birch Tree")
        out = _Page([_good("Birch Tree", 12)]).run(GOAL, state)
        self.assertEqual(out["do"], "finished")


if __name__ == "__main__":
    unittest.main()
