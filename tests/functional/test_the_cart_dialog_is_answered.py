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


class TheSameDialogRaisedByATabSwitch(unittest.TestCase):
    """LIVE 2026-09-07 at Faro. Same card, a different way in, and a different failure.

    Bordeaux raised it with a Back. Here `ensure_sell_tab` tapped 'Sell' with 454 Pig staged
    and the hold 52 units over capacity, and the game asked the same question. Frames 8-11 of
    trace_barter_cmd_2026-09-07T15-00-05, four times over:

        frame 8   tap (174,276) on the Sell rail item
        frame 9   the tap LANDED — title 'Sell', the Notice over it       <- THIS FRAME
        frame 10  `_on_sell_tab` said no, so it logged "the tap was dropped" and re-tapped
                  the rail at (68,276)
        frame 11  back on Purchase, no dialog, cart intact

    The card was never undetectable. The retry tap is OUTSIDE the modal, so it dismissed the
    card as a Cancel — and the dispatcher, which re-perceived between calls, found nothing
    left to see. What was missing was not a reader but the chance to use one.
    """
    STAGE = "tests/stage_suite/frames/faro_cart_confirm_on_tab_switch.png"

    def _frame(self):
        if not os.path.exists(self.STAGE):
            self.skipTest("stage frame not available")
        from PIL import Image
        return Image.open(self.STAGE)

    def _read(self):
        from vision.omniparser import parse_fast_cached
        from vision.region_detectors.dialog import detect_dialog
        im = self._frame()
        els = list(parse_fast_cached(im))
        return im, els, detect_dialog(els, im.width, im.height, frame=im)

    def test_the_dispatcher_classifies_it(self):
        import brain.market_context as ctx
        im, _els, _d = self._read()
        self.assertEqual(ctx.classify(im), ctx.CONFIRM_DIALOG)

    def test_the_card_offers_Ok_and_Cancel(self):
        _im, _els, d = self._read()
        self.assertEqual([getattr(a, "label", a) for a in (d.actions or [])],
                         ["Ok", "Cancel"])

    def test_the_rule_answers_Ok(self):
        """Ok empties the cart and completes the switch — and we are here to TRIM, with the
        hold already over capacity, so the cart is the thing we do not want."""
        from brain.game_rules import answer_dialog
        _im, _els, d = self._read()
        opts = [getattr(a, "label", a) for a in (d.actions or [])]
        self.assertEqual(answer_dialog(opts, d.body_text), "Ok")

    def test_the_market_activity_claims_it_and_taps_Ok_inside_the_card(self):
        from unittest import mock
        from brain.activities.market import FreeHold, MarketActivity
        im, els, d = self._read()
        taps = []
        act = MarketActivity(capture_fn=lambda: im, tap_fn=lambda x, y: taps.append((x, y)),
                             omni_fn=lambda _f: els)
        with mock.patch.object(MarketActivity, "_port_name", return_value="Faro"):
            claimed = act.on_dialog(d, FreeHold(keep=("Water", "Food", "Pig", "Raisin")))
        self.assertIsNotNone(claimed, "the game rules must not get this card first")
        self.assertEqual(len(taps), 1)
        x, y = taps[0]
        x0, y0, x1, y1 = d.bbox
        self.assertTrue(x0 <= x <= x1 and y0 <= y <= y1,
                        f"tapped {taps[0]} outside the card {d.bbox} — which is what a tap "
                        "outside does: it CANCELS, and the evidence is gone")


class TheSellTabHandsBackRatherThanTappingAgain(unittest.TestCase):
    """The retry is what destroyed the card, so the primitive taps once and reports."""

    def test_it_taps_the_menu_item_exactly_once(self):
        from unittest import mock
        from PIL import Image
        from actions.buy_materials import ensure_sell_tab
        taps = []
        blank = Image.new("RGB", (2400, 1080))
        with mock.patch("actions.buy_materials._on_sell_tab", return_value=False), \
             mock.patch("actions.buy_materials._sell_menu_item", return_value=(65, 274)):
            ok = ensure_sell_tab(lambda: blank, lambda *a: taps.append(a), 0.0)
        self.assertFalse(ok)
        self.assertEqual(taps, [(65, 274)])
