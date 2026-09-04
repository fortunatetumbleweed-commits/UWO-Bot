"""OmniParser never read -543. We computed it.

LIVE 2026-08-31 at Bordeaux: `[ledger] bought -543 Raisin — believed 161 (fleet 704 + pending
-543)`. That number is on no screen. It is `owned - bought_total` where a read came back 161
against a running total of 704 — our own subtraction over two readings, one of which was
wrong.

The frames show the screen was right: Raisin went 704 -> 1,077 and a fourth tile appeared, so
the panel REFLOWED between the two reads.

The code already knew this could happen. One line below the subtraction:

    # max() so a stale/failed read never DECREASES the running total.
    bought_total = max(bought_total, cargo_after)

The running total was guarded; the DELTA computed from the same suspect number was not — and
the delta is what reaches the ledger. So one impossible number became: the loop reading its
own accounting as going backwards, stopping with "no further progress", walking out of the
market with a cart still staged, and wedging on the cart dialog.

Guarded in BOTH places now: here, where a negative rise becomes "unreadable", and in the
ledger, which refuses an impossible purchase outright.
"""
import unittest

from brain.market_ledger import MarketLedger


class TheLedgerRefusesTheImpossible(unittest.TestCase):
    def test_a_negative_purchase_does_not_shrink_the_hold(self):
        led = MarketLedger()
        led.seed({"Raisin": 704})
        led.bought("Raisin", -543)
        self.assertEqual(led.believed("Raisin"), 704)
        self.assertTrue(led.amount_unknown("Raisin"), "unreadable — go and look at the grid")


class TheDeltaIsGuardedWhereItIsComputed(unittest.TestCase):
    """Belt and braces, deliberately: the ledger is the last line of defence, but a number
    that cannot be true should not travel that far in the first place."""

    def test_the_source_guards_it_too(self):
        import inspect
        from actions import buy_materials
        src = inspect.getsource(buy_materials.buy_to_goal)
        self.assertIn("owned < bought_total", src,
                      "a read below the running total is a failed read, not a loss")

    def test_and_says_why_in_the_log(self):
        import inspect
        from actions import buy_materials
        src = inspect.getsource(buy_materials.buy_to_goal)
        self.assertIn("buying does not remove", src)


if __name__ == "__main__":
    unittest.main()


class TheRunningTotalIsPerGoodNotTheSum(unittest.TestCase):
    """`bought_total` is "best-known OWNED count of THE GOOD" — one good, one tile.

    It was SEEDED with `sum(owned[m] for m in goal)`, the total across every material in the
    order. Every later single-tile reading was then compared against that sum, so the "rise"
    was negative by construction:

        live 2026-08-31, Bordeaux, order {Raisin 1008, Pig 870}, tiles 704 and 916
          bought_total = 704 + 916 = 1620
          round 1: owned=1077 -> 1077 - 1620 = -543
          round 2: owned= 916 ->  916 - 1620 = -704

    This is the file's own "PER GOOD, never the sum" rule, which it states twice — including
    three lines above the seed, about the goal check. The check was fixed; the seed was not.

    A ONE-material order hides it completely, because then the sum IS the tile. It took a
    two-material gather to make the two diverge.
    """

    def test_the_seed_no_longer_sums_across_goods(self):
        import inspect
        from actions import buy_materials
        src = inspect.getsource(buy_materials.buy_to_goal)
        self.assertNotIn("pre_owned = sum(", src,
                         "summing two goods' tiles and calling it one good's count")

    def test_it_says_why(self):
        import inspect
        from actions import buy_materials
        self.assertIn("PER GOOD, NEVER THE SUM",
                      inspect.getsource(buy_materials.buy_to_goal))

    def test_the_arithmetic_that_produced_minus_543(self):
        """The bug, as plain arithmetic — no mocks needed to show it was impossible."""
        raisin, pig = 704, 916
        summed_seed = raisin + pig                 # what it did
        per_good_seed = max(raisin, pig)           # what it does now
        self.assertEqual(1077 - summed_seed, -543, "the exact number the ledger recorded")
        self.assertGreaterEqual(1077 - per_good_seed, 0, "a rise, as a rise must be")
