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


def find_announcement_close_x(frame) -> Optional[tuple]:
    """Locate the announcement popup's close-X — a black disc near the popup's
    top-right, LEFT of the sea mini-map. Returns (x, y) or None.

    CRITICAL: the popup's X is NOT at the screen corner. The old code tapped
    (0.975w, 0.05h) ≈ (2340,54), which at sea/port_overworld is the ☰ HAMBURGER →
    it OPENED Company Overview instead of closing the popup (see memory
    project_home_button_is_chromed_only_escape). Detect the real X instead.
    """
    import numpy as np
    arr = np.asarray(frame.convert("RGB"))
    h, w = arr.shape[:2]
    # Band: right-of-centre but LEFT of the top-right mini-map (which starts ~0.80w).
    x0, x1 = int(0.58 * w), int(0.80 * w)
    y0, y1 = int(0.12 * h), int(0.30 * h)
    band = arr[y0:y1, x0:x1]
    # The disc is NEAR-PURE black with a WHITE CROSS in it. Both halves of that matter.
    #
    # A plain "dark pixels" test fails whenever the popup behind is a dark picture. Live
    # 2026-08-23 the announcement sat on a NIGHT SKY: at the old threshold (<70) 28% of the
    # band read as dark, the densest window landed on empty sky at (1874,234), and the bot
    # tapped it four times while the real button sat at (1695,222). Measured there:
    #   sky            [13 23 51]   → max 51, "dark" at <70, NOT dark at <35
    #   disc           [0 0 0]      → dark at any threshold
    #   cross (centre) [248 248 248]→ bright
    # Tightening to <35 drops the band from 28% to 0.7%; requiring a bright core rejects any
    # remaining dark patch that is merely a shadow.
    dark = (band.max(axis=2) < 35).astype(np.int32)
    bright = (band.min(axis=2) > 200).astype(np.int32)
    if dark.sum() < 50:                          # no black disc present
        return None
    k = 44
    H, W = dark.shape
    if H <= k or W <= k:
        return None
    ii = np.pad(dark, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    ib = np.pad(bright, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    win_dark = (ii[k:, k:] - ii[:-k, k:] - ii[k:, :-k] + ii[:-k, :-k])
    win_bright = (ib[k:, k:] - ib[:-k, k:] - ib[k:, :-k] + ib[:-k, :-k])
    # The white cross DISAMBIGUATES; it is not a hard requirement.
    #
    # On a LIGHT background a dark disc is already unambiguous, so requiring a cross would
    # reject a perfectly good button (and did — tests/test_announcement_dismiss.py draws a
    # plain disc). On DARK artwork the darkness test alone is useless: a night-sky popup made
    # 28% of the band "dark" at the old threshold and the densest window landed on empty sky.
    #
    # So: prefer windows that contain a bright core, and fall back to the blackest window
    # when none does.
    win = np.where(win_bright >= 20, win_dark, 0)
    if win.max() <= 0:
        win = win_dark
    if win.max() < 0.25 * k * k:                 # not dense enough to be the disc
        return None
    yy, xx = np.unravel_index(int(win.argmax()), win.shape)
    return (int(x0 + xx + k // 2), int(y0 + yy + k // 2))


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
        # Dismiss by tapping the popup's OWN close-X (a black disc at its top-right).
        # NEVER the screen corner — at sea/port_overworld that spot is the ☰ hamburger
        # and opens Company Overview (the bug that stranded the gather run 2026-08-17).
        # Back is unreliable for this popup, so it's only a fallback.
        import time as _t
        from capture.adb_capture import capture_screen
        _cap = capture_fn or capture_screen

        def _try_dismiss(f):
            x = find_announcement_close_x(f)
            if x:
                tap_fn(*x)
                logger.info(f"[clear_blockers] announcement — tapped close-X @ {x}")
            else:
                back_fn()
                logger.info("[clear_blockers] announcement — no close-X found, pressed Back")

        _try_dismiss(frame)
        _t.sleep(0.8)
        frame2 = _cap()
        if looks_like_announcement(_ocr_text(frame2)):
            _try_dismiss(frame2)                 # one retry (X or Back)
            _t.sleep(0.8)
            frame2 = _cap()
        cleared = not looks_like_announcement(_ocr_text(frame2))
        logger.info(f"[clear_blockers] announcement dismiss → cleared={cleared}")
        return {"cleared": cleared, "kind": "announcement"}

    if looks_like_promo(text):
        # BACK, THEN LOOK — the same discipline as the announcement above, and the opposite
        # preference, because a promo takes Back and the announcement does not (user,
        # 2026-09-09: "only 2 popups can not be dismissed with back, one is the daily news,
        # the other is the Android system dialog").
        #
        # Back is style-independent, and the style changes between seasons. Hunting the X
        # has never once produced a right coordinate on this path: (2050,100) nineteen
        # times, (2069-2072, 295-297) ten, (2115-2118, 49) seven — every one a HUD rail
        # icon. Live 2026-09-08 the "Moon Rabbit's Part Gift Package" card at Barcelona was
        # tapped at (2071,297) twice, logged as dismissed twice, and never moved; one Back
        # cleared it, still on the port overworld with no main menu. Back is safe HERE
        # precisely because a modal is up to consume it — which is what the warning against
        # Back elsewhere is really about.
        import time as _t
        from capture.adb_capture import capture_screen
        _cap = capture_fn or capture_screen
        back_fn()
        logger.info("[clear_blockers] promo — pressed Back")
        _t.sleep(0.8)
        frame2 = _cap()
        if looks_like_promo(_ocr_text(frame2)):
            # STILL THERE. Now the X is worth trying, bounded to the card so it cannot be a
            # rail button; `handle` presses Back again if it cannot find one.
            r = handle(frame2, llm_fn=llm_fn, tap_fn=tap_fn, back_fn=back_fn)
            _t.sleep(0.8)
            frame2 = _cap()
            logger.info(f"[clear_blockers] promo — retried via {r.get('action')}")
        cleared = not looks_like_promo(_ocr_text(frame2))
        logger.info(f"[clear_blockers] promo dismiss → cleared={cleared}")
        return {"cleared": cleared, "kind": "promo"}

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


def find_close_x(elements, w: int, h: int, within=None):
    """The popup's own close-X, or None. `within` is the popup's box.

    IT MUST BE THE POPUP'S X, NOT THE ONE NEXT TO IT. Without a box this searched a fixed
    band — `0.50w < cx < 0.93w`, `cy < 0.32h` — and preferred the RIGHT-MOST icon in it.
    On the port overworld that band also holds the HUD rail, so the right-most icon is a
    rail button, not the card's X.

    Live 2026-09-08 at Barcelona, the "Moon Rabbit's Part Gift Package" promo: its X sits at
    (1764, 216) and WAS detected, but (2071, 297) is further right and won. The bot tapped
    the rail twice, logged "dismissed promo" both times, and the popup never moved — so the
    Back fallback below, which does work on this card, was never reached. Every close-X this
    path has logged is one of these: (2050,100) nineteen times, (2069-2072, 295-297) ten,
    (2115-2118, 49) seven. Not one is a promo's X.

    Same shape as the Source panel and the sub-menu title: a fixed window spanning the thing
    AND the chrome beside it, with a tie-break that picks the wrong one.

    A box narrows it to the card. Without one, prefer nothing over a guess — the caller's
    Back is style-independent and cannot land on a control.
    """
    if within is None:
        return None
    bx1, by1, bx2, by2 = within
    best = None
    for e in (elements or []):
        lab = (getattr(e, "label", "") or "").strip().lower()
        cx, cy = getattr(e, "cx", None), getattr(e, "cy", None)
        if cx is None or cy is None:
            continue
        if not (bx1 <= cx <= bx2 and by1 <= cy <= by2):
            continue                                  # not this popup's
        small = (e.x2 - e.x1) < 130 and (e.y2 - e.y1) < 130
        is_close = lab in ("x", "×", "close") or (getattr(e, "element_type", "") == "icon" and small)
        # THE CARD'S TOP-RIGHT CORNER, measured against the card rather than the screen.
        if is_close and cx > bx1 + 0.60 * (bx2 - bx1) and cy < by1 + 0.25 * (by2 - by1):
            if best is None or (cx > best[0]):
                best = (int(cx), int(cy))
    return best


def _popup_box(frame, elements):
    """Where the thing in the way is, from the classifier that owns that question."""
    try:
        from vision.screen_perception import parse_screen
        from vision.obstruction_classifier import classify_obstruction, KIND_NONE
        found = classify_obstruction(parse_screen(frame))
        if found is not None and found.kind != KIND_NONE and found.bbox:
            return tuple(found.bbox)
    except Exception as exc:                          # noqa: BLE001 — no box is not a crash
        logger.debug(f"[unexpected_dialog] could not box the popup: {exc}")
    return None


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

    # Dismiss SAFELY: prefer the close-X INSIDE the popup, else Back. Never a purchase.
    #
    # Back is what actually clears this card — verified live 2026-09-08 on the Moon Rabbit
    # promo at Barcelona: one Back and the popup was gone, still on the port overworld, no
    # main menu. It is consumed by the topmost modal, which is the whole reason the
    # codebase's warning about Back (it exits the underlying screen) does not bite here —
    # that warning is about pressing Back when nothing is up to eat it.
    x = find_close_x(elements, getattr(frame, "width", 2400), getattr(frame, "height", 1080),
                     within=_popup_box(frame, elements))
    if x is not None and tap_fn is not None:
        tap_fn(x[0], x[1])
        action = f"tap_close@({x[0]},{x[1]})"
    elif back_fn is not None:
        back_fn()
        action = "back"
    else:
        return {"handled": False, "kind": verdict.get("kind"), "action": None,
                "why": "dismissable but no tap/back primitive provided"}
    # ATTEMPTED, not dismissed. Nothing here has looked again, and this said "dismissed"
    # twice at Barcelona while the promo sat on screen. The next tick re-perceives and will
    # say whether it went; claiming it here only made the log lie.
    logger.info(f"[unexpected_dialog] attempted to clear {verdict.get('kind')} via {action}")
    return {"handled": True, "kind": verdict.get("kind", "promo"), "action": action,
            "why": verdict.get("why", "")}
