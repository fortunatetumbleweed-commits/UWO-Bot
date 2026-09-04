"""Three identical steps is a loop, not a wait. Stop and say so.

Live 2026-09-01 the world map opened the port list, judged it closed (the search box was
parsed as text, not a button), and re-tapped the rail — which toggles an OPEN list shut. It
did that SEVENTEEN times over four minutes:

    42  21:08:55  tap (69,180)
    44  21:09:10  tap (69,180)
    46  21:09:25  tap (69,180)
    ... 17 in all, every ~15s

and nothing stopped it. The stall guard was there, and its own comment states the rule —
"PROGRESS IS CHANGE, NOT ACTION ... an action that changes nothing IS the stall" — but a
WORKING result reset the counter before that test ever ran:

    if getattr(d.last, "status", None) == WORKING:
        stalled = 0

Every one of those ticks returned WORKING {'did': 'opened the port list'}, so something was
always "happening". An activity's word about itself is a conclusion, and the second guiding
principle is that a conclusion is what must not be believed.

Every step is judged now, WORKING included, and the activity's OBSERVED data joins the
comparison — that is what separates a barter committing round after round
(`rounds_committed` 1, 2, 3...) from a rail tapped at the same point with the same result.
Both report WORKING on every tick; only one is getting anywhere.
"""
import unittest

from brain.run_goal import _MAX_RETRIES, _what_this_step_amounted_to


class _Result:
    def __init__(self, status, observed=None):
        self.status, self.observed = status, dict(observed or {})


class _D:
    def __init__(self, last): self.last = last


def _step(state="world_map", goal="ChooseDestination(Barcelona)", intent=None,
          status="working", observed=None, runner_status="running"):
    record = {"state": state, "goal": goal, "intent": intent}
    return _what_this_step_amounted_to(record, _D(_Result(status, observed)), runner_status)


class RepeatingYourselfIsNotProgress(unittest.TestCase):
    def test_the_live_loop_looks_identical_every_time(self):
        a = _step(observed={"did": "opened the port list"})
        b = _step(observed={"did": "opened the port list"})
        self.assertEqual(a, b, "17 taps that the guard could not tell apart")

    def test_the_bound_is_three(self):
        self.assertEqual(_MAX_RETRIES, 3, "three identical goes is a loop, not a wait")


class RealProgressStillCounts(unittest.TestCase):
    """The guard must not stop work that IS moving, however repetitive it looks."""

    def test_a_barter_committing_rounds_keeps_going(self):
        rounds = [_step(state="village", observed={"did": "committed a round",
                                                   "rounds_committed": n})
                  for n in (1, 2, 3, 4)]
        self.assertEqual(len(set(rounds)), 4, "each round is a different step")

    def test_a_market_buying_keeps_going(self):
        buys = [_step(state="building:market", observed={"bought_total": n})
                for n in (460, 920, 1380)]
        self.assertEqual(len(set(buys)), 3)

    def test_moving_between_screens_counts(self):
        self.assertNotEqual(_step(state="port_overworld"), _step(state="sea"))

    def test_a_new_goal_counts(self):
        self.assertNotEqual(_step(goal="Barcelona"), _step(goal="Tripoli"))

    def test_a_new_intent_counts(self):
        self.assertNotEqual(_step(intent="ENTER_BUILDING"), _step(intent="EXIT_BUILDING"))

    def test_the_task_advancing_counts(self):
        self.assertNotEqual(_step(runner_status="running"), _step(runner_status="arrived"))

    def test_the_activity_finishing_counts(self):
        self.assertNotEqual(_step(status="working"), _step(status="finished"))


class ItIsTheSameRuleInBothLoops(unittest.TestCase):
    def test_neither_loop_excuses_a_working_tick_any_more(self):
        import inspect
        from brain import run_goal
        for fn in (run_goal.run_goal, run_goal.run_task):
            src = inspect.getsource(fn)
            self.assertIn("_what_this_step_amounted_to", src, fn.__name__)
            self.assertNotIn('status", None) == WORKING:\n            stalled = 0', src,
                             f"{fn.__name__} still resets on WORKING alone")

    def test_it_stops_loudly(self):
        import inspect
        from brain import run_goal
        for fn in (run_goal.run_goal, run_goal.run_task):
            self.assertIn("logger.error", inspect.getsource(fn),
                          "a loop nobody stopped is a defect, not a warning")


if __name__ == "__main__":
    unittest.main()
