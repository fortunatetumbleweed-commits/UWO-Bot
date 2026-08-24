# brain/human_escalation.py
#
# Human-in-the-loop escalation: when all automated recovery fails, ask the
# human operator to describe what they see and what to do.
#
# Flow:
#   1. Print current state + screenshot thumbnail to terminal.
#   2. Prompt operator for a plain-English description of the situation.
#   3. Send description + screenshot to Claude → parsed action plan.
#   4. Execute the plan.
#   5. Re-perceive to verify the state changed.
#   6. Save the scenario + plan as KB knowledge so the same situation is
#      handled automatically next time.
#
# Knowledge is saved to memory/knowledge/fsm/interruptors.json (if the
# scenario looks like an overlay/popup that can fire anywhere) or to
# memory/knowledge/fsm/learned_recoveries.json (state-specific recoveries).
#
# Entry point:
#   from brain.human_escalation import escalate
#   result = escalate(context="stuck in negotiation", perceive_result=pr)
#
# The returned PerceiveResult reflects the state AFTER the operator's
# action.  If the state didn't change the operator is prompted again.

from __future__ import annotations

import json
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

_LEARNED_DIR = Path("memory/knowledge/fsm")
_LEARNED_FILE = _LEARNED_DIR / "learned_recoveries.json"

_MAX_ESCALATION_ROUNDS = 5   # how many operator prompts before giving up


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class ActionStep:
    type: str            # "tap" | "find_and_tap" | "press_back" | "wait" | "swipe"
    label: str = ""      # button label for find_and_tap
    x: int = 0           # pixel coord for tap / swipe
    y: int = 0
    x2: int = 0          # swipe end
    y2: int = 0
    seconds: float = 0.0  # for wait
    # Optional search region for find_and_tap (pixel coords, -1 = no constraint)
    x_min: int = -1
    y_min: int = -1
    x_max: int = -1
    y_max: int = -1


@dataclass
class EscalationPlan:
    scenario_id: str
    category: str         # "interruptor" | "unknown_state" | "flow_step" | "other"
    description: str      # human's description
    actions: list[ActionStep] = field(default_factory=list)
    detection_keywords: list[str] = field(default_factory=list)
    notes: str = ""
    save_as_knowledge: bool = True
    # State the human / Claude says the bot is ACTUALLY in right now.
    # Populated from the parse response; used to correct PerceiveResult before acting.
    corrected_state: Optional[dict] = None  # {state, flow, flow_step, detail}


# ── Action executor ───────────────────────────────────────────────────────────

# Outcomes returned by _execute_action.  Used by _execute_plan to enforce the
# Plan-Completeness rule (CLAUDE.md → "Flow Completeness & Self-Correction"
# → "Claude-generated plans are recommendations, not truth"): a plan is valid
# only when its execution produces at least one positive transaction.
ACTION_TRANSACTION = "transaction"   # tap landed AND chain of positive-button
                                     # dialogs drained cleanly, OR label is a
                                     # commit keyword (recruit/buy/sell/...)
ACTION_NAVIGATION  = "navigation"    # tap landed but no follow-up dialogs
                                     # appeared and label isn't a commit
                                     # keyword (e.g. opening a sub-menu)
ACTION_MISS        = "miss"          # find_and_tap could not locate its label
ACTION_NOOP        = "noop"          # wait / unknown / no-op


# Label keywords that strongly imply a tap commits state, even when no
# follow-up positive-button dialog appears (e.g. a one-shot OK on a
# result screen with no chain to drain).  Conservative: missing a
# transaction is safer than counting navigation as one.
_COMMIT_LABEL_KEYWORDS = (
    "recruit", "buy", "purchase", "sell",
    "depart", "set sail", "sail",
    "hire", "invest", "confirm",
)


def _label_implies_commit(label: str) -> bool:
    if not label:
        return False
    low = label.lower()
    return any(kw in low for kw in _COMMIT_LABEL_KEYWORDS)


# Heuristic mapping from blocker text → goal keywords for the context-
# aware drain.  Conservative, low-maintenance: each entry covers a
# blocker family the bot frequently encounters, with the relevant
# button-label keywords that resolve it.  Universal commit labels
# (ok/confirm/yes/...) are added in find_positive_button_for_context
# automatically — no need to repeat them here.
_BLOCKER_KEYWORDS_MAP = {
    "crew":   ["recruit", "hire", "crew", "mate"],
    "mate":   ["recruit", "hire", "crew", "mate"],
    "supply": ["supply", "auto supply", "supplies"],
    "food":   ["supply", "auto supply", "supplies"],
    "water":  ["supply", "auto supply", "supplies"],
    "fund":   ["deposit", "withdraw", "loan"],
    "money":  ["deposit", "withdraw", "loan"],
    "ducat":  ["deposit", "withdraw", "loan"],
}


def _extract_goal_keywords(blocker_text: Optional[str]) -> list[str]:
    """Translate a blocker description into button-label keywords for
    context-aware drain.  Empty result means no specific context — the
    drain falls back to the legacy any-positive-button matcher.

    The mapping is heuristic by design: the blocker text comes from
    Claude / OCR / KB and varies; matching on substrings is more
    forgiving than requiring exact keys.
    """
    if not blocker_text:
        return []
    low = blocker_text.lower()
    keywords: list[str] = []
    for needle, kws in _BLOCKER_KEYWORDS_MAP.items():
        if needle in low:
            for k in kws:
                if k not in keywords:
                    keywords.append(k)
    return keywords


def _drain_screen_signature(frame=None) -> str:
    """Cheap OCR-based screen signature.  Used to detect whether a drain
    actually advanced the screen state.  Same idea as Fix D's dismissal
    no-op tracker; reuses the OCR cache so it's cheap to call twice in
    succession on the same/adjacent frames."""
    try:
        from capture.adb_capture import capture_screen as _cap
        from actions.sail_actions import _ocr_frame
        if frame is None:
            frame = _cap()
        tokens = _ocr_frame(frame, min_conf=0.3)
        parts = sorted({str(t[0]).lower().strip() for t in tokens})
        return "|".join(parts)[:300]
    except Exception:
        return ""


# Drain outcome tri-state.  _drain_positive_button_chain returns one of:
#   DRAIN_NO_BUTTON  — no positive button was visible after the step.
#                      Caller may treat the original tap as a one-shot
#                      commit if its label is a commit keyword.
#   DRAIN_NO_OP      — drain tapped >=1 buttons but the screen signature
#                      did not change.  The taps were no-ops on highlighted
#                      tabs / non-actionable elements.  Caller MUST classify
#                      the step as navigation regardless of label — the
#                      drain has positively proven nothing committed.
#   integer >= 1     — that many taps fired AND the screen advanced;
#                      a real transaction chain.
DRAIN_NO_BUTTON = 0
DRAIN_NO_OP     = -1


def _drain_positive_button_chain(
    goal_keywords: Optional[list[str]] = None,
) -> int:
    """Tap through any positive-button dialogs that appeared after the
    previous step, until the cycle closes or no positive button remains.

    When *goal_keywords* is supplied, the drain only taps buttons whose
    labels match the goal keywords or UNIVERSAL_COMMIT_LABELS.  This
    prevents tapping irrelevant gold elements (e.g. the harbour's gold
    "Trade Goods" tab when the goal is to recruit crew).

    Returns:
      DRAIN_NO_BUTTON (0)  — no positive button was visible to drain
                              (or no goal-relevant one when filtering).
      DRAIN_NO_OP   (-1)   — drain fired taps but screen signature did
                              not change (e.g. tapped an active tab).
      n (>= 1)             — n meaningful taps fired and screen advanced.
    """
    try:
        from brain.commit_actions import commit_via_positive_taps
        pre_sig = _drain_screen_signature()
        tapped = commit_via_positive_taps(
            max_taps=4, goal_keywords=goal_keywords,
        )
        if not tapped:
            return DRAIN_NO_BUTTON
        post_sig = _drain_screen_signature()
        if pre_sig and post_sig and pre_sig == post_sig:
            # Cycle-closed drain that didn't advance the screen — taps
            # were no-ops on highlighted-but-non-actionable elements
            # (typically active TABS on a panel).  Don't count toward
            # the transaction tally AND don't let the label heuristic
            # rescue this step — the drain has empirical proof the tap
            # committed nothing.
            logger.warning(
                f"  [escalate] drained {len(tapped)} tap(s) but screen "
                "signature is unchanged — drain was a no-op (likely "
                "tapped an active tab, not a commit dialog).  Returning "
                "DRAIN_NO_OP so caller forces NAVIGATION classification."
            )
            return DRAIN_NO_OP
        logger.info(
            f"  [escalate] drained {len(tapped)} follow-up positive-button "
            f"tap(s) after step — chain closed (signature changed)"
        )
        return len(tapped)
    except Exception as e:
        logger.debug(f"  [escalate] post-step drain crashed: {e}")
        return DRAIN_NO_BUTTON


def _execute_action(
    step: ActionStep,
    goal_keywords: Optional[list[str]] = None,
    *,
    auto_drain: bool = True,
) -> str:
    """Execute a single plan step and return an outcome tag.

    Outcome tags:
      ACTION_TRANSACTION — a tap (or swipe) was issued; the caller treats
        these as positive transactions for plan-completeness accounting.
      ACTION_NAVIGATION  — Back/Home press; does not satisfy the
        transaction rule.
      ACTION_MISS        — find_and_tap could not locate the labelled
        button.  Caller applies the refinement chain
        (commit_via_positive_taps → Claude revamp).
      ACTION_NOOP        — wait / unknown step type.

    *auto_drain* controls whether `_drain_positive_button_chain` fires
    after each successful tap to chase follow-up confirmation dialogs.
    Default True for autonomous / learned-recovery / Claude plans where
    the chain may be under-specified.  Set False when the human
    explicitly authored every step in the plan — extra implicit taps
    would defeat their intent (2026-05-20 recruit-crew teaching session:
    the drain after the user's "tap Recruit" tapped Emergency-Recruit
    and Normal-Recruit menu items, closing the cycle without ever
    reaching the OK the user told it to tap).
    """
    from actions.adb_actions import tap
    from actions.sail_actions import press_back, _find_button
    from capture.adb_capture import capture_screen

    # Tap-then-drain rule (post-2026-05-04 livefix): after each successful
    # labelled / positional tap, drain any positive-button dialogs that
    # appeared.  A "transaction" is then defined by ONE of:
    #   - the drain produced >= 1 tap (chain of dialogs dismissed cleanly), OR
    #   - the label itself contains a commit keyword (recruit / buy /
    #     confirm / etc.) — a one-shot commit with no confirmation chain.
    # When neither condition holds, the original tap was navigation
    # (e.g. opening a sub-menu where a fresh choice is still required).
    if step.type == "tap":
        tap(step.x, step.y)
        time.sleep(1.5)
        if not auto_drain:
            # Human-authored plan — trust the explicit step sequence,
            # don't run extra implicit taps between steps.  Classify
            # raw coord taps as navigation; the next plan step decides
            # if it's a commit.
            return ACTION_NAVIGATION
        drained = _drain_positive_button_chain(goal_keywords=goal_keywords)
        # Raw tap has no label.  Drain >=1 = transaction.  Drain == NO_BUTTON
        # could be navigation OR a clean one-shot commit; we can't tell
        # without a label.  Drain == NO_OP is positively a no-op tap.
        if drained > 0:
            return ACTION_TRANSACTION
        return ACTION_NAVIGATION  # treat both NO_BUTTON and NO_OP as nav

    if step.type == "find_and_tap":
        frame = capture_screen()
        kwargs: dict = {}
        if step.x_min >= 0: kwargs["x_min"] = step.x_min
        if step.y_min >= 0: kwargs["y_min"] = step.y_min
        if step.x_max >= 0: kwargs["x_max"] = step.x_max
        if step.y_max >= 0: kwargs["y_max"] = step.y_max
        btn = _find_button(frame, step.label, **kwargs)
        region_str = f" region=({step.x_min},{step.y_min},{step.x_max},{step.y_max})" if kwargs else ""
        if btn:
            logger.info(f"  [escalate] Found button {step.label!r} @ {btn}{region_str} — tapping")
            tap(*btn)
            time.sleep(1.5)
            if not auto_drain:
                # Human-authored: trust the label.  A find_and_tap with
                # a commit-keyword label (recruit/buy/...) OR a universal
                # commit label (ok/confirm/yes/...) = transaction.
                from brain.commit_actions import UNIVERSAL_COMMIT_LABELS
                label_lc = step.label.lower()
                is_commit = (
                    _label_implies_commit(step.label)
                    or any(kw in label_lc for kw in UNIVERSAL_COMMIT_LABELS)
                )
                return ACTION_TRANSACTION if is_commit else ACTION_NAVIGATION
            drained = _drain_positive_button_chain(goal_keywords=goal_keywords)
            if drained > 0:
                return ACTION_TRANSACTION
            if drained == DRAIN_NO_OP:
                # Drain proved the tap committed nothing — even if the
                # label looks like a commit keyword, the screen state
                # didn't advance, so this step is honestly navigation.
                # This is the harbour Trade Goods case: tap "Recruit"
                # at left side, drain finds gold tab, taps it, screen
                # unchanged.  Without this branch the label heuristic
                # would still call it a transaction.
                logger.info(
                    f"  [escalate] {step.label!r} tap drained as no-op — "
                    "classifying as NAVIGATION (label heuristic suppressed)"
                )
                return ACTION_NAVIGATION
            # DRAIN_NO_BUTTON: no follow-up dialog appeared at all.
            # Was the tap itself a commit?  Use the label as the signal
            # (recruit/buy/confirm/etc.).  This catches the one-shot
            # OK-on-result-screen case where there are no further
            # dialogs to drain because the commit happened in one tap
            # AND the post-frame is the home state with no positive
            # button visible.
            if _label_implies_commit(step.label):
                return ACTION_TRANSACTION
            return ACTION_NAVIGATION
        logger.warning(f"  [escalate] Button {step.label!r} not found{region_str} — needs refinement")
        return ACTION_MISS

    if step.type == "press_back":
        press_back()
        time.sleep(1.5)
        return ACTION_NAVIGATION

    if step.type == "wait":
        time.sleep(max(step.seconds, 1.0))
        return ACTION_NOOP

    if step.type == "swipe":
        from actions.adb_actions import swipe
        swipe(step.x, step.y, step.x2, step.y2)
        time.sleep(1.5)
        if not auto_drain:
            return ACTION_NAVIGATION
        drained = _drain_positive_button_chain(goal_keywords=goal_keywords)
        return ACTION_TRANSACTION if drained > 0 else ACTION_NAVIGATION
        # NO_BUTTON and NO_OP both → NAVIGATION (no commit-keyword label
        # to fall back on for swipes).

    logger.warning(f"  [escalate] Unknown action type {step.type!r} — skipping")
    return ACTION_NOOP


def _refine_with_positive_button(
    goal_keywords: Optional[list[str]] = None,
) -> bool:
    """First fallback when a plan step misses: tap whatever positive
    (gold / yellow) button is currently visible.  Returns True if at least
    one positive button was tapped — that counts as a transaction toward
    the Plan-Completeness rule.

    When *goal_keywords* is supplied, the refinement is context-aware:
    only buttons matching the goal keywords or universal commit labels
    are tapped.  Prevents the harbour Trade-Goods false positive.
    """
    try:
        from brain.commit_actions import commit_via_positive_taps
        tapped = commit_via_positive_taps(max_taps=3, goal_keywords=goal_keywords)
        if tapped:
            logger.info(
                f"  [escalate] Refined missed step via commit_via_positive_taps "
                f"({len(tapped)} tap(s)) — counts as transaction"
            )
            return True
        logger.info(
            "  [escalate] Refinement via commit_via_positive_taps found no "
            "positive button" + (f" matching goal {goal_keywords!r}" if goal_keywords else "")
        )
        return False
    except Exception as e:
        logger.warning(f"  [escalate] Refinement step crashed: {e}")
        return False


def _execute_plan(
    plan: EscalationPlan,
    goal_keywords: Optional[list[str]] = None,
    blocker_text: Optional[str] = None,
    *,
    auto_drain: bool = True,
) -> tuple[int, bool]:
    """Execute *plan* and return ``(transactions, early_exit)``.

    *auto_drain* forwards to `_execute_action` — set False when the
    human is actively teaching steps in `escalate()` so the post-tap
    drain doesn't insert implicit taps between the human's explicit
    instructions.  See `_execute_action` docstring for the failure
    mode this protects against.

    transactions: number of positive transactions produced.
    early_exit:   True iff a mid-plan blocker re-check confirmed the
                  original blocker has resolved (depart-panel anchor
                  visible AND blocker text absent).  Callers should treat
                  early_exit=True as plan success regardless of the
                  transaction count.

    Per the Plan-Completeness rule (CLAUDE.md), a plan that produces zero
    transactions is invalid AND no early_exit fired — callers must NOT
    save it as a learned recovery in that case.  When early_exit fires,
    the plan is treated as successful (the blocker is what we were trying
    to remove, so its disappearance is the strongest possible success
    signal) and saving is appropriate.

    On a step miss (find_and_tap target not visible), the runner applies
    the fallback chain:
      1. commit_via_positive_taps — tap a visible positive button instead.
      2. Claude revamp — if no positive button is found, defer to the
         caller (currently surfaced via the return value; a future
         revision can invoke the revamp helper inline).

    Per-step blocker re-check (2026-05-12 livefix): before each step
    AND after each step's action, if *blocker_text* was supplied, the
    runner consults `_check_blocker_resolved(blocker_text)` on the
    current frame.  Returns early with early_exit=True the moment the
    blocker is positively gone — even if subsequent plan steps haven't
    run yet.  Origin: the 'has_enough_crew' escalation loop of
    2026-05-12 17:19, which kept tapping no-op info labels for 4.5
    minutes after the harbour depart panel had already cleared.
    """
    logger.info(
        f"  [escalate] Executing plan {plan.scenario_id!r} "
        f"({len(plan.actions)} steps)"
        + (f"  blocker={blocker_text!r}" if blocker_text else "")
    )

    def _blocker_gone() -> bool:
        """True iff we have positive evidence the blocker has resolved."""
        if not blocker_text:
            return False
        try:
            from actions.sail_actions import _check_blocker_resolved
        except Exception:
            return False
        try:
            return _check_blocker_resolved(blocker_text) is True
        except Exception as e:
            logger.debug(f"  [escalate] blocker re-check failed: {e}")
            return False

    # Pre-plan blocker check: the screen may have changed since the caller
    # decided escalation was needed.  Skip the whole plan if so.
    if _blocker_gone():
        logger.info(
            f"  [escalate] Pre-plan blocker check: {blocker_text!r} already "
            "resolved — skipping plan entirely"
        )
        return 0, True

    transactions = 0
    for step_idx, step in enumerate(plan.actions, start=1):
        outcome = _execute_action(
            step, goal_keywords=goal_keywords, auto_drain=auto_drain,
        )
        if outcome == ACTION_TRANSACTION:
            transactions += 1
        elif outcome == ACTION_MISS:
            # First-line refinement: tap any visible positive button
            # (filtered by goal_keywords when supplied).
            if _refine_with_positive_button(goal_keywords=goal_keywords):
                transactions += 1
            # Persistent miss falls through silently here — _resolve_*
            # callers (e.g. _resolve_blocker_with_reasoning) handle the
            # second-line Claude revamp using the returned transaction count.

        # Post-step blocker re-check: if the action just removed the
        # blocker (game side-effect, redistribution auto-fired, or the
        # step itself was the resolution), exit the plan as success.
        if _blocker_gone():
            logger.info(
                f"  [escalate] Step {step_idx}/{len(plan.actions)}: blocker "
                f"{blocker_text!r} resolved — exiting plan early (transactions={transactions})"
            )
            return transactions, True

    if transactions == 0:
        logger.warning(
            f"  [escalate] Plan {plan.scenario_id!r} executed all "
            f"{len(plan.actions)} step(s) without any positive transaction "
            "— plan is INVALID per CLAUDE.md plan-completeness rule"
        )
    return transactions, False


# ── Claude parser ─────────────────────────────────────────────────────────────

_PARSE_SYSTEM = """\
You are an assistant for an autonomous bot playing "Uncharted Waters Origin" (UWO).
The bot is stuck and the human operator has described what they see and what to do.

Your job:
1. Look at the screenshot carefully.
2. Read the operator's description.
3. Identify the exact UI element the operator is referring to.
4. Produce a structured action plan the bot can execute.
5. Classify the scenario so the bot can learn to handle it automatically next time.

Screen resolution: 2400×1080 (landscape). The screenshot is sent at half resolution
(1200×540) to save bandwidth — report all coordinates in the ORIGINAL 2400×1080 space
(double the x,y you see in the image).
Fixed chrome positions:
  Home (⌂): ~(2300, 45)   Back (←): ~(60, 45)

── Choosing action type ──────────────────────────────────────────────────────
PREFER "tap" with exact pixel coords when you can visually locate the element
in the screenshot. This is the most reliable action — it goes directly to the
right pixel without any text-matching step that could land on the wrong element.

Use "find_and_tap" ONLY when you cannot determine the exact position (e.g. the
button position shifts depending on content). When you do use find_and_tap,
ALWAYS add region constraints derived from where you see the element in the
screenshot — never leave all four region fields at -1 if you know the location.

── Region constraint reference (screen 2400×1080) ────────────────────────────
Location phrase      x_min   y_min   x_max   y_max
────────────────     ──────  ──────  ──────  ──────
top-left             -1      -1      800     380
top-right            1600    -1      -1      380
top-center           600     -1      1800    380
bottom-left          -1      700     800     -1
bottom-right         1440    700     -1      -1
bottom-center        600     700     1800    -1
left side            -1      -1      800     -1
right side           1440    -1      -1      -1
center               600     350     1800    730
full screen          -1      -1      -1      -1

── Response format ────────────────────────────────────────────────────────────
Respond ONLY with valid JSON:
{
  "scenario_id": "short_snake_case_id",
  "category": "interruptor|unknown_state|flow_step|other",
  "corrected_state": {
    "state": "<port_overworld|sea|building|world_map|loading|unknown>",
    "flow":  "<market_purchase|market_sell|harbor_departure|null>",
    "flow_step": "<basket|confirm|negotiate|result|null>",
    "detail": "<brief description, e.g. 'building: purchase'>"
  },
  "actions": [
    {"type": "tap",          "x": 1920, "y": 980},
    {"type": "find_and_tap", "label": "ok", "x_min": 800, "y_min": 700, "x_max": 1600, "y_max": -1},
    {"type": "press_back"},
    {"type": "wait",         "seconds": 2.0}
  ],
  "detection_keywords": ["keyword1", "keyword2"],
  "notes": "brief description for KB"
}

find_and_tap fields: label (required), x_min/y_min/x_max/y_max (all default -1 = no constraint).

corrected_state: the ACTUAL current state from the screenshot and operator description.
The bot's local perception may be wrong — this corrects it.
Set flow/flow_step to null if no active transaction dialog is open.

Category guide:
  interruptor   — popup/overlay that can fire on top of any screen; dismiss and resume
  unknown_state — full-screen state the bot has never seen
  flow_step     — dialog that is part of a transaction the bot already started
  other         — anything else
"""

_PARSE_PROMPT = """\
Bot's perceived state: {state}
Detail: {detail}
Active flow: {flow}

Operator description:
{description}

Look at the screenshot, identify what the operator is describing, and produce the action plan.
"""


def _parse_with_claude(
    description: str,
    state: str,
    detail: str,
    flow: Optional[str],
    frame,
) -> Optional[EscalationPlan]:
    """
    Send operator description + screenshot to Claude and parse the response
    into an EscalationPlan.  Returns None if Claude is unavailable.
    """
    import base64, io, os
    try:
        import anthropic
    except ImportError:
        logger.warning("  [escalate] anthropic not installed — cannot parse with Claude")
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("  [escalate] ANTHROPIC_API_KEY not set — cannot parse with Claude")
        return None

    client = anthropic.Anthropic(api_key=api_key)

    # Use half-resolution (1200×540) — enough to identify button positions while
    # keeping the API call fast.  Coordinates in the response must be for the
    # ORIGINAL 2400×1080 frame (Claude is told the screen is 2400×1080 in the prompt).
    thumb = frame.copy()
    thumb.thumbnail((1200, 540))
    buf = io.BytesIO()
    thumb.save(buf, format="PNG")
    img_b64 = base64.b64encode(buf.getvalue()).decode()

    prompt = _PARSE_PROMPT.format(
        state=state, detail=detail,
        flow=flow or "none",
        description=description,
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=_PARSE_SYSTEM,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image",
                     "source": {"type": "base64",
                                "media_type": "image/png",
                                "data": img_b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        raw = response.content[0].text.strip()
        logger.debug(f"  [escalate] Claude raw response: {raw[:200]}")
        if not raw:
            logger.error("  [escalate] Claude returned empty response")
            return None
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
    except Exception as exc:
        logger.error(f"  [escalate] Claude parse failed: {exc}")
        return None

    actions = []
    for a in data.get("actions", []):
        actions.append(ActionStep(
            type    = a.get("type", "wait"),
            label   = a.get("label", ""),
            x       = a.get("x", 0),
            y       = a.get("y", 0),
            x2      = a.get("x2", 0),
            y2      = a.get("y2", 0),
            seconds = a.get("seconds", 1.0),
            x_min   = a.get("x_min", -1),
            y_min   = a.get("y_min", -1),
            x_max   = a.get("x_max", -1),
            y_max   = a.get("y_max", -1),
        ))

    # Extract corrected_state — convert "null" strings to Python None
    cs_raw = data.get("corrected_state") or {}
    corrected_state: Optional[dict] = None
    if cs_raw and isinstance(cs_raw, dict):
        corrected_state = {
            "state":     cs_raw.get("state") or None,
            "flow":      cs_raw.get("flow") or None,
            "flow_step": cs_raw.get("flow_step") or None,
            "detail":    cs_raw.get("detail") or "",
        }

    return EscalationPlan(
        scenario_id        = data.get("scenario_id", "unknown_escalation"),
        category           = data.get("category", "other"),
        description        = description,
        actions            = actions,
        detection_keywords = data.get("detection_keywords", []),
        notes              = data.get("notes", ""),
        corrected_state    = corrected_state,
    )


# ── Knowledge persistence ─────────────────────────────────────────────────────

def _save_knowledge(plan: EscalationPlan) -> None:
    """
    Persist the escalation plan so the bot can recognise this scenario next time.

    - "interruptor" → append to interruptors.json (perceive.py will pick it up)
    - others        → append to learned_recoveries.json
    """
    _LEARNED_DIR.mkdir(parents=True, exist_ok=True)

    if plan.category == "interruptor" and plan.detection_keywords:
        _save_as_interruptor(plan)
    else:
        _save_as_learned_recovery(plan)


def _save_as_interruptor(plan: EscalationPlan) -> None:
    """Append a new interruptor entry to interruptors.json."""
    from brain.fsm_registry import get_fsm_registry

    entry = {
        "id":          plan.scenario_id,
        "description": plan.notes or plan.description[:120],
        "atomic":      True,
        "detection":   " + ".join(plan.detection_keywords),
        "dismissal":   _actions_to_dismissal(plan.actions),
        "resumes":     "current_state_unchanged",
        "learned_at":  datetime.now(timezone.utc).isoformat(),
        "learned_from": "human_escalation",
    }

    registry = get_fsm_registry()
    if plan.scenario_id not in registry.interruptors:
        registry.add_interruptor(entry)
        logger.info(f"  [escalate] Saved new interruptor: {plan.scenario_id!r}")
    else:
        logger.info(f"  [escalate] Interruptor {plan.scenario_id!r} already in KB — skipping")


def _save_as_learned_recovery(plan: EscalationPlan) -> None:
    """Persist *plan* to learned_recoveries.json.

    Plan-completeness rule (CLAUDE.md → "Claude-generated plans are
    recommendations, not truth"): callers must NOT invoke this when the
    plan executed without producing any positive transaction.  This
    function does not re-verify because it has no access to runtime
    execution state — the gate is the caller's responsibility.  Saving a
    transaction-less plan here reproduces the broken-recovery cycle and
    each subsequent encounter re-fires the broken plan.
    """
    """Append to learned_recoveries.json."""
    entries: list[dict] = []
    if _LEARNED_FILE.exists():
        try:
            entries = json.loads(_LEARNED_FILE.read_text())
        except Exception:
            entries = []

    # Deduplicate by scenario_id
    ids = {e["id"] for e in entries}
    if plan.scenario_id in ids:
        logger.info(f"  [escalate] {plan.scenario_id!r} already in learned recoveries")
        return

    entries.append({
        "id":                plan.scenario_id,
        "category":          plan.category,
        "description":       plan.notes or plan.description[:120],
        "detection_keywords": plan.detection_keywords,
        "actions":           [vars(a) for a in plan.actions],
        "learned_at":        datetime.now(timezone.utc).isoformat(),
    })
    _LEARNED_FILE.write_text(json.dumps(entries, indent=2, ensure_ascii=False))
    logger.info(f"  [escalate] Saved learned recovery: {plan.scenario_id!r}")


def _actions_to_dismissal(actions: list[ActionStep]) -> str:
    """Summarise action list into a human-readable dismissal description."""
    parts = []
    for a in actions:
        if a.type == "find_and_tap":
            parts.append(f"tap_{a.label.lower()}")
        elif a.type == "press_back":
            parts.append("press_back")
        elif a.type == "tap":
            parts.append(f"tap({a.x},{a.y})")
        elif a.type == "wait":
            parts.append(f"wait_{a.seconds:.0f}s")
    return "_then_".join(parts) if parts else "tap_ok"


# ── Learned recovery matcher ──────────────────────────────────────────────────


# Phase 4 runtime safety: track recent fires of each learned recovery and
# auto-disable any recipe that fires from the same screen signature multiple
# times in a row without the screen actually changing.  Even a recipe that
# was approved by Claude or by a human teaching session can be wrong (game
# patch shifted UI, overfit keywords, etc.) — the bot needs to detect the
# loop and stop digging.
#
# Two-layer:
#   - In-memory _recovery_fire_log: holds the last few (signature, t) per
#     scenario_id within this process.  Resets on restart.
#   - Persistent demotion: when the in-memory tracker decides a recipe is
#     looping, also write confidence='low' + failure_count to the entry
#     in learned_recoveries.json so the next process starts skipping it
#     too without needing to re-detect the loop.
_FAILURE_DEMOTION_THRESHOLD = 3   # consecutive same-sig fires before disable
_recovery_fire_log: dict[str, deque] = {}
_session_disabled_recoveries: set[str] = set()


def _signature_from_tokens(ocr_tokens: list, top_n: int = 8) -> str:
    """
    Cheap screen signature for fire-tracking purposes.  Take the top-N
    longest alphabetic OCR tokens, lowercase them, sort and join.  Two
    frames produce the same signature when their OCR text is essentially
    identical — the screen hasn't moved.
    """
    if not ocr_tokens:
        return ""
    cleaned = sorted({
        t.lower() for t, *_ in ocr_tokens
        if isinstance(t, str) and len(t) >= 4 and t.isalpha()
    })[:top_n]
    return "|".join(cleaned)


def _persist_recovery_demotion(scenario_id: str) -> None:
    """
    Write confidence='low' (and increment failure_count) to the named
    learned_recovery in the KB so the next process skips it too.  Idempotent
    — calling repeatedly is harmless.
    """
    if not _LEARNED_FILE.exists():
        return
    try:
        entries = json.loads(_LEARNED_FILE.read_text())
    except Exception as e:
        logger.warning(f"  [escalate] could not read {_LEARNED_FILE} for demotion: {e}")
        return

    found = False
    for entry in entries:
        if entry.get("id") == scenario_id:
            entry["confidence"]    = "low"
            entry["failure_count"] = int(entry.get("failure_count", 0)) + 1
            entry["last_failed_at"] = datetime.now(timezone.utc).isoformat()
            found = True
            break
    if not found:
        return
    try:
        _LEARNED_FILE.write_text(
            json.dumps(entries, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info(
            f"  [escalate] Persisted demotion: {scenario_id!r} → confidence=low"
        )
    except Exception as e:
        logger.warning(f"  [escalate] could not write demotion for {scenario_id!r}: {e}")


def should_skip_recovery_for_loop(scenario_id: str, signature: str) -> bool:
    """
    Pre-fire guard.  Returns True when this recovery has already fired
    _FAILURE_DEMOTION_THRESHOLD - 1 times consecutively from the same
    signature without changing it — i.e. firing again would just be the
    Nth attempt at a clearly-not-working recipe.

    On True, the function ALSO persists confidence='low' to the KB so
    future processes skip without needing to re-detect.

    On False, records this fire in the in-memory log so a future call
    can detect the loop.

    Caller pattern (in perceive's interruptor pass):
        sig = _signature_from_tokens(ocr_tokens)
        if should_skip_recovery_for_loop(plan.scenario_id, sig):
            continue   # don't fire — recipe is stuck
        _execute_plan(plan)
    """
    if scenario_id in _session_disabled_recoveries:
        return True

    log = _recovery_fire_log.setdefault(scenario_id, deque(maxlen=_FAILURE_DEMOTION_THRESHOLD))

    # Pre-fire check: if the last (THRESHOLD - 1) fires were all from this
    # same signature, firing again would be the THRESHOLD-th attempt.
    same_sig_count = sum(1 for s, _ in log if s == signature)
    if same_sig_count >= _FAILURE_DEMOTION_THRESHOLD - 1:
        logger.error(
            f"  [escalate] Auto-disabling learned recovery {scenario_id!r} — "
            f"would be attempt {same_sig_count + 1} from the same screen "
            f"signature without progress.  This is a stuck-loop signature "
            f"(human/Claude approved recipes can still go wrong if the game "
            f"UI drifts)."
        )
        _session_disabled_recoveries.add(scenario_id)
        _persist_recovery_demotion(scenario_id)
        return True

    log.append((signature, time.time()))
    return False


def _entry_is_demoted(entry: dict) -> bool:
    """True when an entry's persisted confidence says 'do not use'."""
    return entry.get("confidence", "high") in ("low", "unverified-skip")


def _match_learned_recovery(state: str, detail: str) -> Optional[EscalationPlan]:
    """
    Check if the current state matches any previously learned recovery.
    Returns an EscalationPlan if matched, None otherwise.

    Demoted entries (confidence='low' from auto-disable or manual edit)
    are excluded — they failed enough times that the bot should NOT
    trust them as a quick fix.
    """
    if not _LEARNED_FILE.exists():
        return None
    try:
        entries = json.loads(_LEARNED_FILE.read_text())
    except Exception:
        return None

    text = f"{state} {detail}".lower()
    for entry in entries:
        if _entry_is_demoted(entry):
            continue
        if entry.get("id") in _session_disabled_recoveries:
            continue
        keywords = entry.get("detection_keywords", [])
        if keywords and all(kw.lower() in text for kw in keywords):
            actions = [ActionStep(**a) for a in entry.get("actions", [])]
            logger.info(
                f"  [escalate] Matched learned recovery: {entry['id']!r} "
                f"(keywords: {keywords})"
            )
            return EscalationPlan(
                scenario_id        = entry["id"],
                category           = entry.get("category", "other"),
                description        = entry.get("description", ""),
                actions            = actions,
                detection_keywords = keywords,
                save_as_knowledge  = False,   # already saved
            )

    return None


# ── Human prompt ──────────────────────────────────────────────────────────────

class TeachingAbortedError(Exception):
    """
    Raised when an interactive teaching session is aborted before the
    human confirms completion — either the initial prompt timed out
    (human walked away / didn't engage) or the operator typed 'cancel'
    explicitly.

    Caller should treat the current task as failed and clean up: the
    chat loop / task_runner catches this, logs the abort, and returns
    the bot to the chat prompt rather than continuing the loop.
    """


# Wall-clock timeout for the FIRST prompt in a teaching session.  Subsequent
# prompts (after the human has engaged with at least one action) wait
# indefinitely — once they're committed we don't penalise them for thinking.
_INITIAL_PROMPT_TIMEOUT_SECS = 600   # 10 minutes


def _input_with_timeout(prompt: str, timeout_secs: Optional[int]) -> Optional[str]:
    """
    input() with an optional wall-clock timeout (POSIX only — we run on macOS).

    timeout_secs:
        None → wait indefinitely.
        int  → wait up to N seconds; on expiry return None.

    Returns the trimmed line, or None on timeout / EOF / KeyboardInterrupt /
    blank input (all treated as "no usable response").
    """
    import select

    print(prompt, end='', flush=True)
    if timeout_secs is None:
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            return None
        line = line.strip()
        return line if line else None

    ready, _, _ = select.select([sys.stdin], [], [], timeout_secs)
    if not ready:
        print()  # newline so subsequent log lines aren't appended to the prompt
        return None
    try:
        line = sys.stdin.readline()
    except Exception:
        return None
    if not line:
        return None
    line = line.strip()
    return line if line else None


def _print_teach_header(
    state: str, detail: str, flow: Optional[str], goal: Optional[str],
    step_num: int, accumulated: list,
    is_initial: bool,
) -> None:
    """Render the teaching-session prompt header.  Pure I/O; no logic."""
    title = (
        "BOT STUCK — TEACHING SESSION (initial prompt)"
        if is_initial else
        f"TEACHING SESSION — step {step_num}  ({len(accumulated)} action(s) accumulated)"
    )
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)
    if goal:
        print(f"  Goal          : {goal}")
    print(f"  Current state : {state}")
    print(f"  Detail        : {detail}")
    if flow:
        print(f"  Active flow   : {flow}")
    if accumulated:
        print(f"  So far ({len(accumulated)}):")
        for i, act in enumerate(accumulated, 1):
            print(f"    {i}. {act.type} "
                  + (f"({act.x},{act.y})" if act.type == "tap" else "")
                  + (f" label={act.label!r}" if getattr(act, "label", "") else ""))
    print()
    if is_initial:
        print("  Tell me ONE action to perform (you'll be asked again after).")
        print("  Or type 'cancel' to abort with no learning.")
        print(f"  (Times out after {_INITIAL_PROMPT_TIMEOUT_SECS}s of no input.)")
    else:
        print("  Tell me the NEXT action, or:")
        print("    'done'    — goal is complete, save what we did")
        print("    'cancel'  — discard everything, abort")
    print("  Examples: 'tap the OK button', 'tap (1280, 880)', 'press back', 'wait 2s'")
    print("-" * 70)


def _prompt_operator_for_step(
    state: str, detail: str, flow: Optional[str], goal: Optional[str],
    step_num: int, accumulated: list,
    timeout_secs: Optional[int],
) -> Optional[str]:
    """
    Show the teaching header and prompt for ONE action.  Returns the
    operator's input string (which will be parsed by the caller for
    'done' / 'cancel' / natural-language action), or None on timeout /
    EOF / blank input / non-interactive stdin.
    """
    if not sys.stdin.isatty():
        logger.warning(
            "  [escalate] Not running interactively — cannot prompt operator. "
            "Running headless; escalation skipped."
        )
        return None

    is_initial = step_num == 1 and not accumulated
    _print_teach_header(
        state, detail, flow, goal,
        step_num=step_num, accumulated=accumulated,
        is_initial=is_initial,
    )
    return _input_with_timeout("  > ", timeout_secs)


def _prompt_operator(state: str, detail: str, flow: Optional[str]) -> Optional[str]:
    """
    Backward-compatible single-prompt helper.  Kept so callers outside the
    teaching loop (none today, but the function signature is part of the
    module's public-ish surface) continue to work.  The teaching loop in
    escalate() now uses _prompt_operator_for_step instead.
    """
    return _prompt_operator_for_step(
        state, detail, flow, goal=None,
        step_num=1, accumulated=[],
        timeout_secs=None,
    )


# ── Main entry point ──────────────────────────────────────────────────────────

def _build_corrected_result(
    cs: dict,
    original: "brain.perceive.PerceiveResult",
) -> "brain.perceive.PerceiveResult":
    """Build a PerceiveResult from a corrected_state dict (Claude/human-supplied)."""
    from brain.perceive import PerceiveResult
    return PerceiveResult(
        state      = cs.get("state") or original.state,
        port       = original.port,
        detail     = cs.get("detail") or original.detail,
        flow       = cs.get("flow") or None,
        flow_step  = cs.get("flow_step") or None,
        confidence = "high",
        corrected  = True,
    )


_TEACHING_MAX_STEPS = 20   # safety cap on accumulated actions per session


def _is_control_command(text: str) -> Optional[str]:
    """
    Recognise teaching-loop control commands.  Returns one of
    'done' | 'cancel' | None.  Case-insensitive; accepts a few aliases
    so the human doesn't need to memorise exact wording.
    """
    t = (text or "").strip().lower()
    if t in ("done", "complete", "finish", "finished", "ok done", "yes done"):
        return "done"
    if t in ("cancel", "abort", "quit", "stop", "give up"):
        return "cancel"
    return None


def _save_taught_recovery(
    accumulated: list, scenario_state: str, scenario_detail: str,
    scenario_flow: Optional[str], goal: Optional[str],
) -> None:
    """
    Persist an accumulated multi-step recipe as a single learned_recovery
    with provenance='human_taught' and confidence='unverified'.  Phase 4
    verification on later replays graduates the entry to 'high' (or
    demotes / disables if the recipe stops working).

    Saving only the COMPLETE accumulated list (after the human said
    'done') is the structural fix for the bug class observed in May-1
    runs: prior code saved each individual step as its own recovery,
    so an incomplete teach left the KB with single-tap "recipes" that
    the proactive matcher then replayed without ever finishing the
    flow.
    """
    if not accumulated:
        return

    # Build a synthetic plan that mirrors the existing save format but
    # carries the FULL accumulated step list.
    detection_keywords = _derive_keywords(scenario_state, scenario_detail)
    scenario_id = f"taught__{_slug(scenario_state)}__{int(time.time())}"
    plan = EscalationPlan(
        scenario_id        = scenario_id,
        category           = "flow_step",
        description        = (
            f"Human-taught multi-step recovery (goal: {goal or 'unspecified'}). "
            f"Saved with confidence=unverified — Phase 4 verification on replay "
            f"will promote or demote based on observed outcome."
        ),
        actions            = list(accumulated),
        detection_keywords = detection_keywords,
        save_as_knowledge  = True,
    )
    _save_as_learned_recovery(plan)
    logger.info(
        f"  [escalate] Persisted taught recovery {scenario_id!r} with "
        f"{len(accumulated)} action(s) — confidence=unverified"
    )


def _derive_keywords(state: str, detail: str) -> list[str]:
    """
    Pick a handful of detection keywords from the scenario detail so the
    proactive matcher can find this recovery on later encounters.  Skips
    generic words and very short tokens.
    """
    text = (detail or "").lower().replace("building:", "").split(" — ", 1)[0]
    seen, out = set(), []
    for tok in text.split():
        tok = tok.strip(".,()[]{}\"'")
        if len(tok) < 4 or not tok.replace(" ", "").isalpha():
            continue
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
        if len(out) >= 5:
            break
    return out


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s)[:48]


class _SkipPositiveFallback(Exception):
    """Internal: the positive-button fallback does not apply here (no goal to finish)."""


def escalate(
    context: str,
    perceive_result: "brain.perceive.PerceiveResult",
    goal: Optional[str] = None,
) -> "brain.perceive.PerceiveResult":
    """
    Human-in-the-loop escalation entry point — interactive teaching loop.

    Called when automated recovery has failed.  Workflow:

      1. Try matching learned_recoveries.json once — if matched, replay
         the taught recipe and return immediately (existing behaviour).

      2. Initial prompt (single step) with a wall-clock timeout
         (_INITIAL_PROMPT_TIMEOUT_SECS, default 10 min).  If the human
         doesn't engage within the window the bot raises
         TeachingAbortedError so the caller can quit the task and
         return to the main menu.

      3. Once the human types a first instruction, enter the teaching
         loop with NO timeout (engaged session — give them as long as
         they need).  Each iteration:
             a. Parse input — 'done' / 'cancel' short-circuit; otherwise
                pass to Claude as a single-action description.
             b. Execute the action.
             c. Re-perceive and show the new state to the human.
             d. Prompt for the next action.
         Loop terminates on 'done' (save accumulated steps as one
         learned_recovery with confidence='unverified') or 'cancel'
         (discard everything, return without saving).

    Discarding partial progress is by design — a half-taught recipe
    that the human couldn't finish is by definition an unverified
    edge.  Phase 4 on the verified-end side handles graduation /
    demotion as those recipes get replayed.
    """
    from brain.perceive import perceive, reclassify_with_claude
    from capture.adb_capture import capture_screen
    from actions.adb_actions import tap as _tap_home_button

    state  = perceive_result.state
    detail = perceive_result.detail
    flow   = perceive_result.flow
    current_result = perceive_result

    logger.warning(
        f"  [escalate] All automated recovery failed — entering teaching loop. "
        f"context={context!r}  state={state!r}  goal={goal!r}"
    )

    frame = capture_screen()

    # ── Try learned recovery first ────────────────────────────────────────────
    plan = _match_learned_recovery(state, detail)
    if plan:
        logger.info(f"  [escalate] Applying learned recovery: {plan.scenario_id!r}")
        _execute_plan(plan)
        result = reclassify_with_claude(capture_screen(), perceive())
        if result.state != state:
            logger.info(
                f"  [escalate] Learned recovery worked: "
                f"{state!r} → {result.state!r}"
            )
            return result
        logger.warning(
            "  [escalate] Learned recovery did not change state — "
            "falling through to interactive teaching"
        )

    # ── Positive-button fallback (before asking human) ────────────────────────
    # Many escalations land on a screen that has a single gold/yellow positive
    # button visible (Confirm / Recruit / OK on a result dialog).  The
    # commit_via_positive_taps primitive cycles those taps until the
    # transaction closes — exactly what the human would do if asked.  Try
    # it once before opening the teaching loop.
    try:
        from brain.commit_actions import commit_via_positive_taps

        # A positive-button drain is only legitimate when something unexpected interrupted
        # a goal the bot was PURSUING — i.e. it has a goal and has already committed an
        # action, so there is a transaction to finish (user 2026-08-22). With no goal there
        # is nothing to finish, and the search degrades to "tap whatever looks positive":
        # on 2026-08-22 that tapped `Trade Info` and then the `Requested Trade Goods` panel
        # title on the Market landing page, where no transaction existed at all.
        goal_keywords = _extract_goal_keywords(goal) if goal else None
        if not goal_keywords:
            logger.info(
                "  [escalate] No goal in context — skipping the positive-button fallback "
                "(nothing to commit) and falling through to teaching"
            )
            raise _SkipPositiveFallback

        logger.info(
            f"  [escalate] Pre-teaching fallback: trying commit_via_positive_taps "
            f"(goal_keywords={goal_keywords}, up to 3 cycles)"
        )
        tapped = commit_via_positive_taps(max_taps=3, goal_keywords=goal_keywords)
        if tapped:
            result = reclassify_with_claude(capture_screen(), perceive())
            if result.state != state:
                logger.info(
                    f"  [escalate] Positive-button fallback worked: "
                    f"{state!r} → {result.state!r} after {len(tapped)} tap(s)"
                )
                return result
            logger.warning(
                f"  [escalate] Positive-button fallback ran {len(tapped)} tap(s) "
                f"but state is still {state!r} — falling through to teaching"
            )
        else:
            logger.info(
                "  [escalate] No positive button present — falling through to teaching"
            )
    except Exception as e:
        logger.warning(f"  [escalate] commit_via_positive_taps fallback failed: {e}")

    # ── Interactive teaching loop ─────────────────────────────────────────────
    accumulated: list[ActionStep] = []
    for step_num in range(1, _TEACHING_MAX_STEPS + 1):
        # Initial prompt has a wall-clock timeout; subsequent prompts wait
        # indefinitely (engaged session).
        timeout = _INITIAL_PROMPT_TIMEOUT_SECS if step_num == 1 else None
        description = _prompt_operator_for_step(
            state, detail, flow, goal,
            step_num=step_num, accumulated=accumulated,
            timeout_secs=timeout,
        )
        if description is None:
            if step_num == 1:
                logger.error(
                    f"  [escalate] Initial teaching prompt timed out after "
                    f"{_INITIAL_PROMPT_TIMEOUT_SECS}s — aborting task."
                )
                raise TeachingAbortedError(
                    f"No human response within {_INITIAL_PROMPT_TIMEOUT_SECS}s"
                )
            # Mid-session blank — treat as cancel to preserve unambiguous semantics.
            logger.info(
                f"  [escalate] Empty input at step {step_num} — treating as cancel; "
                "discarding accumulated steps."
            )
            return reclassify_with_claude(capture_screen(), current_result)

        # ── Control commands ──────────────────────────────────────────────────
        cmd = _is_control_command(description)
        if cmd == "done":
            logger.info(
                f"  [escalate] Operator confirmed 'done' after {len(accumulated)} "
                "action(s) — saving taught recovery"
            )
            _save_taught_recovery(
                accumulated,
                scenario_state  = perceive_result.state,
                scenario_detail = perceive_result.detail,
                scenario_flow   = perceive_result.flow,
                goal            = goal,
            )
            return reclassify_with_claude(capture_screen(), current_result)
        if cmd == "cancel":
            logger.warning(
                f"  [escalate] Operator cancelled after {len(accumulated)} action(s) "
                "— discarding (no learned recovery saved)"
            )
            return reclassify_with_claude(capture_screen(), current_result)

        # ── Parse single-action description with Claude ──────────────────────
        frame = capture_screen()
        plan = _parse_with_claude(description, state, detail, flow, frame)
        if plan is None:
            logger.info("  [escalate] Claude unavailable — using heuristic")
            plan = _heuristic_plan(description, state)

        logger.info(
            f"  [escalate] step {step_num}: {plan.scenario_id!r}  "
            f"actions={[a.type for a in plan.actions]}"
        )

        # corrected_state still helps perceive — apply locally before action.
        if plan.corrected_state:
            current_result = _build_corrected_result(plan.corrected_state, current_result)
            state  = current_result.state
            detail = current_result.detail
            flow   = current_result.flow
            logger.info(
                f"  [escalate] Perception corrected: state={state!r} "
                f"detail={detail!r}"
            )

        # Execute the plan but DO NOT save individual steps — accumulation
        # only.  Saving happens once at 'done'.
        # auto_drain=False: the human is explicitly authoring each step;
        # the post-tap drain would insert implicit taps between their
        # instructions, which broke the 2026-05-20 recruit-crew session
        # (the drain after "tap Recruit" cycled through Emergency/Normal
        # Recruit menu items and closed before the OK tap could fire).
        _execute_plan(plan, auto_drain=False)
        accumulated.extend(plan.actions)
        time.sleep(2.0)

        # Re-perceive for the next prompt.
        new_frame = capture_screen()
        current_result = reclassify_with_claude(new_frame, perceive(new_frame))
        state  = current_result.state
        detail = current_result.detail
        flow   = current_result.flow
        logger.info(f"  [escalate] After step {step_num}: state={state!r}")

    # Step cap reached without 'done' — discard.
    logger.warning(
        f"  [escalate] Teaching session reached step cap "
        f"({_TEACHING_MAX_STEPS}) without 'done' — discarding {len(accumulated)} "
        "accumulated action(s)"
    )
    return current_result


# ── Heuristic fallback (no Claude) ───────────────────────────────────────────

def _heuristic_plan(description: str, state: str) -> EscalationPlan:
    """
    Very simple keyword matching for when Claude is unavailable.
    Parses obvious patterns like "tap ok", "press back", "tap close".
    """
    desc = description.lower()
    actions: list[ActionStep] = []

    if any(w in desc for w in ("tap ok", "press ok", "click ok", "tap okay")):
        actions.append(ActionStep(type="find_and_tap", label="ok"))
    elif any(w in desc for w in ("tap close", "tap x", "close it", "dismiss")):
        actions.append(ActionStep(type="find_and_tap", label="close"))
    elif any(w in desc for w in ("press back", "go back", "tap back")):
        actions.append(ActionStep(type="press_back"))
    elif any(w in desc for w in ("tap confirm", "confirm")):
        actions.append(ActionStep(type="find_and_tap", label="confirm"))
    elif any(w in desc for w in ("tap home", "home button")):
        actions.append(ActionStep(type="tap", x=2300, y=45))
    else:
        # Last resort: tap centre
        actions.append(ActionStep(type="tap", x=1200, y=600))

    # Extract rough keyword hints from description
    keywords = [w for w in desc.split() if len(w) > 4 and w.isalpha()][:4]

    return EscalationPlan(
        scenario_id        = f"heuristic_{state}_{int(time.time())}",
        category           = "other",
        description        = description,
        actions            = actions,
        detection_keywords = keywords,
        notes              = f"Heuristic plan — Claude unavailable",
        save_as_knowledge  = False,  # don't save heuristic guesses
    )
