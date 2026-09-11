"""The card asking what to discard is a QUESTION, and the commit loop must not answer it.

Live 2026-09-07 at San Village, frame 222 of trace_barter_cmd_2026-09-07T15-20-16. The round
produced 379 Bambara Groundnut into a hold at 4,952/4,952 and the game asked:

    Insufficient Empty Space — "Cannot receive item due to insufficient space. Please
    organize your Cargo Hold. Unreceived trade goods will be discarded."
    Cargo: 223 water · 213 food · 1,812 Pig · 2,704 Groundnut          [Receive]

The bot tapped Receive, accepting what fit and throwing away 379 units of the product —
while 1,812 Pig sat there that NO remaining round could use, the Raisin having run out two
rounds earlier. Dumping those Pig is precisely the "safe to jettison" condition.

Nothing was broken in the handler that knows this. `village._on_overflow`, `_read_overflow`
and `_default_jettison` — the order of sacrifice — never ran, because the dialog never
reached the dispatcher. `barter_commit_verified` drives the round with

    commit_fn = lambda: commit_via_positive_taps(goal_keywords=["exchange"])

a six-tap loop that captures between taps; it walked Exchange -> Ok -> Receive and closed.
Grepping the whole run log for "overflow", "jettison", "discard" or "Insufficient" returns
nothing at all.

Two general faults let it through, neither of them about villages:

  * the goal-keyword filter GAVE UP instead of refusing — asked for `exchange` on a card
    that has no such button, it dropped the filter and took `commits[0]`, which was Receive;
  * with that fixed, the text search then reached the barter panel's own `Exchange` sitting
    BEHIND the card — and a tap outside a card is the gesture that dismisses it, destroying
    the reading before anyone can act on it.
"""
import os
import unittest


class TheCommitLoopLeavesItAlone(unittest.TestCase):
    STAGE = "tests/stage_suite/frames/san_overflow_pig_should_be_dumped.png"

    def _frame(self):
        if not os.path.exists(self.STAGE):
            self.skipTest("stage frame not available")
        from PIL import Image
        return Image.open(self.STAGE)

    def test_it_taps_nothing_on_the_card_it_was_not_asked_to_close(self):
        from brain.commit_actions import commit_via_positive_taps
        im = self._frame()
        taps = []
        commit_via_positive_taps(goal_keywords=["exchange"], settle_secs=0,
                                 capture_fn=lambda: im,
                                 tap_fn=lambda x, y: taps.append((x, y)))
        self.assertEqual(taps, [], "Receive discards the product; Exchange is behind the card")

    def test_the_goal_filter_refuses_rather_than_falling_through(self):
        from vision.omniparser import parse_fast_cached
        from brain.commit_actions import _yellow_commit_button
        im = self._frame()
        els = list(parse_fast_cached(im))
        self.assertIsNone(_yellow_commit_button(els, im, ["exchange"]),
                          "'Receive' matches neither 'exchange' nor a universal commit word")
        self.assertIsNotNone(_yellow_commit_button(els, im, None),
                             "without a stated goal the old behaviour is unchanged")


class TheCardIsStillReadable(unittest.TestCase):
    """Because the point of not tapping is that someone else gets to read it."""

    STAGE = "tests/stage_suite/frames/san_overflow_pig_should_be_dumped.png"

    def _frame(self):
        if not os.path.exists(self.STAGE):
            self.skipTest("stage frame not available")
        from PIL import Image
        return Image.open(self.STAGE)

    def test_the_pending_units_can_be_read_off_it(self):
        from vision.omniparser import parse_fast_cached
        from actions.overflow_dialog import read_overflow
        im = self._frame()
        ov = read_overflow(parse_fast_cached(im))
        self.assertIsNotNone(ov, "the overflow reader must recognise this card")


if __name__ == "__main__":
    unittest.main()


class AndTheVillageClaimsItRatherThanReceiving(unittest.TestCase):
    """Not tapping is only half of it — someone has to act on the card.

    `on_dialog` runs BEFORE the context is classified, and its single "will be discarded"
    test matched this card as well as the notice it was written for. It logged the notice's
    reasoning and handed back, and the default answer to a card whose only button is
    `Receive` is Receive. So the overflow context and `_on_overflow` behind it were
    unreachable even once the commit loop stopped swallowing the card.

    The two cards differ in furniture, not wording:

        NOTICE  "Complete the trade? ... has not been claimed yet."     [Cancel] [Ok]
        CARD    "Insufficient Empty Space ... Please organize your Cargo Hold."
                Received: 379 · Cargo 4,952/4,952 · water/food/Pig/Groundnut   [Receive]
    """
    STAGE = "tests/stage_suite/frames/san_overflow_pig_should_be_dumped.png"

    def _frame(self):
        if not os.path.exists(self.STAGE):
            self.skipTest("stage frame not available")
        from PIL import Image
        return Image.open(self.STAGE)

    def _dialog_for(self, im):
        from vision.omniparser import parse_fast_cached
        from vision.region_detectors.dialog import detect_dialog
        els = list(parse_fast_cached(im))
        return detect_dialog(els, im.width, im.height, frame=im)

    def test_the_village_context_calls_it_the_overflow_prompt(self):
        from brain.village_context import OVERFLOW_PROMPT, classify
        self.assertEqual(classify(self._frame()), OVERFLOW_PROMPT)

    def test_on_dialog_claims_it_and_jettisons_instead_of_receiving(self):
        from brain.activities.village import Barter, VillageActivity
        im = self._frame()
        jettisoned = []
        act = VillageActivity(overflow_fn=lambda *a: 379,
                              jettison_fn=lambda pending: jettisoned.append(pending),
                              recipe_fn=lambda good: {"Pig": 3},
                              saw_fn=lambda: {})
        act.on_tick_frame(im)
        res = act.on_dialog(self._dialog_for(im),
                            Barter(good="Bambara Groundnut", village="San Village"))
        self.assertIsNotNone(res, "an unclaimed card falls through to a blind Receive")
        self.assertEqual(jettisoned, [379], "the pending units are what it frees space for")

    def test_the_discard_NOTICE_is_still_left_to_the_default(self):
        """It has nothing to organise — the goods are gone either way, so Ok is right."""
        import types
        from brain.activities.village import Barter, VillageActivity
        notice = types.SimpleNamespace(is_modal=True, body_text=(
            "Notice", "Complete the trade? 486 Bambara Groundnut has not been claimed yet.",
            "Unclaimed trade goods will be discarded.", "Cancel", "Ok"))
        act = VillageActivity(saw_fn=lambda: {})
        self.assertIsNone(act.on_dialog(notice, Barter(good="Bambara Groundnut",
                                                       village="San Village")))
