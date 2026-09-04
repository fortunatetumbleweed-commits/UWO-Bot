"""The ONE way the bot touches the screen.

Every tap, swipe, key press and wait in NEW code goes through here. Nothing should
hand-roll `tap(x, y)` on a literal coordinate or `time.sleep(1.2)` again — those two
habits produced both classes of live failure this project keeps re-hitting:

  • **Fixed cadence → anti-cheat.** A loop that swiped every 1.000s tripped the
    anti-cheat and terminated the game (2026-08-21). The fingerprint is the INTERVAL,
    so every wait here is jittered and there is deliberately no un-jittered option.
    See `memory/feedback_new_device_loops_need_jitter`.
  • **Absolute coordinates → silent mis-taps.** The game re-bakes a camera-cutout
    safe-area offset per screen at each world-switch, so the same panel renders ~110px
    apart between sessions (AUDIT.md). A constant that worked in one session taps empty
    space in the next — and nothing reports it, because a tap always "succeeds". So the
    primitives here take ELEMENTS and LABELS, not points.

The rule of thumb: if you are about to write a number that means "where on the screen",
stop and find the element instead. `tap_at` exists for the handful of genuinely
calibrated HUD controls; it logs loudly so those stay visible and countable.
"""
from __future__ import annotations

import random
import time
from types import SimpleNamespace
from collections.abc import Mapping
from typing import Optional, Sequence, Tuple

from loguru import logger

# Jittered dwell profiles, in seconds. Named for what you are WAITING FOR, so call sites
# read as intent rather than as magic numbers.
DWELL = {
    "tap": (0.45, 0.85),        # between taps in a sequence (keeps 3 taps > 1s apart)
    "panel": (1.1, 1.8),        # a tab/panel swapping its contents
    "screen": (1.8, 2.6),       # a world-switch: port <-> map <-> building
    "scroll": (0.9, 1.9),       # a list settling after a swipe
    "dialog": (1.4, 2.2),       # a dialog opening or closing
}


def settle(kind: str = "panel", *, why: str = "") -> None:
    """Wait for the UI to catch up — ALWAYS jittered, never a fixed interval."""
    lo, hi = DWELL.get(kind, DWELL["panel"])
    delay = random.uniform(lo, hi)
    if why:
        logger.debug(f"[ui] settle {delay:.2f}s ({kind}) — {why}")
    time.sleep(delay)


def _centre(el) -> Optional[Tuple[int, int]]:
    # DETECTED ELEMENTS COME IN TWO SHAPES. OmniParser yields objects with .cx/.cy; the
    # region detectors (`detect_left_menu` and friends) yield DICTS with the same keys.
    # Reading only attributes made every dict-shaped element "have no position": at San
    # Village 2026-08-24 the bot found the Barter row at (85,617), passed the dict to
    # tap_element, and the tap SILENTLY DID NOT HAPPEN — then the panel was reported as
    # impossible to open, and the mission was abandoned with the fleet standing there.
    # The warning said "element has no position", which reads like a detection failure; the
    # detection was perfect and the handoff was what broke.
    if isinstance(el, Mapping):
        cx, cy = el.get("cx"), el.get("cy")
        if cx is not None and cy is not None:
            return int(cx), int(cy)
        box = el.get("bbox")
        if box and len(box) == 4:
            x1, y1, x2, y2 = box
            return (int(x1) + int(x2)) // 2, (int(y1) + int(y2)) // 2
        return None
    cx, cy = getattr(el, "cx", None), getattr(el, "cy", None)
    if cx is not None and cy is not None:
        return int(cx), int(cy)
    x1, y1 = getattr(el, "x1", None), getattr(el, "y1", None)
    x2, y2 = getattr(el, "x2", None), getattr(el, "y2", None)
    if None in (x1, y1, x2, y2):
        return None
    return (x1 + x2) // 2, (y1 + y2) // 2


def _refuse_if_blocked(frame, x: int, y: int, why: str) -> bool:
    """True when this point must NOT be tapped because a modal covers the screen.

    Perception reads straight THROUGH a scrim, so every detector happily finds controls
    that cannot be pressed; "I can see it" is not "I can press it". And the tap is not
    merely wasted — outside a dialog the bot opened itself, it is the DISMISS gesture
    (live 2026-08-27: a cleanup tap destroyed the Trade Goods Info dialog holding the
    reading that would have corrected the misread which caused the cleanup).

    Panels wear the same chrome as dialogs but never dim and never cover a control, so
    they never block. `frame` is optional throughout: checked when the caller already has
    one in hand, skipped rather than paying for a capture per tap.
    """
    if frame is None:
        return False
    from vision.overlay import detect_overlay
    overlay = detect_overlay(frame)
    if not overlay.blocks(x, y):
        return False
    logger.warning(f"[ui] refusing to tap ({x},{y}) — a modal is up "
                   f"({overlay.state}, bbox={overlay.bbox}) and this point is outside it; "
                   f"the tap would dismiss it, not press the control — {why}")
    return True


def tap_element(el, *, why: str = "", dwell: str = "panel", frame=None) -> bool:
    """Tap a DETECTED element (OmniParser/OCR). The position comes from what is on
    screen this frame, so it follows the cutout shift instead of fighting it."""
    from actions.adb_actions import tap
    pos = _centre(el)
    if pos is None:
        logger.warning(f"[ui] tap_element: element has no position — {why}")
        return False
    label = (getattr(el, "label", "") or "").strip()
    if _refuse_if_blocked(frame, *pos, why or (label or "element")):
        return False
    logger.info(f"[ui] tap {label or 'element'} @ {pos} — {why}")
    tap(*pos)
    settle(dwell)
    return True


def tap_text(frame, *labels: str, why: str = "", dwell: str = "panel",
             x_min: Optional[int] = None, x_max: Optional[int] = None,
             y_min: Optional[int] = None, y_max: Optional[int] = None) -> bool:
    """Find a control by its LABEL on this frame and tap it. Returns False if it isn't
    there — a miss is reportable, unlike a blind tap at a remembered point."""
    from actions.sail_actions import _find_button
    from actions.adb_actions import tap
    pos = _find_button(frame, *labels, x_min=x_min, x_max=x_max, y_min=y_min, y_max=y_max)
    if pos is None:
        logger.warning(f"[ui] tap_text: none of {labels} found — {why}")
        return False
    if _refuse_if_blocked(frame, *pos, why or str(labels[0])):
        return False
    logger.info(f"[ui] tap {labels[0]!r} @ {pos} — {why}")
    tap(*pos)
    settle(dwell)
    return True


def tap_at(x: int, y: int, *, why: str, dwell: str = "panel", frame=None) -> bool:
    """Tap a CALIBRATED coordinate. Only for controls with no detectable label — the
    sea rudder, the HUD arrows. `why` is required and logged so every remaining
    absolute tap stays visible; prefer `tap_element`/`tap_text` wherever possible."""
    from actions.adb_actions import tap
    if _refuse_if_blocked(frame, x, y, why):
        return False
    logger.info(f"[ui] tap CALIBRATED ({x},{y}) — {why}")
    tap(x, y)
    settle(dwell)
    return True


def scroll(x: int, y: int, dy: int, *, why: str = "", dwell: str = "scroll") -> None:
    """Scroll a list by `dy` px (negative = content moves up). Jittered swipe AND
    jittered dwell — this is the loop that tripped the anti-cheat when it was neither."""
    from actions.adb_actions import swipe
    logger.info(f"[ui] scroll ({x},{y}) dy={dy} — {why}")
    swipe(x, y, x, y + dy)
    settle(dwell)


def back(*, why: str = "", dwell: str = "panel") -> None:
    """Android BACK. Remember it is context-dependent — on an overworld or the main
    menu it can open 'Exit game?'. Know the state before calling."""
    from actions.adb_actions import press_back
    logger.info(f"[ui] back — {why}")
    press_back()
    settle(dwell)


def find_menu_icon(frame, *, band_y: int = 120, search_pad: int = 260):
    """Locate the ☰ main-menu icon BY IDENTITY, using its template.

    An earlier version took "the rightmost icon in the top band". That is a positional
    guess with no check of WHAT it found, and OmniParser often does not emit the ☰ at all
    (a plain three-line glyph on busy chrome). It then silently settled for whatever else
    was detected — live 2026-08-21 it tapped the CHAT icon at x=2119, opened the chat
    window, and on the retry tapped MAIL at x=2023, while the real ☰ sat at x≈2213. Each
    attempt hit a different wrong control and reported "the ☰ was detected".

    Template matching answers "is this the hamburger?" instead of "what is furthest
    right?", and it searches a band rather than a fixed point, so it still follows the
    camera-cutout shift. Returns an element-like object with cx/cy, or None."""
    import numpy as np
    try:
        import cv2
        from vision.chrome_detector import ChromeDetector
        from config.settings import CHROME_HAMBURGER_REGION
    except Exception as exc:
        logger.debug(f"[ui] hamburger template unavailable: {exc}")
        return None

    det = ChromeDetector()
    tmpl = getattr(det, "_templates", {}).get("hamburger")
    if tmpl is None:
        logger.warning("[ui] no hamburger template on disk — cannot identify the ☰")
        return None

    # Search a band around the calibrated region, widened so the cutout shift can't put
    # the icon outside it. The region is a SEARCH WINDOW, never a tap target.
    l, t, r, b = CHROME_HAMBURGER_REGION
    w, h = getattr(frame, "width", 2400), getattr(frame, "height", 1080)
    box = (max(0, l - search_pad), 0, min(w, r + search_pad), min(h, max(b, band_y)))
    try:
        frame_bgr = cv2.cvtColor(np.array(frame), cv2.COLOR_RGB2BGR)
        region = frame_bgr[box[1]:box[3], box[0]:box[2]]
        if region.size == 0 or tmpl.shape[0] > region.shape[0] or tmpl.shape[1] > region.shape[1]:
            return _menu_icon_from_strip(frame, band_y=band_y)
        res = cv2.matchTemplate(region, tmpl, cv2.TM_CCOEFF_NORMED)
        _minv, score, _minl, maxloc = cv2.minMaxLoc(res)
    except Exception as exc:
        # An unreadable frame is not a reason to crash a read — fall through to the strip.
        logger.debug(f"[ui] ☰ template match failed: {exc}")
        return _menu_icon_from_strip(frame, band_y=band_y)
    if score >= getattr(det, "threshold", 0.7):
        cx = box[0] + maxloc[0] + tmpl.shape[1] // 2
        cy = box[1] + maxloc[1] + tmpl.shape[0] // 2
        logger.info(f"[ui] ☰ matched by template @ ({cx},{cy}) score={score:.2f}")
        return SimpleNamespace(label="hamburger", cx=cx, cy=cy,
                               x1=cx - 20, y1=cy - 20, x2=cx + 20, y2=cy + 20)
    return _menu_icon_from_strip(frame, band_y=band_y, best_score=score)


def _menu_icon_from_strip(frame, *, band_y: int = 120, best_score: float = 0.0):
    """Infer the ☰ from the icon STRIP it belongs to, when the template can't confirm it.

    The top-right chrome is an evenly-spaced row (Pass, bell, mail, chat, ☰) and the ☰ is
    its last member. OmniParser frequently misses the ☰ itself — a plain three-line glyph
    over changing scenery — but it does detect its neighbours, so the row doubles as a
    RULER: measure the pitch between detected icons and step one place right of the
    rightmost. Measured on the failing frames: icons at 1922/2024/2118, pitch ≈98 →
    predicted ≈2216, against a true ☰ at ≈2213.

    This is still relative — it is derived from icons detected on THIS frame, so it moves
    with the camera-cutout shift. Returns None when the row is too sparse to measure,
    because a guess with nothing behind it is what tapped the chat window."""
    from vision.omniparser import parse_fast_cached
    w = getattr(frame, "width", 2400)
    xs = sorted({_centre(e)[0] for e in parse_fast_cached(frame)
                 if getattr(e, "element_type", "") == "icon"
                 and _centre(e) is not None and _centre(e)[1] <= band_y
                 and _centre(e)[0] > w * 0.70})
    ys = [_centre(e)[1] for e in parse_fast_cached(frame)
          if getattr(e, "element_type", "") == "icon"
          and _centre(e) is not None and _centre(e)[1] <= band_y
          and _centre(e)[0] > w * 0.70]
    if len(xs) < 3:
        logger.info(f"[ui] ☰ unmatched (template {best_score:.2f}) and only {len(xs)} "
                    "strip icon(s) — too sparse to infer a position, refusing to guess")
        return None
    pitches = sorted(b - a for a, b in zip(xs, xs[1:]))
    pitch = pitches[len(pitches) // 2]                  # median, robust to a missed icon
    if not (40 <= pitch <= 200):
        logger.info(f"[ui] ☰ strip pitch {pitch}px is implausible — refusing to guess")
        return None
    cx, cy = xs[-1] + pitch, (sum(ys) // len(ys))
    if cx > w - 20:
        logger.info(f"[ui] ☰ inferred position {cx} is off-frame — the rightmost icon "
                    "detected is probably already the ☰")
        cx = xs[-1]
    logger.info(f"[ui] ☰ inferred from the icon strip @ ({cx},{cy}) "
                f"(pitch {pitch}px, template only {best_score:.2f})")
    return SimpleNamespace(label="hamburger?", cx=cx, cy=cy,
                           x1=cx - 20, y1=cy - 20, x2=cx + 20, y2=cy + 20)


def find_top_right_icon(frame, **kw):
    """Deprecated alias — use `find_menu_icon`, which identifies the ☰ rather than
    guessing at the rightmost icon."""
    return find_menu_icon(frame, **{k: v for k, v in kw.items() if k == "band_y"})


def row_label(anchor, texts: Sequence, *, max_gap: int = 260, row_tol: int = 50):
    """The text belonging to `anchor`'s row: the NEAREST one to its right.

    Nearest HORIZONTALLY, then by row alignment — that order matters. Sorting by row
    alignment first let a floating tooltip 406px away beat the real label 1px away
    because it happened to sit 4px better vertically, and 'Cassava' was read as
    'he Village' (live 2026-08-21). A label sits next to its row; an overlay does not."""
    cands = [t for t in texts
             if t.x1 >= getattr(anchor, "x2", anchor.x1) - 20
             and t.x1 - getattr(anchor, "x2", anchor.x1) <= max_gap
             and abs(t.y1 - anchor.y1) <= row_tol]
    if not cands:
        return None
    return min(cands, key=lambda t: (t.x1, abs(t.y1 - anchor.y1)))

# Screen dimensions come from the FRAME, never from a remembered constant. The phone can
# sit at either landscape rotation, and the game re-bakes its safe-area offset per screen,
# so "the middle of the display" is not a fixed pixel (user 2026-08-22: "always use
# relative positions in bboxes or by perceive").
def screen_centre(frame=None) -> tuple:
    """Centre of the CURRENT frame. Captures one if not given."""
    if frame is None:
        from capture.adb_capture import capture_screen
        frame = capture_screen()
    return (frame.width // 2, frame.height // 2)


def tap_centre(frame=None, *, why: str = "") -> tuple:
    """Tap the centre of the current frame — for waking a cinematic or nudging a
    full-screen overlay, where the point is deliberately "somewhere harmless".

    Prefer `tap_element` / `tap_text` for anything with a target. A centre tap is only safe
    where nothing actionable sits under it: on a market grid the centre is a goods tile, and
    tapping it added 385 Lac Powder to the cart (live 2026-08-21).
    """
    from actions.adb_actions import tap
    x, y = screen_centre(frame)
    logger.info(f"[ui] tap centre @ ({x}, {y}) — {why or 'wake/dismiss'}")
    tap(x, y)
    return (x, y)


# In every chromed screen the TITLE is the sub-menu item currently selected, and that item is
# highlighted in the left menu (user 2026-08-22: "if Purchase is highlighted, the title is
# Purchase, when Supply is highlighted, the title is Supply"). Verified across the 8 frames
# labelled `sub_menu` — title "Recruit Crew" against a menu of Supply / Repair / Recruit Crew —
# and on the market pages of live run 24.
#
# That makes the title the canonical answer to "which sub-screen am I on?", which is a question
# the bot kept answering by tapping. The title is ALSO a back control (see
# `_find_button(..., allow_title=…)`), so it must be READ, never tapped, to establish position.
_TITLE_MAX_X_FRAC = 0.20      # the title sits at the top-LEFT, beside the back chevron
_TITLE_MAX_Y_FRAC = 0.11


def _title_element(frame):
    """The chromed screen title's own element, or None when unreadable.

    Longest wins: "Recruit Crew" over a stray "Crew" from the same band. Returning the
    ELEMENT rather than its text is what lets `chrome_is_dimmed` measure the glyphs the
    title reader actually used, instead of a hardcoded corner of the screen.
    """
    try:
        from vision.omniparser import parse_fast_cached
        els = [e for e in parse_fast_cached(frame)
               if (e.label or "").strip()
               and e.cx < _TITLE_MAX_X_FRAC * frame.width
               and e.cy < _TITLE_MAX_Y_FRAC * frame.height]
    except Exception as exc:
        logger.debug(f"[ui] could not read the title: {exc}")
        return None
    return max(els, key=lambda e: len((e.label or "").strip())) if els else None


def active_submenu(frame=None) -> Optional[str]:
    """The sub-menu item currently open, read from the chromed title. None if unreadable.

    None means "could not tell", never "none is open" — a caller that treats an unreadable
    title as "not where I want to be" will navigate away from a screen it had already reached.

    THIS IS A READING, NOT A PERMISSION. It says which screen is open, and stays true while a
    dialog covers it — see `chrome_is_dimmed` for whether the screen can be acted on.
    """
    if frame is None:
        from capture.adb_capture import capture_screen
        frame = capture_screen()
    el = _title_element(frame)
    return (el.label or "").strip() if el is not None else None


# THE TITLE SAYS WHICH SCREEN; ITS BRIGHTNESS SAYS WHETHER YOU CAN ACT ON IT (user,
# 2026-09-03). `active_submenu` reading 'Barter' under a dialog is a TRUE reading and a
# useless precondition: Barter genuinely IS the open submenu and nothing on it is reachable.
#
# Live 2026-09-03 at San Village, `_open_barter_panel` opens with
#
#     if on_submenu("barter", frame): return True   # already there - do not tap again
#
# and the Notice was drawn OVER the Barter screen, whose chromed title still read 'Barter'.
# So it returned True without tapping and without logging, on every tick, while the dispatcher
# logged "opened the barter panel" three times for doing nothing at all. The stall guard ended
# the mission with the dialog still up.
#
# The game answers this itself: it composites a flat 50% scrim over whatever a dialog covers,
# and the chromed title is behind it. Measured p99 over the title's own glyphs —
#
#     undimmed                253-255     a white glyph saturates
#     one dialog over it      140-146
#     stacked dialogs          32-64      scrims multiply, so depth shows
#
# so the threshold is the midpoint of 128 and 255: read off the compositing, not fitted.
#
# p99 AND NOT p97, which was the first attempt: a SHORT title has few glyph pixels, so p97
# lands on an anti-aliased edge rather than a stroke. The fully-actionable Sell screen
# measured 198.6 that way and would have been called dimmed; at p99 it reads 255.
#
# Measured across 139 titles from the recorded sessions. The other things that land mid-range
# there — 'Safe Waters' and 'Lauless Waters' (the sea HUD), 'TaylorFP' (a player name),
# 'icon' — are not chromed titles at all and cannot reach this test, because `on_submenu`
# only asks it once the name has already matched.
_TITLE_LIT_P99 = 192.0


def chrome_is_dimmed(frame=None) -> Optional[bool]:
    """True when a dialog is dimming this screen's chrome. None when it cannot be told.

    None is NOT False, for the same reason `active_submenu` returns None rather than "":
    a caller that reads "could not tell" as "nothing in the way" acts into a modal.
    """
    if frame is None:
        from capture.adb_capture import capture_screen
        frame = capture_screen()
    el = _title_element(frame)
    if el is None:
        return None
    try:
        import numpy as np
        a = np.asarray(frame.convert("RGB")).astype(float)
        lum = 0.2126 * a[:, :, 0] + 0.7152 * a[:, :, 1] + 0.0722 * a[:, :, 2]
        glyphs = lum[el.y1:el.y2, el.x1:el.x2]
        if glyphs.size == 0:
            return None
        return bool(float(np.percentile(glyphs, 99)) < _TITLE_LIT_P99)
    except Exception as exc:                      # noqa: BLE001 — unmeasurable is not "clear"
        logger.debug(f"[ui] could not measure the title's brightness: {exc}")
        return None


def on_submenu(name: str, frame=None) -> bool:
    """True when `name` is the open sub-menu AND the screen can be acted on.

    Every caller asks this as a PRECONDITION — "already there, do not tap again", "did the tap
    work?", "is this the page I think it is?" — so a title that is present but dimmed must
    answer False. `active_submenu` remains the pure reading for callers that only want to know
    which screen is open.
    """
    if frame is None:
        from capture.adb_capture import capture_screen
        frame = capture_screen()
    title = (active_submenu(frame) or "").strip().lower()
    if not title or title != name.strip().lower():
        return False
    if chrome_is_dimmed(frame):
        logger.info(f"[ui] the title says {name!r} but its chrome is dimmed — something is "
                    "drawn over this screen, so it cannot be acted on")
        return False
    return True
