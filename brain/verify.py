# brain/verify.py
#
# Verification primitives for the planner architecture.
# See docs/planner_architecture.md for the full design.
#
# Two checks, different cadences:
#
#   light_check — "have I made progress?"
#     Cadence: after every executed step.
#     Cost:    cheap, mostly local; Claude only when locally ambiguous.
#     Asks:    Did the screen change in a way consistent with the action firing?
#
#   heavy_check — "have I solved it?"
#     Cadence: at checkpoint steps + at plan completion.
#     Cost:    Claude-vision call with curious-observation prompt.
#     Asks:    Did the goal predicate get satisfied?
#
# Both functions take optional callable injection points for the Claude/Moondream
# calls so they can be unit-tested without external services.
#
# This is Layer 2 of the migration.  Layer 1 (brain/plan.py) defined the data
# types these functions consume; Layer 3 (planner facade) wires them into the
# central plan-execute-verify-replan loop.

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from loguru import logger

from brain.plan import (
    Cue, CueCatalog, CueProvenance, CueStats,
    Goal, ProgressExpectation,
    PROVENANCE_HAND_AUTHORED,
)


# ── Status vocabulary ────────────────────────────────────────────────────────

# Light-check outcomes
PROGRESS    = "progress"
NO_PROGRESS = "no_progress"
CLARIFY     = "clarify"   # transient mid-animation; caller should re-perceive once

# Heavy-check outcomes
GOAL_ACHIEVED = "goal_achieved"
NOT_YET       = "not_yet"
UNCERTAIN     = "uncertain"


# ── Result types ─────────────────────────────────────────────────────────────


@dataclass
class LightResult:
    """Outcome of a light_check call."""
    status:           str   # PROGRESS / NO_PROGRESS / CLARIFY
    reason:           str   = ""
    matched_expected: bool  = False   # did the change match the expected_progress annotation?
    tier:             int   = 0       # which tier produced this verdict (0=local, 1=moondream, 2=claude)


@dataclass
class CueObservation:
    """A single cue observed during a heavy_check call."""
    cue:      str
    present:  bool
    confirms: bool = True   # does this observation confirm the goal?


@dataclass
class HeavyResult:
    """Outcome of a heavy_check call."""
    status:           str    # GOAL_ACHIEVED / NOT_YET / UNCERTAIN
    confidence:       float = 0.0
    cues_observed:    list[CueObservation] = field(default_factory=list)
    new_cues:         list[str] = field(default_factory=list)   # cues Claude noticed that weren't in the catalog
    evidence_summary: str = ""
    raw:              Optional[dict] = None   # raw Claude response for debugging


# ── Light verification ───────────────────────────────────────────────────────


def _state_signature(perceive_result) -> tuple:
    """
    Cheap signature for light_check Tier 0 — derived from the perceive
    output already computed at the perceive() call site.  No additional
    OCR or chrome work.

    Includes:
      - state name
      - first 80 chars of detail (Qwen-augmented description)
      - active flow id (if any)
      - active flow step (if any)

    Two perceives that produce the same tuple are treated as "same screen"
    for progress purposes.  Different tuples → progress was made (the
    bot is somewhere new, even if not yet at the goal).
    """
    return (
        getattr(perceive_result, "state", None),
        (getattr(perceive_result, "detail", "") or "")[:80],
        getattr(perceive_result, "flow", None),
        getattr(perceive_result, "flow_step", None),
    )


def _evaluate_expected_progress(
    before, after, expected: ProgressExpectation,
) -> tuple[bool, str]:
    """
    Check whether the post-action perceive matches the expectation.

    Returns (matched, reason).  Matched=True when the kind-specific check
    is satisfied; reason explains either the match or the miss.

    Note: matched=False does not necessarily mean "no progress" — it just
    means "progress wasn't of the kind we expected".  Some progress can
    still be useful (e.g. an unexpected popup appeared).  The caller
    decides how to weight matched vs unmatched.
    """
    kind = expected.kind or "screen_change"

    if kind == "state_change":
        target = expected.expected_state
        if target and getattr(after, "state", None) == target:
            return True, f"state == {target!r}"
        return False, (
            f"expected state {target!r}, got {getattr(after, 'state', None)!r}"
        )

    if kind == "detail_match":
        sub = (expected.expected_detail_substring or "").lower()
        actual = (getattr(after, "detail", "") or "").lower()
        if sub and sub in actual:
            return True, f"detail contains {sub!r}"
        return False, f"detail {actual[:80]!r} does not contain {sub!r}"

    if kind in ("screen_change", "frame_change_required"):
        if _state_signature(before) != _state_signature(after):
            return True, "perceive signature changed"
        return False, "perceive signature unchanged"

    # 'free_text' or unknown kind — no local verdict, only signature delta.
    if _state_signature(before) != _state_signature(after):
        return True, "perceive signature changed (free-text expectation)"
    return False, "perceive signature unchanged (free-text expectation)"


def light_check(
    before_perceive, after_perceive,
    expected: ProgressExpectation,
    *,
    moondream_progress_check: Optional[Callable[[Any, Any], Optional[bool]]] = None,
) -> LightResult:
    """
    Light verification — did the action take effect?

    Tier 0 (always): cheap signature comparison + expected_progress evaluation.
      - signature changed → PROGRESS
      - signature unchanged → NO_PROGRESS
      - signature changed but expected didn't match → still PROGRESS (the
        action did something; just not what we hoped for)

    Tier 1 (only when caller supplies moondream_progress_check and
    Tier 0 returns NO_PROGRESS): ask Moondream "did the screen visibly
    change between these two frames?".  Catches cases where the perceive
    output didn't capture a real visual change (e.g. an animation that
    affects pixels but not state classification).

    Tier 2 (Claude) is invoked separately by the planner when
    consecutive NO_PROGRESS verdicts arise on a step that has succeeded
    historically — it's a diagnostic, not an inline path here.

    Parameters:
      before_perceive: PerceiveResult before the action fired
      after_perceive:  PerceiveResult after
      expected:        ProgressExpectation from the PlanStep
      moondream_progress_check: optional injected callable for Tier 1.
                       Signature: (before_frame, after_frame) -> bool|None
                       (None = model unavailable/unsure).

    Returns LightResult.
    """
    matched, reason = _evaluate_expected_progress(before_perceive, after_perceive, expected)
    sig_before = _state_signature(before_perceive)
    sig_after  = _state_signature(after_perceive)

    if sig_before != sig_after:
        return LightResult(
            status=PROGRESS,
            reason=reason if matched else f"signature changed; {reason}",
            matched_expected=matched,
            tier=0,
        )

    # Tier 0 says no change.  Try Tier 1 if available — sometimes the
    # screen visibly changed but perceive didn't pick up the difference
    # (e.g. mid-animation, or only colour/icon state changed).
    if moondream_progress_check is not None:
        try:
            verdict = moondream_progress_check(before_perceive, after_perceive)
        except Exception as e:
            logger.debug(f"[verify.light] moondream tier-1 failed: {e}")
            verdict = None
        if verdict is True:
            return LightResult(
                status=PROGRESS,
                reason="signature unchanged but Moondream sees visible change",
                matched_expected=False,
                tier=1,
            )
        if verdict is False:
            return LightResult(
                status=NO_PROGRESS,
                reason="signature unchanged AND Moondream confirms no change",
                matched_expected=False,
                tier=1,
            )
        # verdict is None — model unavailable or unsure.  Fall through.

    return LightResult(
        status=NO_PROGRESS,
        reason="perceive signature unchanged (no Tier-1 result)",
        matched_expected=False,
        tier=0,
    )


# ── Heavy verification ───────────────────────────────────────────────────────


_HEAVY_PROMPT_TEMPLATE = """You are verifying whether a multi-step task has actually achieved its goal.
Be strictly skeptical — only answer 'yes' if the goal is unambiguously achieved.

GOAL: {goal_id} — {predicate_text}

CUES we've learned to look for in prior runs:
{cue_list}

ALSO look at the screenshot for any visual indicators relevant to this goal
that aren't in the list above — colour states (red/green/yellow), badges,
dots, meter fills, button enabled-vs-disabled, NPC dialogue text changes,
numerical changes, etc.  Be opinionated and specific.

Reply as JSON ONLY (no prose outside the JSON):

{{
  "goal_achieved": "yes" | "no" | "uncertain",
  "confidence":    <number 0.0 to 1.0>,
  "cues_observed": [
    {{ "cue": "<text from list above>", "present": true|false, "confirms_goal": true|false }},
    ...
  ],
  "new_cues": [
    "<additional indicator you noticed not already in the list>",
    ...
  ],
  "evidence_summary": "<one sentence describing what you actually see>"
}}
"""


def _format_cue_list(catalog: Optional[CueCatalog]) -> str:
    if catalog is None or not catalog.cues:
        return "  (no cues recorded yet — this is the first verification)"
    lines = []
    for c in catalog.cues:
        s = c.stats
        lines.append(
            f"  • {c.cue}  "
            f"(confidence: {c.confidence}, observed {s.observed}×, "
            f"confirmed {s.confirmed}, refuted {s.refuted})"
        )
    return "\n".join(lines)


def _parse_heavy_response(raw_text: str) -> Optional[dict]:
    """Strip code fences and parse JSON; tolerant of wrappers."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning(f"[verify.heavy] could not parse Claude JSON: {e}\nRaw: {raw_text[:300]!r}")
        return None


def check_goal_locally(frame, goal: Goal) -> Optional[HeavyResult]:
    """Public alias of _evaluate_local_predicate — used by plan_loop to
    short-circuit when a step's transaction already achieved the goal,
    before light_check has a chance to reject the next step.

    Returns the same shape as heavy_check (HeavyResult with status set
    to GOAL_ACHIEVED / NOT_YET), or None when no local predicate is
    defined or the predicate abstains.
    """
    return _evaluate_local_predicate(frame, goal)


def _evaluate_local_predicate(frame, goal: Goal) -> Optional[HeavyResult]:
    """Try the L4 local predicate for *goal* against *frame*.  Returns:
      - HeavyResult(GOAL_ACHIEVED)  when predicate is True.
      - HeavyResult(NOT_YET)        when predicate is False.
      - None                        when no predicate is defined OR the
                                    predicate returns None (state
                                    insufficient — caller should fall
                                    back to Claude heavy_check).
    """
    try:
        from brain.predicate import load_local_predicate, evaluate
        from vision.state_extractor import extract_state
    except Exception as e:
        logger.debug(f"[verify.heavy] local predicate path unavailable: {e}")
        return None

    spec = load_local_predicate(goal.goal_id)
    if spec is None:
        return None

    # OCR the frame.  _ocr_frame is cached per-frame so this is free if
    # the frame was already perceived this tick.  Wrap in try/except so
    # mocked/synthetic frames in tests don't crash the predicate path;
    # any OCR failure simply abstains and falls back to Claude.
    try:
        from actions.sail_actions import _ocr_frame
        tokens = _ocr_frame(frame, min_conf=0.3)
        state  = extract_state(tokens)
        result = evaluate(state, spec)
    except Exception as e:
        logger.debug(f"[verify.heavy] local predicate evaluation failed: {e}")
        return None

    if result is None:
        logger.debug(
            f"[verify.heavy] local predicate for {goal.goal_id!r} abstained "
            f"(state insufficient) — falling back to Claude"
        )
        return None

    summary = (
        f"local predicate: fleet_current={state.fleet_current!r}, "
        f"min_crew={state.min_crew!r}, has_dialog={state.has_dialog} "
        f"→ {'satisfied' if result else 'unsatisfied'}"
    )
    if result:
        logger.info(f"[verify.heavy] {goal.goal_id!r}: L4 predicate ✓  {summary}")
        return HeavyResult(
            status=GOAL_ACHIEVED, confidence=1.0,
            evidence_summary=summary,
        )
    logger.info(f"[verify.heavy] {goal.goal_id!r}: L4 predicate ✗  {summary}")
    return HeavyResult(
        status=NOT_YET, confidence=1.0,
        evidence_summary=summary,
    )


def heavy_check(
    frame,
    goal: Goal,
    catalog: Optional[CueCatalog] = None,
    *,
    claude_call: Optional[Callable[[Any, str], Optional[str]]] = None,
) -> HeavyResult:
    """
    Heavy verification — has the goal predicate been satisfied?

    Two-tier evaluation (L4-aware, post-2026-05-12):
      1. If the goal has a `local_predicate` defined in
         memory/knowledge/goals/<goal_id>.json, evaluate it against L3
         state extracted from the post-action frame's OCR.  When the
         predicate returns a definitive True/False, return immediately
         — no Claude call.  Empirically this replaces ~5-7 Claude
         heavy_check calls per recruit cycle with a deterministic local
         answer (docs/four_layer_perception.md).
      2. If no predicate is defined, or the predicate abstains (None
         because required fields weren't visible on this frame), fall
         through to the existing Claude prompt.

    Parameters:
      frame:        PIL.Image of the current screen (post-action).
      goal:         the Goal whose predicate is being evaluated.
      catalog:      optional pre-loaded CueCatalog; None means no prior
                    cues recorded.
      claude_call:  optional injected callable for testing.  Signature:
                    (frame_image, prompt_text) -> raw_response_text | None.
                    When None, the production call to Anthropic is
                    attempted; missing API key returns UNCERTAIN.

    Returns HeavyResult.
    """
    # ── L4 fast-path: local predicate evaluation ────────────────────────────
    local_result = _evaluate_local_predicate(frame, goal)
    if local_result is not None:
        return local_result

    prompt = _HEAVY_PROMPT_TEMPLATE.format(
        goal_id        = goal.goal_id,
        predicate_text = goal.predicate_text or goal.description or "(no predicate text)",
        cue_list       = _format_cue_list(catalog),
    )

    if claude_call is None:
        claude_call = _default_claude_call

    raw = claude_call(frame, prompt)
    if not raw:
        logger.warning(f"[verify.heavy] no response for goal {goal.goal_id!r}")
        return HeavyResult(status=UNCERTAIN, evidence_summary="claude call returned no response")

    parsed = _parse_heavy_response(raw)
    if not parsed:
        return HeavyResult(status=UNCERTAIN, evidence_summary="claude returned non-JSON")

    # Map verdict
    verdict_raw = (parsed.get("goal_achieved") or "uncertain").strip().lower()
    if verdict_raw == "yes":
        status = GOAL_ACHIEVED
    elif verdict_raw == "no":
        status = NOT_YET
    else:
        status = UNCERTAIN

    # Parse observed cues
    observations: list[CueObservation] = []
    for entry in parsed.get("cues_observed", []) or []:
        if not isinstance(entry, dict) or "cue" not in entry:
            continue
        observations.append(CueObservation(
            cue      = str(entry["cue"]),
            present  = bool(entry.get("present", False)),
            confirms = bool(entry.get("confirms_goal", True)),
        ))

    new_cues = [str(s) for s in (parsed.get("new_cues") or []) if s]

    return HeavyResult(
        status           = status,
        confidence       = float(parsed.get("confidence", 0.0)),
        cues_observed    = observations,
        new_cues         = new_cues,
        evidence_summary = str(parsed.get("evidence_summary", "")),
        raw              = parsed,
    )


def _default_claude_call(frame, prompt: str) -> Optional[str]:
    """
    Production Claude API call.  Encapsulated so it can be mocked in
    tests via the claude_call parameter to heavy_check.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("[verify.heavy] no ANTHROPIC_API_KEY set; skipping Claude call")
        return None
    try:
        import anthropic
    except ImportError:
        logger.warning("[verify.heavy] anthropic package not available")
        return None

    import base64
    from io import BytesIO

    buf = BytesIO()
    thumb = frame.copy()
    thumb.thumbnail((1000, 600))
    thumb.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
    except Exception as e:
        logger.warning(f"[verify.heavy] Claude API error: {e}")
        return None

    return response.content[0].text


# ── Cue catalog updates ──────────────────────────────────────────────────────


def update_cue_catalog(
    catalog: CueCatalog,
    result: HeavyResult,
    *,
    context: str = "",
    promote_to_medium_after: int = 5,
    promote_to_high_after: int = 15,
    refuted_to_low_after: int = 3,
) -> CueCatalog:
    """
    Apply a heavy_check observation to the cue catalog.  Mutates and
    returns the catalog.  Caller decides whether to persist via
    save_cue_catalog().

    Behaviour:
      - For each existing cue mentioned in result.cues_observed:
          stats.observed += 1
          if confirms_goal == result-says-confirms: stats.confirmed += 1
          else                                     : stats.refuted   += 1
          confidence is promoted/demoted by stats thresholds.
      - For each new_cue in result.new_cues that isn't already in the
        catalog, append a new Cue with confidence=unverified and
        provenance pointing at this observation context.
    """
    by_text = {c.cue: c for c in catalog.cues}
    now = datetime.now(timezone.utc).isoformat()

    for obs in result.cues_observed:
        cue = by_text.get(obs.cue)
        if cue is None:
            # Claude reported an observation referencing a cue not in the
            # catalog — treat as a new cue.
            cue = Cue(
                cue        = obs.cue,
                polarity   = "confirms" if obs.confirms else "refutes",
                confidence = "unverified",
                stats      = CueStats(observed=1,
                                       confirmed=1 if obs.present and obs.confirms else 0,
                                       refuted  =1 if obs.present and not obs.confirms else 0),
                provenance = CueProvenance(
                    source        = "claude_observed",
                    first_seen    = now,
                    first_seen_in = context or "heavy_check",
                    noted_during  = "heavy_check",
                    noted_by      = "claude_vision",
                ),
            )
            catalog.cues.append(cue)
            by_text[obs.cue] = cue
            continue

        cue.stats.observed += 1
        if obs.present and obs.confirms:
            cue.stats.confirmed += 1
        elif obs.present and not obs.confirms:
            cue.stats.refuted += 1
        # If not present, neither confirmed nor refuted — just observed-as-absent
        cue.confidence = _grade_cue_confidence(
            cue, promote_to_medium_after, promote_to_high_after,
            refuted_to_low_after,
        )

    for new_cue_text in result.new_cues:
        if new_cue_text in by_text:
            continue   # already covered by cues_observed handling above
        catalog.cues.append(Cue(
            cue        = new_cue_text,
            polarity   = "confirms",
            confidence = "unverified",
            stats      = CueStats(observed=1, confirmed=1),
            provenance = CueProvenance(
                source        = "claude_observed",
                first_seen    = now,
                first_seen_in = context or "heavy_check",
                noted_during  = "heavy_check",
                noted_by      = "claude_vision",
            ),
        ))

    return catalog


def _grade_cue_confidence(
    cue: Cue,
    medium_threshold: int,
    high_threshold: int,
    refute_threshold: int,
) -> str:
    """
    Promote a cue's confidence based on accumulated stats.  Mirrors the
    plan-level promotion logic but with a wider scale because cues are
    reused across plans and accumulate faster.
    """
    s = cue.stats
    if s.refuted >= refute_threshold and s.refuted > s.confirmed:
        return "low"
    if s.confirmed >= high_threshold and s.refuted == 0:
        return "high"
    if s.confirmed >= medium_threshold:
        return "medium"
    return "unverified"
