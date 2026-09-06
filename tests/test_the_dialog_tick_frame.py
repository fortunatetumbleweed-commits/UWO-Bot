"""`on_dialog` must classify THIS tick's screen, not the last one.

Live 2026-09-06 at Tripoli. The dispatcher calls `on_dialog` BEFORE `work()`, and `work()` is
where the market receives the tick's frame — so `on_dialog` classified whatever the previous
tick left behind. The previous tick was the purchase page, `_classify()` answered
PURCHASE_PAGE, and `on_dialog` took its "a page is not a dialog" exit without logging a word.

Its own negotiation handler therefore never ran. The card fell through to `game_rules`, which
answered 'No' correctly but was handed a 725x314 parse artefact for the button and tapped
(1850,524) — about 110px above the real 63x43 control at (1771,617)-(1834,660). Three misses
and the leg failed with "a confirmation dialog will not close".

Given the right frame the market classifies that card as `negotiation` and finds its 'No' by
OCR at (1803,639).
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

import brain.market_context as ctx
from brain.activities.market import Hold, MarketActivity
from brain.dispatcher import WORKING


class TheActivityIsGivenThisTicksFrame(unittest.TestCase):

    def test_on_dialog_classifies_the_frame_it_was_handed(self):
        this_tick, last_tick = object(), object()
        seen = []
        act = MarketActivity(context_fn=lambda f: seen.append(f) or ctx.NEGOTIATION,
                             capture_fn=lambda: last_tick, tap_fn=lambda x, y: None,
                             omni_fn=lambda f: [])
        act._tick_frame = last_tick                    # what the previous tick left behind
        act.on_tick_frame(this_tick)
        # `_HANDLERS` holds the ORIGINAL function, captured at import, so patching the class
        # attribute does not reach it — stub what the handler reads instead.
        with mock.patch("actions.sail_actions._ocr_frame", return_value=[]), \
             mock.patch("actions.route_execution.find_text_button", return_value=(1803, 639)), \
             mock.patch.object(MarketActivity, "_port_name", return_value="Tripoli"):
            act.on_dialog(object(), Hold(orders={"Candle": 709}))
        self.assertIn(this_tick, seen)
        self.assertNotIn(last_tick, seen, "classified the previous tick's screen")

    def test_the_dispatcher_hands_it_over_before_asking(self):
        from brain.dispatcher import Dispatcher
        handed, frame = [], object()
        activity = types.SimpleNamespace(
            name="market",
            on_tick_frame=lambda f: handed.append(f),
            on_dialog=lambda _d, _g: None)
        state = types.SimpleNamespace(state="building:market", frame=frame)
        disp = Dispatcher(perceive=lambda: state, activities={"building:market": activity},
                          next_goal=lambda _r, _s: None, to_intent=lambda _g, _s: None,
                          dispatch=lambda _i: None, dialog=lambda _s: None)
        dialog = types.SimpleNamespace(kind=lambda: "confirmation", actions=(),
                                       close_button=None, body_text=(), title_bar=None,
                                       bbox=None, anchors_fired=("actions",))
        disp._offer_dialog(activity, dialog, state)
        self.assertEqual(handed, [frame])

    def test_an_activity_without_the_hook_is_left_alone(self):
        """Only the market implements it today; the others must not break."""
        from brain.dispatcher import Dispatcher
        activity = types.SimpleNamespace(name="village", on_dialog=lambda _d, _g: None)
        state = types.SimpleNamespace(state="village", frame=object())
        disp = Dispatcher(perceive=lambda: state, activities={"village": activity},
                          next_goal=lambda _r, _s: None, to_intent=lambda _g, _s: None,
                          dispatch=lambda _i: None, dialog=lambda _s: None)
        dialog = types.SimpleNamespace(kind=lambda: "confirmation", actions=(),
                                       close_button=None, body_text=(), title_bar=None,
                                       bbox=None, anchors_fired=("actions",))
        disp._offer_dialog(activity, dialog, state)          # must not raise


if __name__ == "__main__":
    unittest.main()
