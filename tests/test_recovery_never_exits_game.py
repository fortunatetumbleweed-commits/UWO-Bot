"""Blind recovery must never confirm the 'Exit Game?' prompt.

Live 2026-08-21: recovery pressed Back on port_overworld (which opens the exit
confirmation), the screen then no longer classified as port_overworld so the call-site
guard `state != "port_overworld"` lapsed, and `_tap_dialog_ok` tapped OK — closing the
game mid-session. Back is the safe answer: pressing it again dismisses the prompt."""
import types
import unittest
from unittest import mock

from brain.recovery import _tap_dialog_ok


def _tokens(*texts):
    return [(t, 0.9, 100 + i * 50, 500) for i, t in enumerate(texts)]


class NeverConfirmExitTests(unittest.TestCase):
    def _run(self, tokens):
        taps = []
        with mock.patch("actions.sail_actions._ocr_frame", return_value=tokens), \
             mock.patch("actions.adb_actions.tap", side_effect=lambda x, y: taps.append((x, y))):
            return _tap_dialog_ok(object()), taps

    def test_it_refuses_the_exit_game_prompt(self):
        hit, taps = self._run(_tokens("Exit Game?", "Do you want to exit?", "OK", "Cancel"))
        self.assertFalse(hit)
        self.assertEqual(taps, [])          # nothing tapped at all

    def test_it_refuses_every_phrasing(self):
        for phrase in ("exit game", "leave game", "quit game", "close the game"):
            hit, taps = self._run(_tokens(phrase.title(), "Yes", "No"))
            self.assertFalse(hit, phrase)
            self.assertEqual(taps, [], phrase)

    def test_it_still_clears_an_ordinary_blocking_dialog(self):
        # The case this function exists for: the cart-emptying Notice.
        hit, taps = self._run(_tokens("Moving to another menu will empty the cart.",
                                      "Continue", "Cancel"))
        self.assertTrue(hit)
        self.assertEqual(len(taps), 1)

    def test_a_plain_ok_dialog_is_unaffected(self):
        hit, taps = self._run(_tokens("Purchase complete", "OK"))
        self.assertTrue(hit)
        self.assertEqual(len(taps), 1)


if __name__ == "__main__":
    unittest.main()


class ConsultBeforeTappingTests(unittest.TestCase):
    """Recovery must UNDERSTAND a dialog before touching it (user 2026-08-21).

    The cache already held ten analyses of the Exit Game prompt, all saying
    'tap_decline' — recovery just never asked, and keyword-tapped OK instead."""

    def _run(self, tokens, analysis):
        taps, backs = [], []
        with mock.patch("actions.sail_actions._ocr_frame", return_value=tokens), \
             mock.patch("brain.recovery._consult_dialog", return_value=analysis), \
             mock.patch("actions.adb_actions.tap", side_effect=lambda x, y: taps.append((x, y))), \
             mock.patch("actions.sail_actions.press_back", side_effect=lambda: backs.append(1)):
            return _tap_dialog_ok(object()), taps, backs

    def _analysis(self, dismissal, outcome="still_blocking", purpose="a dialog"):
        return types.SimpleNamespace(dismissal=dismissal, outcome_for_goal=outcome,
                                     purpose=purpose)

    def test_a_decline_verdict_presses_back_never_ok(self):
        hit, taps, backs = self._run(_tokens("Some prompt", "OK", "Cancel"),
                                     self._analysis("tap_decline"))
        self.assertTrue(hit)
        self.assertEqual(taps, [])          # OK was on screen and NOT tapped
        self.assertEqual(len(backs), 1)

    def test_an_irrelevant_verdict_touches_nothing(self):
        hit, taps, backs = self._run(_tokens("Trade Points", "OK"),
                                     self._analysis("tap_anywhere", outcome="irrelevant"))
        self.assertFalse(hit)
        self.assertEqual((taps, backs), ([], []))

    def test_an_accept_verdict_taps_ok(self):
        hit, taps, _ = self._run(_tokens("Empty the cart?", "OK"),
                                 self._analysis("tap_accept"))
        self.assertTrue(hit)
        self.assertEqual(len(taps), 1)

    def test_without_an_analysis_it_falls_back_to_the_keyword_tap(self):
        hit, taps, _ = self._run(_tokens("Purchase complete", "OK"), None)
        self.assertTrue(hit)
        self.assertEqual(len(taps), 1)

    def test_the_exit_guard_wins_even_if_a_model_said_accept(self):
        # Safety invariants are code, not a judgement delegated to a model.
        hit, taps, backs = self._run(_tokens("Exit Game?", "OK", "Cancel"),
                                     self._analysis("tap_accept"))
        self.assertFalse(hit)
        self.assertEqual((taps, backs), ([], []))
