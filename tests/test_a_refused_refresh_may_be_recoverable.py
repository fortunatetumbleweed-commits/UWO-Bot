"""A refusal that spent nothing is about THIS LOOK, not about the shelf.

`refresh_market` refuses three ways and they are not equivalent:

    no restock control          nothing tapped, no gem spent   -> look again
    cost is not a blue gem      control found, price is wrong  -> never retry (real money)
    refresh NOT confirmed       the tap WENT IN, gem may be gone -> never a free retry

The caller collapsed all three into "finished". Live 2026-09-06 at Madeira, ten refreshes had
already succeeded in that same leg — so the shelf was plainly refreshable — when a late
re-seed switched to the Sell tab and its switch-back tap did not land. Frame 382 of
trace_barter_cmd_2026-09-06T21-45-01 shows the Sell panel at the moment of the refusal:

    'Raisin' is sold out and still wanted here — restocking
    no refresh for 'Raisin' — no restock control (market fresh or not on Purchase grid)

The leg ended with Raisin at 1,100 of 1,755, capping the barter at 6 rounds instead of 7 and
leaving 729 Pig unused. One dropped tap, read as a fact about the world.
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

from brain.activities.market import Hold
from brain.activities.market_buy import _MAX_RESTOCK_LOOKS, on_purchase_page
from brain.market_ledger import MarketLedger
from brain.market_state import MarketState


class Tile:
    def __init__(self, name, qty, sold_out=False, active=True):
        self.name, self.available_qty = name, qty
        self.sold_out, self.is_active = sold_out, active


SOLD_OUT = {"raisin": Tile("Raisin", 0, sold_out=True, active=False)}


def _run(state, refresh_result):
    goal = Hold(orders={"Raisin": 1755})
    # THE SEASON KB IS PRODUCTION STATE, and a live run writes to it — "Madeira raisin low"
    # was recorded on 2026-09-07 and made this file pass alone and fail in the suite, because
    # a scarce season stops the refreshes before this test's own bound is reached. A test must
    # not read what the bot writes (CLAUDE.md: "Reset session state you depend on").
    with mock.patch("vision.market_reader.read_market_page_omni",
                    return_value=list(SOLD_OUT.values())), \
         mock.patch("actions.buy_materials._find_purchase_commit", return_value=None), \
         mock.patch("memory.market_kb.season_of", return_value=None), \
         mock.patch("memory.market_kb.note_season"), \
         mock.patch("actions.buy_materials.refresh_market", return_value=refresh_result):
        return on_purchase_page(state, goal, "Madeira", frame=object(),
                                capture_fn=lambda: object(), tap_fn=lambda *a: None,
                                omni_fn=lambda _f: [])


def _state():
    st = MarketState(ledger=MarketLedger())
    st.ledger.seed({"raisin": 1100})
    return st


class ARefusalThatSpentNothingIsRetried(unittest.TestCase):

    def test_the_control_not_being_on_screen_hands_back(self):
        out = _run(_state(), {"ok": False, "acted": False, "reason": "no restock control"})
        self.assertEqual(out["do"], "waited")

    def test_it_is_bounded_and_then_reports(self):
        """A shelf with no control at all is a fact; three looks tells them apart."""
        st = _state()
        for _ in range(_MAX_RESTOCK_LOOKS):
            self.assertEqual(_run(st, {"ok": False, "acted": False})["do"], "waited")
        self.assertEqual(_run(st, {"ok": False, "acted": False})["do"], "finished")

    def test_a_successful_refresh_forgets_the_looks(self):
        """A control that responded resets the budget — the next dry spell gets its own."""
        st = _state()
        _run(st, {"ok": False, "acted": False})
        _run(st, {"ok": True, "acted": True})
        self.assertNotIn("restock_look", st.attempts)


class ARefusalTHATACTEDStillEndsTheLeg(unittest.TestCase):

    def test_a_red_gem_price_is_never_looked_at_twice(self):
        """Red gems are real money, and looking again cannot change the price."""
        out = _run(_state(), {"ok": False, "acted": True, "refused": True,
                              "currency": "red_gem", "reason": "not a blue gem — refused"})
        self.assertEqual(out["do"], "finished")

    def test_a_tap_that_went_in_is_not_retried(self):
        """The gem may already be spent, whatever the verification says."""
        out = _run(_state(), {"ok": False, "acted": True, "reason": "refresh NOT confirmed"})
        self.assertEqual(out["do"], "finished")


if __name__ == "__main__":
    unittest.main()
