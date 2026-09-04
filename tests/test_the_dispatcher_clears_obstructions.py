"""The dispatcher's obstruction clearing must actually be switched on.

`Dispatcher.step` has cleared obstructions since it was written:

    if self._unblock is not None and self._unblock(state):
        logger.info("[dispatch] cleared an obstruction — re-perceiving")
        state = self._perceive()

but production built the Dispatcher without an `unblock`, so the branch never ran (found
2026-08-28). That is why `buy_to_goal` grew FIVE `clear_blockers` calls of its own — the
shared one existed, was correct, and was switched off. An interruption is ORTHOGONAL to the
state and can sit over a village, a market or the sea; clearing it belongs in the one place
that sees every tick.
"""
import inspect
import types

from brain.dispatcher import ActivityResult, Dispatcher, FINISHED


def test_production_wires_an_unblock():
    """Assert the WIRING, not the source text.

    This grepped `run_goal`'s source for "unblock=", which broke the moment the Dispatcher
    construction was lifted into a shared helper so `run_task` could use the same wiring —
    the unblock was still passed, in a function the grep no longer read. Catching a
    real Dispatcher and looking at what it was given survives that, and is what the test
    means: production must hand it a callable, or the branch is dead again.
    """
    from unittest import mock

    from brain import run_goal

    seen = {}

    class _Spy(Dispatcher):
        def __init__(self, **kw):
            seen.update(kw)
            raise _Stop()

    class _Stop(Exception):
        pass

    with mock.patch.object(run_goal, "Dispatcher", _Spy):
        for entry, arg in ((run_goal.run_goal, object()), (run_goal.run_task, mock.Mock())):
            seen.clear()
            try:
                entry(arg)
            except _Stop:
                pass
            assert callable(seen.get("unblock")), (
                f"{entry.__name__} built a Dispatcher with no unblock — the branch is dead")


def _perceives_in_one_tick(*, blocked: bool) -> int:
    """How many times a single tick looks at the screen."""
    looks = {"n": 0}

    class _Act:
        name = "market"

        def work(self, goal, state):
            return ActivityResult(FINISHED, {}, detail="done")

    def perceive():
        looks["n"] += 1
        return types.SimpleNamespace(state="market", port="London")

    d = Dispatcher(perceive=perceive, activities={"market": _Act()},
                   next_goal=lambda r, s: "goal" if r is None else None,
                   to_intent=lambda g, s: None, dispatch=lambda i: None,
                   unblock=lambda _s: blocked)
    d.step()
    return looks["n"]


def test_a_cleared_obstruction_costs_exactly_one_extra_look():
    """Differential, because a tick ALWAYS perceives twice on its own — once to decide, and
    once in `_advance`, since an activity finishing means the world may be somewhere else.
    Clearing something adds a third."""
    assert _perceives_in_one_tick(blocked=True) == _perceives_in_one_tick(blocked=False) + 1


def test_nothing_covering_the_screen_costs_nothing():
    """The check must be cheap when it finds nothing, or it taxes every tick."""
    assert _perceives_in_one_tick(blocked=False) == 2
