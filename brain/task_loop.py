"""The ONE loop. It owns the sequence; everything else is consulted.

See `docs/one_loop_task_drives_state.md`. Each tick:

  1. LOOK      — what state are we in, and what is in the way?
  2. RESOLVE   — if something blocks, clear it by its own rule. A decision that only the task
                 (or the game's rules) can make is handed to the task, never guessed here.
  3. RE-ASK    — after ANY correction, ask the task what to do next. Do NOT resume the
                 intention formed before the correction: the world has changed.
  4. NAVIGATE  — if the step needs a different state, take ONE move toward it and tick again.
  5. ACT       — run the step, record the outcome, tick again.

Step 3 is the part that did not exist. Previously a primitive corrected something and then
carried on with the plan it had made before correcting, which is why a fixed screen got
un-fixed on the very next action.

The task is anything with this shape:

    task.done()                     -> bool
    task.next_step(state)           -> Step | None
    task.decide(unexpected)         -> Optional[str]   # which dialog option, if any
    task.record(step, result)       -> None            # optional

and a Step is:

    step.needs_state                -> str | None      # where it must be run from  (PRE)
    step.expect_state               -> str | None      # where success leaves us    (POST)
    step.may_leave_a_place          -> bool            # may it give up a settlement?
    step.run()                      -> dict            # {'ok': bool, ...}

`needs_state` and `expect_state` are what make "did the action work?" answerable. The
perceive-act-verify design (docs/perceive_act_verify_substrate.md) always wanted to compare
expected against actual, but "expected" had no concrete value to hold — so nothing could
fire. A step that names both gives the loop real data: navigate to the pre-state before
acting, and after acting check the post-state. A mismatch is not a failure to paper over, it
is one of the four cases in `brain.unexpected` — and the loop routes it there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from loguru import logger

from brain import nav_step, unexpected as ux


@dataclass
class TickRecord:
    """One pass of the loop, for the caller to inspect or log."""
    n: int
    state: Optional[str]
    case: str
    did: str
    detail: str = ""


@dataclass
class LoopResult:
    ok: bool
    reason: str
    ticks: List[TickRecord] = field(default_factory=list)

    @property
    def n_ticks(self) -> int:
        return len(self.ticks)


def run_task(task: Any, *, max_ticks: int = 60, capture=None) -> LoopResult:
    """Drive `task` to completion. The only loop in the system.

    `max_ticks` is a runaway guard, not a retry policy — a task that keeps asking for the
    same step is a bug to surface, not to paper over.
    """
    if capture is None:
        from capture.adb_capture import capture_screen
        capture = capture_screen

    ticks: List[TickRecord] = []
    expected: Optional[str] = None      # post-state promised by the last successful step

    for n in range(max_ticks):
        if task.done():
            return LoopResult(True, "task complete", ticks)

        frame = capture()
        # VERIFY THE LAST STEP: `expected` is the post-state the previous step promised.
        # `look` reports a mismatch as STATE_CHANGED — case 1 — instead of the loop silently
        # carrying on as though the action landed.
        u = ux.look(frame, expected_state=expected)

        # 2 — something is in the way. Clear it, then RE-ASK rather than carrying on.
        if u.blocks:
            res = ux.resolve(u, decide=getattr(task, "decide", None))
            ticks.append(TickRecord(n, u.state, u.case, res.get("action") or "resolve",
                                    res.get("reason", "")))
            if not res.get("handled"):
                return LoopResult(False, f"blocked: {res.get('reason')}", ticks)
            continue                       # re-perceive; the world just changed

        # 3 — the task decides, from the state we actually observe.
        step = task.next_step(u.state)
        if step is None:
            ticks.append(TickRecord(n, u.state, u.case, "no-step",
                                    "the task has nothing to do from here"))
            return LoopResult(False, f"the task has no step for state {u.state!r}", ticks)

        # 4 — wrong state for this step: ONE move toward the right one, then tick again.
        needs = getattr(step, "needs_state", None)
        if needs and needs != u.state:
            nav = nav_step.step_toward(
                needs, frame=frame, state=u.state,
                may_leave_a_place=bool(getattr(step, "may_leave_a_place", False)))
            ticks.append(TickRecord(n, u.state, u.case, nav.action or nav.outcome,
                                    nav.reason))
            if not nav.ok:
                return LoopResult(False, f"cannot reach {needs!r}: {nav.reason}", ticks)
            continue

        # 5 — act.
        result = step.run() or {}
        expected = getattr(step, "expect_state", None) if result.get("ok") else None
        ticks.append(TickRecord(n, u.state, u.case, f"run:{getattr(step, 'name', 'step')}",
                                str(result.get("reason", ""))))
        if hasattr(task, "record"):
            task.record(step, result)
        if not result.get("ok", False):
            return LoopResult(False, f"step failed: {result.get('reason')}", ticks)

    logger.warning(f"[task_loop] gave up after {max_ticks} ticks — the task kept asking for "
                   "work without completing; that is a bug to look at, not to retry")
    return LoopResult(False, f"exceeded {max_ticks} ticks", ticks)
