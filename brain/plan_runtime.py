# brain/plan_runtime.py
#
# PlanRuntime — the planner facade.
# See docs/planner_architecture.md § "What 'planner' needs to actually do".
#
# Three responsibilities, all delegating to lower-level pieces:
#
#   1. Lookup   — given a goal, return ranked candidate Plans from the KB.
#                 Combines the new Plan KB (memory/knowledge/plans/) with
#                 the legacy FSM flows.json so existing recipes are
#                 visible without an explicit migration step.
#
#   2. Commit   — record success or failure of a Plan traversal:
#                   - update Plan metadata (record_success / record_failure)
#                   - persist the Plan to the new Plan KB
#                   - apply heavy-check observations to the cue catalog
#
#   3. Skip     — filter out demoted plans (confidence='low' AND
#                 failure_count >= threshold) so callers never select
#                 known-broken paths.
#
# This module is Layer 3a of the migration.  Layer 3b (achieve_goal —
# the central plan-execute-verify-replan loop) lives in
# brain/plan_loop.py and uses this runtime as its plan source.  Layer 4
# adds the replan-on-uncertainty Claude integration.

from __future__ import annotations

from typing import Optional

from loguru import logger

from brain.plan import (
    CueCatalog, Goal, Plan, PROVENANCE_HAND_AUTHORED,
    list_plans_for_goal, load_cue_catalog, plan_from_flow_dict,
    save_cue_catalog, save_plan,
)
from brain.verify import HeavyResult, update_cue_catalog


class PlanRuntime:
    """
    The planner facade.  Stateless apart from delegated KB I/O — all
    persistence happens via brain/plan.py helpers.

    The same instance can be reused across goals and across calls;
    there is no per-call state on the runtime itself.
    """

    # ── Lookup ────────────────────────────────────────────────────────────────

    def lookup(self, goal_id: str) -> list[Plan]:
        """
        Return ranked candidate plans for the goal, best-first.

        Sources, merged:
          - new Plan KB:       memory/knowledge/plans/<goal_id>/*.json
          - legacy FSM flows:  any flow whose terminal_state == goal_id
                               (loaded via plan_from_flow_dict)

        Filters:
          - plans whose should_skip() returns True are excluded
            (confidence='low' AND failure_count >= threshold)

        Ordering (descending preference):
          - higher success_rate first
          - shorter avg_duration_secs first
          - higher travel_count first (more evidence)
        """
        plans: list[Plan] = list(list_plans_for_goal(goal_id))

        # Legacy flows: pull anything whose terminal_state matches.
        try:
            from brain.fsm_registry import get_fsm_registry
            registry = get_fsm_registry()
            for flow_id, flow in registry.flows.items():
                if flow.terminal_state != goal_id:
                    continue
                # Avoid duplicating if the flow has already been migrated.
                if any(p.plan_id == flow_id for p in plans):
                    continue
                plans.append(plan_from_flow_dict(flow._raw))
        except Exception as e:
            logger.debug(f"[plan_runtime] could not load legacy flows for {goal_id!r}: {e}")

        viable = [p for p in plans if not p.should_skip()]
        viable.sort(
            key=lambda p: (
                -p.success_rate,
                p.avg_duration_secs if p.avg_duration_secs > 0 else float("inf"),
                -p.travel_count,
            ),
        )
        return viable

    def best_plan(self, goal_id: str) -> Optional[Plan]:
        """The single highest-quality plan for the goal, or None if all demoted/absent."""
        plans = self.lookup(goal_id)
        return plans[0] if plans else None

    # ── Commit ────────────────────────────────────────────────────────────────

    def commit_success(
        self,
        plan: Plan,
        duration_secs: float,
        *,
        goal: Optional[Goal] = None,
        heavy_result: Optional[HeavyResult] = None,
        catalog: Optional[CueCatalog] = None,
        context: str = "",
    ) -> None:
        """
        Record a successful plan traversal.

        Side effects:
          - plan.record_success(duration_secs) updates counters and may
            promote confidence (unverified → high after two confirmed
            successes per architecture-doc Rule 2).
          - The Plan is persisted to the new Plan KB.
          - If a HeavyResult is supplied, its cue observations are
            applied to the cue catalog and the catalog is persisted.
            If `catalog` is None, an empty one is created on demand.

        Parameters:
          plan:         the Plan that succeeded
          duration_secs: how long the traversal took (seconds)
          goal:         optional — used to locate cue catalog
          heavy_result: optional — observations from the final heavy check
          catalog:      optional — pre-loaded cue catalog (else load on demand)
          context:      free-text label for cue provenance (e.g.
                        "achieve_goal completion", recipe id + step)
        """
        plan.record_success(duration_secs)
        save_plan(plan)

        if heavy_result is not None and goal is not None:
            cat = catalog if catalog is not None else load_cue_catalog(goal.goal_id)
            if cat is None:
                cat = CueCatalog(
                    goal           = goal.goal_id,
                    description    = goal.description,
                    predicate_text = goal.predicate_text,
                )
            update_cue_catalog(cat, heavy_result, context=context)
            save_cue_catalog(cat)

        logger.info(
            f"[plan_runtime] commit_success: {plan.plan_id!r} "
            f"goal={plan.goal_id!r} success={plan.success_count} "
            f"travel={plan.travel_count} confidence={plan.confidence}"
        )

    def commit_failure(
        self,
        plan: Plan,
        *,
        goal: Optional[Goal] = None,
        heavy_result: Optional[HeavyResult] = None,
        catalog: Optional[CueCatalog] = None,
        reason: str = "",
        context: str = "",
    ) -> None:
        """
        Record a failed plan traversal.

        Side effects:
          - plan.record_failure() increments counters; may demote
            confidence to 'low' once threshold is reached.
          - The Plan is persisted with updated state.
          - If a HeavyResult is supplied (heavy returned NOT_YET / UNCERTAIN
            with observations), its observations still feed the cue
            catalog — refutations are valuable signal.

        Parameters:
          plan:         the Plan that failed
          goal:         optional — for cue catalog context
          heavy_result: optional — observations from the failed heavy check
          catalog:      optional — pre-loaded catalog
          reason:       free-text reason for logs
          context:      free-text provenance label
        """
        before_conf = plan.confidence
        # Extract the failed step id from `context` (callers pass
        # f"step {step_id}") so the demotion policy can apply the
        # same-step-streak rule (Fix C).
        failed_step_id: Optional[str] = None
        if context.startswith("step "):
            failed_step_id = context[len("step "):].strip() or None
        plan.record_failure(failed_step_id=failed_step_id)
        save_plan(plan)

        if heavy_result is not None and goal is not None:
            cat = catalog if catalog is not None else load_cue_catalog(goal.goal_id)
            if cat is None:
                cat = CueCatalog(
                    goal           = goal.goal_id,
                    description    = goal.description,
                    predicate_text = goal.predicate_text,
                )
            update_cue_catalog(cat, heavy_result, context=context)
            save_cue_catalog(cat)

        if plan.confidence != before_conf:
            logger.warning(
                f"[plan_runtime] commit_failure: {plan.plan_id!r} demoted "
                f"{before_conf} → {plan.confidence}  reason={reason!r}  "
                f"fail={plan.failure_count} success={plan.success_count}"
            )
        else:
            logger.info(
                f"[plan_runtime] commit_failure: {plan.plan_id!r} "
                f"reason={reason!r}  fail={plan.failure_count} "
                f"confidence={plan.confidence}"
            )


# ── Module-level singleton ────────────────────────────────────────────────────


_instance: Optional[PlanRuntime] = None


def get_plan_runtime() -> PlanRuntime:
    """Return the process-wide PlanRuntime instance."""
    global _instance
    if _instance is None:
        _instance = PlanRuntime()
    return _instance
