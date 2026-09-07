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
        # (the shelf when Purchase was tapped, the hold's total then)
        st.awaiting_credit = ((("pig", 457),), None)
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
                                 awaiting_credit=((("pig", 457),), None))
        with mock.patch("brain.commit_actions.tap_one_positive", return_value=True), \
             mock.patch.object(MarketActivity, "_read_result_goods", return_value=[]), \
             mock.patch.object(MarketActivity, "_port_name", return_value="Faro"):
            act.work(Hold(orders={"Pig": 1505}),
                     types.SimpleNamespace(state="building:market", port="Faro",
                                           frame=object()))
        self.assertEqual(act._state.awaiting_credit, ((("pig", 457),), None))


if __name__ == "__main__":
    unittest.main()


class ABeliefKnownToBeStaleIsReRead(unittest.TestCase):
    """The drop is uncomputable exactly when it matters most.

    A bought-out shelf reports `available_qty=None` — measured on the Faro grid — which
    `shelf_signature` records as -1 and `credit_the_shelf_drop` rightly refuses to read as
    zero ("unread is not zero"). So the one purchase that EMPTIES a shelf teaches nothing.

    Live 2026-09-07: Pig was seeded at 1,505 and stayed 1,505 for twenty minutes and a dozen
    purchases across two ports — every buy emptied the shelf, every credit was skipped. It
    read short against its 1,755 target the whole time, kept buying, and filled the hold to
    4,952/4,952 with Pig, leaving no room for the Raisin that gates the barter.

    Keeping a count we KNOW is stale is worse than having none: the next tick re-reads the
    hold from the sell grid, which is authoritative.
    """

    def _state(self, before):
        st = MarketState(ledger=MarketLedger())
        st.ledger.seed({"pig": 1505})
        st.awaiting_credit = (before, None)
        return st

    def test_an_unreadable_shelf_drops_the_ledger_for_a_re_read(self):
        st = self._state((("pig", 457),))
        _buy_tick(st, [Tile("Pig", None, sold_out=True, active=False)])
        self.assertIsNone(st.ledger, "kept a count it knew was stale")

    def test_a_READABLE_drop_keeps_the_ledger(self):
        st = self._state((("pig", 457),))
        _buy_tick(st, [Tile("Pig", 0)])
        self.assertIsNotNone(st.ledger)
        self.assertEqual(st.ledger.believed("Pig"), 1505 + 457)

    def test_the_pending_credit_is_spent_either_way(self):
        st = self._state((("pig", 457),))
        _buy_tick(st, [Tile("Pig", None, sold_out=True, active=False)])
        self.assertIsNone(st.awaiting_credit)


class TheCargoRiseSaysWhatTheShelfCannot(unittest.TestCase):
    """A sold-out shelf reads UNREADABLE, not zero — rightly, since the reader genuinely
    failed — so the one purchase that empties a shelf teaches nothing.

    Live 2026-09-07 that was every purchase: Pig sat at its seeded 1,505 for twenty minutes
    and a dozen buys across two ports, read short against 1,755 the whole time, and filled the
    hold to 4,952/4,952 while the Raisin that gates the barter had nowhere to go.

    The cargo total is on the SAME screen and rises with every purchase (user: "it should be
    reading the pigs now in the cargo, it increases after every purchase").
    """

    def _state(self, cargo_before):
        st = MarketState(ledger=MarketLedger())
        st.ledger.seed({"pig": 1505})
        st.awaiting_credit = ((("pig", 457),), cargo_before)
        return st

    def _tick(self, st, total_now, tiles=None):
        with mock.patch("brain.activities.market_buy._safe_total", return_value=total_now):
            return _buy_tick(st, tiles or [Tile("Pig", None, sold_out=True, active=False)])

    def test_the_rise_is_credited_when_the_shelf_cannot_be_read(self):
        st = self._state(2340)
        self._tick(st, 2797)
        self.assertEqual(st.ledger.believed("Pig"), 1505 + 457)

    def test_the_SHELF_still_wins_when_it_can_be_read(self):
        """Two sources, and the direct one is preferred — the cargo total is an aggregate."""
        st = self._state(2340)
        self._tick(st, 9999, tiles=[Tile("Pig", 0)])
        self.assertEqual(st.ledger.believed("Pig"), 1505 + 457)

    def test_TWO_GOODS_HERE_MAKE_THE_AGGREGATE_MEANINGLESS(self):
        """It names no good; with two on the order it says nothing about either."""
        st = MarketState(ledger=MarketLedger())
        st.ledger.seed({"pig": 1505, "raisin": 100})
        st.awaiting_credit = ((("pig", 457),), 2340)
        goal = Hold(orders={"Pig": 1755, "Raisin": 1997})
        with mock.patch("brain.activities.market_buy._safe_total", return_value=2797), \
             mock.patch("vision.market_reader.read_market_page_omni",
                        return_value=[Tile("Pig", None, sold_out=True, active=False),
                                      Tile("Raisin", 300)]), \
             mock.patch("actions.buy_materials._find_purchase_commit", return_value=None), \
             mock.patch("memory.market_kb.note_season"), \
             mock.patch("actions.buy_materials.refresh_market",
                        return_value={"ok": False, "acted": True}):
            on_purchase_page(st, goal, "Faro", frame=object(), capture_fn=lambda: object(),
                             tap_fn=lambda *a: None, omni_fn=lambda _f: [])
        self.assertIsNone(st.ledger, "guessed which good an aggregate belonged to")

    def test_a_hold_that_did_not_rise_credits_nothing(self):
        st = self._state(2340)
        self._tick(st, 2340)
        self.assertIsNone(st.ledger, "nothing learned, so the belief is re-read")

    def test_an_unreadable_total_falls_through_to_the_re_read(self):
        st = self._state(None)
        self._tick(st, None)
        self.assertIsNone(st.ledger)
