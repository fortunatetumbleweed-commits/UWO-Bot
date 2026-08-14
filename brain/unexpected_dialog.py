"""Handle UNEXPECTED dialogs — classify one (OCR text + OmniParser -> LLM) and
dismiss it SAFELY (close-X or Back), NEVER a purchase.

The game frequently throws PROMOTIONAL / store popups ("Chance Sea Ship Pack",
"Shortcut to Growth 2", …) that block scripted flows (navigate_to_building hung on
one). This recognises them and dismisses them. It NEVER taps Purchase/Buy — the
only actions it takes are the close-X or the Back button, so it cannot spend
(real-money buttons are refused anyway).

Findings that shaped this (2026-08-11):
- OmniParser barely parses these graphical promos (≈5 elements, no X/Purchase/text)
  -> classify from OCR TEXT, not OmniParser labels.
- The promos carry a distinctive phrase ("may be shown again … even if not
  purchased") -> a cheap, reliable phrase match; the LLM is the fallback for
  ambiguous unexpected dialogs.
- Back reliably closes these promos (a dialog Back != the overworld "Exit Game?").
"""
from __future__ import annotations

import json
from typing import Callable, Optional

from loguru import logger

# Distinctive store/promo phrases — normal screens never say these.
_PROMO_PHRASES = (
    "may be shown again",
    "not purchased, it may be displayed",
    "shown again in the same form",
    "displayed again after a certain time",
)

_CLASSIFY_PROMPT = (
    "An UNEXPECTED dialog is on the game screen (Uncharted Waters Origin). Here is "
    "the text and buttons on it:\n{desc}\n\nIs this a PROMOTIONAL / store / reward / "
    "info popup that the player does NOT need to act on and should just DISMISS "
    "(close)? NEVER buy anything. Reply ONLY JSON: "
    '{{"dismiss": true|false, "kind": "<promo|reward|info|needs_action>", '
    '"why": "<short>"}}.'
)


# The idle lock / screensaver ("<port> … Slide up to unlock").
_LOCK_PHRASES = ("slide up to unlock", "slide up to")

# The daily-news / anniversary / patch-notes announcement popup — identified by
# its tab row (Perk/Season/Event/Product/Competition) or the attendance/
# anniversary banner. It blocks nav (covers the world map at sea); its close X is
# at the top-right corner OUTSIDE the popup (user 2026-08-12), and Back also
# dismisses it. Detected here by TEXT so it's robust and NOT gated by perceive's
# 60-min Moondream-confirm suppression (which left a real popup un-dismissed and
# stalled the London↔Amsterdam sail).


def looks_like_promo(text: str) -> bool:
    """Cheap, reliable promo detector from OCR text (no LLM)."""
    t = (text or "").lower()
    return any(p in t for p in _PROMO_PHRASES)


def looks_like_announcement(text: str) -> bool:
    """The daily-news/announcement popup: its tab row (Perk…Competition) or the
    attendance/anniversary banner. Robust — matches distinctive tokens, not a
    fragile pixel signature."""
    t = (text or "").lower()
    tab_row = ("perk" in t and "season" in t and "competition" in t)
    updates_panel = ("updates" in t and "notices" in t)   # the residual news panel
    return (tab_row or updates_panel
            or "attendance time" in t or "anniversary stars" in t)


def looks_like_lock_screen(text: str) -> bool:
    """The idle lock/screensaver the game drops into between steps."""
    t = (text or "").lower()
    return any(p in t for p in _LOCK_PHRASES)


def clear_blockers(frame=None, *, llm_fn: Optional[Callable[[str], str]] = None,
                   tap_fn=None, back_fn=None, swipe_fn=None, wake_fn=None,
                   capture_fn=None) -> dict:
    """Clear an unexpected NON-GAME screen blocking a flow: the lock/screensaver
    (wake + swipe up) or a promo/store popup (dismiss via X/Back). Returns
    {cleared, kind}. Safe — never taps a purchase. Call this before perceiving or
    when a flow is stuck. Primitives are injectable for tests; real ones default.
    """
    from actions.adb_actions import tap as _tap, press_back as _back, swipe as _swipe, wake as _wake
    tap_fn = tap_fn or _tap
    back_fn = back_fn or _back
    swipe_fn = swipe_fn or _swipe
    wake_fn = wake_fn or _wake
    if frame is None:
        from capture.adb_capture import capture_screen
        frame = (capture_fn or capture_screen)()
    text = _ocr_text(frame)

    if looks_like_lock_screen(text):
        wake_fn()
        swipe_fn(1200, 900, 1200, 200, 300)        # slide up to unlock
        logger.info("[clear_blockers] dismissed lock/screensaver (wake + swipe up)")
        return {"cleared": True, "kind": "lock"}

    if looks_like_announcement(text):
        # Back closes the daily-news popup; some event/competition popups need the
        # close X at the TOP-RIGHT CORNER (outside the popup content). Try Back,
        # then fall back to the corner X if it persists.
        back_fn()
        try:
            import time as _t
            _t.sleep(0.8)
            from capture.adb_capture import capture_screen
            frame2 = (capture_fn or capture_screen)()
            if looks_like_announcement(_ocr_text(frame2)):
                w, h = frame2.size
                tap_fn(int(w * 0.975), int(h * 0.05))    # top-right-corner X
                logger.info("[clear_blockers] announcement persisted after Back — tapped corner X")
                return {"cleared": True, "kind": "announcement"}
        except Exception as exc:
            logger.debug(f"[clear_blockers] announcement corner-X fallback failed: {exc}")
        logger.info("[clear_blockers] dismissed daily-news/announcement popup (Back)")
        return {"cleared": True, "kind": "announcement"}

    if looks_like_promo(text):
        r = handle(frame, llm_fn=llm_fn, tap_fn=tap_fn, back_fn=back_fn)
        return {"cleared": bool(r.get("handled")), "kind": "promo", "why": r.get("why")}

    return {"cleared": False, "kind": None}


def _ocr_text(frame) -> str:
    from actions.sail_actions import _ocr_frame
    return " | ".join(t[0] for t in _ocr_frame(frame, min_conf=0.3))


def classify(desc: str, llm_fn: Callable[[str], str]) -> dict:
    """Ask the LLM what the dialog is + whether to dismiss it. {} on failure."""
    try:
        raw = llm_fn(_CLASSIFY_PROMPT.format(desc=desc)) or ""
        s, e = raw.index("{"), raw.rindex("}") + 1
        obj = json.loads(raw[s:e])
        return {"dismiss": bool(obj.get("dismiss")),
                "kind": obj.get("kind", "?"), "why": obj.get("why", "")}
    except Exception as exc:
        logger.debug(f"[unexpected_dialog] classify failed: {exc}")
        return {}


def find_close_x(elements, w: int, h: int):
    """A small X/close icon in the upper-right of the CONTENT (dialog corner) —
    not the far screen-corner HUD icons. Returns (cx, cy) or None."""
    best = None
    for e in (elements or []):
        lab = (getattr(e, "label", "") or "").strip().lower()
        cx, cy = getattr(e, "cx", None), getattr(e, "cy", None)
        if cx is None or cy is None:
            continue
        small = (e.x2 - e.x1) < 90 and (e.y2 - e.y1) < 90
        is_close = lab in ("x", "×", "close") or (getattr(e, "element_type", "") == "icon" and small)
        if is_close and 0.50 * w < cx < 0.93 * w and cy < 0.32 * h:
            if best is None or (cx > best[0]):     # prefer the right-most
                best = (int(cx), int(cy))
    return best


def handle(frame, elements=None, llm_fn: Optional[Callable[[str], str]] = None,
           tap_fn: Optional[Callable] = None,
           back_fn: Optional[Callable] = None) -> dict:
    """Classify the current screen's dialog and dismiss it if it's a promo / info /
    reward popup. Returns {handled, kind, action, why}. NEVER taps a purchase/commit
    — only the close-X or Back.
    """
    if elements is None:
        try:
            from vision.omniparser import parse_fast_cached
            elements = parse_fast_cached(frame) or []
        except Exception:
            elements = []
    text = _ocr_text(frame)

    if looks_like_promo(text):
        verdict = {"dismiss": True, "kind": "promo", "why": "store/promo phrase in text"}
    elif llm_fn is not None:
        labels = [(getattr(e, "label", "") or "").strip() for e in elements
                  if (getattr(e, "label", "") or "").strip()]
        verdict = classify(f"TEXT: {text[:500]}\nBUTTONS/LABELS: {labels}", llm_fn)
    else:
        verdict = {}

    if not verdict.get("dismiss"):
        return {"handled": False, "kind": verdict.get("kind", "unknown"),
                "action": None, "why": verdict.get("why", "not a dismissable dialog")}

    # Dismiss SAFELY: prefer the close-X, else Back. NEVER a content/purchase button.
    x = find_close_x(elements, getattr(frame, "width", 2400), getattr(frame, "height", 1080))
    if x is not None and tap_fn is not None:
        tap_fn(x[0], x[1])
        action = f"tap_close@({x[0]},{x[1]})"
    elif back_fn is not None:
        back_fn()
        action = "back"
    else:
        return {"handled": False, "kind": verdict.get("kind"), "action": None,
                "why": "dismissable but no tap/back primitive provided"}
    logger.info(f"[unexpected_dialog] dismissed {verdict.get('kind')} via {action}")
    return {"handled": True, "kind": verdict.get("kind", "promo"), "action": action,
            "why": verdict.get("why", "")}
