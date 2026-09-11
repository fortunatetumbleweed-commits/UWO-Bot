"""A written rule may name a button the box parser missed.

`answer_dialog` will not choose an option nobody offered — it must not invent a button. But
the option list comes from OmniParser's action boxes, and a mis-boxed button simply is not in
it, so the honest refusal leaves a card standing that anyone can see the answer to.

Live 2026-09-06 at bootstrap: the Attempt Negotiation card, its 'No' returned as a 725x364
phantom that the size guard rightly dropped, leaving `options=['buy']`:

    'attempt_negotiation' applies but 'no' is not offered (options=['buy']) — not guessing
    could not reach a state the task runner can work in

The 'No' was plainly on screen; OCR puts it at (1803,639).
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

from brain.dispatcher import Dispatcher
from brain.game_rules import answer_dialog, rule_answer

NEGOTIATION = ["Attempt Negotiation", "Remaining negotiation attempts"]


class ARuleCanNameAButtonNobodyOffered(unittest.TestCase):

    def test_answer_dialog_still_refuses_to_invent_one(self):
        """The guard that makes this necessary must stay."""
        self.assertIsNone(answer_dialog(["buy"], NEGOTIATION))

    def test_rule_answer_names_it_anyway(self):
        self.assertEqual(rule_answer(NEGOTIATION), "no")

    def test_only_WRITTEN_rules_name_anything(self):
        """The positive default is deliberately absent: "take the positive option" is only
        meaningful among options that were actually seen."""
        self.assertIsNone(rule_answer(["Purchase the trade goods?"]))
        self.assertIsNone(rule_answer([]))


class TheDispatcherLooksWithTheOtherReader(unittest.TestCase):

    def _dispatch(self, *, found):
        taps = []
        state = types.SimpleNamespace(state="sub_menu:purchase", frame=object())
        disp = Dispatcher(perceive=lambda: state, activities={},
                          next_goal=lambda _r, _s: None, to_intent=lambda _g, _s: None,
                          dispatch=lambda _i: None, dialog=lambda _s: None)
        dialog = types.SimpleNamespace(
            kind=lambda: "confirmation",
            actions=(types.SimpleNamespace(label="buy", bbox=(718, 736, 797, 790)),),
            close_button=None, body_text=tuple(NEGOTIATION), title_bar=None,
            bbox=(33, 121, 2239, 1053), anchors_fired=("actions",))
        with mock.patch.object(Dispatcher, "_words_inside", return_value=[]), \
             mock.patch.object(Dispatcher, "_button_by_text", return_value=found), \
             mock.patch("actions.ui.tap_at",
                        side_effect=lambda x, y, why=None: taps.append((x, y))):
            rec = disp._offer_dialog(None, dialog, state)
        return rec, taps

    def test_it_taps_where_the_second_reader_found_it(self):
        rec, taps = self._dispatch(found=(1803, 639))
        self.assertEqual(taps, [(1803, 639)])
        self.assertIn("no", str(rec))

    def test_a_button_neither_reader_can_place_is_REPORTED(self):
        """Not guessed at. A point with a plausible name is how a tap lands where nobody
        meant it."""
        rec, taps = self._dispatch(found=None)
        self.assertEqual(taps, [])


class TheSecondReaderIsAskedOnlyAboutANamedButton(unittest.TestCase):

    def test_it_returns_the_point_for_that_label(self):
        state = types.SimpleNamespace(frame=object())
        with mock.patch("actions.sail_actions._ocr_frame", return_value=[]), \
             mock.patch("actions.route_execution.find_text_button",
                        return_value=(1803, 639)) as f:
            self.assertEqual(Dispatcher._button_by_text(None, state, "no"), (1803, 639))
        self.assertEqual(f.call_args[0][1], "no")

    def test_no_label_means_no_search(self):
        state = types.SimpleNamespace(frame=object())
        with mock.patch("actions.sail_actions._ocr_frame") as ocr:
            self.assertIsNone(Dispatcher._button_by_text(None, state, None))
        ocr.assert_not_called()


if __name__ == "__main__":
    unittest.main()
