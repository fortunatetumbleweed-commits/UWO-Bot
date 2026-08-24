# actions/barter_executor.py
# P4 barter executors — verified, deterministic, driven by the SAFE tap primitive.
#
#   #24 barter_commit_verified — tap the yellow Exchange commit, confirm the result
#       dialog, and VERIFY the barter actually happened (amity changed OR cargo up),
#       like brain.verified_recruit. Does ONE commit and REPORTS — the caller (the
#       BarterMission #31) decides on no-progress, per "escalate, don't absorb".
#   #25 decide_negotiation / execute_negotiation — the "Attempt Negotiation" haggle
#       popup (No / Use 1 chance / Use all). Haggling, NOT a lottery; a FAILED try
#       REDUCES amity, so the default is to skip (carry surplus materials instead).
#
# All taps go through actions.adb_actions (input swipe) — never raw `input tap`.

from __future__ import annotations

import time
from typing import Callable, Optional, Tuple

from loguru import logger


# ── #24 Barter commit (verified) ───────────────────────────────────────────────

def _barter_progressed(before: dict, after: dict) -> Tuple[bool, str]:
    """A barter happened if amity moved (barter always shifts amity ±) or cargo rose
    (output received)."""
    a0, a1 = before.get("amity"), after.get("amity")
    if a0 is not None and a1 is not None and a1 != a0:
        return True, f"amity {a0}→{a1}"
    c0, c1 = before.get("cargo"), after.get("cargo")
    if c0 is not None and c1 is not None and c1 > c0:
        return True, f"cargo {c0}→{c1}"
    return False, "no amity/cargo change"


def barter_commit_verified(
    *,
    settle_secs: float = 2.5,
    capture_fn: Optional[Callable] = None,
    commit_fn: Optional[Callable] = None,
    confirm_fn: Optional[Callable] = None,
    read_panel_fn: Optional[Callable] = None,
    read_cargo_fn: Optional[Callable] = None,
) -> dict:
    """One verified Exchange commit on the (already-open) barter panel with a good
    selected. Returns {ok, reason, before, after, tapped}.

    ok=False ⇒ the commit did NOT change amity/cargo; the caller escalates rather
    than blindly re-tapping."""
    if capture_fn is None:
        from capture.adb_capture import capture_screen
        capture_fn = capture_screen
    if read_panel_fn is None:
        from actions.barter_reader import read_barter_panel
        read_panel_fn = read_barter_panel
    if read_cargo_fn is None:
        from vision.hud_readers import read_cargo
        from vision.omniparser import parse_fast_cached
        read_cargo_fn = lambda frame: (lambda c: c[0] if c else None)(
            read_cargo(parse_fast_cached(frame)))
    if commit_fn is None:
        from brain.commit_actions import commit_via_positive_taps
        commit_fn = lambda: commit_via_positive_taps(goal_keywords=["exchange"])
    if confirm_fn is None:
        confirm_fn = _confirm_result_dialog

    def _state():
        frame = capture_fn()
        panel = read_panel_fn(frame)
        return {"amity": getattr(panel, "amity_points", None),
                "cargo": read_cargo_fn(frame)}

    before = _state()
    tapped = commit_fn() or []               # tap yellow Exchange (never red-gem)
    time.sleep(settle_secs)
    confirm_fn(capture_fn)                    # OK the "Barter Calculations" result dialog
    time.sleep(1.0)
    after = _state()

    progressed, why = _barter_progressed(before, after)
    if progressed:
        from memory.observed_facts import forget
        forget("hold")     # a round consumed materials — the remembered hold is now wrong
        logger.info(f"[barter_commit] OK: {why}")
        return {"ok": True, "reason": why, "before": before, "after": after, "tapped": tapped}
    logger.warning(f"[barter_commit] NO PROGRESS: tapped {tapped}, {why} — escalate, don't re-tap")
    return {"ok": False, "reason": "barter commit made no change",
            "before": before, "after": after, "tapped": tapped}


def _confirm_result_dialog(capture_fn) -> bool:
    """Tap OK on the 'Barter Calculations' result dialog if present.  (Backstop —
    commit_via_positive_taps usually closes it already.)  NOTE: find_positive_button
    takes ELEMENTS + frame dims (the frame+keywords form was a long-dead signature that
    only surfaced live, 2026-08-20 — tests had this mocked)."""
    from brain.commit_actions import find_positive_button
    from vision.omniparser import parse_fast_cached
    from actions.adb_actions import tap
    frame = capture_fn()
    btn = find_positive_button(parse_fast_cached(frame),
                               frame_w=frame.width, frame_h=frame.height)
    # Only tap a DIALOG affirmative ('ok'/'confirm').  Without this gate the backstop could
    # return the panel's own Exchange button when no dialog is up → an unintended SECOND
    # barter commit (blind action on an assumed state).
    if btn is not None and any(k in (getattr(btn, "label", "") or "").lower()
                               for k in ("ok", "confirm")):
        tap(btn.cx, btn.cy)
        return True
    return False


# ── #25 Negotiation decision + executor ────────────────────────────────────────

# The haggle popup choices, by button text.
_NEGOTIATE_BUTTONS = {"no": ("no",), "once": ("use 1", "1 chance"),
                      "all": ("use all", "all remaining")}


def decide_negotiation(*, carrying_surplus_materials: bool, spare_cargo_space: int,
                       protect_amity: bool = True) -> str:
    """Decide how to handle the 'Attempt Negotiation' popup: 'no' | 'once' | 'all'.

    A failed negotiation REDUCES amity, so the default is conservative: skip it when
    protecting amity or when we carry surplus materials (just barter another round
    instead). Only haggle when we can't barter more (no surplus) and there's spare
    hold for the extra output."""
    if protect_amity or carrying_surplus_materials:
        return "no"
    if spare_cargo_space > 0:
        return "once"
    return "no"


def execute_negotiation(choice: str, *, capture_fn: Optional[Callable] = None,
                        tap_fn: Optional[Callable] = None,
                        find_button_fn: Optional[Callable] = None) -> dict:
    """Tap the negotiation-popup button for `choice` ('no'|'once'|'all')."""
    if capture_fn is None:
        from capture.adb_capture import capture_screen
        capture_fn = capture_screen
    if tap_fn is None:
        from actions.adb_actions import tap as tap_fn
    if find_button_fn is None:
        from actions.route_execution import find_text_button
        from actions.sail_actions import _ocr_frame
        find_button_fn = lambda kw: _find_negotiate_button(_ocr_frame(capture_fn()), kw)

    for kw in _NEGOTIATE_BUTTONS.get(choice, ("no",)):
        pos = find_button_fn(kw)
        if pos:
            tap_fn(*pos)
            return {"ok": True, "choice": choice, "tapped": pos}
    return {"ok": False, "choice": choice, "reason": "negotiation button not found"}


def _find_negotiate_button(tokens, keyword):
    from actions.route_execution import find_text_button
    return find_text_button(tokens, keyword, min_ratio=0.6)


# ── #26 Gifting executor (amity climb / recover) ───────────────────────────────
# Gift options: Friendship Token + blue-gem are REPEATABLE; the ducat gift is
# one-time (grayed once used). Prefer the Friendship Token (free-ish). NEVER a
# red-gem cost (not a gift option, but the currency gate stays on principle).
# From the walkthrough: after a successful gift the red note clears, the Gifting
# button disables, and amity rises (e.g. to Friendly).

_GIFT_PREFERENCE = ("friendship", "token", "gift token")


def gift_verified(
    *,
    settle_secs: float = 2.5,
    capture_fn: Optional[Callable] = None,
    read_amity_fn: Optional[Callable] = None,
    select_gift_fn: Optional[Callable] = None,
    gift_commit_fn: Optional[Callable] = None,
    confirm_fn: Optional[Callable] = None,
) -> dict:
    """One verified gift on the (already-open) Gifting panel. Selects the repeatable
    Friendship Token, commits Gift, confirms Yes, and VERIFIES amity rose.

    Returns {ok, reason, amity_before, amity_after}. ok=False ⇒ amity did not rise;
    caller escalates."""
    if capture_fn is None:
        from capture.adb_capture import capture_screen
        capture_fn = capture_screen
    if read_amity_fn is None:
        from actions.barter_reader import read_barter_panel
        read_amity_fn = lambda frame: getattr(read_barter_panel(frame), "amity_points", None)
    if select_gift_fn is None:
        select_gift_fn = _select_friendship_token
    if gift_commit_fn is None:
        gift_commit_fn = lambda: _tap_text_button(capture_fn, "gift")
    if confirm_fn is None:
        confirm_fn = lambda: _tap_text_button(capture_fn, "yes")

    before = read_amity_fn(capture_fn())
    select_gift_fn(capture_fn)
    time.sleep(0.8)
    gift_commit_fn()
    time.sleep(0.8)
    confirm_fn()
    time.sleep(settle_secs)
    after = read_amity_fn(capture_fn())

    if before is not None and after is not None and after > before:
        logger.info(f"[gift] OK: amity {before}→{after}")
        return {"ok": True, "reason": f"amity {before}→{after}",
                "amity_before": before, "amity_after": after}
    logger.warning(f"[gift] NO PROGRESS: amity {before}→{after} did not rise — escalate")
    return {"ok": False, "reason": "gift did not raise amity",
            "amity_before": before, "amity_after": after}


def _select_friendship_token(capture_fn) -> bool:
    from actions.sail_actions import _ocr_frame
    from actions.route_execution import find_text_button
    from actions.adb_actions import tap
    tokens = _ocr_frame(capture_fn())
    for kw in _GIFT_PREFERENCE:
        pos = find_text_button(tokens, kw, min_ratio=0.6)
        if pos:
            tap(*pos)
            return True
    return False


def _tap_text_button(capture_fn, keyword: str) -> bool:
    from actions.sail_actions import _ocr_frame
    from actions.route_execution import find_text_button
    from actions.adb_actions import tap
    pos = find_text_button(_ocr_frame(capture_fn()), keyword, min_ratio=0.7)
    if pos:
        tap(*pos)
        return True
    return False
