"""The market was already turning over, so there was no gem dialog to confirm.

User, 2026-09-05, watching the Madeira Raisin leg: "it tapped at the refresh, but the market
was refreshing at the time, so there is no blue gem dialog."

    [refresh] tap ↻ refresh icon (timer was 00.00:11)
    [refresh] no OK button found on the refresh dialog
    [refresh] verify: accepted=False tile[Raisin] active=False → refreshed=False
    no refresh for 'Raisin' — the shelf stays empty and this leg stops here

Eleven seconds before the shelf refilled for free, the leg gave up at 868 of 1,260.

An unconfirmed refresh breaks the buy loop, so "unconfirmed" must not include "about to
restock by itself". The timer still does not GATE the refresh — the tap has already
happened — it only answers what comes after: is this shelf dead, or nearly full again?
"""

from __future__ import annotations

import unittest

from actions.buy_materials import _WAIT_OUT_RESTOCK_S, _timer_seconds


class TheRestockClockIsReadWithOcrsSeparators(unittest.TestCase):
    """'00:18.26', '00.28.40' and '00.00:11' are all real readings of this one field, so the
    separators carry no meaning and only the digit groups do."""

    def test_the_madeira_reading(self):
        self.assertEqual(_timer_seconds("00.00:11"), 11)

    def test_the_same_field_with_other_separators(self):
        self.assertEqual(_timer_seconds("00:18.26"), 18 * 60 + 26)
        self.assertEqual(_timer_seconds("00.28.40"), 28 * 60 + 40)
        self.assertEqual(_timer_seconds("00:01:30"), 90)

    def test_a_two_group_reading_is_minutes_and_seconds(self):
        self.assertEqual(_timer_seconds("1:05"), 65)

    def test_unreadable_is_None_not_zero(self):
        """Zero would mean 'restocking right now' and send the caller into a needless wait."""
        for text in (None, "", "xx", "1:2:3:4"):
            self.assertIsNone(_timer_seconds(text), repr(text))


class OnlyANearlyExpiredTimerIsWorthWaitingOut(unittest.TestCase):

    def test_the_live_case_is_inside_the_window(self):
        self.assertLessEqual(_timer_seconds("00.00:11"), _WAIT_OUT_RESTOCK_S)

    def test_a_full_timer_is_not(self):
        """18 minutes is what a blue gem is FOR — waiting it out would park the leg."""
        self.assertGreater(_timer_seconds("00:18.26"), _WAIT_OUT_RESTOCK_S)
        self.assertGreater(_timer_seconds("00.28.40"), _WAIT_OUT_RESTOCK_S)

    def test_the_window_stays_short(self):
        self.assertLessEqual(_WAIT_OUT_RESTOCK_S, 120,
                             "a leg must never park on a shelf waiting for a full timer")


if __name__ == "__main__":
    unittest.main()
