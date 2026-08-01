# brain/known_goals.py
#
# Layer 4c — wired entry points that invoke achieve_goal for specific
# goals using the live perceive / capture / dispatch / replan callables.
#
# Each function in this module is a thin caller of achieve_goal that
# supplies:
#   - the Goal definition (goal_id, predicate_text, cue_catalog_path)
#   - production callables for perceive_fn, capture_fn, execute_step_fn
#   - the replan_fn (uses brain.replan.request_replan with the default
#     Anthropic API path; falls back to escalation when no API key)
#
# These functions are OPT-IN.  The existing recovery.execute_resolution
# path is unchanged.  Callers that want to try the new architecture for
# a specific goal call the function here directly:
#
#   from brain.known_goals import achieve_has_enough_crew
#   ok = achieve_has_enough_crew(home_port="London")
#
# Once a goal's path stabilises (cue catalog matures, plans accumulate
# success_count, demoted plans drop out), the corresponding branch of
# execute_resolution can be replaced by a call to the function here.
# Until then both paths can coexist.

from __future__ import annotations

from typing import Optional

from loguru import logger

from brain.plan import Goal
from brain.plan_loop import achieve_goal, AchieveGoalResult
from brain.plan_actions import execute_step
from brain.replan import request_replan


# ── Pre-defined goals ────────────────────────────────────────────────────────


HAS_ENOUGH_CREW = Goal(
    goal_id="has_enough_crew",
    description=(
        "Recruitment committed; fleet has crew sufficient for departure.  "
        "The bot may use any provider (inn.recruit_crew, harbor.recruit_crew, "
        "redistribute_crew); the goal is satisfied when the fleet's crew count "
        "has increased since the plan started and the harbor no longer reports "
        "'not enough crew'."
    ),
    predicate_text=(
        "fleet crew count strictly increased since the plan started AND "
        "no 'not enough crew' warning is visible at the harbor screen"
    ),
    cue_catalog_path="memory/knowledge/verification/has_enough_crew.json",
)


# ── Live callables ───────────────────────────────────────────────────────────
#
# These are extracted as module-level helpers so tests can stub them via
# monkeypatching, and live callers import them indirectly through the
# achieve_* functions.


def _live_perceive_fn(frame):
    from brain.perceive import perceive
    return perceive(frame)


def _live_capture_fn():
    from capture.adb_capture import capture_screen
    return capture_screen()


# ── Wired entry points ───────────────────────────────────────────────────────


def achieve_has_enough_crew(
    *,
    home_port: Optional[str] = None,
    max_steps: int = 30,
    max_replans: int = 3,
) -> AchieveGoalResult:
    """
    Resolve a 'not enough crew' departure blocker via the planner
    architecture.  Opt-in entry point for testing the new path side-by-
    side with execute_resolution.

    On success: the bot's fleet has crew sufficient for departure
    (verified by heavy_check against the has_enough_crew cue catalog).

    On failure: the AchieveGoalResult.reason field tells the caller
    why (REASON_NO_PLAN_AVAILABLE, REASON_LIGHT_NO_PROGRESS,
    REASON_HEAVY_UNCERTAIN, REASON_PLAN_EXHAUSTED, etc.) so the
    caller can decide whether to escalate to the human teach loop or
    fall back to the legacy execute_resolution path.

    Parameters:
      home_port:    optional — passed through to action handlers that
                    need to know which port to return to during
                    exit_to_port_overworld.  Not all actions consume it.
      max_steps:    safety cap for the inner achieve_goal loop.
      max_replans:  cap on replan calls per pursuit.

    Returns the full AchieveGoalResult so the caller can inspect
    plan, history, and termination reason.
    """
    logger.info(
        f"[known_goals] achieve_has_enough_crew (home_port={home_port!r}, "
        f"max_steps={max_steps}, max_replans={max_replans})"
    )

    result = achieve_goal(
        HAS_ENOUGH_CREW,
        perceive_fn=_live_perceive_fn,
        capture_fn=_live_capture_fn,
        execute_step_fn=execute_step,
        replan_fn=request_replan,
        max_steps=max_steps,
        max_replans=max_replans,
    )

    logger.info(
        f"[known_goals] achieve_has_enough_crew → success={result.success} "
        f"reason={result.reason!r} duration={result.duration_secs:.1f}s "
        f"steps_executed={len(result.history)}"
    )
    return result
