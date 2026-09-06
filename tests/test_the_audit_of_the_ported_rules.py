"""Rules the refactor nearly dropped, checked against the flow they came from.

Both were found by AUDITING the port rather than by a run failing — the negotiation stall at
Barcelona was the warning that `_react_after_purchase`'s chain held more than one rule, and
these are the two that had no counterpart on the new path.
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

import brain.market_context as ctx
from brain.activities.market import Hold, MarketActivity
from brain.activities.market_buy import _stocked_but_unmoved, on_purchase_page
from brain.dispatcher import UNRECOGNISED, WORKING
from brain.market_state import MarketState


class Tile:
    """A Purchase tile, as the grid reader hands one over."""

    def __init__(self, name, qty, sold_out=False, active=True):
        self.name, self.available_qty = name, qty
        self.sold_out, self.is_active = sold_out, active


def _goods(**kw):
    return {n.lower(): Tile(n, q) for n, q in kw.items()}


class TheOverloadNoticeIsAnswered(unittest.TestCase):
    """"...will be exceeded by N slots. Purchase the trade goods?" — OK.

    A PLAIN ACKNOWLEDGEMENT: the game hands back what fits, it spends no gems, and cancelling
    abandons a purchase the hold has room for. `_react_after_purchase` matched it on BOTH its
    phrases; the new table had no entry at all, so it would have stalled exactly as the
    negotiation did — hand back, dispatcher routes here again, same screen, forever.
    """

    def test_it_has_a_handler_at_all(self):
        self.assertIn(ctx.CARGO_FULL_NOTICE, MarketActivity._HANDLERS)

    def _answer(self, pressed: bool):
        act = MarketActivity(context_fn=lambda f: ctx.CARGO_FULL_NOTICE,
                             capture_fn=lambda: object(), tap_fn=lambda x, y: None,
                             omni_fn=lambda f: [])
        with mock.patch("brain.commit_actions.tap_one_positive",
                        return_value=pressed) as tap:
            res = act.work(Hold(orders={"Iron": 500}), types.SimpleNamespace(
                state="building:market", port="Barcelona", frame=object()))
        return res, tap

    def test_it_is_answered_rather_than_handed_back(self):
        res, tap = self._answer(True)
        self.assertEqual(res.status, WORKING)
        self.assertEqual(tap.call_args.kwargs["goal_keywords"], ["ok"])

    def test_it_acts_on_the_DISPATCHER_S_frame_and_captures_nothing_new(self):
        """Per-frame perception sharing, and more: a handler that re-captures acts on a
        screen the dispatcher never saw."""
        res, tap = self._answer(True)
        self.assertIsNotNone(tap.call_args.kwargs.get("capture_fn"))

    def test_an_unfindable_OK_hands_back_rather_than_tapping_blind(self):
        """A refusal is the safe answer — `game_rules` still gets its turn at the dialog."""
        res, _ = self._answer(False)
        self.assertEqual(res.status, UNRECOGNISED)


class BoughtNothingFromAStockedShelfMeansTheHoldIsFull(unittest.TestCase):
    """The room is the problem, not the shelf (user, 2026-09-04).

    Madeira frame 259: Raisin 217 on the tile, fully active, and the hold full of Pig. The
    old loop read the 0 as a possible sold-out and spent a blue gem at 205 and rising.
    """

    def test_a_shelf_that_did_not_move_while_stocked_names_the_good(self):
        state = MarketState(last_signature=(("raisin", 217),))
        self.assertEqual(_stocked_but_unmoved(state, {"Raisin": 900}, _goods(Raisin=217)),
                         "Raisin")

    def test_a_shelf_that_dropped_is_not_it(self):
        state = MarketState(last_signature=(("raisin", 217),))
        self.assertIsNone(_stocked_but_unmoved(state, {"Raisin": 900}, _goods(Raisin=100)))

    def test_UNREAD_IS_NOT_UNMOVED(self):
        """A missing reading yields no claim — the rule `credit_the_shelf_drop` follows, and
        the reason this cannot fire on a bad parse."""
        state = MarketState(last_signature=(("raisin", -1),))
        self.assertIsNone(_stocked_but_unmoved(state, {"Raisin": 900}, _goods(Raisin=217)))

    def test_a_sold_out_shelf_is_a_STOCK_problem_and_not_this_one(self):
        state = MarketState(last_signature=(("raisin", 0),))
        goods = {"raisin": Tile("Raisin", 0, sold_out=True, active=False)}
        self.assertIsNone(_stocked_but_unmoved(state, {"Raisin": 900}, goods))

    def test_the_buy_round_STOPS_rather_than_buying_or_refreshing_again(self):
        state = MarketState(last_intent="tapped Purchase", last_signature=(("raisin", 217),))
        with mock.patch("vision.market_reader.read_market_page_omni",
                        return_value=[Tile("Raisin", 217)]), \
             mock.patch("actions.buy_materials._find_purchase_commit", return_value=None):
            out = on_purchase_page(
                state, Hold(orders={"Raisin": 900}), "Madeira", frame=object(),
                capture_fn=lambda: object(), omni_fn=lambda f: [],
                tap_fn=lambda x, y: self.fail(f"tapped {x},{y} with a full hold"))
        self.assertEqual(out["do"], "finished")
        self.assertTrue(out.get("cargo_full"))


if __name__ == "__main__":
    unittest.main()


class TheNegotiationIsDeclinedEvenWithNoOwner(unittest.TestCase):
    """`_on_negotiation` covers the market's own tick. It does NOT cover BOOTSTRAP.

    Live 2026-09-06: the bot was restarted with the Attempt Negotiation card already on
    screen from the previous run. Bootstrap has no market activity, so nothing owned the
    dialog, `game_rules` had no rule for it, and the run died at step 1 —
    *"nothing owns this confirmation dialog and the game rules will not answer it
    (options=['No'])"*.

    This is the only card in the game whose right answer is NOT the positive one, so the
    default is not merely unhelpful here — it would say YES.
    """

    def test_game_rules_declines_it(self):
        from brain.game_rules import answer_dialog
        self.assertEqual(
            answer_dialog(["No", "Use one chance", "Use All"], ["Attempt Negotiation"]), "No")

    def test_the_default_still_applies_to_every_other_card(self):
        from brain.game_rules import answer_dialog
        self.assertEqual(answer_dialog(["Cancel", "OK"],
                                       ["The Cargo Hold's Trade Goods slot will be exceeded "
                                        "by 52 slots. Purchase the trade goods?"]), "OK")


class TheRulesAreShownWhatTheDialogContains(unittest.TestCase):
    """`body_text` is the detector's INTERPRETATION, and it can lose the deciding words.

    Live 2026-09-06: the Attempt Negotiation screen is not a centred modal — portrait and
    "Want me to try negotiating?" LEFT, three choices RIGHT — so the detector bounded it at
    (33,121)-(2239,1080) and `body_text` came back `['Purchase', '3,330/4,952', '162']`.
    'Attempt Negotiation' and 'Remaining negotiation attempts' were both on screen and both
    dropped, so `game_rules` was asked to rule on a card it could not read.
    """

    def _dialog_and_state(self):
        from brain.dispatcher import Dispatcher
        el = lambda t, x, y: types.SimpleNamespace(label=t, cx=x, cy=y)
        seen = [el("Attempt Negotiation", 1200, 221), el("Purchase", 300, 400),
                el("Remaining negotiation attempts", 1200, 297), el("", 5, 5),
                el("somewhere else entirely", 4000, 4000)]
        dialog = types.SimpleNamespace(bbox=(33, 121, 2239, 1080),
                                       body_text=("Purchase", "3,330/4,952", "162"),
                                       title_bar=None)
        return Dispatcher, dialog, types.SimpleNamespace(frame=object()), seen

    def test_it_reads_the_labels_the_bounds_contain(self):
        Dispatcher, dialog, state, seen = self._dialog_and_state()
        with mock.patch("vision.omniparser.parse_fast_cached", return_value=seen):
            words = Dispatcher._words_inside(None, dialog, state)
        self.assertIn("Attempt Negotiation", words)
        self.assertNotIn("somewhere else entirely", words, "outside the bounds")
        self.assertNotIn("", words, "blank labels are not text")

    def test_the_negotiation_is_then_answered_instead_of_wedging_the_run(self):
        from brain.game_rules import answer_dialog
        Dispatcher, dialog, state, seen = self._dialog_and_state()
        with mock.patch("vision.omniparser.parse_fast_cached", return_value=seen):
            text = list(dialog.body_text) + Dispatcher._words_inside(None, dialog, state)
        self.assertEqual(answer_dialog(["No", "Use 1 chance", "Use all remaining chances"],
                                       text), "No")

    def test_body_text_alone_could_NOT_answer_it(self):
        """The assertion that makes the one above mean something."""
        from brain.game_rules import answer_dialog
        _, dialog, _, _ = self._dialog_and_state()
        self.assertIsNone(answer_dialog(["No"], list(dialog.body_text)))

    def test_a_frame_or_bbox_it_cannot_read_yields_nothing_rather_than_raising(self):
        from brain.dispatcher import Dispatcher
        self.assertEqual(Dispatcher._words_inside(
            None, types.SimpleNamespace(bbox=None), types.SimpleNamespace(frame=object())), [])


class AHandlerMayNotClaimATapItDidNotMake(unittest.TestCase):
    """`_offer_dialog` returns the moment an activity claims a dialog, so a false claim ends
    the dispatcher's turn on it and its own fallbacks never run.

    Live 2026-09-06 at Barcelona, five times over:

        [market] the result_dialog dialog is ours — answering it
        [commit] iter 0: no positive button found — settled after 0 tap(s)
        [dispatch] market answered the informational dialog -> working
                   {'did': 'cleared the result dialog'}

    The card carried only a close X — top-right at (1528,327)-(1579,380) — which the
    dispatcher had been closing at (1553,354) all morning. The market intercepted, tapped
    nothing, said it had cleared it, and the task stopped with NOTHING CHANGED for 3 ticks.

    The goods HAD been read and recorded before the tap, which is why it went unnoticed: the
    ledger was right and only the screen was stuck.
    """

    def _market(self, pressed):
        act = MarketActivity(context_fn=lambda f: ctx.RESULT_DIALOG,
                             capture_fn=lambda: object(), tap_fn=lambda x, y: None,
                             omni_fn=lambda f: [])
        state = types.SimpleNamespace(state="building:market", port="Barcelona",
                                      frame=object())
        with mock.patch("brain.commit_actions.tap_one_positive", return_value=pressed), \
             mock.patch.object(MarketActivity, "_read_result_goods", return_value=[]):
            return act.work(Hold(orders={"Iron": 500}), state)

    def test_no_button_pressed_means_the_dialog_is_HANDED_BACK(self):
        self.assertEqual(self._market(False).status, UNRECOGNISED)

    def test_a_pressed_button_still_reports_the_dialog_answered(self):
        res = self._market(True)
        self.assertEqual(res.status, WORKING)
        self.assertEqual(res.observed["did"], "cleared the result dialog")
