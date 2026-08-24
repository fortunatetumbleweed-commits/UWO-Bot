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
    """Locate the Purchase-grid restock refresh button, or None if no timer is shown
    (market not refreshable / not on the Purchase grid). Anchors on the OCR timer, then
    finds the WHITE pill to its right. ocr_fn(frame, min_conf) -> [(word,conf,x,y)]."""
    if ocr_fn is None:
        from actions.sail_actions import _ocr_frame as ocr_fn
    toks = ocr_fn(frame, min_conf=0.2)
    timer = next(((w.strip(), int(x), int(y)) for w, _c, x, y in toks
                  if 120 < y < 220 and _TIMER_RE.match(w.strip().replace(" ", ""))), None)
    if timer is None:
        return None
    text, tx, ty = timer

    arr = np.asarray(frame.convert("RGB"))
    y0, y1 = ty - _PILL_HALF_H, ty + _PILL_HALF_H
    x0, x1 = tx + _PILL_X0, tx + _PILL_X1
    h, w = arr.shape[:2]
    y0, y1, x0, x1 = max(0, y0), min(h, y1), max(0, x0), min(w, x1)
    strip = arr[y0:y1, x0:x1]
    if strip.size == 0:
        return None
    # White pill = columns that are mostly bright (the rounded white rectangle).
    white = (strip[:, :, 0] > 200) & (strip[:, :, 1] > 200) & (strip[:, :, 2] > 200)
    col_white = white.mean(axis=0)                      # fraction white per column
    cols = np.where(col_white > 0.35)[0]
    if len(cols) < 20:                                  # no clear pill → not refreshable
        return None
    pill_x0, pill_x1 = int(cols.min()) + x0, int(cols.max()) + x0
    # Tap the ↻ REFRESH ICON — NOT the pill centre / gem-cost, which is a no-op
    # (verified live 2026-08-17: a centre-tap on the gem did nothing). The circular-
    # arrow icon is the actual button; tapping it opens a "spend N blue gems?" confirm.
    # Anchor the icon on the OCR'd TIMER (robust across ports: Malé +92, Masulipatnam
    # +101) rather than the noisy pill edge.
    cx = tx + _REFRESH_ICON_FROM_TIMER
    # The blue-gem cost icon is the only coloured thing on the white pill — scan the
    # whole pill (blue >> red → blue_gem; refuse red = real money).
    currency = _gem_currency(arr, pill_x0, ty - 14, pill_x1, ty + 14)
    return RestockButton(cx=cx, cy=ty, currency=currency, timer=text)
