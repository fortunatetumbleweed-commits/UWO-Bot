"""Template-matching locator for popup close-X buttons whose icon is
visually distinct from regular dialog Xs.

Some popups (notably the daily-news / patch-notes overlay) put their
close X *outside* the popup body — a small black circle with a white X
sitting at the outer top-right corner of the popup.  DialogModel's
inner-cluster X search misses these because the X isn't inside the
cluster bbox.

This module is the dedicated locator for those outer close icons.
Pattern mirrors `vision.chrome_detector`: load PNG templates from
`vision/assets/close_buttons/` and run `cv2.matchTemplate` over a
constrained search region for speed and to control false positives.

Each template ships with its own search region — the region only needs
to be wide enough to cover where that specific popup's X can appear.
The daily-news X has been seen at (1790, 220) and (1823, 267) at
1080×2400, so the region covers the right-upper quadrant comfortably.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import Image
from loguru import logger

# ── Asset / region config ────────────────────────────────────────────

_ASSETS_DIR = Path(__file__).parent / "assets" / "close_buttons"

# Per-button (template path, search region).  Region is (l, t, r, b)
# in 2400×1080 pixel coordinates.  Templates are skipped if the file
# is missing — graceful degradation.
_BUTTONS: dict[str, tuple[str, tuple[int, int, int, int]]] = {
    # Daily-news / patch-notes overlay close icon: black circle, white
    # X.  Sits at the outer top-right of the popup card.  Observed
    # positions across content variants: (1790, 220), (1823, 267).
    "daily_news": ("daily_news_close_x.png", (1400, 50, 2100, 400)),
}

# Match confidence threshold.  TM_CCOEFF_NORMED ranges -1..1; chrome
# uses 0.70 as its default.  The close-X templates are small icons
# with high contrast, so we can be a touch stricter without losing
# legitimate matches.
_MATCH_THRESHOLD: float = 0.75


# ── API ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CloseButtonMatch:
    """Result of a successful template match."""
    button:     str          # registry key, e.g. "daily_news"
    cx:         int          # centre-x of the matched template in the full frame
    cy:         int          # centre-y
    confidence: float        # peak match score (TM_CCOEFF_NORMED)


_template_cache: dict[str, Optional[np.ndarray]] = {}


def _load_template(name: str) -> Optional[np.ndarray]:
    if name in _template_cache:
        return _template_cache[name]
    filename, _ = _BUTTONS[name]
    path = _ASSETS_DIR / filename
    if not path.exists():
        logger.debug(f"[close_button_matcher] template missing: {path}")
        _template_cache[name] = None
        return None
    tmpl = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if tmpl is None:
        logger.warning(f"[close_button_matcher] unreadable: {path}")
        _template_cache[name] = None
        return None
    _template_cache[name] = tmpl
    logger.debug(f"[close_button_matcher] loaded template '{name}' ({tmpl.shape})")
    return tmpl


def find_close_button(
    frame: Image.Image,
    button: str = "daily_news",
    threshold: float = _MATCH_THRESHOLD,
) -> Optional[CloseButtonMatch]:
    """Locate the named close button in `frame` via template matching.

    Returns CloseButtonMatch with centre coords if the best match score
    in the search region exceeds `threshold`, else None.
    """
    if button not in _BUTTONS:
        logger.warning(f"[close_button_matcher] unknown button {button!r}")
        return None
    tmpl = _load_template(button)
    if tmpl is None:
        return None

    _, region = _BUTTONS[button]
    l, t, r, b = region
    frame_bgr = cv2.cvtColor(np.array(frame), cv2.COLOR_RGB2BGR)
    crop = frame_bgr[t:b, l:r]
    if crop.size == 0 or tmpl.shape[0] > crop.shape[0] or tmpl.shape[1] > crop.shape[1]:
        logger.debug(
            f"[close_button_matcher] {button}: search region too small "
            f"({crop.shape} vs template {tmpl.shape})"
        )
        return None

    result = cv2.matchTemplate(crop, tmpl, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    if max_val < threshold:
        logger.debug(
            f"[close_button_matcher] {button}: best score {max_val:.3f} "
            f"below threshold {threshold:.2f}"
        )
        return None
    # max_loc is (x, y) of the template's top-left corner inside the crop.
    th, tw = tmpl.shape[:2]
    cx = l + max_loc[0] + tw // 2
    cy = t + max_loc[1] + th // 2
    logger.info(
        f"[close_button_matcher] {button}: matched at ({cx}, {cy}) "
        f"conf={max_val:.3f}"
    )
    return CloseButtonMatch(button=button, cx=cx, cy=cy, confidence=max_val)
