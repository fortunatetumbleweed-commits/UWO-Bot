"""The tracked tile can wander, and a total is only a per-good count while the ledger is blind.

Live 2026-09-05 at Madeira. The order was Raisin alone, with 1,368 Pig already aboard:

    bought 217 Raisin — believed 651     (three shelves, every amount READ)
    round 4/60: owned=1368 (+717)        (Pig's count — no 717 Raisin shelf exists)
    goal met — Raisin ~1368/1260

The result carried `met: True` and `Raisin state: 'short', have: 651` together. The mission
believed Raisin was gathered, and the run went on to fail one port from Hutu.

The shortcut's own premise is "every unit the cargo counter saw ARRIVE can only be this
material" — true of arrivals, false of the tracked tile's absolute count.
"""

from __future__ import annotations

import unittest

from brain.market_ledger import MarketLedger


class TheLedgerWinsWhenItHasRead_Everything(unittest.TestCase):

    def _madeira(self):
        """Three Raisin shelves, every amount readable: the ledger knows 651."""
        led = MarketLedger()
        for _ in range(3):
            led.bought("Raisin", 217)
        return led

    def test_the_ledger_knows_the_real_count(self):
        self.assertEqual(self._madeira().believed("Raisin"), 651)

    def test_the_amount_is_known_when_every_purchase_was_read(self):
        self.assertFalse(self._madeira().amount_unknown("Raisin"),
                         "a ledger that read every purchase IS the answer")

    def test_an_unread_amount_is_what_blindness_means(self):
        led = MarketLedger()
        led.bought("Raisin")                      # amount unreadable
        self.assertTrue(led.amount_unknown("Raisin"))
        self.assertFalse(led.amount_unknown("Pig"), "blindness is per good")


class TheShortcutFiresOnlyForABlindLedger(unittest.TestCase):
    """`_met` is a closure, so drive it through buy_to_goal's own construction."""

    def _met(self, ledger, goal, total_seen):
        import actions.buy_materials as B

        met, why = B._goal_met(ledger, goal)
        if met:
            return True, why
        # The branch under test, as it now stands in buy_to_goal._met.
        only_good = next(iter(goal)) if len(goal) == 1 else None
        if only_good is not None and ledger.amount_unknown(only_good):
            if total_seen >= int(goal[only_good]):
                return True, f"{only_good} ~{total_seen}/{goal[only_good]}"
        return met, why

    def test_madeira_is_not_met(self):
        """The case that broke: ledger fully read at 651, tile wandered to Pig's 1368."""
        led = MarketLedger()
        for _ in range(3):
            led.bought("Raisin", 217)
        met, _ = self._met(led, {"Raisin": 1260}, total_seen=1368)
        self.assertFalse(met, "a wandering tile must not overrule a ledger that can answer")

    def test_a_blind_ledger_still_gets_the_shortcut(self):
        """Its reason for existing: amounts unreadable, one good, so the total IS its count."""
        led = MarketLedger()
        led.bought("Raisin")                      # unreadable
        met, _ = self._met(led, {"Raisin": 1260}, total_seen=1368)
        self.assertTrue(met, "with nothing else being bought, arrivals are this good's")

    def test_a_blind_ledger_short_of_the_goal_is_still_short(self):
        led = MarketLedger()
        led.bought("Raisin")
        met, _ = self._met(led, {"Raisin": 1260}, total_seen=651)
        self.assertFalse(met)

    def test_a_genuinely_met_ledger_needs_no_shortcut(self):
        led = MarketLedger()
        for _ in range(6):
            led.bought("Raisin", 217)             # 1302
        met, _ = self._met(led, {"Raisin": 1260}, total_seen=0)
        self.assertTrue(met, "the ledger alone settles it")


if __name__ == "__main__":
    unittest.main()
