# brain/commit_actions.py
#
# Phase 5e L3 — generic "commit via positive taps" primitive.
#
# Many in-game transactions (recruit crew, buy/sell, deposit, withdraw,
# collect rewards) follow the same pattern:
#
#   1. The bot is on a sub-screen with a primary action button.
#   2. Tap the positive button → a confirmation dialog or follow-up
#      screen appears.
#   3. Tap the positive button on the new screen.
#   4. Eventually the flow cycles back to a previously-seen screen
#      (or terminates with no positive button to tap).
#
# Rather than hand-author the exact click sequence for every transaction
# (which couples the plan to fixed coordinates and breaks when the UI
# shifts), the bot follows a generic guideline: tap whatever the
# positive button is, on whatever screen is in front of it, until the
# screen cycles or a stop condition fires.
#
# OmniParser provides the "what's on the screen" structure; this module
# provides the "tap the positive thing" loop.
#
# Per the user's framing (2026-05-02): "click the positive button on the
# screen, until no new dialog shows up, and no new screen shows up.
# After that it's verify-goal time."

from __future__ import annotations

import time
from typing import Iterable, Optional

from loguru import logger

from vision.omniparser import DetectedElement


# ── Positive / negative label sets ───────────────────────────────────────────
#
# Lowercased.  These are the canonical English action verbs the game uses.
# Whole-word matching (via brain.commit_actions._matches) prevents short
# verbs from accidentally matching unrelated words.

POSITIVE_LABELS: frozenset[str] = frozenset({
    # Generic acknowledge / proceed
    "ok", "okay", "yes", "confirm", "continue", "next", "accept",
    "done", "apply", "got it", "received",
    # Transaction commits
    "recruit", "hire", "buy", "purchase", "sell", "trade",
    "deposit", "withdraw", "transfer",
    # Voyage commits
    "depart", "set sail", "sail",
    # Reward / collection
    "collect", "claim", "redeem",
})

NEGATIVE_LABELS: frozenset[str] = frozenset({
    # Cancel / refuse
    "cancel", "no", "decline", "reject",
    # Exit / dismiss without commit
    "back", "close", "exit", "quit", "skip", "leave",
    # Game's close glyph
    "×", "x",
})


def _matches_any(target: str, labels: Iterable[str]) -> bool:
    """Whole-word match of *target* against any label in *labels*.

    'OK button' matches 'ok'; 'Stock max' does not.  Mirrors the same
    word-boundary semantics as actions.sail_actions._label_matches.
    """
    import re
    cand = target.lower()
    for word in labels:
        pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(word.lower())}(?![A-Za-z0-9])")
        if pat.search(cand):
            return True
    return False


# Right-panel action region used as a FALLBACK when YOLO misses the
# button shape and the only positive-verb element is element_type='text'.
# Action buttons in this game live in the right panel below the header
# (cx_norm > 0.50, cy_norm > 0.30).  This region is intentionally
# generous: it's only consulted when no element_type='button' candidate
# is available, so a wrongly-included title-bar text would still be
# caught by the cy_norm > 0.30 threshold.
_ACTION_REGION_CX_NORM_MIN: float = 0.50
_ACTION_REGION_CY_NORM_MIN: float = 0.30


# Universal dialog-progression keywords always allowed even when
# context-filtering is active.  These exist so that intermediate confirm
# dialogs ("OK", "Confirm") can be tapped while pursuing any goal —
# closing a confirmation is part of completing the chain regardless of
# which transaction is being committed.
UNIVERSAL_COMMIT_LABELS: frozenset[str] = frozenset({
    "ok", "okay", "yes", "confirm", "continue", "next", "accept",
    "done", "apply", "got it", "received", "collect", "claim", "redeem",
})


def find_positive_button_for_context(
    elements: Iterable[DetectedElement],
    *,
    frame_w: int,
    frame_h: int,
    goal_keywords: Optional[Iterable[str]] = None,
) -> Optional[DetectedElement]:
    """Context-aware variant of find_positive_button.

    When *goal_keywords* is supplied, only buttons whose labels match
    one of (goal_keywords ∪ UNIVERSAL_COMMIT_LABELS) are considered.
    Buttons that match POSITIVE_LABELS but neither the goal keywords
    nor universal labels are SKIPPED — preventing the harbour
    Trade-Goods false positive (find_positive_button picked the gold
    'Trade Goods' tab because POSITIVE_LABELS includes 'trade', even
    though the bot was trying to recruit crew).

    When *goal_keywords* is None or empty, this falls back to the
    existing find_positive_button behaviour for backward compatibility.
    """
    if not goal_keywords:
        return find_positive_button(elements, frame_w=frame_w, frame_h=frame_h)

    allowed = frozenset(k.lower() for k in goal_keywords) | UNIVERSAL_COMMIT_LABELS

    button_candidates: list[DetectedElement] = []
    text_fallback:     list[DetectedElement] = []

    for el in elements:
        label = (el.label or "").strip()
        if not label:
            continue
        if _matches_any(label, NEGATIVE_LABELS):
            continue
        if not _matches_any(label, allowed):
            continue

        if el.element_type == "button":
            button_candidates.append(el)
            continue
        if el.element_type in ("text", "icon"):
            cx_norm = el.cx / frame_w
            cy_norm = el.cy / frame_h
            if (cx_norm >= _ACTION_REGION_CX_NORM_MIN
                    and cy_norm >= _ACTION_REGION_CY_NORM_MIN):
                text_fallback.append(el)

    candidates = button_candidates or text_fallback
    if not candidates:
        return None
    candidates.sort(key=lambda el: (-el.cx, -el.cy))
    return candidates[0]


def find_positive_button(
    elements: Iterable[DetectedElement],
    *,
    frame_w: int,
    frame_h: int,
) -> Optional[DetectedElement]:
    """Return the positive-action button on the screen, or None if no
    candidate matches.

    Discriminator: OmniParser's element_type.

    OmniParser (parse_fast) emits three element types:
      - 'button': YOLO icon detection that overlapped an OCR text
                  region — this is a structurally-recognised tappable
                  control with text on it (e.g. the gold 'Recruit'
                  button on the recruit screen).
      - 'icon':   YOLO icon detection without an overlapping text label.
      - 'text':   OCR text that did NOT land inside any YOLO-detected
                  icon — usually titles, headers, body text, or list
                  entries.

    Title bars, screen headers, and highlighted menu list items all
    come back as 'text' because they don't have a discrete button
    shape that YOLO recognises (the menu-item highlighting in this
    game is a left-edge glow streak, not a button rectangle).  Filtering
    to element_type='button' naturally rejects them without needing
    position-based heuristics.

    Selection:
      1. Among element_type='button' candidates whose label whole-word-
         matches POSITIVE_LABELS and does not match NEGATIVE_LABELS,
         pick the rightmost-bottommost (UWO action-button convention).
      2. Fallback: if no button-type candidates exist (YOLO occasionally
         misses a gold button shape), search text/icon candidates whose
         centre is in the right-panel action region
         (cx_norm > 0.50, cy_norm > 0.30) — this avoids title bars and
         menu items even on the fallback path.
    """
    button_candidates: list[DetectedElement] = []
    text_fallback:     list[DetectedElement] = []

    for el in elements:
        label = (el.label or "").strip()
        if not label:
            continue
        if _matches_any(label, NEGATIVE_LABELS):
            continue
        if not _matches_any(label, POSITIVE_LABELS):
            continue

        if el.element_type == "button":
            button_candidates.append(el)
            continue
        if el.element_type in ("text", "icon"):
            cx_norm = el.cx / frame_w
            cy_norm = el.cy / frame_h
            if (cx_norm >= _ACTION_REGION_CX_NORM_MIN
                    and cy_norm >= _ACTION_REGION_CY_NORM_MIN):
                text_fallback.append(el)

    candidates = button_candidates or text_fallback
    if not candidates:
        return None

    # Right-bottom preference (UWO action buttons sit in the right panel).
    candidates.sort(key=lambda el: (-el.cx, -el.cy))
    return candidates[0]


# Position tolerance for the button-cycle equality check.  Two buttons
# are considered the same if their normalised centres are within this
# distance and their (lower-cased, whitespace-collapsed) labels match.
# 0.04 of normalised screen ≈ 100 px on a 2400-wide frame — wider than
# any incidental jitter, narrower than meaningful position changes.
_BUTTON_POSITION_TOLERANCE: float = 0.04


def _button_key(btn: DetectedElement, frame_w: int, frame_h: int) -> tuple:
    """Hashable identity for a tapped button: (label_lower_compact, cx_norm, cy_norm)."""
    label = " ".join((btn.label or "").lower().strip().split())
    return (label, btn.cx / frame_w, btn.cy / frame_h)


def _same_button(a: tuple, b: tuple) -> bool:
    """True if two button-keys identify the same on-screen button.

    Labels must match exactly (lower-cased, whitespace-collapsed); the
    normalised centres must be within _BUTTON_POSITION_TOLERANCE on both
    axes.  This catches 'Recruit @ (2150, 800)' on round 1 vs round 2
    (same button, possibly with a few pixels of jitter) while keeping
    distinct buttons (e.g. 'OK' on a confirm dialog vs 'OK' on a
    notice popup) separate when they're at clearly-different positions.
    """
    if a[0] != b[0]:
        return False
    return (
        abs(a[1] - b[1]) <= _BUTTON_POSITION_TOLERANCE
        and abs(a[2] - b[2]) <= _BUTTON_POSITION_TOLERANCE
    )


def commit_via_positive_taps(
    *,
    max_taps: int = 6,
    settle_secs: float = 3.0,
    capture_fn=None,
    tap_fn=None,
    goal_keywords: Optional[Iterable[str]] = None,
) -> list[tuple]:
    """Tap the positive button on the screen repeatedly until the
    transaction cycle closes, then return the recorded tap sequence.

    Stop conditions, in priority order:

      1. The next positive button matches a button we've already tapped
         in this session (same label, same approximate normalised
         position).  After a recruit round closes (Recruit → OK → back
         at Recruit screen), the next iteration sees 'Recruit' as the
         positive button — same label and position as the first tap —
         and stops.  Robust to small screen jitter (ship counts
         updating, hover tooltips) that exact screen-signature equality
         would mishandle.

      2. No positive button is found on the current screen
         (transaction settled cleanly, or we're not on a transaction
         screen).

      3. *max_taps* iterations reached (safety cap — should rarely fire).

    Importantly:
      - Negative-label buttons (Cancel, No, Close, Back) are NEVER
        tapped.  Dialogs in this flow are dismissed via their POSITIVE
        button (OK / Confirm / Continue) only.
      - Non-button screen regions are NEVER tapped — there is no
        'tap outside dialog' fallback.

    Context-aware mode (post-2026-05-04 livefix): when *goal_keywords*
    is supplied, only buttons whose labels match the goal keywords or
    UNIVERSAL_COMMIT_LABELS (ok / confirm / continue / etc.) are
    tapped.  This prevents tapping irrelevant gold elements like the
    'Trade Goods' tab when the goal is 'recruit crew'.  Without
    goal_keywords, the legacy behaviour is preserved (any positive
    button matches).

    Returns the list of (label, cx_norm, cy_norm) tuples that were
    tapped, so the caller can save the sequence as a learned edge.

    *capture_fn* / *tap_fn* are injected for testability.
    """
    if capture_fn is None:
        from capture.adb_capture import capture_screen
        capture_fn = capture_screen
    if tap_fn is None:
        from actions.adb_actions import tap as adb_tap
        tap_fn = adb_tap

    from vision.omniparser import parse_fast_cached

    tapped: list[tuple] = []  # sequence of button keys actually tapped

    for iteration in range(max_taps):
        frame = capture_fn()
        elements = parse_fast_cached(frame)

        if goal_keywords:
            btn = find_positive_button_for_context(
                elements, frame_w=frame.width, frame_h=frame.height,
                goal_keywords=goal_keywords,
            )
        else:
            btn = find_positive_button(
                elements, frame_w=frame.width, frame_h=frame.height,
            )
        if btn is None:
            logger.info(
                f"[commit] iter {iteration}: no positive button found — "
                f"settled after {len(tapped)} tap(s)"
            )
            return tapped

        key = _button_key(btn, frame.width, frame.height)

        # Cycle check: would this tap repeat a button we've already
        # pressed?  After Recruit -> OK -> Recruit cycle, the
        # second 'Recruit' matches the first.
        if any(_same_button(prev, key) for prev in tapped):
            logger.info(
                f"[commit] iter {iteration}: positive button "
                f"{btn.label!r} already tapped — cycle closed after "
                f"{len(tapped)} tap(s)"
            )
            return tapped

        logger.info(
            f"[commit] iter {iteration}: tap {btn.label!r} @ "
            f"({btn.cx},{btn.cy}) (positive)"
        )
        tap_fn(btn.cx, btn.cy)
        tapped.append(key)
        time.sleep(settle_secs)

    logger.warning(
        f"[commit] reached max_taps={max_taps} without closing cycle — "
        f"transaction may not have settled"
    )
    return tapped
