"""The game's "daily Trade Count is spent" Notice is a DIALOG, so the dispatcher hears it.

    Notice / You have used all your daily Trade Count: / 17.3348 / OK

Live 2026-08-30 at Svear the Barter item was not locked — the tap went through and the game
answered AFTER it. `_open_barter_panel` caught that by capturing its own frame once more
after tapping, which is the sub-loop shape this refactor removes; without the catch the run
looped, tap → notice → dismiss → tap, until it stalled.

The Notice arrives at `on_dialog` like any other. So the activity records the fact on the
tick it is told, and the next tick ends the goal on it. Same fact, same conclusion, read
where dialogs are already read.
"""

from __future__ import annotations

import types
import unittest

from brain.activities.village import Barter, VillageActivity
from brain.dispatcher import FINISHED


def _dialog(*lines):
    return types.SimpleNamespace(body_text=lines, is_modal=True)


NOTICE = _dialog("Notice", "You have used all your daily Trade Count:", "17.3348", "OK")
GOAL = Barter(good="Birch Tree", village="Svear Village")


class TheNoticeIsRecordedNotSwallowed(unittest.TestCase):

    def test_hearing_it_records_the_goal_it_was_raised_against(self):
        """Against the goal, not beside it — `on_dialog` runs before `work` registers one,
        so a bare flag would be set on one tick and cleared by the key check on the next."""
        act = VillageActivity()
        self.assertIsNone(act._day_spent_for)
        act.on_dialog(NOTICE, GOAL)
        self.assertEqual(act._day_spent_for, ("Birch Tree", "Svear Village"))

    def test_and_still_lets_the_default_press_OK(self):
        """Recording is not answering. OK is the right button and it is not ours to claim."""
        act = VillageActivity()
        self.assertIsNone(act.on_dialog(NOTICE, GOAL))

    def test_an_unrelated_dialog_leaves_the_flag_alone(self):
        act = VillageActivity()
        act.on_dialog(_dialog("Notice", "486 Bambara Groundnut has not been claimed yet."),
                      GOAL)
        self.assertIsNone(act._day_spent_for)


class TheNextTickEndsTheGoalOnIt(unittest.TestCase):

    def _tick(self, act):
        return act.work(GOAL, types.SimpleNamespace(state="village", frame=object()))

    def test_the_top_menu_finishes_rather_than_tapping_barter_again(self):
        taps = []
        act = VillageActivity(context_fn=lambda _s: "village_top_menu",
                              open_panel_fn=lambda *a: taps.append(1) or True)
        act.on_dialog(NOTICE, GOAL)
        res = self._tick(act)
        self.assertEqual(res.status, FINISHED)
        self.assertEqual(taps, [], "the day is over — do not go back for another round")

    def test_a_fresh_goal_starts_over(self):
        """Tomorrow, or another village, is not this one."""
        act = VillageActivity(context_fn=lambda _s: "village_top_menu",
                              open_panel_fn=lambda *a: True)
        act.on_dialog(NOTICE, GOAL)
        self._tick(act)
        act.work(Barter(good="Birch Tree", village="Melanesian Village"),
                 types.SimpleNamespace(state="village", frame=object()))
        self.assertFalse(act._day_spent)
        self.assertIsNone(act._day_spent_for)


if __name__ == "__main__":
    unittest.main()
