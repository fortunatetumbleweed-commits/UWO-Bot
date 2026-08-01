# brain/flow_completeness.py
#
# Flow completeness rule (project guideline, see CLAUDE.md → "Flow Completeness
# & Self-Correction").  A learned flow is COMPLETE only when it (a) terminates
# at a recognized state and (b) contains at least one positive transaction.
# Otherwise it is INCOMPLETE and learning must continue.
#
# This module provides:
#   - status constants
#   - step categorisation (navigation / transaction / dismissal / cancel)
#   - is_flow_complete() — single source of truth for the completeness rule
#
# It is deliberately small and dependency-free so it can be imported by both
# the FSM registry, the flow runner, and the audit script without circular
# imports.

from __future__ import annotations

from typing import Iterable, Optional


# ── Flow status ───────────────────────────────────────────────────────────────

STATUS_COMPLETE   = "complete"
STATUS_INCOMPLETE = "incomplete"
STATUS_CANCELLED  = "cancelled"
STATUS_UNKNOWN    = "unknown"  # legacy / not yet evaluated

VALID_STATUSES = {STATUS_COMPLETE, STATUS_INCOMPLETE, STATUS_CANCELLED, STATUS_UNKNOWN}


# ── Step categories ───────────────────────────────────────────────────────────

CATEGORY_NAVIGATION  = "navigation"
CATEGORY_TRANSACTION = "transaction"
CATEGORY_DISMISSAL   = "dismissal"
CATEGORY_CANCEL      = "cancel"

VALID_CATEGORIES = {
    CATEGORY_NAVIGATION, CATEGORY_TRANSACTION,
    CATEGORY_DISMISSAL,  CATEGORY_CANCEL,
}


# ── Categoriser ───────────────────────────────────────────────────────────────

# Labels / step ids that signify a positive transaction (commits or advances
# game state).  Conservative: missing a transaction (false negative) is safer
# than counting navigation as a transaction (false positive), because the rule
# only requires *one* transaction to mark a flow complete.
_COMMIT_LABEL_KEYWORDS = (
    "recruit", "buy", "purchase", "sell",
    "depart", "set sail", "sail",
    "hire", "invest",
)

# Step ids whose semantic role is unambiguously transactional in this codebase.
_TRANSACTION_STEP_IDS = {"basket", "confirm", "departure"}

# Step ids whose role is dismissal (clicking through a receipt, closing).
_DISMISSAL_STEP_IDS = {"result", "loading", "negotiate"}

# Cancel: explicit back/home press, or labels matching navigation away.
_CANCEL_LABEL_KEYWORDS = ("back", "home", "× home", "cancel")
_CANCEL_RECOVERY_ACTIONS = {"press_back", "tap_home", "press_home"}

# Phrases in a step's `detection` text that signal navigation (the post-tap
# screen is itself a menu/panel/screen, not a commit-result).  These override
# label-keyword matches because the label can name a workflow that opens a
# screen (e.g. "Recruit Crew" the menu item vs "Recruit" the gold button).
_NAVIGATION_DETECTION_PHRASES = (
    "sub-menu", "submenu", "sub menu",
    "menu opens", "menu appears",
    "panel opens", "panel appears",
)


def categorize_step(step: dict) -> str:
    """
    Classify a flow step as navigation / transaction / dismissal / cancel.

    Honours an explicit step["category"] field if already present.  Otherwise
    inspects the recovery_action and labels.
    """
    explicit = step.get("category")
    if explicit in VALID_CATEGORIES:
        return explicit

    action    = (step.get("recovery_action") or "").lower()
    labels    = [str(l).lower() for l in (step.get("recovery_button_labels") or [])]
    sid       = (step.get("id") or "").lower()
    detection = (step.get("detection") or "").lower()

    # 1. Cancel — explicit back/home.
    if action in _CANCEL_RECOVERY_ACTIONS:
        return CATEGORY_CANCEL
    if any(any(kw in lbl for kw in _CANCEL_LABEL_KEYWORDS) for lbl in labels):
        return CATEGORY_CANCEL

    # 2. Wait actions are dismissals — they advance time, not state.
    if action == "wait":
        return CATEGORY_DISMISSAL

    # 3. Navigation override — if the detection text says the result is a
    #    menu/panel/screen opening, this step is navigation regardless of
    #    label keywords.  Catches the "Recruit Crew menu item" vs "Recruit
    #    gold button" ambiguity (both contain the word 'recruit', but only
    #    the latter commits state).
    if any(p in detection for p in _NAVIGATION_DETECTION_PHRASES):
        return CATEGORY_NAVIGATION

    # 4. Transaction by label keyword (most reliable signal once we've ruled
    #    out menu/panel openings above).
    if any(any(kw in lbl for kw in _COMMIT_LABEL_KEYWORDS) for lbl in labels):
        return CATEGORY_TRANSACTION

    # 4. Step id semantics (codebase convention).
    if sid in _TRANSACTION_STEP_IDS:
        return CATEGORY_TRANSACTION
    if sid in _DISMISSAL_STEP_IDS:
        return CATEGORY_DISMISSAL

    # 5. OK / close / X buttons without a commit keyword → dismissal.
    if labels and all(lbl in {"ok", "confirm", "close", "×", "x", "no"} for lbl in labels):
        return CATEGORY_DISMISSAL

    # 6. Default — opening a sub-menu / panel without explicit commit.
    return CATEGORY_NAVIGATION


def categorize_steps(steps: Iterable[dict]) -> list[str]:
    """Convenience: categorise each step in order."""
    return [categorize_step(s) for s in steps]


# ── Completeness rule ─────────────────────────────────────────────────────────

def has_positive_transaction(steps: Iterable[dict]) -> bool:
    """True if any step is categorised as a transaction."""
    return any(categorize_step(s) == CATEGORY_TRANSACTION for s in steps)


def is_flow_complete(
    flow_dict_or_obj,
    terminal_state_recognized: Optional[bool] = None,
) -> bool:
    """
    A flow is complete when both:
      1. terminal_state is recognized (caller passes the lookup result), AND
      2. at least one step is categorised as a transaction.

    `flow_dict_or_obj` can be a flow dict (from flows.json) or an FSMFlow with
    a `.steps` attribute.

    `terminal_state_recognized` — caller looks up flow.terminal_state in the
    registry and passes True/False.  If None, the rule cannot be evaluated for
    that condition and we conservatively return False.
    """
    if terminal_state_recognized is None:
        return False
    if not terminal_state_recognized:
        return False
    steps = (
        flow_dict_or_obj.get("steps", []) if isinstance(flow_dict_or_obj, dict)
        else getattr(flow_dict_or_obj, "steps", []) or []
    )
    return has_positive_transaction(steps)


def evaluate_flow_status(
    flow_dict_or_obj,
    terminal_state_recognized: bool,
    cancelled: bool = False,
) -> str:
    """
    Map a flow's evaluation to one of the STATUS_* constants.

    - cancelled (Back/Home during attempt) → STATUS_CANCELLED
    - complete per is_flow_complete()      → STATUS_COMPLETE
    - else                                 → STATUS_INCOMPLETE
    """
    if cancelled:
        return STATUS_CANCELLED
    if is_flow_complete(flow_dict_or_obj, terminal_state_recognized):
        return STATUS_COMPLETE
    return STATUS_INCOMPLETE
