"""Closing an overlay is not finishing the work it interrupted.

Live 2026-09-09, mid-ocean on the London route, with the barter done and the cargo aboard:

    main_menu <- ArriveAshore(what='the leg to Sans to London')
    [main_menu] closing via the X @ (2216, 49)
    main_menu -> finished
    [classify] → sea
    'sea' cannot start ENTER_BUILDING (it affords ['OPEN_WORLD_MAP'])

`_OneGoalLeg.next_goal` reads any FINISHED as `DONE` and retires the leg, so a closed menu
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


class AClearedOverlayIsNotTheGoalBeingDone(unittest.TestCase):
    """The guard lives in `step`, on the ACTIVITY — not on whatever is on screen afterwards.

    `step` converts a FINISHED from a `CLEARS_SCREEN` activity into UNRECOGNISED, so the
    runner is asked again and returns the same goal, still unfilled. That is the whole
    mechanism, and it predates the main-menu bug: what was missing was the MARKER, not the
    guard.

    A second check was added in `_advance` on 2026-09-09 and removed on 2026-09-10. It read
    `state` AFTER the re-perceive, so it judged whatever had appeared since rather than the
    screen the activity ran in — see `TheCheckMustJudgeTheActivityNotTheAftermath` below.
    """

    def test_the_guard_rewrites_a_cleared_overlay_as_unrecognised(self):
        import inspect
        from brain.dispatcher import Dispatcher
        src = inspect.getsource(Dispatcher.step)
        self.assertIn('getattr(activity, "CLEARS_SCREEN", False)', src,
                      "gated on the activity, which is the only thing that knows")
        guard = src[src.index('if result.status == FINISHED and getattr(activity,'):]
        self.assertIn("UNRECOGNISED", guard[:400],
                      "a cleared overlay is reported as 'do not know where we are', "
                      "never as the goal being done")

    def test_unrecognised_keeps_the_goal_because_the_runner_re_offers_it(self):
        """That is what UNRECOGNISED means to the runner — still going, ask me again."""
        from brain.dispatcher import ActivityResult, UNRECOGNISED
        from brain.mission_runner import _OneGoalLeg
        goal = object()
        r = _OneGoalLeg.__new__(_OneGoalLeg)
        r.goal, r._asked, r.status, r.reason = goal, True, None, None
        self.assertIs(goal, r.next_goal(ActivityResult(UNRECOGNISED, {}), None),
                      "the same goal comes back, still unfilled")

    def test_finished_from_a_real_world_still_retires_the_leg(self):
        from brain.dispatcher import ActivityResult, FINISHED
        from brain.mission_runner import _OneGoalLeg
        r = _OneGoalLeg.__new__(_OneGoalLeg)
        r.goal, r._asked, r.status, r.reason = object(), True, None, None
        self.assertIsNone(r.next_goal(ActivityResult(FINISHED, {}), None))


class TheCheckMustJudgeTheActivityNotTheAftermath(unittest.TestCase):
    """Live 2026-09-10: Trabzon was selected twice, the second time mid-voyage.

        09:17:05  world_map -> finished {'where': 'Trabzon'}      course set, sailing
        09:17:13  [classify] -> transient — a full-screen notice  the departure notice
        09:17:15  'transient' was covering the world ... untouched
        09:17:30  OPEN_WORLD_MAP(purpose="choose port 'Trabzon'") selected AGAIN

    `world_map` finished legitimately and the leg was done. A departure notice arrived while
    `_advance` was re-perceiving, and the removed check — reading the post-perceive state —
    called that "a covering screen finished" and suppressed the ask. The completed goal was
    frozen and re-issued.
    """

    def test_advance_no_longer_second_guesses_the_screen_it_lands_on(self):
        import inspect
        from brain.dispatcher import Dispatcher
        src = inspect.getsource(Dispatcher._advance)
        self.assertNotIn("_is_a_covering_screen", src,
                         "judging the aftermath is what selected Trabzon twice")

    def test_the_runner_is_asked_after_every_result_advance_sees(self):
        from brain.dispatcher import ActivityResult, Dispatcher, FINISHED
        from brain.run_goal import default_activities

        class _S:
            def __init__(self, where):
                self.state, self.frame = where, None

        for where in ("transient", "world_map", "building:market"):
            with self.subTest(landed_on=where):
                asked, state = [], _S(where)
                d = Dispatcher(perceive=lambda: state, activities=default_activities(),
                               next_goal=lambda r, s: (asked.append(1), None)[1],
                               to_intent=lambda g, s: None, dispatch=lambda i: None)
                d.goal = object()
                d._advance(ActivityResult(FINISHED, {}), state)
                self.assertEqual(1, len(asked),
                                 "whatever is on screen now, the result came from an "
                                 "activity `step` has already judged")


if __name__ == "__main__":
    unittest.main()
