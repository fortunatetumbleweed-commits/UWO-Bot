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
