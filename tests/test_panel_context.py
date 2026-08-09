"""A2 Phase 3 — panel context reader.

Identify which chromed screen a frame is from its left-menu items matched
against the tiered vocab. Robust to game/UI drift because it keys on the game's
own menu wording, not pixel fingerprints.
"""
import unittest

from vision.panel_context import identify_context


class IdentifyContextTests(unittest.TestCase):
    def test_village_from_menu(self):
        # exactly what detect_left_menu returns on the real village frames
        pc = identify_context(["Explore", "Gifting", "Loot", "Recruit Crew",
                               "Barter", "Berber Village"])
        self.assertIsNotNone(pc)
        self.assertEqual(pc.context, "village")
        self.assertGreaterEqual(pc.score, 4)
        self.assertEqual(pc.village_name, "Berber Village")

    def test_market_from_menu(self):
        pc = identify_context(["Purchase", "Sell"])
        self.assertIsNotNone(pc)
        self.assertEqual(pc.context, "market")

    def test_bank_fuzzy_ocr_noise(self):
        # real OCR: 'Deposit/' (truncated), 'Savings Account' (vs vocab 'Saving')
        pc = identify_context(["Deposit/", "Withdrawal", "Savings Account", "Insurance"])
        self.assertIsNotNone(pc)
        self.assertEqual(pc.context, "bank")

    def test_inn_from_menu(self):
        pc = identify_context(["Recruit", "Hire", "Party", "Employee", "Manage Mate"])
        self.assertIsNotNone(pc)
        self.assertEqual(pc.context, "inn")

    def test_harbor_not_village_despite_recruit_crew(self):
        # 'Recruit Crew' is shared, but Supply/Repair make harbor the winner —
        # and a lone shared item must not misfire as village.
        pc = identify_context(["Supply", "Repair", "Recruit Crew"])
        self.assertIsNotNone(pc)
        self.assertEqual(pc.context, "harbor")

    def test_below_threshold_returns_none(self):
        # a single matching item is not enough to decide a context
        self.assertIsNone(identify_context(["Barter"]))

    def test_empty_returns_none(self):
        self.assertIsNone(identify_context([]))
        self.assertIsNone(identify_context(["totally", "unrelated", "words"]))


if __name__ == "__main__":
    unittest.main()
