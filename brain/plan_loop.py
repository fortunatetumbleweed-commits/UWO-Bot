# brain/plan_loop.py
#
# achieve_goal — the central plan-execute-verify-replan loop.
# See docs/planner_architecture.md § "The central loop".
#
# Layer 3b of the migration.  Pure orchestration over the Layer 1
# (Plan/Goal data types), Layer 2 (light/heavy verification) and
# Layer 3a (PlanRuntime) primitives.  All external dependencies
# (perceive, capture, action execution, Claude/Moondream calls) are
# injected callables so the loop is testable in isolation and the
# live wiring can be deferred to Layer 4.
#
# Layer 4 will:
#   - add the replan-on-uncertainty Claude call (currently treated as
#     loop termination)
#   - generate plans on the fly when none exist (currently returns failure)
#   - wire the loop into the live bot's execute_resolution / recover_to_*
#     entry points

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Callable, Optional

from loguru import logger

from brain.plan import (
    CueCatalog, Goal, Plan, PlanStep,
    load_cue_catalog,
)
from brain.plan_runtime import PlanRuntime, get_plan_runtime
from brain.verify import (
    GOAL_ACHIEVED, NOT_YET, NO_PROGRESS, PROGRESS, UNCERTAIN,
    HeavyResult, LightResult,
    heavy_check, light_check,
)


# ── Result + history records ─────────────────────────────────────────────────


@dataclass
class ExecutedStep:
    """One step's execution record for the achieve_goal history buffer."""
    step:             PlanStep
    before_perceive:  Any
    after_perceive:   Any
    light_result:     LightResult
    heavy_result:     Optional[HeavyResult] = None
    duration_secs:    float = 0.0


@dataclass
class AchieveGoalResult:
    """Outcome of an achieve_goal run."""
    success:    bool
    reason:     str
    plan:       Optional[Plan] = None
    history:    list[ExecutedStep] = field(default_factory=list)
    duration_secs: float = 0.0
    final_perceive: Any = None


# ── Termination reasons for telemetry ────────────────────────────────────────

REASON_GOAL_ACHIEVED       = "goal_achieved"
REASON_NO_PLAN_AVAILABLE   = "no_plan_available"
REASON_LIGHT_NO_PROGRESS   = "light_no_progress"
REASON_HEAVY_NOT_YET       = "heavy_not_yet_at_end_of_plan"
REASON_HEAVY_UNCERTAIN     = "heavy_uncertain"
REASON_PLAN_EXHAUSTED      = "plan_exhausted_without_goal"
REASON_MAX_STEPS_REACHED   = "max_steps_reached"
REASON_EXECUTE_RAISED      = "execute_step_raised_exception"


# ── The central loop ─────────────────────────────────────────────────────────


def achieve_goal(
    goal:            Goal,
    *,
    perceive_fn:     Callable[[Any], Any],
    capture_fn:      Callable[[], Any],
    execute_step_fn: Callable[[dict], None],
    runtime:         Optional[PlanRuntime] = None,
    catalog:         Optional[CueCatalog] = None,
    max_steps:       int = 30,
    heavy_claude_call:        Optional[Callable] = None,
    moondream_progress_check: Optional[Callable] = None,
    replan_fn:       Optional[Callable] = None,
    max_replans:     int = 3,
) -> AchieveGoalResult:
    """
    Drive the bot toward `goal` using the plan-execute-verify-replan loop.

    Parameters:
      goal:            target Goal whose predicate the heavy_check evaluates
      perceive_fn:     called as perceive_fn(frame) → PerceiveResult-shaped
                       object.  The loop uses .state, .detail, .flow,
                       .flow_step for state-signature comparison.
      capture_fn:      called as capture_fn() → PIL.Image (or any frame
                       value the perceive_fn / execute_step_fn / heavy
                       call accept).
      execute_step_fn: called as execute_step_fn(action_dict) for each
                       PlanStep.action.  The action dict shape is
                       producer-defined (kind / x / y / labels / region
                       / target etc.); this loop is agnostic.
      runtime:         PlanRuntime to consult for plan lookup + commit.
                       Defaults to the module singleton.
      catalog:         optional pre-loaded CueCatalog.  None → load on
                       demand inside commit_success / commit_failure.
      max_steps:       safety budget; aborts the loop if exceeded.
      heavy_claude_call:        passed through to heavy_check
      moondream_progress_check: passed through to light_check

    Returns AchieveGoalResult.

    Layer 3b limitations (Layer 4 will lift):
      - NO_PROGRESS terminates the loop with failure (no replan yet)
      - UNCERTAIN terminates the loop with failure (no replan yet)
      - Plan exhausted without GOAL_ACHIEVED terminates with failure
        (no plan extension yet)
      - No on-the-fly Claude plan generation when lookup returns None
    """
    runtime = runtime or get_plan_runtime()
    history: list[ExecutedStep] = []
    started_at = monotonic()

    # ── Lookup the best plan for this goal ────────────────────────────────────
    plan = runtime.best_plan(goal.goal_id)
    if plan is None or not plan.steps:
        logger.warning(f"[achieve_goal] no viable plan for goal {goal.goal_id!r}")
        return AchieveGoalResult(
            success    = False,
            reason     = REASON_NO_PLAN_AVAILABLE,
            plan       = plan,
            history    = history,
            duration_secs = monotonic() - started_at,
        )

    logger.info(
        f"[achieve_goal] starting goal={goal.goal_id!r} "
        f"plan={plan.plan_id!r} ({len(plan.steps)} steps, "
        f"confidence={plan.confidence})"
    )

    # ── Walk the plan ─────────────────────────────────────────────────────────
    # Walk steps via explicit index so replan can mutate plan.steps mid-loop
    # (insert_step / replace amend the list; we re-read the next index).
    last_perceive: Any = None
    last_after_frame: Any = None
    replans_used = 0
    step_index = 0
    while step_index < len(plan.steps):
        step = plan.steps[step_index]

        if len(history) >= max_steps:
            logger.warning(f"[achieve_goal] reached max_steps={max_steps} — bailing")
            runtime.commit_failure(plan, goal=goal, reason=REASON_MAX_STEPS_REACHED,
                                    context=f"step {step_index + 1}")
            return _build_result(
                success=False, reason=REASON_MAX_STEPS_REACHED,
                plan=plan, history=history, started_at=started_at,
                final_perceive=last_perceive,
            )

        step_start = monotonic()
        before_frame   = capture_fn()
        before_perceive = perceive_fn(before_frame)

        # Execute the action.  If the executor raises, terminate with
        # failure so the planner can record it.
        try:
            execute_step_fn(step.action)
        except Exception as e:
            logger.error(
                f"[achieve_goal] execute_step raised on step {step.step_id!r}: {e}"
            )
            step.record_failure()
            runtime.commit_failure(
                plan, goal=goal,
                reason=f"{REASON_EXECUTE_RAISED}: {e}",
                context=f"step {step.step_id}",
            )
            return _build_result(
                success=False, reason=REASON_EXECUTE_RAISED,
                plan=plan, history=history, started_at=started_at,
                final_perceive=before_perceive,
            )

        after_frame    = capture_fn()
        after_perceive = perceive_fn(after_frame)
        # Timing fix (2026-05-12): perceive_fn may have run interruptor
        # dismissals (tap+press_back) during after_perceive, so `after_frame`
        # is the pre-dismissal capture and can show stale screen state — e.g.
        # the recruit confirmation dialog still visible with pre-hire stepper
        # values.  Re-capture so L4 / heavy / replan all evaluate the settled
        # screen, not the in-flight one.
        after_frame    = capture_fn()
        last_perceive  = after_perceive
        last_after_frame = after_frame
        step_duration  = monotonic() - step_start

        # L4 early-success check (post-2026-05-12 livefix) ───────────────────
        # If the goal has a local predicate AND it now evaluates True,
        # the plan has succeeded — regardless of which step we're on or
        # what light_check would say.  This catches the case where the
        # transaction step (step_3b_commit_recruit) achieves the goal
        # and the tail steps (exit, navigate, …) become no-ops that
        # would otherwise fail light_check and terminate the plan with
        # success=False.
        try:
            from brain.verify import check_goal_locally
            local_heavy = check_goal_locally(after_frame, goal)
        except Exception as e:
            logger.debug(f"[achieve_goal] L4 early-check failed: {e}")
            local_heavy = None
        if local_heavy is not None and local_heavy.status == GOAL_ACHIEVED:
            logger.info(
                f"[achieve_goal] step {step.step_id!r} → L4 predicate says "
                f"GOAL_ACHIEVED ({local_heavy.evidence_summary}); "
                "short-circuiting plan as success"
            )
            step.record_success(step_duration)
            plan.record_step_success(step.step_id)
            record = ExecutedStep(
                step=step, before_perceive=before_perceive,
                after_perceive=after_perceive,
                light_result=None,
                heavy_result=local_heavy,
                duration_secs=step_duration,
            )
            history.append(record)
            runtime.commit_success(
                plan,
                duration_secs=monotonic() - started_at,
                goal=goal, heavy_result=local_heavy, catalog=catalog,
                context=f"achieve_goal {goal.goal_id} step {step.step_id} (L4 short-circuit)",
            )
            return _build_result(
                success=True, reason=REASON_GOAL_ACHIEVED,
                plan=plan, history=history, started_at=started_at,
                final_perceive=after_perceive,
            )

        # Light verification ──────────────────────────────────────────────────
        light = light_check(
            before_perceive, after_perceive, step.expected_progress,
            moondream_progress_check=moondream_progress_check,
        )
        record = ExecutedStep(
            step=step, before_perceive=before_perceive,
            after_perceive=after_perceive, light_result=light,
            duration_secs=step_duration,
        )

        if light.status == NO_PROGRESS:
            step.record_failure()
            history.append(record)
            handled = _try_replan(
                plan, step_index, history, after_perceive, after_frame,
                catalog=catalog, goal=goal,
                reason=REASON_LIGHT_NO_PROGRESS,
                replan_fn=replan_fn,
                replans_used=replans_used, max_replans=max_replans,
            )
            if handled is None:
                # No replan available or replan said escalate / replan budget
                # exhausted.  Terminate as before.
                logger.warning(
                    f"[achieve_goal] step {step.step_id!r} → NO_PROGRESS "
                    f"({light.reason!r}); terminating"
                )
                runtime.commit_failure(
                    plan, goal=goal, reason=REASON_LIGHT_NO_PROGRESS,
                    context=f"step {step.step_id}",
                )
                return _build_result(
                    success=False, reason=REASON_LIGHT_NO_PROGRESS,
                    plan=plan, history=history, started_at=started_at,
                    final_perceive=after_perceive,
                )
            replans_used += 1
            step_index = handled       # next step_index after replan
            continue

        # Light reports progress (or CLARIFY which we treat as PROGRESS for
        # now — Layer 4 may handle CLARIFY with a re-perceive cycle).
        step.record_success(step_duration)
        # Fix C: reset the plan's consecutive-failure tracker so a
        # transient earlier miss doesn't accumulate into demotion.
        plan.record_step_success(step.step_id)

        # Heavy verification at checkpoint steps ──────────────────────────────
        if step.is_checkpoint:
            heavy = heavy_check(
                after_frame, goal,
                catalog=catalog if catalog is not None else load_cue_catalog(goal.goal_id),
                claude_call=heavy_claude_call,
            )
            record.heavy_result = heavy
            history.append(record)

            if heavy.status == GOAL_ACHIEVED:
                logger.info(
                    f"[achieve_goal] step {step.step_id!r} heavy → GOAL_ACHIEVED "
                    f"(confidence={heavy.confidence:.2f}); committing success"
                )
                runtime.commit_success(
                    plan,
                    duration_secs=monotonic() - started_at,
                    goal=goal, heavy_result=heavy, catalog=catalog,
                    context=f"achieve_goal {goal.goal_id} step {step.step_id}",
                )
                return _build_result(
                    success=True, reason=REASON_GOAL_ACHIEVED,
                    plan=plan, history=history, started_at=started_at,
                    final_perceive=after_perceive,
                )

            if heavy.status == UNCERTAIN:
                handled = _try_replan(
                    plan, step_index, history, after_perceive, after_frame,
                    catalog=catalog, goal=goal,
                    reason=REASON_HEAVY_UNCERTAIN,
                    replan_fn=replan_fn,
                    replans_used=replans_used, max_replans=max_replans,
                )
                if handled is None:
                    logger.warning(
                        f"[achieve_goal] step {step.step_id!r} heavy → UNCERTAIN "
                        f"(confidence={heavy.confidence:.2f}); terminating"
                    )
                    runtime.commit_failure(
                        plan, goal=goal, heavy_result=heavy,
                        reason=REASON_HEAVY_UNCERTAIN,
                        context=f"step {step.step_id}",
                    )
                    return _build_result(
                        success=False, reason=REASON_HEAVY_UNCERTAIN,
                        plan=plan, history=history, started_at=started_at,
                        final_perceive=after_perceive,
                    )
                replans_used += 1
                step_index = handled
                continue

            # NOT_YET: continue the plan if there are more steps.
            logger.info(
                f"[achieve_goal] step {step.step_id!r} heavy → NOT_YET; continuing"
            )

        else:
            history.append(record)

        step_index += 1

    # ── Plan exhausted without GOAL_ACHIEVED ──────────────────────────────────
    # Per architecture doc, this triggers replan to extend the plan.  When
    # replan is unavailable / escalates / exhausted, terminate.
    final_heavy_status = (
        history[-1].heavy_result.status
        if (history and history[-1].heavy_result is not None)
        else None
    )
    reason = (
        REASON_HEAVY_NOT_YET if final_heavy_status == NOT_YET
        else REASON_PLAN_EXHAUSTED
    )
    handled = _try_replan(
        plan, len(plan.steps) - 1, history, last_perceive, last_after_frame,
        catalog=catalog, goal=goal,
        reason=reason,
        replan_fn=replan_fn,
        replans_used=replans_used, max_replans=max_replans,
    )
    if handled is not None:
        # Plan was extended/replaced — recurse the loop by calling ourselves
        # back into the walking phase.  We do this by tail-iterating: set
        # step_index to handled and re-enter the while loop.  Since we
        # already exited the loop, recreate it via a continuation.
        replans_used += 1
        # The handled value is the next step_index in the (now amended) plan.
        return _continue_walking(
            goal, plan, handled, history, last_perceive, started_at,
            perceive_fn=perceive_fn, capture_fn=capture_fn,
            execute_step_fn=execute_step_fn, runtime=runtime,
            catalog=catalog, max_steps=max_steps,
            heavy_claude_call=heavy_claude_call,
            moondream_progress_check=moondream_progress_check,
            replan_fn=replan_fn,
            replans_used=replans_used, max_replans=max_replans,
        )

    logger.warning(
        f"[achieve_goal] plan {plan.plan_id!r} exhausted without GOAL_ACHIEVED; "
        f"reason={reason}"
    )
    runtime.commit_failure(
        plan, goal=goal,
        heavy_result=history[-1].heavy_result if history else None,
        reason=reason, context="plan_exhausted",
    )
    return _build_result(
        success=False, reason=reason,
        plan=plan, history=history, started_at=started_at,
        final_perceive=last_perceive,
    )


def _try_replan(
    plan: Plan, step_index: int, history: list,
    current_perceive: Any, current_frame: Any,
    *, catalog: Optional[CueCatalog], goal: Goal,
    reason: str, replan_fn: Optional[Callable],
    replans_used: int, max_replans: int,
) -> Optional[int]:
    """
    Invoke replan_fn (if provided) and apply the decision to the plan.

    Returns:
      next step_index to attempt (after applying the replan decision), OR
      None if no replan was performed (caller should terminate as before).
    """
    if replan_fn is None:
        return None
    if replans_used >= max_replans:
        logger.warning(
            f"[achieve_goal] replan budget exhausted ({replans_used}/{max_replans}); "
            "terminating instead of replanning again"
        )
        return None

    logger.info(
        f"[achieve_goal] invoking replan (used={replans_used}/{max_replans}, "
        f"reason={reason!r}, current_step_idx={step_index})"
    )
    response = replan_fn(
        goal=goal,
        plan=plan,
        current_step_idx=step_index,
        history=history,
        current_perceive=current_perceive,
        replan_reason=reason,
        frame=current_frame,
        catalog=catalog,
    )

    # Local import to avoid circulars and keep plan_loop independent of replan.
    from brain.replan import (
        DECISION_CONTINUE, DECISION_INSERT_STEP, DECISION_REPLACE,
        DECISION_ESCALATE, apply_replan_to_plan,
    )

    logger.info(
        f"[achieve_goal] replan decision={response.decision!r}  "
        f"reasoning={response.reasoning[:80]!r}"
    )

    if response.decision == DECISION_ESCALATE:
        return None
    if response.decision == DECISION_CONTINUE:
        # Re-attempt the same step (counter on it has already incremented for
        # NO_PROGRESS path) — the planner's counterpart would be Layer 5
        # asking for a fresh perceive before retrying.  For now, advance.
        return step_index + 1
    if response.decision in (DECISION_INSERT_STEP, DECISION_REPLACE):
        apply_replan_to_plan(plan, step_index, response)
        return step_index   # the inserted / replaced step is now at this index

    # Unknown decision — treat as escalate.
    return None


def _continue_walking(
    goal: Goal, plan: Plan, step_index: int, history: list,
    last_perceive: Any, started_at: float, **kw,
) -> "AchieveGoalResult":
    """
    Helper for plan-exhausted replan: re-enter the walk with the amended plan.

    We don't recurse achieve_goal directly because the lookup phase shouldn't
    re-run (that would pick a different plan, possibly an older candidate).
    Instead, run the same walking loop with the now-amended plan.in-flight.
    """
    # Build a minimal in-place loop that continues from the given index.
    # Code duplication here is intentional: keeping the main loop linear
    # while still supporting plan extension after exhaustion.  Future
    # refactor can unify these into a single walker method.
    runtime         = kw["runtime"] or get_plan_runtime()
    perceive_fn     = kw["perceive_fn"]
    capture_fn      = kw["capture_fn"]
    execute_step_fn = kw["execute_step_fn"]
    catalog         = kw["catalog"]
    max_steps       = kw["max_steps"]
    heavy_claude_call        = kw["heavy_claude_call"]
    moondream_progress_check = kw["moondream_progress_check"]
    replan_fn       = kw["replan_fn"]
    replans_used    = kw["replans_used"]
    max_replans     = kw["max_replans"]

    last_after_frame = None

    while step_index < len(plan.steps):
        if len(history) >= max_steps:
            runtime.commit_failure(plan, goal=goal, reason=REASON_MAX_STEPS_REACHED)
            return _build_result(
                success=False, reason=REASON_MAX_STEPS_REACHED,
                plan=plan, history=history, started_at=started_at,
                final_perceive=last_perceive,
            )
        step = plan.steps[step_index]
        step_start = monotonic()
        before_frame    = capture_fn()
        before_perceive = perceive_fn(before_frame)
        try:
            execute_step_fn(step.action)
        except Exception as e:
            step.record_failure()
            runtime.commit_failure(plan, goal=goal,
                                    reason=f"{REASON_EXECUTE_RAISED}: {e}")
            return _build_result(
                success=False, reason=REASON_EXECUTE_RAISED,
                plan=plan, history=history, started_at=started_at,
                final_perceive=before_perceive,
            )
        after_frame    = capture_fn()
        after_perceive = perceive_fn(after_frame)
        # Timing fix (2026-05-12): see primary loop for rationale.  Re-capture
        # so L4 / heavy / replan see the post-dismissal settled screen.
        after_frame    = capture_fn()
        last_perceive  = after_perceive
        last_after_frame = after_frame
        step_duration = monotonic() - step_start

        # L4 early-success short-circuit — mirror of the primary path.
        try:
            from brain.verify import check_goal_locally
            local_heavy = check_goal_locally(after_frame, goal)
        except Exception as e:
            logger.debug(f"[achieve_goal] L4 early-check failed: {e}")
            local_heavy = None
        if local_heavy is not None and local_heavy.status == GOAL_ACHIEVED:
            logger.info(
                f"[achieve_goal] step {step.step_id!r} → L4 GOAL_ACHIEVED "
                f"(short-circuit; {local_heavy.evidence_summary})"
            )
            step.record_success(step_duration)
            plan.record_step_success(step.step_id)
            record = ExecutedStep(step=step, before_perceive=before_perceive,
                                   after_perceive=after_perceive,
                                   light_result=None, heavy_result=local_heavy,
                                   duration_secs=step_duration)
            history.append(record)
            runtime.commit_success(plan,
                                    duration_secs=monotonic() - started_at,
                                    goal=goal, heavy_result=local_heavy, catalog=catalog,
                                    context=f"achieve_goal {goal.goal_id} step {step.step_id} (L4 short-circuit)")
            return _build_result(
                success=True, reason=REASON_GOAL_ACHIEVED,
                plan=plan, history=history, started_at=started_at,
                final_perceive=after_perceive,
            )

        light = light_check(before_perceive, after_perceive, step.expected_progress,
                             moondream_progress_check=moondream_progress_check)
        record = ExecutedStep(step=step, before_perceive=before_perceive,
                                after_perceive=after_perceive, light_result=light,
                                duration_secs=step_duration)

        if light.status == NO_PROGRESS:
            step.record_failure()
            history.append(record)
            handled = _try_replan(plan, step_index, history, after_perceive, after_frame,
                                   catalog=catalog, goal=goal,
                                   reason=REASON_LIGHT_NO_PROGRESS,
                                   replan_fn=replan_fn,
                                   replans_used=replans_used, max_replans=max_replans)
            if handled is None:
                runtime.commit_failure(plan, goal=goal, reason=REASON_LIGHT_NO_PROGRESS)
                return _build_result(
                    success=False, reason=REASON_LIGHT_NO_PROGRESS,
                    plan=plan, history=history, started_at=started_at,
                    final_perceive=after_perceive,
                )
            replans_used += 1
            step_index = handled
            continue

        step.record_success(step_duration)
        if step.is_checkpoint:
            heavy = heavy_check(after_frame, goal,
                                 catalog=catalog if catalog is not None
                                          else load_cue_catalog(goal.goal_id),
                                 claude_call=heavy_claude_call)
            record.heavy_result = heavy
            history.append(record)

            if heavy.status == GOAL_ACHIEVED:
                runtime.commit_success(plan,
                                        duration_secs=monotonic() - started_at,
                                        goal=goal, heavy_result=heavy, catalog=catalog,
                                        context=f"achieve_goal {goal.goal_id} step {step.step_id}")
                return _build_result(
                    success=True, reason=REASON_GOAL_ACHIEVED,
                    plan=plan, history=history, started_at=started_at,
                    final_perceive=after_perceive,
                )
            if heavy.status == UNCERTAIN:
                handled = _try_replan(plan, step_index, history, after_perceive, after_frame,
                                       catalog=catalog, goal=goal,
                                       reason=REASON_HEAVY_UNCERTAIN,
                                       replan_fn=replan_fn,
                                       replans_used=replans_used, max_replans=max_replans)
                if handled is None:
                    runtime.commit_failure(plan, goal=goal, heavy_result=heavy,
                                            reason=REASON_HEAVY_UNCERTAIN)
                    return _build_result(
                        success=False, reason=REASON_HEAVY_UNCERTAIN,
                        plan=plan, history=history, started_at=started_at,
                        final_perceive=after_perceive,
                    )
                replans_used += 1
                step_index = handled
                continue
        else:
            history.append(record)
        step_index += 1

    final_heavy_status = (history[-1].heavy_result.status
                          if (history and history[-1].heavy_result is not None) else None)
    reason = (REASON_HEAVY_NOT_YET if final_heavy_status == NOT_YET
              else REASON_PLAN_EXHAUSTED)
    runtime.commit_failure(plan, goal=goal,
                            heavy_result=history[-1].heavy_result if history else None,
                            reason=reason)
    return _build_result(
        success=False, reason=reason,
        plan=plan, history=history, started_at=started_at,
        final_perceive=last_perceive,
    )


def _build_result(
    *,
    success: bool, reason: str,
    plan: Optional[Plan], history: list[ExecutedStep],
    started_at: float, final_perceive: Any,
) -> AchieveGoalResult:
    return AchieveGoalResult(
        success        = success,
        reason         = reason,
        plan           = plan,
        history        = history,
        duration_secs  = monotonic() - started_at,
        final_perceive = final_perceive,
    )
