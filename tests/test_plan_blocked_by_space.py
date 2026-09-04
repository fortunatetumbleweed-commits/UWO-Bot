"""An overloaded hold must clear surplus and re-plan, not report a dead end.

Live 2026-08-21: an over-buy left the hold at 3823/4108. Every mission launched afterwards
exited immediately at "plan: 0 round(s) (limited by space)" having sold nothing — 13 actions
and out. The capability to fix it existed TWICE and was reachable neither way:

  * `clear_surplus_at_current_port` runs BEFORE planning, but is gated on
    `clear_surplus=False` and only enabled by the `--clear-surplus` flag.
  * the `sell_surplus` sub-task lives INSIDE the mission graph, which is only built when
    the plan has rounds > 0.

So the hold was too full to plan, and the step that would empty it sat behind the plan.
Being overloaded is precisely the condition surplus-clearing exists for, so that case now
clears and re-plans once.
"""
from unittest import mock

import pytest

import brain.barter_command as bc


def _plan(rounds):
    p = mock.MagicMock()
    p.rounds, p.limited_by, p.cushion = rounds, "space", 0.15
    p.total_needs = {"Ebony": 350} if rounds else {"Ebony": 0}
    p.output_qty, p.reserved_per_round, p.peak_per_round = 679 * rounds, 500, 430
    return p


def _check():
    """The village reading, as the passive task runner hands it back.

    `run_barter_command` no longer calls `read_village_barter_remote`: it returns a
    `RemoteCheck` work order and `run_task` turns the crank (Guiding Principle #7). So the
    thing to stub is the crank, and what it returns answers `trade_for` / `rounds_remaining`
    exactly as the old `VillageCheck` did.
    """
    chk = mock.MagicMock()
    chk.ok, chk.rounds_remaining = True, 7
    chk.status, chk.reason = "have-recipe", ""
    chk.trades = [mock.MagicMock()]                 # non-empty: the read completed
    # The hold is a second consultation of the same crank, so the stub answers it too. Real
    # numbers, not MagicMocks: the plan does arithmetic with them.
    chk.capacity, chk.used = 4108, 500
    chk.trade_for.return_value = mock.MagicMock(obtain=mock.MagicMock(), materials={})
    return chk


@pytest.fixture
def wiring(monkeypatch):
    calls = {"cleared": 0, "plans": 0, "mission": 0, "trim_to": None}
    plans = []

    def fake_plan(*_a, **_kw):
        calls["plans"] += 1
        return plans.pop(0)

    def fake_clear(_good, trim_to=None):
        calls["cleared"] += 1
        calls["trim_to"] = trim_to
        return {"ok": True, "port": "Jakarta", "reason": "trimmed {'Ebony': 1331}"}

    def fake_mission(*_a, **_kw):
        calls["mission"] += 1
        return {"ok": True, "step": "mission"}

    # All of these are imported INSIDE run_barter_command, so patch them at the source
    # module — patching this module's namespace would never be consulted.
    monkeypatch.setattr("brain.run_goal.run_task", lambda *a, **k: _check())
    monkeypatch.setattr("brain.barter_quantity.plan_barter_rounds", fake_plan)
    monkeypatch.setattr("brain.barter_quantity.free_space_for_barter",
                        lambda cap, used: max(cap - used, 0))
    monkeypatch.setattr("brain.barter_mission_live.clear_surplus_at_current_port", fake_clear)
    monkeypatch.setattr("actions.fleet_status.read_fleet_status",
                        lambda *a, **k: {"cargo_used": 500, "cargo_capacity": 4108})
    monkeypatch.setattr(bc, "_run_mission_for", fake_mission)
    return calls, plans


CMD = "barter Box of Nutmeg at Melanesian Village"


class TestOverloadedHold:
    def test_zero_rounds_clears_surplus_and_replans(self, wiring):
        """The deadlock: too full to plan, and the fix sits behind the plan.

        Three plan calls: the blocked one, the 'ideal' one that sizes the trim target, and
        the re-plan against the freed hold."""
        calls, plans = wiring
        plans.extend([_plan(0), _plan(2), _plan(2)])
        res = bc.run_barter_command(CMD, cargo_capacity=4108, cargo_used=3823)
        assert calls["cleared"] == 1, "an overloaded hold must clear surplus"
        assert calls["plans"] == 3, "blocked plan, ideal plan for the trim target, re-plan"
        assert res.get("ok"), "and then run the mission"
        assert calls["mission"] == 1

    def test_the_trim_target_is_what_the_mission_would_need(self, wiring):
        """Whole-good clearing keeps materials, so a hold full of over-bought MATERIAL is
        only freed by trimming it back to the needed quantity."""
        calls, plans = wiring
        plans.extend([_plan(0), _plan(2), _plan(2)])
        bc.run_barter_command(CMD, cargo_capacity=4108, cargo_used=3823)
        assert calls["trim_to"] == {"Ebony": 350}, \
            "the trim target must come from what the mission needs, not a guess"

    def test_a_feasible_plan_sells_nothing(self, wiring):
        """Clearing is consequential — only when space is the blocker."""
        calls, plans = wiring
        plans.append(_plan(2))
        bc.run_barter_command(CMD, cargo_capacity=4108, cargo_used=500)
        assert calls["cleared"] == 0
        assert calls["plans"] == 1

    def test_dry_run_never_sells(self, wiring):
        calls, plans = wiring
        plans.append(_plan(0))
        res = bc.run_barter_command(CMD, cargo_capacity=4108, cargo_used=3823, dry_run=True)
        assert calls["cleared"] == 0, "a dry run must not sell cargo"
        assert not res.get("ok")

    def test_still_blocked_after_clearing_reports_why(self, wiring):
        """If clearing did not free enough, say so rather than looping."""
        calls, plans = wiring
        plans.extend([_plan(0), _plan(2), _plan(0)])
        res = bc.run_barter_command(CMD, cargo_capacity=4108, cargo_used=3823)
        assert calls["cleared"] == 1
        assert not res.get("ok")
        assert res["step"] == "plan"
        assert "no feasible rounds" in res["reason"]

    def test_explicit_pre_clear_is_not_repeated(self, wiring):
        """--clear-surplus already cleared before planning; don't sell twice."""
        calls, plans = wiring
        plans.extend([_plan(0), _plan(2), _plan(0)])
        bc.run_barter_command(CMD, cargo_capacity=4108, cargo_used=3823, clear_surplus=True)
        assert calls["cleared"] == 1, "the pre-plan clear ran; the fallback must not re-run it"
