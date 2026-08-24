"""The ONE loop: the task owns the sequence, everything else is consulted.

From docs/one_loop_task_drives_state.md. The behaviour that did not exist before is step 3 —
after ANY correction the loop RE-ASKS the task what to do next, instead of resuming the
intention it formed before correcting. That is why a fixed screen used to get un-fixed by the
very next action.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from brain import task_loop, unexpected as ux, nav_step as ns


def _step(name, needs_state=None, ok=True, may_leave=False, on_run=None):
    def run():
        if on_run:
            on_run()
        return {"ok": ok, "reason": name}
    return types.SimpleNamespace(name=name, needs_state=needs_state,
                                 may_leave_a_place=may_leave, run=run)


class _Task:
    """Minimal task: a list of steps, consumed one at a time."""

    def __init__(self, steps, decide=None):
        self._steps = list(steps)
        self.asked = []              # states the loop asked us about
        self.ran = []
        self._decide = decide

    def done(self):
        return not self._steps

    def next_step(self, state):
        self.asked.append(state)
        return self._steps[0] if self._steps else None

    def record(self, step, result):
        self.ran.append(step.name)
        if result.get("ok"):
            self._steps.pop(0)

    def decide(self, u):
        return self._decide(u) if self._decide else None


def _run(task, cases, *, nav=None):
    """Drive the loop with a scripted sequence of `look` verdicts."""
    seq = list(cases)
    with patch.object(ux, "look", side_effect=lambda _f, **_k: seq.pop(0)), \
         patch.object(ux, "resolve",
                      side_effect=lambda u, **k: {"handled": True, "action": "cleared"}), \
         patch.object(ns, "step_toward",
                      side_effect=nav or (lambda t, **k: ns.StepResult(ns.MOVED, "x", t))):
        return task_loop.run_task(task, capture=lambda: object(), max_ticks=12)


def _clear(state="port_overworld"):
    return ux.Unexpected(case=ux.CLEAR, state=state)


class TheLoopOwnsTheSequence(unittest.TestCase):

    def test_a_single_step_task_completes(self):
        task = _Task([_step("barter")])
        res = _run(task, [_clear(), _clear()])
        self.assertTrue(res.ok)
        self.assertEqual(task.ran, ["barter"])

    def test_steps_run_in_order(self):
        task = _Task([_step("a"), _step("b")])
        res = _run(task, [_clear()] * 4)
        self.assertTrue(res.ok)
        self.assertEqual(task.ran, ["a", "b"])

    def test_a_failing_step_stops_the_loop(self):
        task = _Task([_step("a", ok=False), _step("b")])
        res = _run(task, [_clear()] * 4)
        self.assertFalse(res.ok)
        self.assertNotIn("b", task.ran)


class CorrectionsForceAReAsk(unittest.TestCase):
    """The point of the design: after a correction, the plan is re-derived, not resumed."""

    def test_a_blocker_is_cleared_and_the_task_is_asked_again(self):
        task = _Task([_step("barter")])
        popup = ux.Unexpected(case=ux.SYSTEM_POPUP, state="port_overworld")
        res = _run(task, [popup, _clear(), _clear()])
        self.assertTrue(res.ok)
        # The task was asked only AFTER the popup was cleared — never before.
        self.assertEqual(task.asked, ["port_overworld"])

    def test_no_step_runs_while_something_blocks(self):
        task = _Task([_step("barter")])
        popup = ux.Unexpected(case=ux.SYSTEM_POPUP, state="port_overworld")
        with patch.object(ux, "resolve", return_value={"handled": False,
                                                       "reason": "could not dismiss"}):
            with patch.object(ux, "look", side_effect=lambda _f, **_k: popup):
                res = task_loop.run_task(task, capture=lambda: object(), max_ticks=4)
        self.assertFalse(res.ok)
        self.assertEqual(task.ran, [], "nothing may run while the screen is blocked")

    def test_a_dialog_decision_is_the_task_s(self):
        chosen = []
        task = _Task([_step("barter")], decide=lambda u: chosen.append(u.positive) or "Ok")
        dialog = ux.Unexpected(case=ux.ACTION_DIALOG, state="world_map",
                               options=("Cancel", "Ok"), positive="Ok")
        captured = {}

        def _resolve(u, **kw):
            captured["decide"] = kw.get("decide")
            return {"handled": True, "action": "tap:Ok"}

        with patch.object(ux, "resolve", side_effect=_resolve):
            seq = [dialog, _clear(), _clear()]
            with patch.object(ux, "look", side_effect=lambda _f, **_k: seq.pop(0)):
                res = task_loop.run_task(task, capture=lambda: object(), max_ticks=8)
        self.assertTrue(res.ok)
        self.assertIsNotNone(captured["decide"], "the task's decider must reach resolve()")


class NavigationIsOneMovePerTick(unittest.TestCase):

    def test_a_step_in_the_wrong_state_navigates_first(self):
        task = _Task([_step("sell", needs_state="market")])
        moves = []
        res = _run(task,
                   [_clear("port_overworld"), _clear("market"), _clear("market")],
                   nav=lambda t, **k: moves.append(t) or ns.StepResult(ns.MOVED, "x", t))
        self.assertTrue(res.ok)
        self.assertEqual(moves, ["market"], "one move, then the loop ticks again")

    def test_an_unreachable_state_stops_the_loop(self):
        task = _Task([_step("sell", needs_state="market")])
        res = _run(task, [_clear("sea")] * 4,
                   nav=lambda t, **k: ns.StepResult(ns.NO_ROUTE, "sea", None, "no route"))
        self.assertFalse(res.ok)
        self.assertIn("cannot reach", res.reason)

    def test_permission_to_leave_a_place_comes_from_the_step(self):
        task = _Task([_step("sail", needs_state="world_map", may_leave=True)])
        seen = {}
        res = _run(task, [_clear("village"), _clear("world_map"), _clear("world_map")],
                   nav=lambda t, **k: seen.update(k) or ns.StepResult(ns.MOVED, "village", t))
        self.assertTrue(res.ok)
        self.assertTrue(seen["may_leave_a_place"])


class RunawayGuard(unittest.TestCase):

    def test_a_task_that_never_finishes_is_surfaced_not_retried(self):
        task = _Task([_step("a", ok=True)])
        task.record = lambda *_a, **_k: None      # never consumes the step
        res = _run(task, [_clear()] * 20)
        self.assertFalse(res.ok)
        self.assertIn("exceeded", res.reason)


if __name__ == "__main__":
    unittest.main()


class DesiredStateIsConcrete(unittest.TestCase):
    """`needs_state` / `expect_state` give perceive-act-verify something to compare.

    The verify design (docs/perceive_act_verify_substrate.md) always wanted expected-vs-actual,
    but "expected" had no concrete value, so nothing could fire. A step that names both lets
    the loop navigate to the pre-state and CHECK the post-state — and a mismatch routes to the
    four cases in brain.unexpected instead of being carried silently forward.
    """

    def test_the_post_state_is_checked_on_the_next_tick(self):
        task = _Task([_step("sail"), _step("barter")])
        task._steps[0].expect_state = "village"
        seen = []

        def _look(_f, **kw):
            seen.append(kw.get("expected_state"))
            return _clear("village")

        with patch.object(ux, "look", side_effect=_look), \
             patch.object(ns, "step_toward",
                          side_effect=lambda t, **k: ns.StepResult(ns.MOVED, "x", t)):
            task_loop.run_task(task, capture=lambda: object(), max_ticks=6)
        # First tick has nothing to verify; the tick AFTER the sail carries its promise.
        self.assertIsNone(seen[0])
        self.assertEqual(seen[1], "village")

    def test_a_failed_step_promises_nothing(self):
        """A step that did not succeed must not assert where the bot now is."""
        task = _Task([_step("sail", ok=False)])
        task._steps[0].expect_state = "village"
        seen = []

        def _look(_f, **kw):
            seen.append(kw.get("expected_state"))
            return _clear("sea")

        with patch.object(ux, "look", side_effect=_look):
            task_loop.run_task(task, capture=lambda: object(), max_ticks=3)
        self.assertTrue(all(s is None for s in seen))

    def test_a_step_without_a_promise_verifies_nothing(self):
        task = _Task([_step("a")])
        seen = []

        def _look(_f, **kw):
            seen.append(kw.get("expected_state"))
            return _clear()

        with patch.object(ux, "look", side_effect=_look):
            task_loop.run_task(task, capture=lambda: object(), max_ticks=4)
        self.assertTrue(all(s is None for s in seen))
