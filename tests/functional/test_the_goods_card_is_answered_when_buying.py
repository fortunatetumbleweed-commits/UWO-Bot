"""`Put In Bulk` off turns one tap into a card, and the buy had no answer for it.

Live 2026-09-08 at Jakarta (frame 28 of trace_barter_cmd_2026-09-08T10-31-17). The trim ran
first, found nothing to trim, and so never touched the checkbox — and nothing else had. The
first Ebony tap opened the per-item card instead of loading the shelf:

    Trade Goods Info | Ebony | 1/158 | [Cancel] [Load]      with Put In Bulk UNCHECKED

    obstruction: kind='dialog' ... actions=['Cancel']
    market does not own this confirmation dialog
    no rule and no positive option among ['Cancel'] — leaving it alone
    gather:Jakarta: a confirmation dialog nobody will answer

Everything downstream behaved: `game_rules` refused to press a lone `Cancel` rather than
guessing, and the runner reported instead of flailing. What was missing was an owner.

The handling is not new (user, 2026-09-08: "This dialog we should have code to handle it, it
was supported for sure"). `_sell_one_good` waits for this card and taps `Max`;
`_buy_load_one_good` types a quantity and taps `Load`. Both live on the old `buy_goods` /
`sell_goods` flows, which the mission stopped using — the dispatcher path never inherited
them, and the card was listed "unhandled by design" until the trim needed it.

Two changes, and the order matters: CHECK THE BOX so the card does not appear (user: "It is
faster to check Put In Bulk" — one tap loads the shelf, against Max-then-Load's three), and
ANSWER the card when it does, because the box may be off for reasons of its own.
"""
import os
import types
import unittest
from unittest import mock

_CARD  = "tests/stage_suite/frames/jakarta_goods_card_bulk_off.png"
_GRID  = "tests/stage_suite/frames/jakarta_purchase_bulk_off.png"


def _open(path):
    from PIL import Image
    if not os.path.exists(path):
        raise unittest.SkipTest(f"stage frame not available: {path}")
    return Image.open(path)


def _frame():
    return _open(_CARD)


class TheBoxIsCheckedBeforeATileIsTapped(unittest.TestCase):

    def test_bulk_off_is_turned_on_instead_of_staging(self):
        """Driven on the real frame, with the checkbox genuinely read rather than mocked:
        `_is_bulk_mode_on` reports OFF on this capture. Ebony is on the grid and wanted, so
        the buy would otherwise tap its tile and open the very card this avoids."""
        from brain.activities.market_buy import on_purchase_page
        from brain.market_ledger import MarketLedger
        from brain.market_state import MarketState
        st = MarketState()
        st.ledger = MarketLedger()          # seeded and empty: nothing aboard yet
        bulk, taps = [], []
        goal = types.SimpleNamespace(orders={"Ebony": 942})
        from vision.omniparser import parse_fast_cached
        grid = _open(_GRID)
        out = on_purchase_page(st, goal, "Jakarta", frame=grid,
                               capture_fn=lambda: grid,
                               tap_fn=lambda x, y: taps.append((x, y)),
                               omni_fn=lambda f: list(parse_fast_cached(f)),
                               set_bulk_fn=lambda on, _f: bulk.append(on))
        self.assertEqual(bulk, [True], f"the box was not checked (did={out.get('do')!r})")
        self.assertEqual(taps, [], "and no tile was tapped in the same tick")

    def test_a_box_already_on_is_left_alone(self):
        from brain.activities.market_buy import on_purchase_page
        from brain.market_ledger import MarketLedger
        from brain.market_state import MarketState
        st = MarketState()
        st.ledger = MarketLedger()
        bulk = []
        goal = types.SimpleNamespace(orders={"Ebony": 942})
        from vision.omniparser import parse_fast_cached
        grid = _open(_GRID)
        with mock.patch("actions.market_actions._is_bulk_mode_on", return_value=True):
            on_purchase_page(st, goal, "Jakarta", frame=grid,
                             capture_fn=lambda: grid, tap_fn=lambda *a: None,
                             omni_fn=lambda f: list(parse_fast_cached(f)),
                             set_bulk_fn=lambda on, _f: bulk.append(on))
        self.assertEqual(bulk, [], "it tapped a checkbox that was already right")


class AndTheCardIsANSWEREDWhenItAppears(unittest.TestCase):
    """Because the box may be off for reasons of its own."""

    def _run(self, state):
        from brain.activities.market_buy import on_goods_info
        taps = []
        out = on_goods_info(state, types.SimpleNamespace(orders={"Ebony": 942}),
                            frame=_frame(), tap_fn=lambda x, y: taps.append((x, y)),
                            omni_fn=lambda _f: [])
        return out, taps

    def test_first_it_takes_the_whole_shelf(self):
        from brain.market_state import MarketState
        out, taps = self._run(MarketState())
        self.assertEqual(len(taps), 1, "Max is one tap")
        self.assertEqual(out["do"], "waited")

    def test_then_it_loads(self):
        from brain.market_state import MarketState
        st = MarketState()
        st.did("tapped Max on the goods card")
        out, taps = self._run(st)
        self.assertEqual(len(taps), 1)
        self.assertEqual(out["do"], "staged")

    def test_the_market_activity_claims_it_for_a_buy(self):
        """It refused before — the card was the trim's alone, so nothing owned this one."""
        from brain.activities.market import Hold, MarketActivity
        from brain.dispatcher import UNRECOGNISED
        im = _frame()
        act = MarketActivity(capture_fn=lambda: im, tap_fn=lambda *a: None,
                             omni_fn=lambda _f: [])
        with mock.patch.object(type(act), "_port_name", return_value="Jakarta"), \
             mock.patch("brain.activities.market_buy.on_goods_info",
                        return_value={"do": "waited", "why": "Max"}) as h:
            res = act._on_goods_info(Hold(orders={"Ebony": 942}), "Jakarta")
        h.assert_called_once()
        self.assertNotEqual(res.status, UNRECOGNISED)


if __name__ == "__main__":
    unittest.main()
