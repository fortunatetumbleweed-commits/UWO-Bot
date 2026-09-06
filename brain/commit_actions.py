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
from typing import Iterable, Optional, Sequence

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


# In this game a POSITIVE button is identified by its YELLOW BACKGROUND; the wording on it
# is secondary (user 2026-08-22). Measured on the dialog's gold OK button and the controls
# the text search wrongly picked:
#
#     Ok                     yellow 0.64   <- the real commit
#     Cancel                 yellow 0.00
#     Trade Info             yellow 0.00   <- tapped 2026-08-22 via the "trade" substring
#     Requested Trade Goods  yellow 0.00   <- tapped 2026-08-22, same cause
#     Trade Points           yellow 0.00
#
# so the two populations are not close and a modest threshold separates them.
_POSITIVE_YELLOW_HUE = (35.0, 60.0)     # degrees; the game's gold
# 0.45 was calibrated on the DIALOG OK button (64-91% gold) and clipped paler commit buttons.
# The village barter panel's Exchange is a pale gold gradient — median saturation 0.39 — so it
# scored 0.067 and was rejected as "not a commit button" while being plainly live on screen
# (2026-08-23). It passed one frame and failed the next, because it sat right on the boundary.
#
# Measured on that panel, the populations are nowhere near each other at 0.30:
#
#     Exchange                 0.654      <- the real commit button
#     Negotiate                0.000
#     Check Village Influence  0.000
#     (Trade Info / Trade Points / Requested Trade Goods, 2026-08-22: 0.000)
#
# So 0.30 keeps every false positive this gate exists to stop, and stops clipping real
# buttons. The hue band is doing the discriminating; the saturation floor only has to exclude
# near-grey.
_POSITIVE_YELLOW_MIN_SAT = 0.30
_POSITIVE_YELLOW_MIN_VAL = 0.50
_POSITIVE_YELLOW_MIN_FRAC = 0.25


def has_positive_background(frame, el, min_frac: float = _POSITIVE_YELLOW_MIN_FRAC) -> bool:
    """True when `el` is drawn on the game's positive (gold) background.

    Text alone is not enough to call a control "positive": POSITIVE_LABELS contains the
    word "trade", which matches Trade Info, Trade Goods, Trade Points and Requested Trade
    Goods — none of which commit anything. The colour is what actually distinguishes a
    commit button, so it is required.

    Returns False when the element carries no bbox or the frame cannot be read — this gates
    an action, so "cannot tell" must not mean "go ahead".
    """
    try:
        import colorsys
        import numpy as _np
        x1, y1 = int(getattr(el, "x1")), int(getattr(el, "y1"))
        x2, y2 = int(getattr(el, "x2")), int(getattr(el, "y2"))
        a = _np.asarray(frame.convert("RGB")).astype(float)[y1:y2, x1:x2]
        if a.size == 0:
            return False
        px = a.reshape(-1, 3) / 255.0
        hsv = _np.array([colorsys.rgb_to_hsv(*p) for p in px])
        h, sat, val = hsv[:, 0] * 360.0, hsv[:, 1], hsv[:, 2]
        lo, hi = _POSITIVE_YELLOW_HUE
        frac = float(((h >= lo) & (h <= hi)
                      & (sat >= _POSITIVE_YELLOW_MIN_SAT)
                      & (val >= _POSITIVE_YELLOW_MIN_VAL)).mean())
        return frac >= min_frac
    except Exception as exc:
        logger.debug(f"[commit] positive-background check skipped: {exc}")
        return False


def _yellow_commit_button(elements, frame, goal_keywords: Optional[Iterable[str]]):
    """Prefer the game's signature yellow COMMIT button (a `<cost> <VERB>` pill)
    over a plain text-verb match.

    OmniParser labels this button with the COST ("205,848") and spins the verb
    off as a separate text element, so a text-verb search matches the wrong
    control — e.g. the "Normal Recruit" TYPE SELECTOR instead of the yellow
    "Recruit" commit (2026-08-13 recruit-loop bug). `detect_commit_buttons`
    recognises the button by colour+layout and recovers {verb, cost, currency}.

    Returns a lightweight btn (label/cx/cy) or None. NEVER returns a red-gem
    (real-money) commit — those are dropped here so this primitive can't
    auto-spend real money; the currency gate in action_executor remains the
    backstop.
    """
    try:
        from vision.region_detectors.commit_button import detect_commit_buttons
        commits = detect_commit_buttons(elements, frame)
    except Exception as exc:
        logger.debug(f"[commit] yellow-commit detect failed: {exc}")
        return None
    commits = [c for c in (commits or []) if getattr(c, "currency", "") != "red_gem"]
    if not commits:
        return None
    if goal_keywords:
        kws = [k.lower() for k in goal_keywords]
        matched = [c for c in commits if c.verb and any(k in c.verb.lower() for k in kws)]
        if matched:
            commits = matched
    c = commits[0]
    import types
    return types.SimpleNamespace(
        label=(c.verb or (f"commit ({c.cost})" if c.cost else "commit")),
        cx=int(c.cx), cy=int(c.cy),
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

        # Prefer the yellow COMMIT button (colour+layout) — it's the real
        # positive action on purchase/recruit screens, where a text-verb match
        # would pick a type-selector or tab instead.  Fall back to the
        # text-verb search when no yellow commit is present (dialogs, etc.).
        btn = _yellow_commit_button(elements, frame, goal_keywords)
        if btn is None:
            if goal_keywords:
                btn = find_positive_button_for_context(
                    elements, frame_w=frame.width, frame_h=frame.height,
                    goal_keywords=goal_keywords,
                )
            else:
                btn = find_positive_button(
                    elements, frame_w=frame.width, frame_h=frame.height,
                )
            # The text search matches on words, and "trade" is a word that appears on
            # several controls that commit nothing. Require the game's yellow background
            # before treating a text match as a positive button.
            if btn is not None and not has_positive_background(frame, btn):
                # A DIMMING OVERLAY IS NOT A DISABLED BUTTON. The colour test needs a minimum
                # brightness, and a popup darkens everything behind it — so a perfectly live
                # gold button reads as "not positive" while a dialog sits on top.
                #
                # Live 2026-08-23: after the first barter round an overlay remained, Exchange
                # was still gold and still tappable (user), but this gate rejected it twice
                # and the mission gave up with a second round funded and unspent.
                #
                # Clear what is in the way, then judge — the same order used everywhere else
                # (brain/unexpected.py, sail_to._handle_unknown, _at_a_village).
                cleared = False
                try:
                    from brain.unexpected_dialog import clear_blockers
                    cleared = bool(clear_blockers(frame).get("cleared"))
                except Exception as exc:
                    logger.debug(f"[commit] blocker check failed: {exc}")
                if cleared:
                    # The loop re-captures and re-parses at the top of every iteration, so
                    # `continue` IS the re-perceive.
                    logger.info(f"[commit] iter {iteration}: a blocker was dimming the "
                                "screen — cleared it, re-perceiving before judging")
                    continue
                logger.info(
                    f"[commit] iter {iteration}: {getattr(btn, 'label', '?')!r} matched by "
                    "text but has no positive (yellow) background — not a commit button"
                )
                btn = None
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

    if max_taps > 1:
        logger.warning(
            f"[commit] reached max_taps={max_taps} without closing cycle — "
            f"transaction may not have settled"
        )
    return tapped


def tap_one_positive(*, goal_keywords: Sequence[str] = (), capture_fn=None,
                     tap_fn=None) -> bool:
    """Press the positive control ONCE and hand back. True when a tap went in.

    THE ONE-SHOT FORM, for a caller inside the dispatcher's cycle. `commit_via_positive_taps`
    keeps pressing until the cycle closes, which is right for an escalation path that owns
    the screen until it settles — and wrong for an activity handler, which must do one thing
    and return so the dispatcher can look again.

    Live 2026-09-05 at San Village (FC-3, `docs/market_as_contexts.md`): the village's
    `_on_confirm` called the looping form, iteration 1 tapped `OK`, the game raised
    "Insufficient Empty Space", and iteration 2 tapped its `Receive` — then `OK` on
    "Unclaimed trade goods will be discarded". Three screens, one handler call, no tick
    between them, so the village's own `overflow_prompt` handler never ran and 360 units were
    discarded. Nothing in that loop was careless; holding control across a capture is what
    swallows.

    The bound moves to the caller, where the goal's lifetime already is — the dispatcher
    re-perceives after every action anyway, so "tap again" is the next tick's decision, made
    on a screen that has been looked at.
    """
    tapped = commit_via_positive_taps(max_taps=1, goal_keywords=goal_keywords,
                                      capture_fn=capture_fn, tap_fn=tap_fn)
    return bool(tapped)
