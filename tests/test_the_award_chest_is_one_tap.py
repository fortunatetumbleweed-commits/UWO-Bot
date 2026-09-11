"""Claiming the Trade Point award is ONE TAP and a hand-back.

`get_trade_point_award` wrapped the claim in its own perceive loop: tap the chest, then up
to three captures two seconds apart waiting for the counter to fall below 1,000, and if it
had not, tap the chest a second time and watch again — six captures and twelve seconds
inside one tick.

The loop's own test is what it cannot pass: the claim opens a reward dialog, and that dialog
sits on top of the counter. So a claim that WORKED reads, from inside the loop, exactly like
one that did nothing — and the answer it draws is to tap the chest again, through the dialog.

The dispatcher perceives every tick and already has a handler for the dialog. So the
primitive says what it saw and taps once; the next tick reports what came of it.
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

from actions.market_actions import tap_the_award_chest


def _counter(points, x2=1200, y1=100, y2=140):
    return points, types.SimpleNamespace(x2=x2, y1=y1, y2=y2)


class OneTapAndNoWatching(unittest.TestCase):

    def _run(self, reading):
        taps = []
        with mock.patch("actions.market_actions._read_trade_points", return_value=reading):
            pts = tap_the_award_chest(object(), lambda x, y: taps.append((x, y)),
                                      lambda _f: [])
        return pts, taps

    def test_a_pending_award_is_tapped_once(self):
        pts, taps = self._run(_counter(1240))
        self.assertEqual(pts, 1240)
        self.assertEqual(len(taps), 1, "a second tap would land on the reward dialog")

    def test_the_chest_sits_at_the_right_end_of_the_counter_row(self):
        _pts, taps = self._run(_counter(1000, x2=1200, y1=100, y2=140))
        self.assertEqual(taps, [(1151, 120)])

    def test_below_a_thousand_nothing_is_claimed_and_nothing_is_tapped(self):
        pts, taps = self._run(_counter(999))
        self.assertIsNone(pts)
        self.assertEqual(taps, [])

    def test_an_invisible_counter_is_not_a_claim(self):
        pts, taps = self._run((None, None))
        self.assertIsNone(pts)
        self.assertEqual(taps, [])


class TheClaimIsActuallyREACHABLE(unittest.TestCase):
    """It sat below the scroll branch, and the normal clear never gets there.

    The claim needed `scrolled_pages` spent AND the previous tick not to have been a scroll.
    A clear ends the other way round: scroll, look, nothing sellable, `last_intent ==
    "scrolled"`, finished — returning before the claim. Live 2026-09-07 at London the counter
    read 5,441/1,000 with FIVE awards pending and the chest was never tapped; the run before,
    13,894 with thirteen.
    """

    def _finish(self, *, last_intent, scrolled_pages, points=1240):
        import types
        from unittest import mock
        from brain.activities.market_sell import on_sell_page
        from brain.market_state import MarketState
        st = MarketState()
        st.last_intent, st.scrolled_pages = last_intent, scrolled_pages
        taps = []
        goal = types.SimpleNamespace(keep=(), exclude=())
        with mock.patch("actions.sell_goods._sell_page", return_value=[]), \
             mock.patch("actions.sell_goods._find_sell_commit", return_value=None), \
             mock.patch("actions.market_actions.tap_the_award_chest",
                        side_effect=lambda *a: taps.append(1) or points):
            out = on_sell_page(st, goal, frame=object(), capture_fn=lambda: object(),
                               tap_fn=lambda *a: None, omni_fn=lambda _f: [])
        return out, taps

    def test_the_scroll_ending_claims_it(self):
        """The path a real clear actually takes."""
        out, taps = self._finish(last_intent="scrolled", scrolled_pages=1)
        self.assertEqual(taps, [1], "the chest was never tapped on the normal ending")
        self.assertEqual(out["do"], "waited")

    def test_the_pages_spent_ending_claims_it_too(self):
        out, taps = self._finish(last_intent=None, scrolled_pages=99)
        self.assertEqual(taps, [1])

    def test_nothing_pending_still_finishes(self):
        out, _taps = self._finish(last_intent="scrolled", scrolled_pages=1, points=None)
        self.assertEqual(out["do"], "finished")

    def test_a_page_still_worth_scrolling_does_not_claim_yet(self):
        _out, taps = self._finish(last_intent=None, scrolled_pages=0)
        self.assertEqual(taps, [], "the clear is not over")


class TheSellPageClaimsAtMostOncePerVisit(unittest.TestCase):
    """Because the second look would be at a dialog, not at the counter."""

    def test_the_visit_remembers_the_attempt(self):
        from brain.market_state import MarketState
        st = MarketState()
        self.assertFalse(st.award_claimed)
        st.award_claimed = True
        self.assertTrue(st.for_goal(st.key).award_claimed,
                        "the same visit must not go back for a second tap")

    def test_a_new_port_gets_a_fresh_chance_at_the_chest(self):
        from brain.market_state import MarketState
        st = MarketState(key=("Hold", "Faro"))
        st.award_claimed = True
        self.assertFalse(st.for_goal(("Hold", "Madeira")).award_claimed)


if __name__ == "__main__":
    unittest.main()
