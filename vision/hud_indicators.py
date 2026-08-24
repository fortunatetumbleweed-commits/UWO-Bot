# vision/hud_indicators.py
# Ticket #15 — low-level HUD indicator detectors:
#   • red-badge detector — the app-wide "attention / action available" indicator
#     (barter red note = rounds available, recruit red dot, menu badges, …). The
#     user flagged this as important: it's a GAME-WIDE pattern, not one screen.
#   • currency-icon colour classifier — label a HUD value by its icon colour
#     (gold ducat / blue gem / red gem / green action-points), to harden the
#     magnitude-based read in vision.hud_readers.read_currencies.
#
# Price-visibility (is a port's sell price in trade-level range) is already covered
# by actions.world_map.read_port_good_price (.in_range).

from __future__ import annotations

from typing import Optional, Tuple, List

import numpy as np
from PIL import Image


# ── Currency icon colour ──────────────────────────────────────────────────────

def classify_currency_icon(rgb: Tuple[float, float, float]) -> Optional[str]:
    """Classify a currency icon's mean RGB into the currency it denotes.

    ducat = gold (R≈G high, B lower), blue_gem = blue-dominant, red_gem =
    red-dominant, action_points = green-dominant.  None if no clear colour."""
    r, g, b = float(rgb[0]), float(rgb[1]), float(rgb[2])
    # Red / green / blue by a dominant channel.
    if r - g > 40 and r - b > 40:
        return "red_gem"
    if g - r > 30 and g - b > 30:
        return "action_points"
    if b - r > 30 and b - g > 30:
        return "blue_gem"
    # Gold ducat: warm, R and G both high and close, B clearly lower.
    if abs(r - g) < 45 and r - b > 35 and g - b > 35 and r > 120:
        return "ducat"
    return None


def sample_icon_color(image: Image.Image, cx: int, cy: int,
                      offset: int = 55, half: int = 12) -> Tuple[float, float, float]:
    """Mean RGB of a small patch `offset` px left of (cx, cy) — the icon that sits
    just left of a HUD value. Pair with classify_currency_icon()."""
    arr = np.asarray(image.convert("RGB")).astype(float)
    ix = max(half, cx - offset)
    y0, y1 = max(0, cy - half), cy + half
    x0, x1 = max(0, ix - half), ix + half
    patch = arr[y0:y1, x0:x1].reshape(-1, 3)
    return tuple(patch.mean(axis=0)) if len(patch) else (0.0, 0.0, 0.0)


# ── Red attention badge ───────────────────────────────────────────────────────

def _red_mask(arr: np.ndarray) -> np.ndarray:
    """Boolean mask of saturated-red badge pixels.  Calibrated on the barter red
    note (mean RGB ≈ 238,33,67)."""
    R = arr[:, :, 0].astype(int)
    G = arr[:, :, 1].astype(int)
    B = arr[:, :, 2].astype(int)
    return (R > 150) & (R - G > 80) & (R - B > 80)


def _crop_arr(image: Image.Image, box) -> np.ndarray:
    img = image.convert("RGB")
    if box is not None:
        img = img.crop(box)
    return np.asarray(img)


def red_pixel_count(image: Image.Image, box=None) -> int:
    """Number of saturated-red pixels in `box` (whole image if None)."""
    return int(_red_mask(_crop_arr(image, box)).sum())


def has_red_badge(image: Image.Image, box=None, min_pixels: int = 100) -> bool:
    """True if a red attention badge/dot is present in `box`.  Scope `box` to a
    single UI element (menu item, tab) to ask 'does THIS element have a badge?'."""
    return red_pixel_count(image, box) >= min_pixels


def find_red_badges(image: Image.Image, box=None,
                    min_pixels: int = 100) -> List[Tuple[int, int]]:
    """Centroids (full-image coords) of distinct red-badge blobs ≥ min_pixels.
    Falls back to a single bounding-box centroid if scipy isn't available."""
    ox, oy = (box[0], box[1]) if box is not None else (0, 0)
    mask = _red_mask(_crop_arr(image, box))
    if not mask.any():
        return []
    try:
        from scipy import ndimage
        labels, n = ndimage.label(mask)
        out = []
        for i in range(1, n + 1):
            ys, xs = np.where(labels == i)
            if len(xs) >= min_pixels:
                out.append((int(xs.mean()) + ox, int(ys.mean()) + oy))
        return out
    except Exception:
        ys, xs = np.where(mask)
        if len(xs) < min_pixels:
            return []
        return [(int(xs.mean()) + ox, int(ys.mean()) + oy)]
