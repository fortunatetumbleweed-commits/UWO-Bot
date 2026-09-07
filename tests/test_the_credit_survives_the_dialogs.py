"""What was bought must survive the cards the buying itself raises.

Units bought are not in the result card — it reports MONEY — so they come from the SHELF
DROP: the tile's stock before the purchase, less after. That "before" has to cross the ticks
in between, and between tapping Purchase and coming back to the grid the game shows a confirm
and a result, each answered by a handler that records what IT did.

It used to ride on `last_intent`, a single slot describing the PREVIOUS TICK, so every one of
those handlers overwrote it and the credit never ran.

Live 2026-09-07 at Faro: the ledger seeded `pig: 729`, ~914 Pig were bought over the next four
minutes, and the hold panel read 1,643 while the ledger still said 729 — already past its
1,505 target and still staging a SOLD OUT tile. Masked until then by a separate bug that
re-seeded the ledger on nearly every tick; fixing that exposed this.
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

import brain.market_context as ctx
from brain.activities.market import Hold, MarketActivity
from brain.activities.market_buy import on_purchase_page
from brain.market_ledger import MarketLedger
from brain.market_state import MarketState


class Tile:
    def __init__(self, name, qty, sold_out=False, active=True):
        self.name, self.available_qty = name, qty
        self.sold_out, self.is_active = sold_out, active
        self.season = None


def _buy_tick(state, tiles):
    goal = Hold(orders={"Pig": 1505})
    with mock.patch("vision.market_reader.read_market_page_omni", return_value=tiles), \
         mock.patch("actions.buy_materials._find_purchase_commit", return_value=None), \
         mock.patch("memory.market_kb.note_season"), \
         mock.patch("actions.buy_materials.refresh_market",
                    return_value={"ok": False, "acted": True}):
        return on_purchase_page(state, goal, "Faro", frame=object(),
                                capture_fn=lambda: object(), tap_fn=lambda *a: None,
                                omni_fn=lambda _f: [])


class TheShelfDropIsCreditedAfterTheCards(unittest.TestCase):

    def _state(self):
        st = MarketState(ledger=MarketLedger())
        st.ledger.seed({"pig": 729})
        st.awaiting_credit = (("pig", 457),)      # the shelf when Purchase was tapped
        return st

    def test_the_purchase_is_credited(self):
        st = self._state()
        _buy_tick(st, [Tile("Pig", 0, sold_out=True, active=False)])
        self.assertEqual(st.ledger.believed("Pig"), 729 + 457)

    def test_A_DIALOG_IN_BETWEEN_DOES_NOT_LOSE_IT(self):
        """The confirm and result cards are ticks of their own, and each records what it did.

        This is the failure itself: `did()` is the generic slot, and it MUST NOT carry the
        purchase across them."""
        st = self._state()
        st.did("answered a dialog")               # the confirm card
        st.did("cleared the result dialog")       # the result card
        _buy_tick(st, [Tile("Pig", 0, sold_out=True, active=False)])
        self.assertEqual(st.ledger.believed("Pig"), 729 + 457)

    def test_the_slot_is_spent_once(self):
        """A second look must not credit the same purchase twice."""
        st = self._state()
        _buy_tick(st, [Tile("Pig", 0, sold_out=True, active=False)])
        _buy_tick(st, [Tile("Pig", 0, sold_out=True, active=False)])
        self.assertEqual(st.ledger.believed("Pig"), 729 + 457)
        self.assertIsNone(st.awaiting_credit)

    def test_nothing_pending_credits_nothing(self):
        st = MarketState(ledger=MarketLedger())
        st.ledger.seed({"pig": 729})
        _buy_tick(st, [Tile("Pig", 457)])
        self.assertEqual(st.ledger.believed("Pig"), 729)


class TheDialogHandlersStillRecordWhatTheyDid(unittest.TestCase):
    """`did()` keeps its job — it is only no longer the purchase's carrier."""

    def test_a_dialog_handler_does_not_clear_the_pending_credit(self):
        act = MarketActivity(context_fn=lambda _f: ctx.RESULT_DIALOG,
                             capture_fn=lambda: object(), tap_fn=lambda *a: None,
                             omni_fn=lambda _f: [])
        # Keyed to THIS visit, or `for_goal` rightly hands back a fresh state.
        act._state = MarketState(key=("Hold", "Faro"),
                                 awaiting_credit=(("pig", 457),))
        with mock.patch("brain.commit_actions.tap_one_positive", return_value=True), \
             mock.patch.object(MarketActivity, "_read_result_goods", return_value=[]), \
             mock.patch.object(MarketActivity, "_port_name", return_value="Faro"):
            act.work(Hold(orders={"Pig": 1505}),
                     types.SimpleNamespace(state="building:market", port="Faro",
                                           frame=object()))
        self.assertEqual(act._state.awaiting_credit, (("pig", 457),))


if __name__ == "__main__":
    unittest.main()
