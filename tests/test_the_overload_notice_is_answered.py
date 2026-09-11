"""The trade-goods overload Notice is answered, not walked away from.

Live 2026-09-04 at Madeira, frame 272 of trace_barter_cmd_2026-09-04T17-35-01:

    Notice
    The Cargo Hold's Trade Goods slot will be exceeded by 52 slots
    Purchase the Trade Goods?                              [Cancel]  [OK]

`_react_after_purchase` dispatches on "negotiat", "confirm", "result" and "balance". The
Notice says none of them, so the loop broke on its first pass, `purchase_goods` returned
ok with purchased=False, and the caller left the market through the chromed title with the
dialog still up. 105 slots were free and nothing was bought.

The older market_actions flow has had an "Overload backstop" for this since the
London-Amsterdam trace; the newer buy path simply never got one.

It is an acknowledgement, not a decision: the game hands back what fits, and no gems are
involved — so OK (user, 2026-09-04: "for this one you can tap the Ok button").
"""
from __future__ import annotations

import unittest
from unittest import mock

from actions.buy_materials import _react_after_purchase

NOTICE = ("Notice The Cargo Hold's Trade Goods slot will be exceeded by 52 slots "
          "Purchase the Trade Goods? Cancel OK")
CONFIRM = "Confirm Purchase Total 62,265 Cancel OK"
NEGOTIATE = "Attempt Negotiation? Yes No"
MARKET = "Purchase Madeira Wine Sugar Keris Raisin Put In Bulk Sell Supplies"

OK_AT = (1307, 826)


def _run(screens):
    """Drive the reactor over a scripted sequence of screen texts."""
    seq, taps = list(screens), []

    def _tokens(_frame, min_conf=0.3):
        text = seq.pop(0) if seq else MARKET
        return [(w, 1.0, 0, 0) for w in text.split()]

    def _button(_toks, want, min_ratio=0.85):
        return OK_AT if want == "ok" else (900, 826)

    with mock.patch("actions.sail_actions._ocr_frame", side_effect=_tokens), \
         mock.patch("actions.route_execution.find_text_button", side_effect=_button), \
         mock.patch("time.sleep", lambda *_a, **_k: None):
        confirmed = _react_after_purchase(lambda: object(),
                                          lambda x, y: taps.append((x, y)))
    return confirmed, taps


class TheNoticeIsAnswered(unittest.TestCase):

    def test_the_frame_272_notice_gets_an_OK(self):
        confirmed, taps = _run([NOTICE, MARKET])
        self.assertTrue(confirmed, "the purchase was abandoned with the dialog still up")
        self.assertIn(OK_AT, taps)

    def test_the_chain_continues_through_the_confirm_behind_it(self):
        """The Notice arrives with a Confirm behind it; answering one must not stop the
        reactor before the other."""
        confirmed, taps = _run([NOTICE, CONFIRM, MARKET])
        self.assertTrue(confirmed)
        self.assertEqual(taps.count(OK_AT), 2)

    def test_a_negotiation_popup_is_still_declined(self):
        confirmed, taps = _run([NEGOTIATE, CONFIRM, MARKET])
        self.assertIn((900, 826), taps, "the negotiation popup takes No, not OK")


class ItStillAnswersOnlyWhatItCanNAME(unittest.TestCase):
    """A Notice is a SHAPE, not a meaning. Matching the bare word would make this a blind OK
    on every notice the game raises."""

    def test_a_plain_market_screen_is_not_a_dialog(self):
        confirmed, taps = _run([MARKET])
        self.assertFalse(confirmed)
        self.assertEqual(taps, [])

    def test_an_unrelated_notice_is_left_alone(self):
        confirmed, taps = _run(["Notice Your ship has been damaged Cancel OK"])
        self.assertFalse(confirmed)
        self.assertEqual(taps, [])

    def test_BOTH_phrases_are_required(self):
        for text in ("Notice exceeded by 52 slots Cancel OK",
                     "Notice Purchase the Trade Goods? Cancel OK"):
            with self.subTest(text=text):
                confirmed, taps = _run([text])
                self.assertFalse(confirmed)
                self.assertEqual(taps, [])


if __name__ == "__main__":
    unittest.main()
