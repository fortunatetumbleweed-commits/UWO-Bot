"""Leaving a market with goods staged raises a dialog, and something must answer it.

LIVE 2026-08-31 at Bordeaux. The buy gave up mid-purchase — on an impossible `-543 Raisin`
reading — and left goods in the cart. The next leg needed the world map, so the dispatcher
dispatched EXIT_BUILDING, whose Back raised:

    "Moving to another menu will empty the cart. Continue?"   [Cancel] [Ok]

The next step's Back CANCELLED that dialog; the step after reopened it. Nine Backs, then the
stall guard ended the run.

The handler existed the whole time — `abandon_basket_confirm`, with the right dismissal
(`tap_ok`) and the right reasoning: answering Ok COMPLETES the exit we asked for, which is
CLAUDE.md's rule for a dialog the bot's own action raised. It simply carried keywords for
wording the game never uses — "abandon" and "basket", where the dialog says CART — and every
keyword must match, so it could never fire.
"""
import os
import unittest


class TheDialogIsRecognised(unittest.TestCase):
    STAGE = "tests/stage_suite/frames/bordeaux_cart_would_be_emptied.png"

    def _frame(self):
        if not os.path.exists(self.STAGE):
            self.skipTest("stage frame not available")
        from PIL import Image
        return Image.open(self.STAGE)

    def test_the_interruptor_fires_on_the_frame_that_wedged_the_run(self):
        from brain.perceive import _detect_interruptors
        from actions.sail_actions import _ocr_frame
        im = self._frame()
        found, obstruction = _detect_interruptors(im, list(_ocr_frame(im, min_conf=0.3)))
        self.assertIn("abandon_basket_confirm", found)
        self.assertIsNotNone(obstruction, "and it is seen as an obstruction, not a world")

    def test_it_is_answered_with_OK_not_dismissed(self):
        """A dialog raised by OUR OWN action is COMPLETED, never dismissed (CLAUDE.md). Back
        means leave; the dialog asks whether leaving may empty the cart; Ok is the answer."""
        from brain.fsm_registry import get_fsm_registry
        entry = get_fsm_registry().interruptors["abandon_basket_confirm"]
        self.assertEqual(getattr(entry, "dismissal", None), "tap_ok")


class TheKeywordsMatchTheGame(unittest.TestCase):
    def test_they_are_words_the_dialog_actually_contains(self):
        from brain.fsm_registry import get_fsm_registry
        kw = get_fsm_registry().interruptors["abandon_basket_confirm"].detection_keywords
        said = "moving to another menu will empty the cart. continue?"
        for word in kw:
            with self.subTest(keyword=word):
                self.assertIn(word.lower(), said,
                              "EVERY keyword must appear, so one absent word disables the entry")

    def test_the_old_assumed_wording_is_gone(self):
        from brain.fsm_registry import get_fsm_registry
        kw = [k.lower() for k in
              get_fsm_registry().interruptors["abandon_basket_confirm"].detection_keywords]
        self.assertNotIn("abandon", kw, "the game does not say 'abandon'")
        self.assertNotIn("basket", kw, "nor 'basket' — it says 'cart'")


if __name__ == "__main__":
    unittest.main()
