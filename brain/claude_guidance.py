# brain/claude_guidance.py
#
# Frame-by-frame Claude-guided traversal for unknown / unexpected screens.
#
# When the bot lands on a screen it cannot recognize (state == "unknown",
# active sub-flow not in KB, action button still visible after a step that
# was supposed to be terminal, etc.), this module asks Claude for ONE next
# action, executes it, re-perceives, and repeats — up to MAX_GUIDED_STEPS
# iterations.  Each Claude call is over a single screenshot, so the model
# reasons about the actual current state rather than predicting an entire
# sequence in advance.
#
# Disambiguation hints (visual_hint, position_hint, region) are required of
# Claude because UWO frequently shows multiple instances of the same word
# on one screen — a top-left back-arrow tooltip, a tab title, AND a
# bottom-right action button can all match the substring "Recruit".
# Plain label matching alone would tap any of them.
#
# When the loop reaches a known terminal state, the recorded action sequence
# is persisted to memory/knowledge/fsm/flows.json so future encounters of
# the same trigger advance through it without further API calls.

from __future__ import annotations

import base64
import io
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

from brain.perceive import PerceiveResult, perceive

# Default cap on Claude calls per invocation.  10 keeps cost bounded
# (≈ $0.20 per stuck-screen escapade with Sonnet 4.6 + small thumbnails)
# while leaving headroom for multi-step dialogs (e.g. confirm → result → close).
MAX_GUIDED_STEPS = 10

# Position hint → fractional region.  Used as a fallback when Claude provides
# only a position_hint without an explicit region.  Values intentionally err
# wide; explicit region from Claude wins when both are present.
_POSITION_REGIONS: dict[str, dict[str, float]] = {
    "right_panel":  {"x_min_pct": 0.70},
    "left_panel":   {"x_max_pct": 0.30},
    "bottom_right": {"x_min_pct": 0.50, "y_min_pct": 0.50},
    "bottom_left":  {"x_max_pct": 0.50, "y_min_pct": 0.50},
    "top_right":    {"x_min_pct": 0.50, "y_max_pct": 0.50},
    "top_left":     {"x_max_pct": 0.50, "y_max_pct": 0.50},
    "bottom":       {"y_min_pct": 0.50},
    "top":          {"y_max_pct": 0.50},
    "center":       {"x_min_pct": 0.25, "x_max_pct": 0.75,
                     "y_min_pct": 0.25, "y_max_pct": 0.75},
}


@dataclass
class GuidedStep:
    """One Claude-prescribed step during a guided traversal."""
    action:           str             # "tap" | "wait" | "complete"
    reason:           str
    expected_outcome: str
    label:            Optional[str]    = None
    visual_hint:      Optional[str]    = None  # "yellow" | "gold" | "gray" | None
    position_hint:    Optional[str]    = None
    region_pct:       dict             = field(default_factory=dict)
    fallback_coord:   Optional[tuple]  = None
    wait_seconds:     int              = 0
    tapped_at:        Optional[tuple]  = None  # actual coord that was tapped (for replay)


# ── Claude API call ───────────────────────────────────────────────────────────

def _ask_claude_for_next_action(
    frame,
    goal: str,
    perceive_result: PerceiveResult,
    history: list[GuidedStep],
    entry_summary: Optional[str] = None,
) -> Optional[dict]:
    """
    Send the current frame + goal + action history to Claude and ask for
    ONE next action.  Returns the parsed JSON dict, or None on failure.

    `entry_summary` describes the screen state at the moment the guidance
    session started — used to prime Claude for cycle recognition (return
    'complete' when the screen returns to a state similar to entry after
    at least one successful action).
    """
    try:
        import anthropic
    except ImportError:
        logger.warning("[guidance] anthropic package not installed")
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("[guidance] ANTHROPIC_API_KEY not set")
        return None

    thumb = frame.copy()
    thumb.thumbnail((1200, 540))
    buf = io.BytesIO()
    thumb.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    history_summary = (
        "\n".join(
            f"  {i + 1}. {s.action} {s.label or ''} → {s.expected_outcome}"
            for i, s in enumerate(history)
        )
        or "  (none)"
    )

    entry_block = (
        f"SESSION ENTRY (the screen when this guidance session started):\n  {entry_summary}\n\n"
        if entry_summary else ""
    )

    prompt = f"""\
RESPOND WITH A SINGLE VALID JSON OBJECT MATCHING THE SCHEMA BELOW.
NO PROSE.  NO MARKDOWN FENCES.  NO ENUMERATION OR EXPLANATION OUTSIDE THE
JSON FIELDS.  IF YOU NEED TO EXPLAIN A DECISION, PUT IT IN THE 'reason'
FIELD WITHIN THE JSON.

You are guiding a bot through Uncharted Waters Origin (UWO), a mobile sailing MMO.
The bot is on a screen it does not fully recognise.  Help it advance toward the goal.

GOAL: {goal}

{entry_block}CURRENT PERCEPTION:
  state  = {perceive_result.state!r}
  detail = {perceive_result.detail!r}
  flow   = {perceive_result.flow!r}

ACTIONS TAKEN IN THIS GUIDANCE SESSION:
{history_summary}

Look at the screenshot and prescribe ONE NEXT ACTION to advance the goal,
or report "complete" if the goal is already satisfied on this screen.

RESPOND WITH JSON ONLY:
{{
  "action": "tap" | "wait" | "complete",
  "reason": "<one sentence: why this advances the goal>",
  "expected_outcome": "<what should appear on the next screen>",

  // For action == "tap":
  "tap": {{
    "label":         "<text on the button to tap, lowercase>",
    "visual_hint":   "yellow" | "gold" | "gray" | null,
    "position_hint": "right_panel" | "left_panel" | "bottom_right" | "bottom_left" | "top_right" | "top_left" | "bottom" | "top" | "center" | null,
    "region":        {{"y_min_pct": <float>, "y_max_pct": <float>, "x_min_pct": <float>, "x_max_pct": <float>}},
    "fallback_coord":[x, y]
  }},

  // For action == "wait":
  "wait_seconds": <int>
}}

BOUNDED-ACTION CONTRACT (read carefully):
- Your job is to perform the MINIMUM action that completes the immediate
  sub-task described in the goal, then return action="complete".
- A typical sub-task involves one cycle through a dialog tree:
  primary action button → confirmation dialog → result dialog → screen
  returns to a state that may LOOK SIMILAR to where this session started.
- That look-similar state is the SUCCESS state, not a place to keep going.
  After ONE successful primary-action confirmation has flowed through to
  the result-dialog dismissal, return "complete".
- Do NOT try to maximize, repeat, or do extra work.  For blocker-resolution
  goals (e.g. "not enough crew"), the dialog typically pre-fills the
  MINIMUM needed value — just confirm once and stop.  Don't tap "Max" and
  re-recruit, don't enter additional sub-menus, don't try to be helpful.
- Watch for cycle indicators: if you've already taken at least one
  primary-action tap AND the current screen now shows the same primary
  action button you tapped before (or matches the SESSION ENTRY summary),
  you have COMPLETED the cycle — return "complete".

CRITICAL — disambiguation rules:
- The same word (e.g. "Depart", "Recruit", "OK") frequently appears in MULTIPLE
  places on one UWO screen: a top-left back-arrow tooltip, a tab/sub-menu title,
  AND a bottom-right action button can all contain the substring.  Plain label
  matching is NOT enough.
- ALWAYS provide at least one disambiguator with every "tap":
    visual_hint  — primary action buttons in UWO are almost always yellow/amber.
                   Use "yellow" for them.  Strongest hint.
    position_hint — coarse screen quadrant of the actual button.
    region       — fractional bounds (0.0-1.0 of frame); preferred over position_hint
                   when you can identify the button precisely.
- fallback_coord — ONLY when the button has no readable label (icon-only).
  Coordinates in the ORIGINAL 2400x1080 space; the screenshot is sent at
  reduced resolution.

NPC speech bubbles (rounded rectangle with a pointing tail, anchored to a
character sprite, typically center-bottom of the screen) are INFORMATIONAL,
NOT actionable.  Do NOT prescribe a tap on text inside an NPC speech bubble
even if it contains words that look like menu items.

If you see an innkeeper or other NPC narrating ("Here's all the mates we
got", "Have you tried our roast chicken?"), produce ONE of these JSON
objects — never prose explaining why you can't tap:

  - If the goal-relevant action has already been taken in this session
    (e.g. crew was recruited successfully and the dialog is done):
        {{"action": "complete",
          "reason": "<one sentence>",
          "expected_outcome": "<one sentence>"}}

  - If you want the bubble to dismiss itself before the next decision:
        {{"action": "wait", "wait_seconds": 3,
          "reason": "<one sentence>",
          "expected_outcome": "NPC speech bubble dismisses; menu accessible again"}}

When in doubt, prefer "wait" with 2-3 seconds over a guess — the bot can re-poll.
"""

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
        logger.warning(f"[guidance] Claude API call failed: {e}")
        return None

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"[guidance] Claude returned invalid JSON: {e}\nRaw: {raw[:300]!r}")
        return None


# ── Independent verify-complete ──────────────────────────────────────────────


def _ask_claude_to_verify_complete(
    frame, goal: str, result: "PerceiveResult", history: list,
) -> Optional[bool]:
    """
    Independent verification step that runs whenever Claude's per-step
    response says action='complete'.  A FRESH prompt with no narrative
    momentum from the in-loop conversation: just goal + current state +
    one screenshot, asking yes/no.

    Mirrors the human teaching loop's 'done' confirmation — completion
    is a SEPARATE, explicit signal rather than implied by Claude's
    optimistic self-assessment.  Targets the recurring bug pattern from
    May-1 runs where Claude declared complete on a half-finished
    recruit-crew flow because the misperceived post-tap state looked
    plausible to it.

    Returns:
        True   → Claude confirmed completion in the verify call.  Caller
                 should accept the 'complete' signal and exit the loop.
        False  → Claude rejected.  Caller should treat the in-step
                 'complete' as a false alarm and continue the loop.
        None   → Verify call could not run (no API key, no anthropic, etc.).
                 Caller falls back to the prior trust-the-step behaviour.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    try:
        import anthropic
    except ImportError:
        return None

    import base64 as _base64
    from io import BytesIO as _BytesIO
    buf = _BytesIO()
    thumb = frame.copy()
    thumb.thumbnail((1000, 600))
    thumb.save(buf, format="PNG")
    b64 = _base64.b64encode(buf.getvalue()).decode("ascii")

    history_summary = "\n".join(
        f"  {i+1}. {s.action} {s.label or ''}: {s.reason}".rstrip()
        for i, s in enumerate(history[-6:])
    ) or "  (no prior steps recorded)"

    prompt = f"""You are verifying whether a multi-step task has actually been completed.
Be strictly skeptical — only answer yes if the goal is unambiguously achieved.

Goal: {goal}

Current observed state:
  state:  {result.state}
  detail: {result.detail}

Recent steps taken:
{history_summary}

Question: Has the goal been COMPLETED?  Reply with ONLY 'yes' or 'no' on a single line.
'yes' means: the immediate sub-task in the goal has finished AND the
screen has actually changed in the way completion would require (e.g.
the dialog that prompted this teaching session is gone, the bot is
back at a stable parent state, etc.).
'no' means: not yet done, or the screen still shows the dialog the
goal was about, or there is any reason to be uncertain."""

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=10,
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
        logger.warning(f"[guidance] verify-complete API call failed: {e}")
        return None

    raw = response.content[0].text.strip().lower()
    confirmed = raw.startswith("y")
    logger.info(
        f"[guidance] verify-complete → {'YES' if confirmed else 'NO'}  raw={raw[:30]!r}"
    )
    return confirmed


# ── Action execution ──────────────────────────────────────────────────────────

def _region_pct_to_kwargs(region_pct: dict, frame_w: int, frame_h: int) -> dict:
    """Convert pct-based region dict to pixel x_min/x_max/y_min/y_max kwargs."""
    kwargs: dict = {}
    if region_pct.get("y_min_pct") is not None:
        kwargs["y_min"] = int(frame_h * region_pct["y_min_pct"])
    if region_pct.get("y_max_pct") is not None:
        kwargs["y_max"] = int(frame_h * region_pct["y_max_pct"])
    if region_pct.get("x_min_pct") is not None:
        kwargs["x_min"] = int(frame_w * region_pct["x_min_pct"])
    if region_pct.get("x_max_pct") is not None:
        kwargs["x_max"] = int(frame_w * region_pct["x_max_pct"])
    return kwargs


def _resolve_tap_target(frame, step: GuidedStep) -> Optional[tuple[int, int]]:
    """
    Resolve a guided tap step to an (x, y) coordinate using disambiguation
    hints in priority order:
      1. visual_hint == yellow/gold  → _find_yellow_button (constrained by region)
      2. label + region              → _find_button (constrained)
      3. label alone                 → _find_button (unconstrained — last text resort)
      4. fallback_coord              → raw coordinate

    Returns None if no target could be resolved.
    """
    from actions.sail_actions import _find_button, _find_yellow_button

    # Merge explicit region with position_hint defaults — explicit wins.
    region_pct = dict(_POSITION_REGIONS.get(step.position_hint or "", {}))
    region_pct.update(step.region_pct or {})
    kwargs = _region_pct_to_kwargs(region_pct, frame.width, frame.height)

    # 1. Yellow / gold primary action button.
    if step.visual_hint in ("yellow", "gold"):
        coord = _find_yellow_button(frame, **kwargs)
        if coord:
            logger.debug(f"[guidance] yellow button @ {coord}  (region={region_pct})")
            return coord
        logger.debug(f"[guidance] visual_hint={step.visual_hint!r} but no yellow region matched — falling back to label")

    # 2. Label + region.
    if step.label and kwargs:
        coord = _find_button(frame, step.label, **kwargs)
        if coord:
            logger.debug(f"[guidance] label {step.label!r} in region {region_pct} @ {coord}")
            return coord

    # 3. Label alone (last resort — risk of matching wrong instance).
    if step.label:
        coord = _find_button(frame, step.label)
        if coord:
            logger.debug(f"[guidance] label {step.label!r} unconstrained @ {coord}")
            return coord

    # 4. Fallback coordinate (no readable label).
    if step.fallback_coord:
        logger.debug(f"[guidance] using fallback_coord {step.fallback_coord}")
        return tuple(step.fallback_coord)

    return None


def _parse_action(payload: dict) -> Optional[GuidedStep]:
    """Validate Claude's JSON and produce a GuidedStep, or None on bad shape."""
    action = payload.get("action")
    if action not in ("tap", "wait", "complete"):
        logger.warning(f"[guidance] unrecognised action {action!r}")
        return None

    reason   = payload.get("reason", "")
    outcome  = payload.get("expected_outcome", "")

    if action == "tap":
        tap = payload.get("tap") or {}
        return GuidedStep(
            action           = "tap",
            reason           = reason,
            expected_outcome = outcome,
            label            = (tap.get("label") or "").strip().lower() or None,
            visual_hint      = tap.get("visual_hint"),
            position_hint    = tap.get("position_hint"),
            region_pct       = tap.get("region") or {},
            fallback_coord   = tuple(tap["fallback_coord"]) if tap.get("fallback_coord") else None,
        )
    if action == "wait":
        return GuidedStep(
            action           = "wait",
            reason           = reason,
            expected_outcome = outcome,
            wait_seconds     = int(payload.get("wait_seconds", 2)),
        )
    return GuidedStep(action="complete", reason=reason, expected_outcome=outcome)


# ── Main loop ─────────────────────────────────────────────────────────────────

def _frame_signature(perceive_result: PerceiveResult, frame) -> tuple:
    """
    Stable structural signature of a screen for cycle detection.
    Deliberately blind to numerical content (crew counts, gold amounts) so
    a 'now 1040/2275 instead of 87/2275' state-change doesn't break cycle
    matching.  Composed of:
      - nav state
      - OCR title (lowercased, stripped)
      - whether a yellow primary action button is visible
    These three together form a structural fingerprint: if they all match
    a previous capture, the screen has structurally returned to that
    earlier state.
    """
    from actions.sail_actions import _find_yellow_button

    state  = perceive_result.state
    detail = (perceive_result.detail or "").lower()
    title  = detail.replace("building:", "").split(" — ", 1)[0].strip()

    yellow_present = False
    try:
        yellow_present = _find_yellow_button(frame) is not None
    except Exception:
        pass

    return (state, title, yellow_present)


def _signature_summary(sig: tuple) -> str:
    """Human-readable summary of a frame signature for prompt context."""
    state, title, yellow_present = sig
    yellow_str = "primary action button visible" if yellow_present else "no primary action button visible"
    title_str  = f"title={title!r}" if title else "no title"
    return f"state={state!r}  {title_str}  {yellow_str}"


def claude_guided_loop(
    goal: str,
    expected_terminal_state: Optional[str] = None,
    max_steps: int = MAX_GUIDED_STEPS,
) -> tuple[PerceiveResult, list[GuidedStep]]:
    """
    Frame-by-frame Claude-guided traversal of an unknown / unexpected screen.

    Each iteration: capture → perceive → ask Claude for next action → execute.
    Stops when:
      - Claude reports "complete"
      - state matches expected_terminal_state
      - perceive() detects an active known flow (caller should advance it)
      - cycle detected: post-action signature matches the entry signature
        AND at least one tap action has been taken (the framework rail —
        guarantees termination even if Claude doesn't recognise the cycle
        semantically)
      - max_steps reached

    Returns the final PerceiveResult plus the recorded sequence of GuidedSteps.
    The sequence may then be persisted as a learned flow via save_learned_flow().
    """
    from actions.adb_actions import tap as _tap
    from capture.adb_capture import capture_screen

    logger.info(f"[guidance] starting Claude-guided loop  goal={goal!r}  max_steps={max_steps}")

    history: list[GuidedStep] = []
    entry_frame  = capture_screen()
    result       = perceive(entry_frame)
    entry_sig    = _frame_signature(result, entry_frame)
    entry_summary = _signature_summary(entry_sig)
    logger.info(f"[guidance] entry signature: {entry_summary}")

    for i in range(max_steps):
        # Termination checks BEFORE consulting Claude — saves an API call when
        # we are already where we wanted to be.
        if expected_terminal_state and result.state == expected_terminal_state:
            logger.info(f"[guidance] reached expected_terminal_state={expected_terminal_state!r} at step {i}")
            return result, history
        if result.flow:
            logger.info(f"[guidance] active flow {result.flow!r} detected — letting caller advance it")
            return result, history

        # Cycle-detection rail: if at least one tap has fired AND the current
        # signature matches the entry signature, the screen has returned to
        # where this session started.  Treat as a successful cycle completion
        # and exit with synthetic 'complete' so save_learned_flow can persist.
        tap_count = sum(1 for s in history if s.action == "tap")
        if tap_count >= 1:
            current_sig = _frame_signature(result, capture_screen())
            if current_sig == entry_sig:
                logger.info(
                    f"[guidance] cycle detected at step {i}: signature matches entry "
                    f"({entry_summary}) after {tap_count} tap(s) — exiting with 'complete'"
                )
                history.append(GuidedStep(
                    action           = "complete",
                    reason           = "framework cycle detection — screen returned to entry signature",
                    expected_outcome = "loop terminated by framework rail",
                ))
                return result, history

        frame = capture_screen()
        payload = _ask_claude_for_next_action(
            frame, goal, result, history, entry_summary=entry_summary
        )
        if not payload:
            logger.warning(f"[guidance] no usable response from Claude at step {i + 1} — aborting loop")
            return result, history

        step = _parse_action(payload)
        if step is None:
            logger.warning(f"[guidance] could not parse Claude action at step {i + 1} — aborting loop")
            return result, history

        logger.info(
            f"[guidance] step {i + 1}/{max_steps}: action={step.action!r}  "
            f"label={step.label!r}  visual={step.visual_hint!r}  "
            f"pos={step.position_hint!r}  region={step.region_pct or 'none'}  "
            f"reason={step.reason!r}"
        )

        if step.action == "complete":
            # Phase 4: independent verify-complete.  Claude's in-step
            # 'complete' is its own narrative read of the screen, which
            # has historically misjudged half-finished flows (May-1
            # recruit-crew incidents).  Do a separate API call with a
            # fresh prompt asking only yes/no — same shape as the human
            # teaching loop's 'done' confirmation.  When the verify
            # call says no, treat the original 'complete' as a false
            # alarm and continue the loop.  When it can't run (no API
            # key etc.), fall back to the prior trust-the-step
            # behaviour so we don't regress in offline scenarios.
            verify = _ask_claude_to_verify_complete(
                capture_screen(), goal, result, history,
            )
            if verify is False:
                logger.warning(
                    f"[guidance] step {i + 1}: Claude said 'complete' but "
                    "independent verify rejected it — continuing loop"
                )
                # Don't append the rejected complete step; it's not part of
                # the recipe.  Fall through to next iteration.
                continue
            # verify is True or None → accept the complete signal
            history.append(step)
            return result, history

        if step.action == "wait":
            time.sleep(max(1, step.wait_seconds))
            history.append(step)
            result = perceive(capture_screen())
            continue

        # action == "tap"
        coord = _resolve_tap_target(frame, step)
        if coord is None:
            logger.warning(
                f"[guidance] could not resolve tap target  "
                f"label={step.label!r}  visual={step.visual_hint!r}  "
                f"region={step.region_pct or 'none'} — aborting loop"
            )
            return result, history

        step.tapped_at = coord
        _tap(*coord)
        history.append(step)
        time.sleep(1.5)
        result = perceive(capture_screen())

    logger.warning(f"[guidance] reached max_steps={max_steps} without termination")
    return result, history


# ── Persistence: save sequence as a learned flow ──────────────────────────────

_FLOWS_PATH = Path("memory/knowledge/fsm/flows.json")


def _frame_keywords(perceive_result: PerceiveResult, count: int = 4) -> list[str]:
    """
    Pick a few short tokens from the perceive detail to use as flow trigger
    keywords.  Falls back to the state name if detail is empty.  Keywords
    are lower-cased and stripped of punctuation.
    """
    detail = (perceive_result.detail or "").lower()
    # Strip the standard "building: " prefix and the " — " Qwen separator.
    detail = detail.replace("building:", "").split(" — ", 1)[0]
    tokens = [
        t.strip(".,()[]{}\"'")
        for t in detail.split()
        if len(t) >= 4 and t.replace(" ", "").isalpha()
    ]
    if not tokens:
        return [perceive_result.state]
    seen, ordered = set(), []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            ordered.append(t)
        if len(ordered) >= count:
            break
    return ordered


def save_learned_flow(
    starting_state: PerceiveResult,
    final_state: PerceiveResult,
    history: list[GuidedStep],
    goal: str,
) -> Optional[str]:
    """
    Persist a successful guided sequence as a flow definition in flows.json
    so future encounters of the same trigger replay through the existing
    flow advancement machinery without further Claude calls.

    Returns the new flow id, or None if nothing was saved.
    """
    actionable = [s for s in history if s.action in ("tap", "wait")]
    if not actionable:
        logger.info("[guidance] no actionable history — nothing to persist")
        return None
    if final_state.state == "unknown":
        logger.info("[guidance] final state still unknown — declining to persist a possibly-broken flow")
        return None

    # Gate: only persist when Claude explicitly returned action='complete'.
    # Hitting max_steps means the loop ran out the budget without Claude
    # declaring success — the recipe is suspect (probable over-extension,
    # cf. the not-enough-crew over-recruit case in
    # docs/journey_not_enough_crew.md Act 8).  Persisting these polluted the
    # KB last time; refuse to do it again.
    if not any(s.action == "complete" for s in history):
        logger.info(
            "[guidance] loop did not reach a 'complete' termination "
            f"({len(history)} steps recorded) — declining to persist; "
            "Claude did not explicitly say the goal was achieved"
        )
        return None

    # Phase 4 plausibility guard: refuse to persist flows whose declared
    # terminal_state is one of perceive's fallback/uncertainty signals.
    # 'sea_cinematic' is the catch-all returned when chrome is hidden and
    # no other state matched — it is what perceive emits when it doesn't
    # know.  No legitimate single-flow recipe terminates there.  This
    # specifically catches the recurring case where a modal popup (e.g.
    # the recruit-crew confirm dialog) hides chrome, perceive falls
    # through to sea_cinematic, Claude trusts the misclassified state
    # and declares 'complete', and we end up saving
    # `learned_building__sea_cinematic__...` flows whose terminal claim
    # contradicts where the bot actually is.  Two such broken flows are
    # already in flows.json from May-1 runs; this guard stops the leak.
    _IMPLAUSIBLE_TERMINAL_STATES = {"sea_cinematic", "unknown"}
    if final_state.state in _IMPLAUSIBLE_TERMINAL_STATES:
        logger.warning(
            f"[guidance] declining to persist learned flow: terminal_state="
            f"{final_state.state!r} is a perceive fallback signal, not a "
            "stable terminal.  Probable cause: a modal dialog hid chrome "
            "and perceive misclassified the post-tap state.  Goal was: "
            f"{goal!r}.  The Claude-guided actions already executed; only "
            "the persistence is being skipped so the KB doesn't accumulate "
            "broken recipes."
        )
        return None

    if not _FLOWS_PATH.exists():
        logger.warning(f"[guidance] {_FLOWS_PATH} not found — cannot persist learned flow")
        return None

    try:
        flows = json.loads(_FLOWS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"[guidance] cannot read {_FLOWS_PATH}: {e}")
        return None

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    flow_id = f"learned_{starting_state.state}__{final_state.state}__{ts}"
    trigger_kws = _frame_keywords(starting_state)

    steps_def = []
    step_keywords: dict[str, list[str]] = {}
    for i, s in enumerate(actionable):
        sid = f"step_{i + 1}"
        if s.action == "tap":
            step_def = {
                "id":                     sid,
                "description":            s.reason,
                "detection":              s.expected_outcome,
                "recovery_action":        "tap_button",
                "recovery_button_labels": [s.label] if s.label else [],
            }
            if s.region_pct:
                step_def["recovery_region"] = s.region_pct
            if s.visual_hint:
                step_def["visual_hint"] = s.visual_hint
            if s.fallback_coord and not s.label:
                step_def["fallback_coord"] = list(s.fallback_coord)
        else:
            step_def = {
                "id":              sid,
                "description":     s.reason,
                "recovery_action": "wait",
                "wait_seconds":    s.wait_seconds,
            }
        steps_def.append(step_def)
        # detection_keywords for replay: use the FIRST few words of the
        # expected_outcome the previous step predicted (if any), otherwise
        # the reason text.  Replay matches on these via perceive().
        detect_src = (s.expected_outcome or s.reason).lower()
        kws = [
            t.strip(".,()[]{}\"'")
            for t in detect_src.split()
            if len(t) >= 4 and t.isalpha()
        ][:3]
        if kws:
            step_keywords[sid] = kws

    parent_detail = (starting_state.detail or "").lower()
    parent_detail = parent_detail.replace("building:", "").split(" — ", 1)[0].strip()

    flow_def = {
        "id":                          flow_id,
        "description":                 f"Auto-learned via Claude guidance — goal: {goal}",
        "parent_state":                starting_state.state,
        "atomic":                      True,
        "trigger_detection":           " ".join(trigger_kws),
        "step_detection_keywords":     step_keywords,
        "steps":                       steps_def,
        "terminal_state":              final_state.state,
        "learned_at":                  datetime.now(timezone.utc).isoformat(),
        "learned_via":                 "claude_guidance",
    }
    if parent_detail:
        flow_def["parent_state_detail_contains"] = [parent_detail.split()[0]]

    # ── Flow-completeness gating (CLAUDE.md guideline) ────────────────────────
    # A flow is COMPLETE only when it terminates at a recognized state AND
    # contains at least one positive transaction.  Tag the persisted flow
    # accordingly so the runtime perceive-guard knows whether to attach it.
    # We persist incomplete flows too — the partial learning is still
    # valuable; the runtime simply won't treat them as authoritative until
    # the missing transaction step is learned.
    from brain.flow_completeness import (
        evaluate_flow_status, has_positive_transaction, categorize_step,
        STATUS_COMPLETE, STATUS_INCOMPLETE,
    )
    from brain.fsm_registry import get_fsm_registry
    terminal_recognized = final_state.state in get_fsm_registry().states
    # Tag every step with its derived category so the file is self-describing
    # and so categorize_step() returns deterministically on subsequent reads.
    for step_def in steps_def:
        step_def["category"] = categorize_step(step_def)
    flow_def["status"] = evaluate_flow_status(
        flow_def, terminal_state_recognized=terminal_recognized, cancelled=False,
    )
    flow_def["positive_transaction_count"] = 1 if has_positive_transaction(steps_def) else 0
    flow_def["terminal_state_recognized"]  = terminal_recognized
    if flow_def["status"] == STATUS_INCOMPLETE:
        reasons = []
        if not has_positive_transaction(steps_def):
            reasons.append("no positive transaction step")
        if not terminal_recognized:
            reasons.append(f"terminal_state={final_state.state!r} not recognized")
        logger.warning(
            f"[guidance] persisting flow {flow_id!r} as INCOMPLETE — "
            f"{'; '.join(reasons)}.  Perceive guard will skip it; learning will "
            "continue on next encounter until the rule is met."
        )

    flows.append(flow_def)
    try:
        _FLOWS_PATH.write_text(json.dumps(flows, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.error(f"[guidance] failed writing {_FLOWS_PATH}: {e}")
        return None

    # Add each step to flow_detection_order in ui_signals.json so perceive()
    # can detect the new flow on future runs.  Mirrors the convention used
    # by _save_flow_step_to_kb in recovery.py.
    signals_path = Path("memory/knowledge/control/ui_signals.json")
    try:
        signals = json.loads(signals_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"[guidance] cannot read {signals_path} to register flow detection: {e}")
        signals = None

    if signals is not None:
        order: list = signals.get("flow_detection_order", [])
        appended = 0
        for step_def in steps_def:
            pair = [flow_id, step_def["id"]]
            if pair not in order:
                order.append(pair)
                appended += 1
        signals["flow_detection_order"] = order
        try:
            signals_path.write_text(json.dumps(signals, indent=2, ensure_ascii=False), encoding="utf-8")
            logger.info(f"[guidance] registered {appended} step(s) in flow_detection_order")
        except Exception as e:
            logger.error(f"[guidance] failed writing {signals_path}: {e}")

    logger.info(f"[guidance] persisted learned flow {flow_id!r} with {len(steps_def)} steps")
    # Reload KB so the new flow is immediately available.
    try:
        from brain.kb import reload as _reload_kb
        _reload_kb()
    except Exception as e:
        logger.warning(f"[guidance] KB reload after persist failed: {e}")

    return flow_id
