"""Game rules answer dialogs; the barter task drives the phase; the graph stays default.

The reasoning layer exists because the departure Notice cannot be answered from UI mechanics
or from task context — task context argues for the WRONG answer:

    "Moving to Melanesian Village after Auto Supply. Continue?
     Fleet will immediately set sail if Auto Supply is not possible.
     Sailing can be dangerous with a lack of Food and Water."   [Cancel] [Ok]

Reading that as a supply hazard says Cancel. The game rule says OK — it runs Auto Supply,
which resupplies (user, 2026-08-23; docs/game_mechanics.md).
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from brain import barter_task, game_rules

NOTICE = ("Moving to Melanesian Village after Auto Supply:", "Continue?",
          "Fleet will immediately set sail if Auto Supply is not", "possible:",
          "Sailing can be dangerous with a lack of Food and", "Water.")


class GameRulesAnswerDialogs(unittest.TestCase):

    def test_the_departure_notice_is_answered_ok(self):
        self.assertEqual(game_rules.answer_dialog(("Cancel", "Ok"), NOTICE), "Ok")

    def test_an_unknown_dialog_defaults_to_the_positive_option(self):
        """Every dialog goes through the decider, and its default is the positive option
        (user, 2026-08-23). The default lives HERE, in the layer that knows the game — the
        state machine still refuses to answer without a decider, or this layer is decorative.
        """
        self.assertEqual(game_rules.answer_dialog(("Cancel", "Ok"),
                                                  ("Proceed with the exchange?",)), "Ok")

    def test_the_detected_positive_wins_over_word_matching(self):
        """The dialog detector already marks which option commits — prefer its verdict."""
        self.assertEqual(
            game_rules.answer_dialog(("Nope", "Aye"), ("Something unusual",), positive="Aye"),
            "Aye")

    def test_red_gems_are_never_auto_answered(self):
        """Red gems are real money. `brain/action_executor.py` enforces the same discipline
        on commit buttons via the cost-icon colour."""
        self.assertIsNone(game_rules.answer_dialog(("Cancel", "Ok"),
                                                   ("Purchase for 50 Red Gems?",)))

    def test_a_caller_with_detector_evidence_can_flag_red_gems(self):
        """The dialog need not SAY 'red gem' — the cost icon may be the only evidence."""
        self.assertIsNone(game_rules.answer_dialog(("Cancel", "Ok"), ("Purchase now?",),
                                                   spends_red_gem=True))

    def test_no_positive_option_means_no_answer(self):
        self.assertIsNone(game_rules.answer_dialog(("Maybe", "Later"), ("Odd dialog",)))

    def test_a_rule_that_is_not_offered_is_not_forced(self):
        """The rule says Ok, but this dialog has no Ok — do not substitute something else."""
        self.assertIsNone(game_rules.answer_dialog(("Close",), NOTICE))

    def test_no_options_means_no_answer(self):
        self.assertIsNone(game_rules.answer_dialog((), NOTICE))

    def test_the_match_is_case_insensitive(self):
        self.assertEqual(game_rules.answer_dialog(("cancel", "ok"),
                                                  ("MOVING AFTER AUTO SUPPLY",)), "ok")


class TheFlagIsOffByDefault(unittest.TestCase):
    """The graph stays the default until the loop has proven itself on real screens."""

    def test_off_when_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(barter_task.enabled())

    def test_off_for_falsey_values(self):
        for v in ("0", "", "false", "no"):
            with patch.dict(os.environ, {"UWO_TASK_LOOP": v}):
                self.assertFalse(barter_task.enabled(), v)

    def test_on_when_set(self):
        with patch.dict(os.environ, {"UWO_TASK_LOOP": "1"}):
            self.assertTrue(barter_task.enabled())


class TheBarterPhaseTask(unittest.TestCase):

    def _task(self):
        return barter_task.BarterPhaseTask("Melanesian Village", "Box of Nutmeg", 3,
                                           executors={})

    def test_it_sails_when_not_at_the_village(self):
        step = self._task().next_step("sea")
        self.assertEqual(step.name, "sail_to_village")

    def test_sailing_may_give_up_the_current_position(self):
        """Leaving IS the only route onward — and it is this task's call to authorise."""
        self.assertTrue(self._task().next_step("village_of_somewhere_else" ).may_leave_a_place)

    def test_it_barters_once_at_the_village(self):
        step = self._task().next_step("village")
        self.assertEqual(step.name, "barter")
        self.assertEqual(step.needs_state, "village")

    def test_bartering_does_not_authorise_leaving(self):
        self.assertFalse(self._task().next_step("village").may_leave_a_place)

    def test_a_successful_barter_finishes_the_task(self):
        t = self._task()
        step = t.next_step("village")
        with patch("brain.mission_progress.advance") as adv:
            t.record(step, {"ok": True})
        self.assertTrue(t.done())
        adv.assert_called_once_with("sailing_route")

    def test_a_failed_barter_does_not_finish_it(self):
        t = self._task()
        t.record(t.next_step("village"), {"ok": False})
        self.assertFalse(t.done())

    def test_the_task_answers_dialogs_from_the_game_rules(self):
        import types
        u = types.SimpleNamespace(options=("Cancel", "Ok"), text=NOTICE, positive="Ok")
        self.assertEqual(self._task().decide(u), "Ok")

    def test_the_task_takes_the_positive_option_on_an_unknown_dialog(self):
        import types
        u = types.SimpleNamespace(options=("Cancel", "Ok"), text=("Proceed?",), positive="Ok")
        self.assertEqual(self._task().decide(u), "Ok")

    def test_the_task_stops_for_a_red_gem_dialog(self):
        """None leaves the dialog alone and stops the loop — a human decides real money."""
        import types
        u = types.SimpleNamespace(options=("Cancel", "Ok"),
                                  text=("Buy 10 slots for 90 Red Gems?",), positive="Ok")
        self.assertIsNone(self._task().decide(u))


if __name__ == "__main__":
    unittest.main()
