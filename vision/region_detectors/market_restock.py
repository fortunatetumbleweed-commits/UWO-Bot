"""Market restock/refresh control detector.

The Purchase-grid header shows a restock TIMER (e.g. `00:00:36`) counting down to a
free restock, and immediately to its RIGHT a WHITE RECTANGLE = the refresh button:
it shows a blue-gem icon then the number of blue gems needed (user 2026-08-17). Tapping
it restocks the market immediately for that blue-gem cost. OmniParser misses these
small elements, so we anchor on the OCR-located timer, find the white pill to its
right, and colour-check the cost is a BLUE gem (never red = real money).

Used by the gather flow: when a port lacks enough of a material for the barter goal,
refresh with blue gems instead of waiting ~20 min for the timer. See memory
project_unified_purchase_goal_design (restock STEP 2).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
from PIL import Image

_TIMER_RE = re.compile(r"^\d{1,2}[:.]\d{2}[:.]\d{2}$")
# Search window for the white pill, relative to the timer token's OCR centre.
_PILL_X0, _PILL_X1 = 40, 380     # scan this far right of the timer for the pill
_PILL_HALF_H       = 16          # vertical half-window around the timer row
# The ↻ refresh icon (the actual tap target) sits a CONSTANT distance right of the
# restock-timer text; the gem/cost readout further right is NOT tappable. The offset is
# the same at every market — Malé and Masulipatnam both measure timer→↻ = +96. Their
# ABSOLUTE positions differ by 118px, but that is the whole-UI camera-notch/orientation
# shift ([[project_notch_orientation_shifts_ui_118px]]), NOT a per-market difference.
# Anchoring on the OCR timer rides that shift automatically (robust); a fixed absolute
# coord would not.
_REFRESH_ICON_FROM_TIMER = 96

# The header band the restock pill is drawn in, and what makes a pill a pill. Measured over
# four consecutive frames at Antalya: bright runs 1375-1422 and 1424-1578, i.e. a ~200px pill
# split by the ↻ glyph. The filter button further right measures 26 and 23 wide, well under
# the minimum.
_BAND_Y0, _BAND_Y1 = 120, 200
_WHITE_COL_FRACTION = 0.35
_MIN_RUN_PX = 15
_PILL_JOIN_GAP_PX = 40
_PILL_MIN_W = 120


@dataclass
class RestockButton:
    cx: int                 # tap point (centre of the white refresh pill)
    cy: int
    currency: str           # "blue_gem" | "red_gem" | "unknown" — what a refresh SPENDS
    timer: str              # raw restock-timer text (e.g. "00:00:36")


def _gem_currency(arr: np.ndarray, x1: int, y1: int, x2: int, y2: int) -> str:
    """Saturated blue → blue_gem, saturated red → red_gem, else unknown."""
    h, w = arr.shape[:2]
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
    band = arr[y1:y2, x1:x2].reshape(-1, 3)
    if len(band) == 0:
        return "unknown"
    r, g, b = band[:, 0].astype(int), band[:, 1].astype(int), band[:, 2].astype(int)
    blue = ((b > 110) & (b - r > 25) & (b - g > 10)).mean()
    red  = ((r > 130) & (r - g > 40) & (r - b > 40)).mean()
    # The gem is a small icon on a white pill, so its pixel fraction is low; a clear
    # blue signal with negligible red is enough.
    if blue >= 0.015 and blue >= red:
        return "blue_gem"
    if red >= 0.015:
        return "red_gem"
    return "unknown"


def find_restock_button(frame: Image.Image,
                        ocr_fn: Optional[Callable] = None) -> Optional[RestockButton]:
    """Locate the Purchase-grid restock refresh button, or None if it is not on screen.

    THE TIMER IS NOT A GATE (user, 2026-09-05: "right now the timer should not be used at
    all... it should not interfere with the refresh").

    This anchored on the OCR'd countdown and returned None when the regex did not match. The
    countdown is incidental text that happens to sit beside the button, and OCR is noisy on
    it — measured on two consecutive frames of one unchanged screen:

        frame 294  '00.00.51'    matches   -> button found
        frame 295  '00.00:.42'   no match  -> None

    One stray colon. `refresh_market` then reported "no restock control", `buy_to_goal` read
    that as a market that cannot be refreshed and broke out, and the Antalya leg stopped at
    595 Mutton of 1,015 — three barter rounds lost with the ↻ on screen at 3 blue gems.

    So the PILL is the anchor now: a white rounded rectangle in the header band, which is
    what the button actually is. It reads identically on all four of those frames
    (1375-1578), while the text beside it did not.

    The timer is still read when it happens to parse, and reported for the log and for a
    future caller that would rather wait out a nearly-expired countdown than spend a gem.
    Nothing depends on it.
    """
    arr = np.asarray(frame.convert("RGB"))
    band = arr[_BAND_Y0:_BAND_Y1, :, :]
    white = (band[:, :, 0] > 200) & (band[:, :, 1] > 200) & (band[:, :, 2] > 200)
    col = white.mean(axis=0)

    # Contiguous bright runs, then merge the ones separated by a thin dark line: the ↻ glyph
    # splits the pill in two (measured 1375-1422 and 1424-1578 — a 2px gap).
    runs, start_i = [], None
    for i, v in enumerate(col):
        if v > _WHITE_COL_FRACTION and start_i is None:
            start_i = i
        elif v <= _WHITE_COL_FRACTION and start_i is not None:
            if i - start_i >= _MIN_RUN_PX:
                runs.append([start_i, i])
            start_i = None
    if start_i is not None and len(col) - start_i >= _MIN_RUN_PX:
        runs.append([start_i, len(col)])
    if not runs:
        return None

    pills, current = [], list(runs[0])
    for r in runs[1:]:
        if r[0] - current[1] <= _PILL_JOIN_GAP_PX:
            current[1] = r[1]
        else:
            pills.append(current)
            current = list(r)
    pills.append(current)

    pill = max(pills, key=lambda p: p[1] - p[0])
    if (pill[1] - pill[0]) < _PILL_MIN_W:
        return None                      # no pill this wide → not the restock control

    # THE ↻ ICON IS THE TAP TARGET, not the pill centre and not the gem cost — a centre-tap
    # on the gem did nothing (verified live 2026-08-17). It sits in the pill's first segment,
    # left of the dark glyph that splits it.
    first = next((r for r in runs if r[0] >= pill[0]), pill)
    cx = (first[0] + min(first[1], pill[1])) // 2
    cy = (_BAND_Y0 + _BAND_Y1) // 2

    # Blue gems only; a red-gem cost is real money and is refused upstream.
    currency = _gem_currency(arr, pill[0], cy - 14, pill[1], cy + 14)
    if currency not in ("blue_gem", "red_gem"):
        # NO COST ON IT MEANS IT IS NOT THE RESTOCK CONTROL. Other screens have wide white
        # areas in this band — the village barter panel and the hold view both produce one —
        # and the gem is what makes this button that button. Returning it as 'unknown' left
        # the caller to refuse it for the wrong reason.
        return None

    timer = None
    try:
        if ocr_fn is None:
            from actions.sail_actions import _ocr_frame as ocr_fn
        toks = ocr_fn(frame, min_conf=0.2)
        timer = next((w.strip() for w, _c, x, y in toks
                      if _BAND_Y0 < y < _BAND_Y1 and _TIMER_RE.match(w.strip().replace(" ", ""))),
                     None)
    except Exception:                    # noqa: BLE001 — a label, never a gate
        timer = None
    return RestockButton(cx=cx, cy=cy, currency=currency, timer=timer)
