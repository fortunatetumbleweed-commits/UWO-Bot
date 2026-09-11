"""`consult_obstruction` asks whether an obstruction relates to the goal. Give it one.

Live 2026-09-08: 92 consults in one run, every one keyed `no_goal`, every one answering
`irrelevant`, and 177 cache files written under that key. `relates_to_goal` was True 0 times
out of 92 — a check that structurally could not go the other way, because `current_goal()`
returned None on every dispatcher tick. Only the older `brain/goals/*` layer ever pushed a
context, and that layer does not run the barter path.

The consult side already took a `goal_context`, keyed its cache on it and formatted it into
the prompt. Only the publisher was missing.
"""

from __future__ import annotations

import unittest
import unittest.mock as mock


class TheAmbientGoalIsReadableWithoutAScope(unittest.TestCase):

    def setUp(self):
        from brain.goal_context import clear_goal_stack
        clear_goal_stack()
        self.addCleanup(clear_goal_stack)

    def test_nothing_published_reads_as_no_goal(self):
        """The default IS today's behaviour — that is what makes this safe to add."""
        from brain.goal_context import current_goal
        self.assertIsNone(current_goal())

    def test_a_published_goal_is_current(self):
        from brain.goal_context import GoalContext, current_goal, set_ambient_goal
        set_ambient_goal(GoalContext(intent="Hold", target={"goal": "hold Ebony (~942)"}))
        found = current_goal()
        self.assertEqual("Hold", found.intent)
        self.assertEqual("hold Ebony (~942)", found.target["goal"])

    def test_a_pushed_scope_still_wins(self):
        """A narrower action chain describes the moment better than the standing goal."""
        from brain.goal_context import GoalContext, current_goal, goal, set_ambient_goal
        set_ambient_goal(GoalContext(intent="Hold"))
        with goal("recruit_crew"):
            self.assertEqual("recruit_crew", current_goal().intent)
        self.assertEqual("Hold", current_goal().intent)

    def test_clearing_drops_the_ambient_goal_too(self):
        from brain.goal_context import (GoalContext, clear_goal_stack,
                                        current_goal, set_ambient_goal)
        set_ambient_goal(GoalContext(intent="Hold"))
        clear_goal_stack()
        self.assertIsNone(current_goal(), "a goal must not outlive its task run")


class TheDispatcherSaysWhatItIsWorkingOn(unittest.TestCase):

    def setUp(self):
        from brain.goal_context import clear_goal_stack
        clear_goal_stack()
        self.addCleanup(clear_goal_stack)

    def _dispatcher(self, goal):
        from brain.dispatcher import Dispatcher
        d = Dispatcher.__new__(Dispatcher)          # no I/O, no perception
        d.goal = goal
        d._standing_in = None
        return d

    def test_the_goal_and_the_action_both_reach_the_consult(self):
        from brain.activities.market import Hold
        from brain.goal_context import current_goal
        d = self._dispatcher(Hold({"Ebony": 942}))
        d._publish_goal("tapped Sell")
        found = current_goal()
        self.assertEqual("Hold", found.intent)
        self.assertIn("Ebony", found.target["goal"])
        self.assertEqual("tapped Sell", found.target["doing"],
                         "what the activity just did is what names the screen in front of us")

    def test_no_goal_publishes_nothing(self):
        from brain.goal_context import current_goal
        d = self._dispatcher(None)
        d._publish_goal("tapped Sell")
        self.assertIsNone(current_goal())

    def test_an_activity_that_said_nothing_leaves_doing_out(self):
        from brain.activities.market import Hold
        from brain.goal_context import current_goal
        d = self._dispatcher(Hold({"Ebony": 942}))
        d._publish_goal(None)
        self.assertNotIn("doing", current_goal().target)


class TheActionComesFromTheActivitysOwnReport(unittest.TestCase):

    def test_did_is_read_from_the_result(self):
        from brain.dispatcher import ActivityResult, WORKING, _did
        self.assertEqual("scrolled",
                         _did(ActivityResult(WORKING, {"did": "scrolled"})))

    def test_a_result_that_said_nothing_is_none(self):
        from brain.dispatcher import ActivityResult, WORKING, _did
        self.assertIsNone(_did(ActivityResult(WORKING, {"port": "Jakarta"})))
        self.assertIsNone(_did(None))


class TheConsultKeysItsCacheOnTheGoal(unittest.TestCase):
    """The wiring's whole point: a different goal must not reuse another goal's verdict."""

    def test_the_cache_path_carries_the_goal_intent(self):
        from vision.obstruction_consult import _cache_path
        with_goal = _cache_path("dialog", "abc123", "Hold")
        without   = _cache_path("dialog", "abc123", None)
        self.assertNotEqual(with_goal, without)
        self.assertIn("no_goal", without.name)
        self.assertIn("Hold", with_goal.name)


if __name__ == "__main__":
    unittest.main()
