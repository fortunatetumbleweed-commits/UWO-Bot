"""The state machine's single-move API: take ONE step toward a requested UI state.

This is the half of `docs/one_loop_task_drives_state.md` that the state machine owns. It
answers exactly one question — "what is the next move from here to there?" — makes that one
move, and reports what happened. It owns no sequence, no retries and no recovery policy: the
caller's loop owns iteration, so the task is consulted between every move.

Contrast with what it replaces. `open_world_map`, `navigate_to_building` and friends each ran
a private loop that re-perceived, judged the state wrong, and navigated to force it. Those
loops could not know what the bot was trying to achieve, so they were locally reasonable and
globally wrong — one pressed Back until the fleet left the village a mission had just sailed
to (live 2026-08-22).

Route-finding is NOT reimplemented here. `brain.planner.find_path` already BFSes the FSM
graph between arbitrary states; this module executes a single edge of that path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from loguru import logger

# Outcomes of one step.
MOVED = "moved"              # a move was made; re-perceive and ask again
ARRIVED = "arrived"          # already at the requested state
BLOCKED = "blocked"          # something is in the way (see brain.unexpected)
NO_ROUTE = "no_route"        # the FSM knows no way from here to there
REFUSED = "refused"          # the move exists but would cost more than the caller may spend


@dataclass(frozen=True)
class StepResult:
    outcome: str
    state: Optional[str] = None          # where we were when the step was taken
    action: Optional[str] = None         # what the step did
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome in (MOVED, ARRIVED)


# Leaving one of these costs POSITION the task may have sailed for, so it is never done to
# satisfy a state test. The caller must opt in explicitly.
_PLACES = ("port_overworld", "village")


def step_toward(target: str, *, frame=None, state: Optional[str] = None,
                may_leave_a_place: bool = False) -> StepResult:
    """Take at most ONE move from the current state toward `target`.

    Returns immediately after that move — it does not wait for the transition to land. A
    transition in progress is just another state (`loading`), which the caller's next tick
    perceives; that is what lets a task react mid-transition instead of blocking inside a
    poll.

    `may_leave_a_place` must be set for a step that would walk the fleet out of a settlement.
    From a village, leaving IS the only route to the world map — but whether the position can
    be spent is the task's call, not this module's.
    """
    if state is None:
        state = _current_state(frame)
    if state is None:
        return StepResult(BLOCKED, None, None, "state unreadable — re-perceive")
    if state == target:
        return StepResult(ARRIVED, state, None, f"already at {target!r}")

    edges = _find_path(state, target)
    if edges is None:
        return StepResult(NO_ROUTE, state, None, f"no known route {state!r} → {target!r}")
    if not edges:
        return StepResult(ARRIVED, state, None, f"already at {target!r}")

    first = edges[0]
    action = getattr(first, "action", None) or str(first)

    if state in _PLACES and not may_leave_a_place:
        return StepResult(
            REFUSED, state, action,
            f"the fleet is AT {state!r}; moving toward {target!r} would give up that "
            "position — the caller decides whether that is acceptable",
        )

    ok = _execute(action, state)
    if not ok:
        return StepResult(BLOCKED, state, action, f"{action!r} did not run")
    return StepResult(MOVED, state, action, f"{action!r} toward {target!r}")


# ── internals ────────────────────────────────────────────────────────────────


def _current_state(frame) -> Optional[str]:
    try:
        from actions.sail_actions import where_am_i
        if frame is None:
            from capture.adb_capture import capture_screen
            frame = capture_screen()
        return where_am_i(frame).get("location")
    except Exception as exc:
        logger.debug(f"[nav_step] state read failed: {exc}")
        return None


def _find_path(from_state: str, to_state: str):
    try:
        from brain.planner import get_planner
        return get_planner().find_path(from_state, to_state)
    except Exception as exc:
        logger.debug(f"[nav_step] path lookup failed: {exc}")
        return None


def _execute(action: str, state: str) -> bool:
    """Run one FSM edge. Unknown actions are reported, never improvised."""
    from actions import ui
    try:
        if action in ("press_back", "back"):
            ui.back(why=f"one step out of {state!r}")
            return True
        if action in ("exit_screen", "home"):
            from actions.screen_exit import exit_current_screen
            return getattr(exit_current_screen(), "method", None) not in (None, "refused")
        if action == "open_world_map":
            from actions.sail_actions import open_world_map
            return bool(open_world_map())
        logger.warning(f"[nav_step] no executor for FSM action {action!r} — reporting")
        return False
    except Exception as exc:
        logger.warning(f"[nav_step] {action!r} raised: {exc}")
        return False
