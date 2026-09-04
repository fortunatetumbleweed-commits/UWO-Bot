"""An ORDER decides which activity fills it; the STATE only says whether we are there yet.

Live 2026-08-27, arriving at Barcelona to buy the Matchlock Gun the mission was short of:

    [dispatch] transient <- Hold(orders={'Iron': 506, 'Matchlock Gun': 253})
    activity is lost   (x3)
    [dispatch] market <- TrimHold(...)          <- moved on; the shelf was never looked at

The buy order was handed to the arrival cinematic, whose entire repertoire is one tap. It
could not fill the order, the goal ran out of ticks, and the mission sailed to the village
still 95 Matchlock short — so the blue-gem restock it needed was never even reached.
"""
import types

import pytest

from brain.dispatcher import UNRECOGNISED, ActivityResult, _serves_goal


class _Order:
    pass


class _OtherOrder:
    pass


class _Market:
    name = "market"
    SERVES = ("building:market",)
    GOALS = (_Order,)

    def work(self, goal, state):
        return ActivityResult("FINISHED", {}, detail="bought")


class _Notice:
    """A state-clearing activity: declares it CLEARS_SCREEN and ignores what it is handed."""
    name = "transient"
    CLEARS_SCREEN = True

    def work(self, goal, state):
        return ActivityResult("FINISHED", {}, detail="tapped away")


def test_an_activity_serves_only_the_goals_it_declares():
    assert _serves_goal(_Market(), _Order())
    assert not _serves_goal(_Market(), _OtherOrder())


def test_the_marker_is_explicit_not_inferred_from_a_missing_declaration():
    """An activity that simply has not declared GOALS is NOT thereby a screen-clearer.
    Inferring it from the absence reclassified every ordinary activity that had not got
    round to declaring goals, including a working market."""
    from brain.dispatcher import _serves_goal

    class _Undeclared:
        name = "market"

        def work(self, goal, state):
            return ActivityResult("FINISHED", {}, detail="bought")

    assert not getattr(_Undeclared(), "CLEARS_SCREEN", False)
    assert _serves_goal(_Undeclared(), _Order())


def test_a_state_clearing_activity_runs_whatever_the_order():
    """The lock, the notice and the unnameable chromed screen are in the way of EVERY goal,
    which is exactly why they declare none — they must run so the real work can proceed."""
    assert _serves_goal(_Notice(), _Order())
    assert _serves_goal(_Notice(), _OtherOrder())


def test_no_goal_is_always_servable():
    assert _serves_goal(_Market(), None)


def test_the_buy_order_is_not_handed_to_the_cinematic():
    """The live case. `_Notice` stands in for `transient`; the order is a market order."""
    from brain.dispatcher import Dispatcher

    ticks = {"worked": []}

    class _Harbor:
        """Declares goals, but not THIS one — the real 'cannot fill it' case."""
        name = "harbor"
        SERVES = ("building:harbor",)
        GOALS = (_OtherOrder,)

        def work(self, goal, state):
            ticks["worked"].append(goal)
            return ActivityResult("FINISHED", {}, detail="should not run")

    intents = []
    state = types.SimpleNamespace(state="building:harbor", port=None, detail="")
    d = Dispatcher(perceive=lambda: state,
                   activities={"building:harbor": _Harbor()},
                   next_goal=lambda result, st: _Order(),
                   to_intent=lambda goal, st: "ENTER_BUILDING" if isinstance(goal, _Order) else None,
                   dispatch=lambda intent: intents.append(intent))
    d.goal = _Order()
    d.step()
    assert ticks["worked"] == [], \
        "an order must never be handed to an activity that declares it cannot fill it"
    assert intents == ["ENTER_BUILDING"], \
        "the dispatcher should transition toward an activity that CAN fill the order"


def test_clearing_a_screen_does_not_retire_the_order():
    """The live Matchlock case. `TransientActivity` dismissed a notice and returned FINISHED;
    `run_goal` reads FINISHED as "this goal is done", so the buy order was retired by an
    activity whose whole repertoire is one tap — and the mission sailed to the village 95
    Matchlock Gun short, never having looked at the shelf."""
    from brain.dispatcher import FINISHED, Dispatcher

    state = types.SimpleNamespace(state="transient", port=None, detail="")
    seen = []
    d = Dispatcher(perceive=lambda: state,
                   activities={"transient": _Notice()},
                   next_goal=lambda result, st: (seen.append(result.status), _Order())[1],
                   to_intent=lambda goal, st: None,
                   dispatch=lambda intent: None)
    d.goal = _Order()
    d.step()
    assert seen and seen[0] != FINISHED, \
        "a screen-clearing FINISHED must not reach the task runner as the goal's result"
    assert isinstance(d.goal, _Order), "the unfilled order must survive"
