"""BarterMission end-to-end orchestration (#31) — the capstone that composes the
whole barter-nav pipeline into one autonomous loop:

    strategy (#29 best play) → GATHER materials (#20 route + #23 buy) → SAIL to the
    village (#10) → BARTER (#24 commit / #25 negotiate / #27 jettison / #26 gift) →
    SAIL to the sell port (#10) → SELL (profit-aware sell_all_cargo).

The orchestration is a phase SEQUENCE with early-stop on failure ("escalate, don't
absorb" — a failed phase halts the mission and reports which one + why, rather than
flailing). Every phase is INJECTABLE, so the sequencing is unit-testable with mocked
steps; the live defaults wire the real executors (which still need live calibration).
The #32 flywheel consumes the returned result (realized profit → KB).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


def run_barter_phase(
    play,
    *,
    read_state_fn: Callable,
    commit_fn: Callable,
    max_rounds: int = 20,
    negotiation_fn: Optional[Callable] = None,
    jettison_fn: Optional[Callable] = None,
) -> dict:
    """The BARTER phase: run verified barter commits up to the play's round count,
    interposing negotiation / overflow-jettison handling. Stops on the first commit
    that makes no progress (escalate) or when the round budget is spent.

    read_state_fn() -> {"rounds_remaining": int, "overflow": int}. Injectable so this
    is testable without the live game."""
    target = min(getattr(play, "rounds", max_rounds) or max_rounds, max_rounds)
    committed = 0
    for _ in range(target):
        state = read_state_fn() or {}
        if state.get("rounds_remaining", 1) <= 0:
            break
        res = commit_fn() or {}
        if not res.get("ok"):
            return {"ok": committed > 0, "committed": committed,
                    "reason": f"barter commit stalled after {committed}"}
        committed += 1
        # Clear any overflow the commit produced before the next round.
        overflow = (read_state_fn() or {}).get("overflow", 0)
        if overflow and jettison_fn is not None:
            jettison_fn(overflow)
    return {"ok": committed > 0, "committed": committed,
            "reason": f"bartered {committed} round(s)"}
