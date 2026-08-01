# brain/replan.py
#
# Replan — the planner's self-enhancement primitive.
# See docs/planner_architecture.md § "Replan — the planner's self-enhance cycle".
#
# Layer 4b of the migration.  When achieve_goal's verification reports
# trouble (NO_PROGRESS, UNCERTAIN, plan exhausted without GOAL_ACHIEVED),
# the loop pauses and asks Claude four questions in one call:
#
#   Q1. Has any progress been made since the plan started?
#   Q2. Looking at the current screen, what action OPTIONS do you see?
#   Q3. Do the cues you observed change what action makes sense next?
#   Q4. Decision: continue / insert_step / replace / escalate
#
# The structured response (ReplanResponse) tells achieve_goal whether to
# amend the current plan, replace it entirely, or escalate to the human
# teaching loop.  Inserted/replacement steps inherit
# provenance='claude_replan' and start at confidence='unverified';
# normal commit_success / commit_failure dynamics promote / demote them
# from there.

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from loguru import logger

from brain.plan import (
    Goal, Plan, PlanStep, ProgressExpectation, GoalCheckpoint,
    PROVENANCE_CLAUDE_REPLAN,
)


# ── Replan reason vocabulary ─────────────────────────────────────────────────

REPLAN_NO_PROGRESS    = "no_progress_at_step"
REPLAN_HEAVY_NOT_YET  = "heavy_not_yet_at_end"
REPLAN_HEAVY_UNCERTAIN = "heavy_uncertain"
REPLAN_PLAN_EXHAUSTED = "plan_exhausted_without_goal"


# ── Decision vocabulary (matches the doc) ────────────────────────────────────

DECISION_CONTINUE     = "continue"      # plan is fine, just keep going
DECISION_INSERT_STEP  = "insert_step"   # add one or more steps before current
DECISION_REPLACE      = "replace"       # discard remaining plan, use new sequence
DECISION_ESCALATE     = "escalate"      # planner can't decide; hand to human


@dataclass
class ReplanResponse:
    """Structured outcome of a replan call."""
    decision:        str   # one of DECISION_*
    progress_made:   str = "unclear"   # 'yes' | 'partial' | 'no' | 'unclear'
    new_cues:        list[str] = field(default_factory=list)
    options_visible: list[str] = field(default_factory=list)
    inserted_steps:  list[PlanStep] = field(default_factory=list)
    updated_plan:    list[PlanStep] = field(default_factory=list)
    reasoning:       str = ""
    raw:             Optional[dict] = None


# ── Prompt template ──────────────────────────────────────────────────────────


_REPLAN_PROMPT = """You are planning the bot's next move.  The bot has been executing
a plan to reach a goal, but verification just reported a problem.

GOAL: {goal_id} — {predicate_text}

PLAN-IN-PROGRESS:
{plan_steps}

STEPS ALREADY EXECUTED ({n_executed}):
{history}

REPLAN REASON: {replan_reason}

CURRENT OBSERVED STATE:
  state:  {state}
  detail: {detail}

CUES we've learned to look for (from prior runs):
{cue_list}

Reply as JSON ONLY (no prose outside the JSON):

{{
  "progress_made":   "yes" | "partial" | "no" | "unclear",
  "new_cues":        ["<observation>", ...],
  "options_visible": ["tap OK button", "press back", "tap Cancel", ...],
  "decision":        "continue" | "insert_step" | "replace" | "escalate",
  "inserted_steps":  [
    {{"action": {{"kind": "tap_button", "labels": ["OK"], "region": {{...}}}},
      "expected_progress_hint": "<what should happen after this tap>"}}
  ],
  "updated_plan": [
    {{"action": {{...}}, "expected_progress_hint": "..."}}
  ],
  "reasoning": "<one short paragraph explaining your decision>"
}}

Use 'continue' if the next planned step still makes sense.
Use 'insert_step' if you noticed a single missing action (e.g. the recipe
forgot to confirm a dialog).  Use 'replace' if the plan is on the wrong
path entirely.  Use 'escalate' if you don't know enough to advise.
"""


def _format_plan_steps(plan: Optional[Plan], current_step_idx: int) -> str:
    if plan is None or not plan.steps:
        return "  (no plan)"
    lines = []
    for i, step in enumerate(plan.steps):
        marker = "▶" if i == current_step_idx else " "
        lines.append(f"  {marker} step {i + 1}: {step.action} "
                     f"(expected: {step.expected_progress.hint or step.expected_progress.kind})")
    return "\n".join(lines)


def _format_history(history: list) -> str:
    if not history:
        return "  (no steps executed yet)"
    lines = []
    for h in history[-8:]:
        light = h.light_result.status if h.light_result else "?"
        heavy = h.heavy_result.status if h.heavy_result else None
        line = f"  - {h.step.step_id}: action={h.step.action} → light={light}"
        if heavy:
            line += f" heavy={heavy}"
        if h.heavy_result and h.heavy_result.evidence_summary:
            line += f"  ({h.heavy_result.evidence_summary[:80]})"
        lines.append(line)
    return "\n".join(lines)


def _format_cue_list(catalog) -> str:
    if catalog is None or not catalog.cues:
        return "  (no cues recorded yet)"
    lines = []
    for c in catalog.cues:
        lines.append(f"  • {c.cue}  (confidence: {c.confidence})")
    return "\n".join(lines)


def _parse_response(raw_text: str) -> Optional[dict]:
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning(f"[replan] could not parse JSON: {e}\nRaw: {raw_text[:300]!r}")
        return None


def _step_from_response_obj(obj: dict, step_id: str) -> PlanStep:
    """Convert a Claude-emitted step dict to a PlanStep."""
    action = obj.get("action") or {}
    progress_hint = obj.get("expected_progress_hint") or ""
    return PlanStep(
        step_id           = step_id,
        action            = dict(action),
        expected_progress = ProgressExpectation(kind="free_text", hint=progress_hint),
        # No expected_goal by default — Claude can mark a step as a checkpoint
        # if it includes "expected_goal_id" in the response, handled below.
        expected_goal     = (
            GoalCheckpoint(goal_id=obj["expected_goal_id"])
            if obj.get("expected_goal_id") else None
        ),
        notes=f"inserted by claude_replan: {progress_hint}",
    )


# ── The replan call ──────────────────────────────────────────────────────────


def request_replan(
    goal: Goal,
    plan: Optional[Plan],
    current_step_idx: int,
    history: list,
    current_perceive: Any,
    replan_reason: str,
    *,
    frame: Any = None,
    catalog: Any = None,
    claude_call: Optional[Callable[[Any, str], Optional[str]]] = None,
) -> ReplanResponse:
    """
    Ask Claude what to do given the current state.  Returns a structured
    ReplanResponse that achieve_goal applies to the in-flight plan.

    Parameters:
      goal:             the Goal being pursued
      plan:             the current Plan (or None if no plan was found)
      current_step_idx: which step in the plan is currently being attempted
      history:          list[ExecutedStep] — what's been done so far
      current_perceive: the latest PerceiveResult
      replan_reason:    one of REPLAN_* — why we're replanning
      frame:            optional PIL.Image for the API call
      catalog:          optional CueCatalog for context
      claude_call:      optional injected callable for testing.
                        Signature: (frame, prompt) -> raw_text | None.
                        Default: production Anthropic API call.

    Returns ReplanResponse.  When Claude is unavailable or the response
    is unparseable, returns DECISION_ESCALATE so the caller hands the
    situation to the human teaching loop.
    """
    if claude_call is None:
        claude_call = _default_claude_call

    n_steps_executed = len(history)
    plan_text = _format_plan_steps(plan, current_step_idx)
    hist_text = _format_history(history)
    cue_text  = _format_cue_list(catalog)

    prompt = _REPLAN_PROMPT.format(
        goal_id        = goal.goal_id,
        predicate_text = goal.predicate_text or goal.description or "(no predicate text)",
        plan_steps     = plan_text,
        n_executed     = n_steps_executed,
        history        = hist_text,
        replan_reason  = replan_reason,
        state          = getattr(current_perceive, "state", "unknown"),
        detail         = getattr(current_perceive, "detail", "")[:120],
        cue_list       = cue_text,
    )

    raw = claude_call(frame, prompt)
    if not raw:
        logger.warning(f"[replan] no response for goal={goal.goal_id!r} reason={replan_reason!r}")
        return ReplanResponse(
            decision  = DECISION_ESCALATE,
            reasoning = "claude call returned no response",
        )

    parsed = _parse_response(raw)
    if not parsed:
        return ReplanResponse(
            decision  = DECISION_ESCALATE,
            reasoning = "claude returned non-JSON",
        )

    # Decision normalisation
    decision_raw = (parsed.get("decision") or "escalate").strip().lower()
    if decision_raw not in (DECISION_CONTINUE, DECISION_INSERT_STEP,
                             DECISION_REPLACE, DECISION_ESCALATE):
        decision_raw = DECISION_ESCALATE

    # Map step lists to PlanStep objects
    inserted: list[PlanStep] = []
    for i, obj in enumerate(parsed.get("inserted_steps", []) or []):
        if isinstance(obj, dict):
            inserted.append(_step_from_response_obj(obj, step_id=f"replan_inserted_{i + 1}"))

    updated: list[PlanStep] = []
    for i, obj in enumerate(parsed.get("updated_plan", []) or []):
        if isinstance(obj, dict):
            updated.append(_step_from_response_obj(obj, step_id=f"replan_step_{i + 1}"))

    return ReplanResponse(
        decision        = decision_raw,
        progress_made   = (parsed.get("progress_made") or "unclear").strip().lower(),
        new_cues        = list(parsed.get("new_cues") or []),
        options_visible = list(parsed.get("options_visible") or []),
        inserted_steps  = inserted,
        updated_plan    = updated,
        reasoning       = str(parsed.get("reasoning", "")),
        raw             = parsed,
    )


# ── Plan amendment helpers (applied by achieve_goal after replan) ────────────


# Action kinds that are PROTECTED from replan-replace.
#
# These steps embody the user-articulated rule: 'positive button tapping
# is the top priority — replan can not happen before that'.  A
# commit_via_positive_taps step represents the bot's transaction-commit
# loop on the current screen; if replan removes it, the bot exits the
# transactional state without committing (e.g. opens the recruit screen
# but never taps the gold Recruit button).  Replan can INSERT steps
# before or after these to extend the plan, but cannot DROP them.
_PROTECTED_ACTION_KINDS: frozenset[str] = frozenset({
    "commit_via_positive_taps",
})


def _is_protected_step(step) -> bool:
    """True if *step*'s action kind is in the replan-protected set."""
    if not isinstance(step.action, dict):
        return False
    return step.action.get("kind") in _PROTECTED_ACTION_KINDS


def apply_replan_to_plan(
    plan: Plan, current_step_idx: int, response: ReplanResponse,
) -> Plan:
    """
    Mutate the plan according to the replan decision.  Returns the same
    plan instance for fluency.

    insert_step: prepend response.inserted_steps before plan.steps[current_step_idx]
                 so they execute next, then the original step continues.
    replace:    drop plan.steps[current_step_idx:] and append updated_plan,
                EXCEPT preserve any steps in the dropped range whose
                action kind is in _PROTECTED_ACTION_KINDS — those run
                first in the new sequence so the transaction-commit
                loop still has a chance to complete.
    continue / escalate: no plan mutation.
    """
    if response.decision == DECISION_INSERT_STEP:
        before = plan.steps[:current_step_idx]
        after  = plan.steps[current_step_idx:]
        plan.steps = before + list(response.inserted_steps) + after
        plan.notes = (plan.notes + "; " if plan.notes else "") + (
            f"replan inserted {len(response.inserted_steps)} step(s) "
            f"at index {current_step_idx}: {response.reasoning[:80]!r}"
        )
        # The amended plan is no longer purely the original — flag its provenance.
        # Don't flip provenance to claude_replan if the original was hand-authored
        # (preserve the audit trail); just note the modification.
    elif response.decision == DECISION_REPLACE:
        original_remaining = plan.steps[current_step_idx:]
        protected = [s for s in original_remaining if _is_protected_step(s)]
        new_steps = list(response.updated_plan)
        if protected:
            # Run protected steps first so their positive-button cycle
            # completes BEFORE the replan-inserted recovery sequence.
            new_steps = protected + new_steps
        plan.steps = plan.steps[:current_step_idx] + new_steps
        protect_note = (
            f" (preserved {len(protected)} protected step(s): "
            f"{[s.step_id for s in protected]})"
            if protected else ""
        )
        plan.notes = (plan.notes + "; " if plan.notes else "") + (
            f"replan replaced steps from index {current_step_idx}: "
            f"{response.reasoning[:80]!r}{protect_note}"
        )
    return plan


# ── Default Claude API call ──────────────────────────────────────────────────


def _default_claude_call(frame: Any, prompt: str) -> Optional[str]:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("[replan] no ANTHROPIC_API_KEY set; skipping replan call")
        return None
    try:
        import anthropic
    except ImportError:
        logger.warning("[replan] anthropic package not available")
        return None

    import base64
    from io import BytesIO

    content: list[dict] = []
    if frame is not None:
        try:
            buf = BytesIO()
            thumb = frame.copy()
            thumb.thumbnail((1000, 600))
            thumb.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            content.append({"type": "image",
                             "source": {"type": "base64",
                                         "media_type": "image/png",
                                         "data": b64}})
        except Exception as e:
            logger.debug(f"[replan] could not encode frame: {e}")
    content.append({"type": "text", "text": prompt})

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as e:
        logger.warning(f"[replan] Claude API error: {e}")
        return None

    return response.content[0].text
