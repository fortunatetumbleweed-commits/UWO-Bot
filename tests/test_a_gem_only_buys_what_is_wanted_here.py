"""A market refresh is market-WIDE, so it is worth a gem when anything still wanted is sold here.

Live 2026-09-05 at Faro, order {Pig 1719, Raisin 1719}, Pig MET at 1828 and Raisin not
stocked at Faro at all:

    not met — short: Raisin 0/1719
    [refresh] tap the refresh icon (timer was 00.28.43)      <- gem spent
    [refresh] verify: accepted=True tile[Pig] active=True    <- it restocked PIG
    leg finished

It restocked the good it already had enough of and ended the leg. The refresh price
escalates with use — by the 14th refresh that evening the dialog read 345 blue gems.

The rule must NOT narrow to "the emptied good must be the short one": Barcelona stocks both
Iron and Matchlock Gun, and with Iron met and Matchlock short the refresh is exactly right,
because it refills Matchlock too.
"""

from __future__ import annotations

import unittest

from actions.buy_materials import material_states
from brain.market_ledger import MarketLedger


class _Tile:
    """Stands in for a market tile; presence in the dict is what "sold here" means."""


def _still_worth_a_gem(ledger, goal, goods_seen):
    """The guard as it now stands in buy_to_goal, exercised directly."""
    for material, state in material_states(ledger, goal).items():
        if state["state"] == "met":
            continue
        if (goods_seen or {}).get(material.lower()) is not None:
            return material
    return None


def _led(**owned):
    led = MarketLedger()
    led.seed({k.lower(): v for k, v in owned.items()})
    return led


GOAL = {"Pig": 1719, "Raisin": 1719}


class NothingWantedIsSoldHere(unittest.TestCase):

    def test_the_faro_case_spends_nothing(self):
        """Pig met; Raisin short but Faro does not stock it."""
        got = _still_worth_a_gem(_led(Pig=1828, Raisin=0), GOAL, {"pig": _Tile()})
        self.assertIsNone(got, "restocking Pig cannot help a Raisin shortfall")

    def test_everything_met_spends_nothing(self):
        got = _still_worth_a_gem(_led(Pig=1828, Raisin=1800), GOAL,
                                 {"pig": _Tile(), "raisin": _Tile()})
        self.assertIsNone(got)


class SomethingWantedIsSoldHere(unittest.TestCase):

    def test_the_barcelona_case_still_refreshes(self):
        """Iron met, Matchlock short, BOTH stocked — the refresh refills Matchlock."""
        goal = {"Iron": 709, "Matchlock Gun": 355}
        got = _still_worth_a_gem(_led(Iron=800, **{"Matchlock Gun": 100}), goal,
                                 {"iron": _Tile(), "matchlock gun": _Tile()})
        self.assertEqual(got, "Matchlock Gun",
                         "a market-wide refresh is justified by ANY good still wanted here")

    def test_the_ordinary_short_good_still_refreshes(self):
        got = _still_worth_a_gem(_led(Pig=100, Raisin=0), GOAL,
                                 {"pig": _Tile(), "raisin": _Tile()})
        self.assertIsNotNone(got)

    def test_an_unread_amount_counts_as_still_wanted(self):
        """Unknown is not met — an unreadable purchase is no reason to stop buying."""
        led = _led(Raisin=0)
        led.bought("Raisin")                     # amount unreadable
        got = _still_worth_a_gem(led, {"Raisin": 1719}, {"raisin": _Tile()})
        self.assertEqual(got, "Raisin")


class TheGuardIsWiredIn(unittest.TestCase):

    def test_buy_to_goal_consults_it_before_refreshing(self):
        import inspect

        import actions.buy_materials as B

        src = inspect.getsource(B.buy_to_goal)
        self.assertIn("_still_worth_a_gem(goods_after)", src)
        before = src.index("_still_worth_a_gem(goods_after)")
        after = src.index("_do_refresh(attempt, emptied[0])")
        self.assertLess(before, after, "the guard must run BEFORE the gem is spent")


if __name__ == "__main__":
    unittest.main()
