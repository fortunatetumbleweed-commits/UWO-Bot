"""Fleet holdings and this visit's purchases are two numbers with two different owners.

The user's worked example (2026-08-26):

    read the sell grid -> 400 Iron          that 400 is FLEET-owned: it sails
    buy 200            -> pending 200       that 200 is VISIT-owned: it means nothing
                                            once you leave the market
    believed            = 600
    the right panel disagrees, or will not read
                       -> open the Sell panel again, because the sell grid shows what
                          the FLEET already owns
    it reads 600       -> fleet = 600, pending resets to 0
                       -> back to Purchase

The reset is the point: an authoritative read has already ABSORBED the purchases, so keeping
them would double-count.
"""

from __future__ import annotations

import unittest

from brain.market_ledger import MarketLedger


class TheWorkedExample(unittest.TestCase):

    def setUp(self):
        self.led = MarketLedger()
        self.led.seed({"Iron": 400})

    def test_the_seed_is_fleet_owned(self):
        self.assertEqual(self.led.believed("Iron"), 400)
        self.assertFalse(self.led.has_pending())

    def test_a_purchase_is_pending_until_confirmed(self):
        self.led.bought("Iron", 200)
        self.assertEqual(self.led.believed("Iron"), 600)
        self.assertTrue(self.led.has_pending())

    def test_a_disagreeing_panel_sends_us_to_the_sell_grid(self):
        self.led.bought("Iron", 200)
        self.assertTrue(self.led.disagrees("Iron", 451))

    def test_reconciling_absorbs_the_pending_and_resets_it(self):
        self.led.bought("Iron", 200)
        self.led.reconcile({"Iron": 600})
        self.assertEqual(self.led.believed("Iron"), 600)
        self.assertFalse(self.led.has_pending(), "keeping them would double-count")

    def test_buying_again_after_a_reconcile_starts_from_the_new_fleet_count(self):
        self.led.bought("Iron", 200)
        self.led.reconcile({"Iron": 600})
        self.led.bought("Iron", 150)
        self.assertEqual(self.led.believed("Iron"), 750)


class AFailedReadIsNotADisagreement(unittest.TestCase):
    """`None` is no answer, and it is not zero.

    Treating an unreadable panel as `0` is how a loop came to report `0/470` while carrying
    1,300 Iron, and bought past 2,000 (2026-08-26)."""

    def test_an_unreadable_panel_leaves_the_ledger_standing(self):
        led = MarketLedger()
        led.seed({"Iron": 400})
        led.bought("Iron", 200)
        self.assertFalse(led.disagrees("Iron", None))
        self.assertEqual(led.believed("Iron"), 600)

    def test_a_matching_panel_is_not_a_disagreement(self):
        led = MarketLedger()
        led.seed({"Iron": 400})
        led.bought("Iron", 200)
        self.assertFalse(led.disagrees("Iron", 600))

    def test_a_tolerance_can_absorb_a_rounding_difference(self):
        led = MarketLedger()
        led.seed({"Iron": 400})
        self.assertFalse(led.disagrees("Iron", 402, tolerance=5))
        self.assertTrue(led.disagrees("Iron", 402))


class OneMarketCanSupplySeveralMaterials(unittest.TestCase):
    """Barcelona sells both Iron and Matchlock Gun, so one trip to the sell grid must answer
    for every material bought there."""

    def setUp(self):
        self.led = MarketLedger()
        self.led.seed({"Iron": 400, "Matchlock Gun": 146})

    def test_purchases_are_tracked_per_good(self):
        self.led.bought("Iron", 200)
        self.led.bought("Matchlock Gun", 66)
        self.assertEqual(self.led.believed("Iron"), 600)
        self.assertEqual(self.led.believed("Matchlock Gun"), 212)

    def test_the_caller_can_ask_what_is_outstanding(self):
        self.led.bought("Iron", 200)
        self.led.bought("Matchlock Gun", 66)
        self.assertEqual(self.led.pending_goods(), ["iron", "matchlock gun"])

    def test_one_reconcile_absorbs_them_all(self):
        self.led.bought("Iron", 200)
        self.led.bought("Matchlock Gun", 66)
        self.led.reconcile({"Iron": 600, "Matchlock Gun": 212})
        self.assertFalse(self.led.has_pending())
        self.assertEqual(self.led.believed("Matchlock Gun"), 212)

    def test_a_good_never_seen_reads_as_zero_not_an_error(self):
        self.assertEqual(self.led.believed("Candle"), 0)

    def test_names_are_matched_case_and_space_insensitively(self):
        self.led.bought("  iron ", 200)
        self.assertEqual(self.led.believed("IRON"), 600)


class ItRefusesToInventProgress(unittest.TestCase):

    def test_a_purchase_whose_AMOUNT_is_unreadable_is_still_pending(self):
        """The case the ledger exists for. A confirmed buy with an unknown quantity is not
        "nothing bought" — it is precisely when the sell grid must be consulted, so
        `has_pending()` must say so. Refusing to record it is how the loop stopped blind
        exactly when it had most reason to go and look."""
        led = MarketLedger()
        led.seed({"Iron": 400})
        led.bought("Iron")                     # amount unreadable
        self.assertTrue(led.has_pending())
        self.assertTrue(led.amount_unknown("Iron"))
        self.assertEqual(led.pending_goods(), ["iron"])

    def test_an_unknown_amount_is_not_INVENTED(self):
        """`believed` may not guess. It reports the fleet count it knows and nothing more."""
        led = MarketLedger()
        led.seed({"Iron": 400})
        led.bought("Iron")
        self.assertEqual(led.believed("Iron"), 400)

    def test_reconciling_clears_the_unknowns_too(self):
        led = MarketLedger()
        led.seed({"Iron": 400})
        led.bought("Iron")
        led.reconcile({"Iron": 600})
        self.assertFalse(led.has_pending())
        self.assertFalse(led.amount_unknown("Iron"))
        self.assertEqual(led.believed("Iron"), 600)

    def test_seeding_does_not_clear_pending(self):
        """`seed` is the FIRST read of a visit. Absorbing purchases is `reconcile`'s job, and
        conflating them would silently drop a pending count."""
        led = MarketLedger()
        led.bought("Iron", 200)
        led.seed({"Iron": 400})
        self.assertTrue(led.has_pending())
        self.assertEqual(led.believed("Iron"), 600)


if __name__ == "__main__":
    unittest.main()


class ANegativePurchaseIsImpossible(unittest.TestCase):
    """Buying never removes goods, so a delta below zero is a BAD READING, not a fact.

    LIVE 2026-08-31 at Bordeaux: `bought -543 Raisin — believed 161 (fleet 704 + pending
    -543)`. The buy loop then read its own accounting as going backwards, stopped with "no
    further progress", and walked out of the market leaving a STAGED CART — which raised
    "moving to another menu will empty the cart", which nothing answers, so Back toggled the
    dialog until the stall guard ended the run. One impossible number, three failures
    downstream.

    The user's framing is the test: "it is impossible that a quantity is negative... the
    screen's number is correct, it is not negative." So the arithmetic alone proves which
    side is wrong, without needing to look at anything.

    This is CLAUDE.md's existing rule — "the count is a separate reading, and disagreement
    means the READING is wrong" — applied to the one case provable on its own.
    """

    def test_it_is_recorded_as_unreadable_not_as_a_loss(self):
        led = MarketLedger()
        led.seed({"Raisin": 704})
        led.bought("Raisin", -543)
        self.assertEqual(led.believed("Raisin"), 704,
                         "the hold did not shrink; we simply failed to read it")

    def test_it_sends_the_caller_to_the_sell_grid(self):
        led = MarketLedger()
        led.bought("Raisin", -543)
        self.assertTrue(led.amount_unknown("Raisin"),
                        "unreadable is the case the ledger exists for — go and look")
        self.assertTrue(led.has_pending())

    def test_a_real_purchase_is_still_recorded(self):
        led = MarketLedger()
        led.seed({"Raisin": 704})
        led.bought("Raisin", 224)
        self.assertEqual(led.believed("Raisin"), 928)

    def test_zero_is_still_unreadable_not_a_purchase(self):
        led = MarketLedger()
        led.bought("Raisin", 0)
        self.assertTrue(led.amount_unknown("Raisin"))
