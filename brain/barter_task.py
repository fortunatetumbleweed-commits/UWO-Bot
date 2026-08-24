"""The barter phase as a TASK the one loop can drive (opt-in).

Flagged migration: `brain.mission.run_mission` and the sub-task graph remain the default.
This path exists so the loop can be proven against real screens without putting a mission
that finally works end to end at risk. Enable with `UWO_TASK_LOOP=1`.

The slice is deliberately the BARTER phase — get to the village, barter — because it is the
smallest coherent piece and the one where the sub-loops did their damage: a primitive walked
the fleet out of the village the mission had just reached, and another committed blind on the
village interior.

Layering (docs/one_loop_task_drives_state.md):
  - this task owns the SEQUENCE and answers "whether"
  - `brain.game_rules` supplies what the GAME's rules imply
  - `brain.nav_step` moves one step and `brain.unexpected` clears what is in the way
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from loguru import logger

from brain import game_rules, mission_progress


def enabled() -> bool:
    """True when the loop should drive the barter phase instead of the graph."""
    return os.environ.get("UWO_TASK_LOOP", "").strip() not in ("", "0", "false", "no")


@dataclass
class Step:
    """One unit of work, with the states that bracket it.

    `needs_state` is where it must RUN FROM; `expect_state` is where success LEAVES the bot.
    Together they are the concrete "desired state" that perceive-act-verify needed and never
    had — the loop navigates to the first and checks the second, and a mismatch routes to
    `brain.unexpected` rather than being carried silently forward.
    """
    name: str
    run: Callable[[], dict]
    needs_state: Optional[str] = None
    expect_state: Optional[str] = None
    may_leave_a_place: bool = False


class BarterPhaseTask:
    """Get to the village, then barter. No village check, no cargo check.

    Both questions were closed by the phases before this one — see
    `brain.mission_progress`. Re-opening them is what sent the fleet back to sea.
    """

    def __init__(self, village: str, good: str, rounds: int, executors: Any = None):
        self.village = village
        self.good = good
        self.rounds = int(rounds)
        self._ex = executors
        self._done = False
        self._bartered = False

    # ── task protocol ────────────────────────────────────────────────────────

    def done(self) -> bool:
        return self._done

    def next_step(self, state: Optional[str]) -> Optional[Step]:
        if self._bartered:
            return None
        if state == "village":
            return Step("barter", self._barter, needs_state="village",
                        expect_state="village")
        # Getting there may mean leaving where we are — that IS this task's call to make,
        # which is exactly why the permission travels on the step.
        return Step("sail_to_village", self._sail, expect_state="village",
                    may_leave_a_place=True)

    def decide(self, u) -> Optional[str]:
        """Which dialog option to take. Answered by the GAME's rules, never by a default.

        Every dialog goes through the decider. Its default is the positive option; the one
        thing it will not auto-answer is a dialog spending RED GEMS (real money), which
        returns None and stops the loop for a human.
        """
        return game_rules.answer_dialog(u.options, u.text, positive=u.positive)

    def record(self, step: Step, result: dict) -> None:
        if not result.get("ok"):
            return
        if step.name == "barter":
            self._bartered = True
            self._done = True
            mission_progress.advance("sailing_route")

    # ── steps ────────────────────────────────────────────────────────────────

    def _executors(self):
        if self._ex is None:
            from brain.barter_mission_live import make_live_executors
            self._ex = make_live_executors()
        return self._ex

    def _sail(self) -> dict:
        from brain.mission import SubTask
        return self._executors()["sail_to_village"](
            SubTask(id="sail_to_village", kind="sail_to_village", location=self.village,
                    params={"village": self.village})) or {}

    def _barter(self) -> dict:
        from brain.mission import SubTask
        return self._executors()["barter"](
            SubTask(id="barter", kind="barter", location=self.village,
                    params={"rounds": self.rounds, "good": self.good,
                            "village": self.village})) or {}


def run_barter_phase(village: str, good: str, rounds: int, *, executors=None,
                     max_ticks: int = 40) -> dict:
    """Drive the barter phase with the one loop. Returns the same shape as the graph path."""
    from brain.task_loop import run_task

    task = BarterPhaseTask(village, good, rounds, executors=executors)
    logger.info(f"[barter_task] driving the barter phase with the ONE loop "
                f"({village}, {rounds} round(s))")
    res = run_task(task, max_ticks=max_ticks)
    return {"ok": res.ok, "reason": res.reason, "ticks": res.n_ticks,
            "driver": "task_loop"}
