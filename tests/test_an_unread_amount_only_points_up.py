"""`believed` is a LOWER BOUND, so uncertainty cannot make a material short.

Live 2026-09-05 at Madeira, buying Raisin against a goal of 1,719:

    [ledger] bought 110 Raisin — believed 1802 (fleet 1540 + pending 262)
    not met — amount unknown for Raisin — the sell grid must settle it
    [ledger] bought 110 Raisin — believed 1912 ...        (and again, and again)

`material_states` tested `amount_unknown` BEFORE the count, so one unreadable purchase early
in the leg marked the good "unknown" for the rest of it — and `_goal_met` returns False on
any unknown, whatever the total. The escape hatch its own message names ("the sell grid must
settle it") is gated on the CARGO COUNT being unreadable, and the cargo tile was reading
perfectly. The one thing that could clear the flag was unreachable precisely because the
other reading worked. Six blue-gem refreshes in it was still buying, heading for the
60-round cap; the run had to be killed.

An unreadable purchase is one we MADE and could not quantify. It only ADDS. So when
`believed` already clears the goal, the material is met with CERTAINTY — the true holding is
believed + something non-negative.
"""

from __future__ import annotations

import unittest

from actions.buy_materials import _goal_met, material_states
from brain.market_ledger import MarketLedger


def _madeira():
    """The live shape: a seeded fleet, one unreadable purchase, then readable ones."""
    led = MarketLedger()
    led.seed({"raisin": 1540})
    led.bought("Raisin")              # unreadable — poisons the good under the old rule
    led.bought("Raisin", 262)
    return led


class ALowerBoundThatClearsTheGoalIsMet(unittest.TestCase):

    def test_the_madeira_case_is_met(self):
        led = _madeira()
        self.assertGreaterEqual(led.believed("Raisin"), 1719)
        states = material_states(led, {"Raisin": 1719})
        self.assertEqual(states["Raisin"]["state"], "met",
                         "unread purchases only ADD; more cannot make us short")

    def test_the_goal_verdict_agrees(self):
        met, why = _goal_met(_madeira(), {"Raisin": 1719})
        self.assertTrue(met, why)

    def test_the_count_is_reported_not_discarded(self):
        """`have` was None even when the floor was known and sufficient."""
        states = material_states(_madeira(), {"Raisin": 1719})
        self.assertEqual(states["Raisin"]["have"], 1802)


class BelowTheGoalItIsStillGenuinelyUnknown(unittest.TestCase):
    """The original rule is right here and must not be weakened: with an unread purchase and
    a floor BELOW the goal, we truly cannot tell — go and read the sell grid."""

    def _short_and_unknown(self):
        led = MarketLedger()
        led.seed({"raisin": 100})
        led.bought("Raisin")          # unreadable
        return led

    def test_state_is_unknown(self):
        states = material_states(self._short_and_unknown(), {"Raisin": 1719})
        self.assertEqual(states["Raisin"]["state"], "unknown")

    def test_have_stays_None_so_a_floor_is_not_mistaken_for_a_count(self):
        states = material_states(self._short_and_unknown(), {"Raisin": 1719})
        self.assertIsNone(states["Raisin"]["have"])

    def test_the_goal_is_not_met_and_says_why(self):
        met, why = _goal_met(self._short_and_unknown(), {"Raisin": 1719})
        self.assertFalse(met)
        self.assertIn("unknown", why)


class TheOrdinaryCasesAreUnchanged(unittest.TestCase):

    def test_short_with_every_amount_read(self):
        led = MarketLedger()
        led.bought("Raisin", 651)
        states = material_states(led, {"Raisin": 1719})
        self.assertEqual(states["Raisin"]["state"], "short")
        self.assertEqual(states["Raisin"]["have"], 651)

    def test_met_with_every_amount_read(self):
        led = MarketLedger()
        led.bought("Raisin", 1800)
        self.assertEqual(material_states(led, {"Raisin": 1719})["Raisin"]["state"], "met")

    def test_a_surplus_of_one_never_covers_a_shortfall_of_another(self):
        """The rule this file has always had: PER GOOD, never the sum."""
        led = MarketLedger()
        led.bought("Iron", 830)
        led.bought("Matchlock Gun", 158)
        met, why = _goal_met(led, {"Iron": 506, "Matchlock Gun": 305})
        self.assertFalse(met)
        self.assertIn("Matchlock Gun", why)


if __name__ == "__main__":
    unittest.main()
