"""A fresh run knows nothing, and the dispatcher owns finding out.

Live 2026-08-28: the phone was locked, the daily news cleared, and an Investment Season perk
banner was left covering the screen. `open_world_map` — whose job is one tap — was handed
all of it. It waited for a `TransientActivity` it reached ZERO times, read "Season" out of
the banner, accepted it as a port name, declared "Overworld confirmed" over a scene model
that had already said `sub_menu:perk`, and gave up after ten attempts.

The protocol: perceive, clear ONE layer, perceive again, until a state the task runner can
work in — and only then take a work order.
"""
import types

import pytest

from brain.activities.bootstrap import (WORKABLE, KnowWhereWeAre, PositionKnownActivity,
                                        establish_position)
from brain.dispatcher import ActivityResult, FINISHED, WORKING


def _state(where, port=None):
    return types.SimpleNamespace(state=where, port=port, detail="")


class _Clearing:
    """Stands in for IdleLockActivity / TransientActivity: no GOALS, one action, finishes."""

    def __init__(self, name, log):
        self.name, self._log = name, log
        self.CLEARS_SCREEN = True

    def work(self, goal, state):
        self._log.append(self.name)
        return ActivityResult(FINISHED, {}, detail=f"{self.name} cleared")


def _run(layers, log):
    """`layers` are peeled one per tick, then the workable state repeats."""
    seq = list(layers) + ["port_overworld"] * 20

    def perceive():
        return _state(seq.pop(0), port="London" if seq and seq[0] == "port_overworld" else None)

    acts = {"idle_lock": [_Clearing("idle_lock", log)],
            "transient": [_Clearing("transient", log)],
            "port_overworld": [PositionKnownActivity()]}
    return establish_position(perceive=perceive, activities=acts, max_ticks=12)


def test_it_peels_the_layers_one_tick_at_a_time():
    """Two interruptions stacked — the London case — cleared one per tick, because there may
    always be another beneath."""
    log = []
    res = _run(["idle_lock", "transient"], log)
    assert res["ok"] and res["state"] == "port_overworld"
    assert log == ["idle_lock", "transient"], "one layer per tick, in the order met"


def test_an_already_clear_screen_costs_one_tick():
    log = []
    res = _run([], log)
    assert res["ok"] and log == [], "nothing to clear — do not act"


def test_a_screen_nothing_can_clear_is_reported_not_guessed():
    """The alternative is what happened live: read through the popup and invent an answer."""
    log = []
    seq = ["unknown"] * 30

    def perceive():
        return _state(seq.pop(0))

    res = establish_position(perceive=perceive, activities={}, max_ticks=5)
    assert not res["ok"]
    assert res["state"] is None, "an unestablished position must not be reported as a place"


def test_no_work_order_is_taken_on_an_unknown_position():
    """`ok=False` is the signal the caller must not proceed on — the whole point of doing
    this BEFORE the work order rather than after."""
    res = establish_position(perceive=lambda: _state("unknown"), activities={}, max_ticks=3)
    assert res["ok"] is False and "position" in res["reason"]


def test_the_world_map_counts_as_workable():
    """It is a place the bot WORKS in — selecting a destination — not an obstruction."""
    assert "world_map" in WORKABLE


def test_the_lock_and_the_notice_are_NOT_workable():
    for blocking in ("idle_lock", "transient", "unrecognized_chromed_screen", "unknown"):
        assert blocking not in WORKABLE


def test_it_serves_only_its_own_goal():
    """So every state it covers keeps behaving exactly as it does today for real work."""
    from brain.dispatcher import _serves_goal
    assert _serves_goal(PositionKnownActivity(), KnowWhereWeAre())
    assert not _serves_goal(PositionKnownActivity(), object())
