"""A SELL result card reports MONEY, not goods — so the names come from staging.

Live 2026-09-07 at London, frame 273 of trace_barter_cmd_2026-09-07T15-20-16. The card that
closed the mission's whole earning leg:

    Result
      Mate Trade EXP    920,026        Sales Cost   98,211,984
      Contribution            0        Tax          -7,000,656
      Obtained Trade Point 12,985      Surcharge    31,425,888
      Trade Fame              0        Profit      109,703,984
                                       Total Amount 122,637,216
                                       Balance  69,673,220,617                    [OK]

Not one good is named on it. `_read_result_goods` looks for a goods grid that is not there,
so `state.sold` stayed `[]` while 4,382 Bambara Groundnut left the hold for 122.6M ducats,
and every result line the mission logged said `'sold': []`.

That is worse than a cosmetic lie. `market_sell.on_sell_page` refuses to call a clear
finished while `held and not state.sold` — the guard added after Lisboa on 2026-09-06, where
a mission reported SUCCESS still holding the 3,668 units it had sailed there to sell. A
`sold` that can never fill is a guard that can never fire; it survived this run only because
the one thing left aboard was in `exclude`, and so counted as kept rather than declined.

The rule that `sold` is what a RESULT DIALOG confirmed still holds. The card remains the only
thing that authorises an entry — being in `_on_result` means the game has confirmed the
transaction. Staging merely supplies the name the card omits.
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

from brain.market_state import MarketState


class NothingEntersSoldWithoutACard(unittest.TestCase):

    def test_staging_alone_records_nothing_as_sold(self):
        st = MarketState()
        st.sold_pending.append("Bambara Groundnut")
        self.assertEqual(st.sold, [], "staged is not sold — the tap may not even have landed")


class TheCardPromotesWhatWasStaged(unittest.TestCase):

    def _act(self, state):
        from brain.activities.market import MarketActivity
        act = MarketActivity(capture_fn=lambda: object(), tap_fn=lambda *a: None,
                             omni_fn=lambda _f: [])
        act._state = state
        return act

    def _on_result(self, act, *, names=()):
        from brain.activities.market import SellHold
        with mock.patch.object(type(act), "_read_result_goods", return_value=list(names)), \
             mock.patch("brain.commit_actions.tap_one_positive", return_value=True):
            return act._on_result(SellHold(exclude=()), "London")

    def test_the_staged_name_lands_in_sold(self):
        st = MarketState()
        st.sold_pending.append("Bambara Groundnut")
        act = self._act(st)
        self._on_result(act)
        self.assertEqual(st.sold, ["Bambara Groundnut"])

    def test_and_the_pending_list_is_emptied(self):
        """A second card must not re-record the first card's sale."""
        st = MarketState()
        st.sold_pending.append("Bambara Groundnut")
        act = self._act(st)
        self._on_result(act)
        self.assertEqual(st.sold_pending, [])
        self._on_result(act)
        self.assertEqual(st.sold, ["Bambara Groundnut"], "recorded once, not twice")

    def test_a_card_that_DOES_name_goods_is_still_believed(self):
        """The buy side's cards carry them; nothing about that changes."""
        st = MarketState()
        act = self._act(st)
        self._on_result(act, names=["Iron"])
        self.assertEqual(st.sold, ["Iron"])


class TheGuardCanNowFire(TheCardPromotesWhatWasStaged):
    """The whole point: `held and not state.sold` has to be answerable.

    Before this, `sold` was empty on every sale, so the Lisboa guard could never distinguish
    "sold nothing and still holding it" from "sold it all".
    """

    def test_a_sale_makes_sold_non_empty(self):
        st = MarketState()
        st.sold_pending.append("Bambara Groundnut")
        self._on_result(self._act(st))
        self.assertTrue(st.sold, "a mission that sold something can now say so")


if __name__ == "__main__":
    unittest.main()
