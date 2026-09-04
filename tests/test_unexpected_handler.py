"""One handler for the four unexpected cases (user, 2026-08-23).

  1. STATE_CHANGED  — recognised but not what we expected: adopt it, never force the screen.
  2. ACTION_DIALOG  — a decision is offered; only the task (or the game's rules) can answer.
  3. SYSTEM_POPUP   — unsolicited noise: dismiss.
  4. UNKNOWN_SCREEN — chromed, unlearned, nothing blocking: leave via Home.

Cases 2 and 3 are told apart STRUCTURALLY — a popup offering ACTIONS is a decision, one
offering only a close-X is noise — because "did we cause it?" depends on bookkeeping the bot
does not reliably keep.

Grounded in the departure Notice live on 2026-08-23:
    "Moving to Melanesian Village after Auto Supply. Continue? Fleet will immediately set
     sail if Auto Supply is not possible."  [Cancel] [Ok]
which read as case=action_dialog, options=('Ok','Cancel'), positive='Ok'.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from brain import unexpected as ux


def _dialog(actions=(), body=("Continue?",), bbox=(826, 212, 1574, 863), title="Notice"):
    import types
    acts = tuple(types.SimpleNamespace(label=l, is_positive=(l.lower() in ("ok", "confirm")))
                 for l in actions)
    # `title_bar` is part of a DialogModel and `look` carries it out (2026-08-26), because a
    # decision needs it: the same body under "Notice" and under "Insufficient Funds" is not
    # the same question.
    return types.SimpleNamespace(bbox=bbox, body_text=body, actions=acts,
                                 title_bar=types.SimpleNamespace(text=title),
                                 close_button=(1540, 238, 1580, 278))


def _look(*, dialog=None, obstruction=None, state="port_overworld", expected=None):
    import types
    obs = obstruction or types.SimpleNamespace(kind="none", bbox=None, signals=[],
                                               is_obstructed=False)
    with patch.object(ux, "_read_dialog", return_value=dialog), \
         patch.object(ux, "_read_obstruction", return_value=obs), \
         patch("actions.sail_actions.where_am_i",
               return_value={"location": state, "port": None, "detail": ""}):
        return ux.look(object(), expected_state=expected)


class TheFourCases(unittest.TestCase):

    def test_1_a_recognised_but_unexpected_state(self):
        u = _look(state="sea", expected="port_overworld")
        self.assertEqual(u.case, ux.STATE_CHANGED)
        self.assertEqual(u.state, "sea")

    def test_2_a_dialog_offering_a_choice(self):
        u = _look(dialog=_dialog(actions=("Cancel", "Ok")))
        self.assertEqual(u.case, ux.ACTION_DIALOG)
        self.assertEqual(u.options, ("Cancel", "Ok"))
        self.assertEqual(u.positive, "Ok")
        self.assertTrue(u.needs_decision)

    def test_3_a_popup_with_no_choice_is_noise(self):
        """Daily news offers only a close-X — nothing to decide."""
        u = _look(dialog=_dialog(actions=()))
        self.assertEqual(u.case, ux.SYSTEM_POPUP)
        self.assertFalse(u.needs_decision)

    def test_4_an_unlearned_chromed_screen(self):
        u = _look(state="fleet_management")
        self.assertEqual(u.case, ux.UNKNOWN_SCREEN)

    def test_a_clear_screen_is_clear(self):
        self.assertEqual(_look(state="port_overworld").case, ux.CLEAR)

    def test_a_place_is_never_an_unknown_screen(self):
        """Being at sea or in a village is not a screen to escape."""
        for place in ("sea", "village", "world_map", "port_overworld"):
            self.assertNotEqual(_look(state=place).case, ux.UNKNOWN_SCREEN, place)


class ResolvePolicy(unittest.TestCase):

    DIALOG = ux.Unexpected(case=ux.ACTION_DIALOG, state="world_map",
                           text=("Fleet will immediately set sail...",),
                           options=("Cancel", "Ok"), positive="Ok")

    def test_a_dialog_without_a_decider_refuses_rather_than_guessing(self):
        """The whole point of case 2: the state machine must not pick for the task."""
        res = ux.resolve(self.DIALOG)
        self.assertFalse(res["handled"])
        self.assertTrue(res["needs_decision"])

    def test_the_task_s_choice_is_tapped(self):
        taps = []
        res = ux.resolve(self.DIALOG, decide=lambda u: u.positive,
                         tap_label=lambda l: taps.append(l) or True)
        self.assertEqual(taps, ["Ok"])
        self.assertTrue(res["handled"])

    def test_the_task_may_decline(self):
        res = ux.resolve(self.DIALOG, decide=lambda _u: None, tap_label=lambda _l: True)
        self.assertFalse(res["handled"])

    def test_a_state_change_needs_no_action(self):
        res = ux.resolve(ux.Unexpected(case=ux.STATE_CHANGED, state="sea"))
        self.assertTrue(res["handled"])
        self.assertIsNone(res["action"])

    def test_a_clear_screen_needs_no_action(self):
        res = ux.resolve(ux.Unexpected(case=ux.CLEAR, state="port_overworld"))
        self.assertTrue(res["handled"])

    def test_only_blocking_cases_block(self):
        for case in (ux.ACTION_DIALOG, ux.SYSTEM_POPUP, ux.UNKNOWN_SCREEN):
            self.assertTrue(ux.Unexpected(case=case).blocks, case)
        for case in (ux.CLEAR, ux.STATE_CHANGED):
            self.assertFalse(ux.Unexpected(case=case).blocks, case)


if __name__ == "__main__":
    unittest.main()
