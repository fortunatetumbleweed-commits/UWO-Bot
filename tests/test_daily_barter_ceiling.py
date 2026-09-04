"""Plan against the DAY'S barter ceiling, not the total shown on arrival.

The Base tab's 'Daily Barter Progress N/M' is a snapshot at the CURRENT amity grade, and
amity climbs while you barter. Live at San Village 2026-08-24: the tab read 0/5 at Neutral
while the Amity Effect panel already promised "Increase Barter Count by 2", and over one
session amity went Neutral → Favorable → Trusting. Each grade opens more rounds.

A plan built on the arrival total therefore under-provisions the gather, and the fleet sails
away with rounds it was entitled to still unused. What actually stops the bartering is the
day's rounds being spent, or the materials running out — both re-checked at the panel every
round (user, 2026-08-24).
"""

from __future__ import annotations

import unittest

from actions.village_check import MAX_DAILY_BARTER_ROUNDS, VillageCheck


def _check(used, total):
    return VillageCheck(village="San Village", barters_used=used, barters_total=total)


class PlanForTheCeiling(unittest.TestCase):

    def test_the_live_case_plans_for_seven_not_five(self):
        """0/5 shown at Neutral — the day allows 7 once amity rises."""
        self.assertEqual(_check(0, 5).rounds_remaining, 7)

    def test_rounds_already_spent_are_still_subtracted(self):
        """The ceiling raises the total; it does not forgive rounds already used."""
        self.assertEqual(_check(3, 7).rounds_remaining, 4)
        self.assertEqual(_check(2, 5).rounds_remaining, 5)

    def test_a_used_up_day_is_zero(self):
        """THE check that matters: do not sail for rounds that no longer exist."""
        self.assertEqual(_check(7, 7).rounds_remaining, 0)
        self.assertEqual(_check(9, 7).rounds_remaining, 0)

    def test_a_total_above_the_ceiling_is_believed(self):
        """A village (or a future buff) offering more than 7 is not clamped down to it."""
        self.assertEqual(_check(0, 9).rounds_remaining, 9)

    def test_unreadable_stays_unknown(self):
        """Never invent an allowance — unknown must stay unknown."""
        self.assertIsNone(_check(None, 7).rounds_remaining)
        self.assertIsNone(_check(0, None).rounds_remaining)

    def test_the_ceiling_is_seven(self):
        self.assertEqual(MAX_DAILY_BARTER_ROUNDS, 7)


if __name__ == "__main__":
    unittest.main()
