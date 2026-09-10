"""Closing an overlay is not finishing the work it interrupted.

Live 2026-09-09, mid-ocean on the London route, with the barter done and the cargo aboard:

    main_menu <- ArriveAshore(what='the leg to Sans to London')
    [main_menu] closing via the X @ (2216, 49)
    main_menu -> finished
    [classify] → sea
    'sea' cannot start ENTER_BUILDING (it affords ['OPEN_WORLD_MAP'])

`SubTaskRunner.next_goal` reads any FINISHED as `DONE` and retires the leg, so a closed menu
was read as a completed voyage. The leg advanced to selling and the next intent was to walk
into a building in open water. The affordance guard refused it — nothing was tapped — but the
leg had already been retired, and the mission died one step from done.

The main menu is an overlay and has nothing to do with the task (user, 2026-09-09). It now
carries `CLEARS_SCREEN`, the marker the idle lock, a full-screen notice and an unnameable
chromed screen already had, and the dispatcher derives this from it — no second list to keep
in step.
"""

from __future__ import annotations

import unittest

from brain.dispatcher import ActivityResult, Dispatcher, FINISHED, WORKING
from brain.run_goal import default_activities


class _State:
    def __init__(self, where):
        self.state, self.frame = where, None


class TheOverlaysAreAllMarked(unittest.TestCase):

    def test_the_main_menu_is_one(self):
        from brain.activities.main_menu import MainMenuActivity
        self.assertTrue(getattr(MainMenuActivity, "CLEARS_SCREEN", False),
                        "it is drawn over the world, like the lock and a notice")

    def test_and_so_are_the_others_it_joins(self):
        from brain.activities.idle_lock import IdleLockActivity
        from brain.activities.transient import TransientActivity
        from brain.activities.unrecognized_chromed import UnrecognizedChromedActivity
        for cls in (IdleLockActivity, TransientActivity, UnrecognizedChromedActivity):
            with self.subTest(activity=cls.__name__):
                self.assertTrue(getattr(cls, "CLEARS_SCREEN", False))


class AClearedOverlayLeavesTheGoalAlone(unittest.TestCase):

    def _advance(self, where, goal, status=FINISHED):
        asked = []
        state = _State(where)
        d = Dispatcher(perceive=lambda: state, activities=default_activities(),
                       next_goal=lambda r, s: (asked.append(1), None)[1],
                       to_intent=lambda g, s: None, dispatch=lambda i: None)
        d.goal = goal
        d._advance(ActivityResult(status, {}), state)
        return d.goal, len(asked)

    def test_the_voyage_survives_the_main_menu(self):
        goal = object()
        kept, asked = self._advance("main_menu", goal)
        self.assertIs(goal, kept, "a closed menu is not a completed voyage")
        self.assertEqual(0, asked, "the runner is not asked, so the leg is not retired")

    def test_the_same_holds_for_a_notice_and_the_lock(self):
        for where in ("transient", "idle_lock", "unrecognized_chromed_screen"):
            with self.subTest(screen=where):
                goal = object()
                kept, asked = self._advance(where, goal)
                self.assertIs(goal, kept)
                self.assertEqual(0, asked)

    def test_a_real_world_finishing_still_advances_the_task(self):
        """Only overlays are exempt — a market or a village finishing means what it says."""
        _kept, asked = self._advance("building:market", object())
        self.assertEqual(1, asked, "the runner must still be asked for the next leg")

    def test_only_finished_is_exempt_working_still_re_asks(self):
        """WORKING has always meant "still going, ask me again" — that is untouched.

        The exemption is narrow on purpose: it covers the one status the runner reads as
        `DONE`, and nothing else.
        """
        _kept, asked = self._advance("main_menu", object(), status=WORKING)
        self.assertEqual(1, asked)

    def test_with_no_goal_the_runner_is_still_asked(self):
        """Otherwise an overlay at the very start leaves the dispatcher goal-less for good."""
        _kept, asked = self._advance("main_menu", None)
        self.assertEqual(1, asked)


if __name__ == "__main__":
    unittest.main()
