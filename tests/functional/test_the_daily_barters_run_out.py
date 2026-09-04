"""The day's barter rounds are spent — the game says so in TWO different ways.

LIVE 2026-08-30, Svear Village. Run 12 had spent every round the night before, reaching amity
100,000/100,000. Run 13 came back, tapped Barter, and the game raised a Notice instead of the
panel. `_open_barter_panel` only knew the OTHER refusal — a red "Unavailable" ribbon on the
menu item, read BEFORE the tap — so it reported "the panel would not open", the activity
returned UNRECOGNISED, and the dispatcher tried again. Tap, notice, dismiss, tap, for as long
as it was allowed to.

Both refusals mean the same thing and the handling for it already existed: `"unavailable"` ->
`_done("the village's barters for today are used up")`. Only the second detector was missing.
"""
import os
import unittest

from actions.barter_panel import daily_barters_used_up


class _El:
    def __init__(self, label): self.label = label


class TheNoticeIsRead(unittest.TestCase):
    def test_the_sentence_the_game_actually_showed(self):
        # OmniParser reads it as one label, countdown and all.
        els = [_El("Notice"), _El("You have used all your daily Trade Count:"),
               _El("17.3348"), _El("OK")]
        self.assertTrue(daily_barters_used_up(elements=els))

    def test_an_ordinary_village_screen_is_not_a_refusal(self):
        els = [_El("Amity Effect"), _El("Discovery Chance 15%"), _El("Barter"),
               _El("Village Stock"), _El("Increase Barter Count by 4")]
        self.assertFalse(daily_barters_used_up(elements=els),
                         "'Increase Barter Count by 4' must not read as a refusal")

    def test_no_frame_and_no_elements_is_not_a_refusal(self):
        self.assertFalse(daily_barters_used_up())


class AgainstTheRealFrame(unittest.TestCase):
    STAGE = "tests/stage_suite/frames/village_daily_trade_count_spent.png"

    def test_the_frame_that_looped_run_13(self):
        if not os.path.exists(self.STAGE):
            self.skipTest("stage frame not available")
        from PIL import Image
        self.assertTrue(daily_barters_used_up(Image.open(self.STAGE)),
                        "the frame the bot saw four times without understanding it")


if __name__ == "__main__":
    unittest.main()
