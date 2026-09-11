"""Trimming the hold is many ticks, and the TILES are what carry the basket between them.

`sell_down_to` walked four screens per good inside one call — grid, goods card, keypad, grid
— fifteen or more captures for a three-good trim, none of them a screen the dispatcher had
seen. Live 2026-09-06 at Madeira (frame 400 of trace_barter_cmd_2026-09-06T21-45-01) the Load
tap at (1313,943) was dead on the button and the card simply did not close; the walk had
nobody to ask about that, so it aborted and 1,828 Pig sailed on unsold.

Split across ticks, the basket lives on the game's screen between them and the dispatcher may
route anywhere in between. What makes that safe is that STAGING MOVES A GOOD OUT OF ITS TILE
— measured live, Iron 999 read 822 once its 177 surplus was staged. So "what is left to
stage" is a question the grid answers on every tick, and no interruption can lose the place.
`trim_staged` is the report; the tiles are the state.
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

from brain.activities import market_trim as mt
from brain.market_state import MarketState


def _good(name, owned, x=100, y=200):
    return types.SimpleNamespace(name=name, owned_qty=owned, tap_x=x, tap_y=y)


class Goal:
    def __init__(self, keep):
        self.keep_qty = dict(keep)


_FOUND = types.SimpleNamespace(cx=900, cy=1000)


def _run(state, goods, keep, *, taps=None, bulk=None, commit=_FOUND):
    taps = taps if taps is not None else []
    bulk = bulk if bulk is not None else []
    with mock.patch("actions.sell_goods._sell_page", return_value=goods), \
         mock.patch("actions.sell_goods._find_sell_commit", return_value=commit), \
         mock.patch("memory.observed_facts.forget"):
        out = mt.on_sell_page(state, Goal(keep), frame=object(),
                              tap_fn=lambda x, y: taps.append((x, y)),
                              omni_fn=lambda _f: [],
                              set_bulk_fn=lambda on, _f: bulk.append(on))
    return out, taps, bulk


class TheGridSaysWhatIsLeftToDo(unittest.TestCase):

    def test_a_good_over_its_keep_level_is_staged(self):
        st = MarketState()
        st.trim_bulk_off = True
        out, taps, _b = _run(st, [_good("Iron", 999)], {"Iron": 822})
        self.assertEqual(out["do"], "staged")
        self.assertEqual(taps, [(100, 200)])
        self.assertEqual(st.trim_good, "Iron")

    def test_a_good_already_staged_has_left_its_tile_and_is_not_staged_twice(self):
        """The 822 IS the 999 less the 177 now in the cart. Nothing needs to remember that."""
        st = MarketState()
        st.trim_bulk_off = True
        st.trim_staged = {"Iron": 177}
        st.trim_committed = True
        out, taps, _b = _run(st, [_good("Iron", 822)], {"Iron": 822})
        self.assertNotEqual(out["do"], "staged")
        self.assertEqual(taps, [])

    def test_an_interruption_between_ticks_costs_nothing(self):
        """Whatever the dispatcher did in between, the next look at the grid resumes."""
        st = MarketState()
        st.trim_bulk_off = True
        st.trim_staged = {"Candle": 211}
        goods = [_good("Candle", 709), _good("Iron", 999, x=300, y=400)]
        out, taps, _b = _run(st, goods, {"Candle": 709, "Iron": 822})
        self.assertEqual(out["do"], "staged")
        self.assertEqual(taps, [(300, 400)], "Candle is done; Iron is next")

    def test_an_unreadable_grid_is_not_an_empty_hold(self):
        out, _t, _b = _run(MarketState(), None, {"Iron": 822})
        self.assertEqual(out["do"], "blocked")

    def test_a_good_whose_count_could_not_be_read_is_never_sold(self):
        st = MarketState()
        out, taps, _b = _run(st, [_good("Iron", None)], {"Iron": 822})
        self.assertEqual(taps, [])
        self.assertIn("Iron: owned quantity unreadable", out["skipped"])


class PutInBulk(unittest.TestCase):

    def test_it_goes_off_before_the_first_tile(self):
        """With it ON a tile tap loads the WHOLE stack, and selling that dumps the barter's
        materials."""
        st = MarketState()
        out, taps, bulk = _run(st, [_good("Iron", 999)], {"Iron": 822})
        self.assertEqual(bulk, [False])
        self.assertEqual(taps, [], "not in the same tick as the tile tap")
        self.assertEqual(out["do"], "waited")

    def test_it_is_never_touched_when_there_is_nothing_to_trim(self):
        """Two taps and twenty seconds to change nothing, live 2026-08-22."""
        out, _t, bulk = _run(MarketState(), [_good("Iron", 700)], {"Iron": 822})
        self.assertEqual(bulk, [])
        self.assertEqual(out["do"], "finished")

    def test_it_goes_back_on_before_the_trim_finishes(self):
        """The BUY flow silently breaks with it off (live 2026-08-20)."""
        st = MarketState()
        st.trim_bulk_off = True
        st.trim_staged = {"Iron": 177}
        st.trim_committed = True
        out, _t, bulk = _run(st, [_good("Iron", 822)], {"Iron": 822})
        self.assertEqual(bulk, [True])
        self.assertEqual(out["do"], "waited")
        self.assertFalse(st.trim_bulk_off)


class TheCommit(unittest.TestCase):

    def _staged(self):
        st = MarketState()
        st.trim_bulk_off = True
        st.trim_staged = {"Iron": 177}
        return st

    def test_a_full_basket_is_committed(self):
        st = self._staged()
        out, taps, _b = _run(st, [_good("Iron", 822)], {"Iron": 822})
        self.assertEqual(out["do"], "waited")
        self.assertEqual(taps, [(900, 1000)])
        self.assertTrue(st.trim_committed)

    def test_it_is_not_committed_twice(self):
        """The result card comes back to this same grid, and Sell is still on screen."""
        st = self._staged()
        st.trim_committed = True
        _out, taps, _b = _run(st, [_good("Iron", 822)], {"Iron": 822})
        self.assertEqual(taps, [], "the basket is already gone")

    def test_no_sell_button_is_a_refusal_not_a_finish(self):
        st = self._staged()
        out, _t, _b = _run(st, [_good("Iron", 822)], {"Iron": 822}, commit=None)
        self.assertEqual(out["do"], "blocked")


class ATileThatWillNotRespond(unittest.TestCase):
    """One re-tap answers a dropped tap. More than that is a different problem."""

    def test_it_is_reported_rather_than_tapped_forever(self):
        st = MarketState()
        st.trim_bulk_off = True
        seen = []
        for _ in range(4):
            out, taps, _b = _run(st, [_good("Iron", 999)], {"Iron": 822})
            seen.append((out["do"], len(taps)))
        self.assertEqual([n for _d, n in seen], [1, 1, 0, 0])
        self.assertIn("Iron: its tile would not open a quantity dialog", st.trim_skipped)

    def test_and_the_other_goods_still_get_trimmed(self):
        """Per-good, not per-basket: at Tripoli a confirmed basket worth 205,013 ducats was
        thrown away because one later good's dialog misbehaved."""
        st = MarketState()
        st.trim_bulk_off = True
        goods = [_good("Iron", 999), _good("Candle", 920, x=300, y=400)]
        keep = {"Iron": 822, "Candle": 709}
        for _ in range(3):
            _run(st, goods, keep)
        _out, taps, _b = _run(st, goods, keep)
        self.assertEqual(taps, [(300, 400)], "Candle's turn")


if __name__ == "__main__":
    unittest.main()


class TheGoodsCardIsTheAuthority(unittest.TestCase):
    """Its denominator is the GAME stating how many we hold.

    The grid badge was our reading of a 40px overlay, and on 2026-08-27 that overlay's
    melted-wax artwork OCR'd as a leading digit — Candle 148 became 2148, at confidence 0.65
    against OmniParser's 0.9987. So the surplus is recomputed here, never carried in.
    """

    def _card(self, state, field, keep):
        taps = []
        with mock.patch("actions.sell_goods._find_qty_field", return_value=field), \
             mock.patch("vision.overlay.detect_overlay",
                        return_value=types.SimpleNamespace(bbox=(0, 0, 10, 10))):
            out = mt.on_goods_info(state, Goal(keep), frame=object(),
                                   tap_fn=lambda x, y: taps.append((x, y)),
                                   omni_fn=lambda _f: [])
        return out, taps

    def _in_flight(self, good="Candle", owned=2148):
        st = MarketState()
        st.trim_good, st.trim_owned = good, owned
        return st

    def test_the_surplus_comes_from_the_card_not_the_tile(self):
        st = self._in_flight()                       # the tile said 2148
        out, taps = self._card(st, (500, 600, 148), {"Candle": 102})
        self.assertEqual(st.trim_excess, 46, "148 less 102, not 2148 less 102")
        self.assertEqual(taps, [(500, 600)])
        self.assertEqual(out["do"], "staged")

    def test_a_card_that_says_there_is_no_surplus_is_closed(self):
        st = self._in_flight(owned=2148)
        out, _taps = self._card(st, (500, 600, 90), {"Candle": 102})
        self.assertEqual(out["do"], "dismiss")
        self.assertIsNone(st.trim_good)
        self.assertIn("Candle: 90 ≤ keep 102 (per the dialog)", st.trim_skipped)

    def test_a_card_we_did_not_open_is_handed_back(self):
        out, taps = self._card(MarketState(), (500, 600, 148), {"Candle": 102})
        self.assertEqual(out["do"], "unclaimed")
        self.assertEqual(taps, [])

    def test_no_quantity_field_is_handed_back_rather_than_guessed_at(self):
        out, taps = self._card(self._in_flight(), None, {"Candle": 102})
        self.assertEqual(out["do"], "unclaimed")
        self.assertEqual(taps, [])


class TheKeypad(unittest.TestCase):

    def _type(self, state, typed_ok):
        taps = []
        with mock.patch("actions.sail_actions._find_button", return_value=(1313, 943)):
            out = mt.on_keypad(state, Goal({"Pig": 1755}), frame=object(),
                               capture_fn=lambda: object(),
                               tap_fn=lambda x, y: taps.append((x, y)),
                               omni_fn=lambda _f: [],
                               type_qty_fn=lambda *a, **k: typed_ok)
        return out, taps

    def _ready(self):
        st = MarketState()
        st.trim_good, st.trim_owned, st.trim_excess = "Pig", 1828, 73
        return st

    def test_it_types_and_taps_load(self):
        st = self._ready()
        out, taps = self._type(st, True)
        self.assertEqual(taps, [(1313, 943)])
        self.assertEqual(st.trim_staged, {"Pig": 73})
        self.assertEqual(out["do"], "staged")

    def test_the_good_in_flight_is_cleared_once_loaded(self):
        st = self._ready()
        self._type(st, True)
        self.assertIsNone(st.trim_good)
        self.assertIsNone(st.trim_excess)

    def test_a_quantity_that_would_not_confirm_loses_only_that_good(self):
        """Enter was never pressed, so nothing of Pig's is in the basket — and the goods
        already staged are exactly as safe as they were."""
        st = self._ready()
        st.trim_staged = {"Candle": 211, "Iron": 177}
        out, taps = self._type(st, False)
        self.assertEqual(out["do"], "dismiss")
        self.assertEqual(taps, [], "Load is never tapped on an unconfirmed number")
        self.assertEqual(st.trim_staged, {"Candle": 211, "Iron": 177})
        self.assertIn("Pig: could not confirm the typed quantity 73", st.trim_skipped)

    def test_a_keypad_we_did_not_open_is_handed_back(self):
        out, taps = self._type(MarketState(), True)
        self.assertEqual(out["do"], "unclaimed")
        self.assertEqual(taps, [])


class TheActivityRoutesTheTrimsOwnScreens(unittest.TestCase):
    """The goods card and the keypad are dialogs, and the trim is what opened them.

    Adding them to the handler table is what stops the game rules closing a card the trim is
    in the middle of — but only while a TrimHold is running. Under any other goal one of
    these is a dialog nobody here opened, and claiming it would be answering a card we cannot
    account for.
    """

    def _activity(self, context):
        import brain.market_context as ctx
        from brain.activities.market import MarketActivity
        act = MarketActivity(context_fn=lambda _f: context, capture_fn=lambda: object(),
                             tap_fn=lambda *a: None, omni_fn=lambda _f: [])
        return act, ctx

    def test_the_trim_claims_its_goods_card(self):
        from brain.activities.market import TrimHold
        act, ctx = self._activity(ctx_ := "trade_goods_info")
        with mock.patch.object(type(act), "_port_name", return_value="Faro"), \
             mock.patch("brain.activities.market_trim.on_goods_info",
                        return_value={"do": "staged", "why": "typing"}) as h:
            claimed = act.on_dialog(types.SimpleNamespace(body_text=()),
                                    TrimHold(keep_qty={"Pig": 1755}))
        self.assertIsNotNone(claimed)
        h.assert_called_once()

    def test_the_BUY_claims_the_goods_card_too(self):
        """It meets one by accident, and must answer it.

        This asserted that only a trim may claim the goods card. The buy meets one whenever
        `Put In Bulk` is off — a tile tap opens the per-item card instead of loading the
        shelf — and having no owner is what killed the Jakarta leg on 2026-09-08 with
        "a confirmation dialog nobody will answer". The keypad is still the trim's alone.
        """
        from brain.activities.market import Hold
        act, _ctx = self._activity("trade_goods_info")
        with mock.patch("brain.activities.market_buy.on_goods_info",
                        return_value={"do": "waited", "why": "Max"}), \
             mock.patch.object(type(act), "_port_name", return_value="Faro"):
            self.assertIsNotNone(act.on_dialog(types.SimpleNamespace(body_text=()),
                                               Hold(orders={"Pig": 900})))

    def test_the_clear_out_claims_the_goods_card_because_its_own_tap_opens_it(self):
        """It used to be asserted that `FreeHold` opens neither card. It opens this one.

        A clear-out stages by tapping a tile, and with `Put In Bulk` off that tap raises the
        Trade Goods Info card instead of loading — the same way the buy meets it. Live
        2026-09-09 at Bordeaux the card came up on Iron, `FreeHold` was not on the owners
        list, the market disowned a card its own tap had raised, and the dispatcher found
        only `Cancel`: "a confirmation dialog nobody will answer". The trim leg died, 3,465
        Birch Tree stayed aboard, and the gather that followed hit a full hold.
        """
        from brain.activities.market import FreeHold
        act, _ctx = self._activity("trade_goods_info")
        with mock.patch.object(type(act), "_port_name", return_value="Bordeaux"), \
             mock.patch.object(type(act), "_frame", return_value=object()), \
             mock.patch.object(type(act), "_tap_fn", return_value=lambda *a: None), \
             mock.patch.object(type(act), "_omni_fn", return_value=lambda f: []), \
             mock.patch("brain.activities.market_buy.on_goods_info",
                        return_value={"do": "waited", "why": "max"}):
            self.assertIsNotNone(act.on_dialog(types.SimpleNamespace(body_text=()),
                                               FreeHold(keep=("Water",))))

    def test_and_the_keypad_stays_the_trims_alone(self):
        """Only the trim types a figure; the buy takes the whole shelf with Max."""
        from brain.activities.market import Hold
        act, _ctx = self._activity("quantity_dialog")
        self.assertIsNone(act.on_dialog(types.SimpleNamespace(body_text=()),
                                        Hold(orders={"Pig": 900})))

    def test_a_trim_on_the_sell_page_goes_to_the_trim_not_the_clear_out(self):
        from brain.activities.market import TrimHold
        act, _ctx = self._activity("sell_page")
        with mock.patch.object(type(act), "_port_name", return_value="Faro"), \
             mock.patch("brain.activities.market_trim.on_sell_page",
                        return_value={"do": "waited", "why": "bulk off"}) as trim, \
             mock.patch("brain.activities.market_sell.on_sell_page") as clear:
            act.work(TrimHold(keep_qty={"Pig": 1755}),
                     types.SimpleNamespace(state="building:market", frame=object()))
        trim.assert_called_once()
        clear.assert_not_called()

    def test_the_trim_goes_through_the_contexts_like_every_other_goal(self):
        """It was the last goal that did not, and that made the routing unreachable.

        Live 2026-09-07 at Faro, frame 130 of trace_barter_cmd_2026-09-07T15-20-16. The goal
        arrived while the fleet stood on the PURCHASE page:

            15:45:22  next leg: sell_surplus
            15:45:23  market <- TrimHold(keep_qty={'Pig': 1505, 'Raisin': 1712})
            15:45:23  [sell] this is not the Sell grid — not reading it as the hold
            15:45:23  market -> blocked

        Four milliseconds, and no `purchase_page -> _on_purchase_page` line in the log,
        because `_tick` never ran: `work` sent a TrimHold straight to `_trim_to`, which read
        whatever screen was up. `sell_down_to` did its own tab switch, which is why the old
        `_trim_to` needed none; the tick version gets one from `_on_purchase_page`.
        """
        from brain.activities.market import TrimHold
        act, _ctx = self._activity("purchase_page")
        with mock.patch.object(type(act), "_port_name", return_value="Faro"), \
             mock.patch("actions.buy_materials.ensure_sell_tab", return_value=True) as tab, \
             mock.patch("brain.activities.market_trim.on_sell_page") as trim:
            res = act.work(TrimHold(keep_qty={"Pig": 1505}),
                           types.SimpleNamespace(state="building:market", frame=object()))
        tab.assert_called_once()
        trim.assert_not_called()
        self.assertEqual(res.observed["did"], "switched to the sell tab")


class TheFinishingTickSurvivesItsOwnReport(unittest.TestCase):
    """A skip list is a LIST, and reporting it must not crash the leg.

    Live 2026-09-07 at London. The trim did everything right — Put In Bulk off, Pig 1,812
    trimmed to 1,081, `tap Sell for {'Pig': 731}`, bulk restored — and then died on the tick
    that reports what it did:

        18:22:05  [market] sell_page -> _on_sell_page
        18:22:05  [sell] omni grid 1x1: 1 goods
        18:22:05  [run_task] step 15 failed: can only concatenate list (not "tuple") to list

    `_why_nothing_to_stage` returns a list and `trim_skipped` is a list, but the report line
    read `(out.get("skipped") or ()) + tuple(...)`. It raised only when something had actually
    been skipped, so every unit test and the whole first half of the run passed over it. The
    mission stopped with the sale already made and the phone sat idle.
    """

    def _finish_with(self, skipped, trim_skipped=()):
        from brain.activities.market import MarketActivity, TrimHold
        from brain.dispatcher import FINISHED
        act = MarketActivity(capture_fn=lambda: object(), tap_fn=lambda *a: None,
                             omni_fn=lambda _f: [])
        act._state.trim_skipped = list(trim_skipped)
        act._state.trim_staged = {"Pig": 731}
        out = {"do": "finished", "why": "nothing to trim", "skipped": skipped}
        res = act._trim_result(out, TrimHold(keep_qty={"Pig": 1081}), "London")
        self.assertEqual(res.status, FINISHED)
        return res

    def test_a_LIST_of_skips_is_reported_without_raising(self):
        res = self._finish_with(["Raisin: not on the sell page", "Pig: 1081 ≤ keep 1081"])
        self.assertEqual(res.observed["trimmed"], {"Pig": 731})

    def test_skips_from_both_sources_are_reported_together(self):
        self._finish_with(["Raisin: not on the sell page"],
                          trim_skipped=["Candle: its tile would not open a quantity dialog"])

    def test_no_skips_at_all_still_finishes(self):
        self._finish_with([])

    def test_a_tuple_of_skips_also_works(self):
        """The callers are not all the same shape, and neither side should have to care."""
        self._finish_with(("Raisin: not on the sell page",))
