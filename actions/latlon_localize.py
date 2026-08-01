"""Lat/lon → catalogue localization on the world map.

Wraps three pieces:

  • The persisted affine transform (built once by
    `tools/calibrate_latlon.py`) that converts `(lat, lon)` from the
    world map's water-tap readout into the catalogue `(x, y)` coords
    used throughout the navigation stack.

  • A color-based water-vs-land heuristic for candidate tap pixels.
    Tapping on land does nothing useful (the game shows a transient
    "You cannot move to that location" ribbon and no readout); the
    pre-filter avoids wasting taps.

  • `localize_screen_center(frame)` — captures a frame, filters the
    candidate pixel grid to water-only, taps the first one, reads the
    lat/lon, converts to catalogue.  Returns the catalogue coord of
    whichever pixel actually got tapped, plus that pixel — caller can
    derive the implied camera-centre by subtracting the pixel-to-centre
    offset scaled to game units.

Design notes:
  • The affine is loaded lazily on first call and cached.  Missing /
    corrupt file → returns None and callers fall back to their
    existing logic.
  • Water heuristic: average RGB over a small neighborhood of the
    pixel and check `B > R + WATER_BR_DELTA`.  Tuned against the
    Mediterranean view in the labelled frames; should generalise to
    other regions because the game renders sea consistently blue-green
    and land in warm tones.
  • No fallback to ribbon detection — the "cannot move" banner is too
    transient (~1-2 s) to reliably catch at our perception cadence.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional, Tuple

import numpy as np
from PIL import Image

from loguru import logger


_AFFINE_PATH = Path("memory/knowledge/world_map/latlon_to_catalogue.json")


# Water detection on the UWO world map.  The game renders discovered
# sea as a green-tinted teal (R≈53 G≈65 B≈52 in night/atmospheric
# tones, brighter in clear daylight Mediterranean).  Across all
# variations, G is the dominant channel and R is consistently below
# G — that's the stable signature.  B varies (sea-green vs sea-blue
# depending on region), so we don't use it.
#
# Land in this view is warm-toned (tan deserts, ochre coast,
# rust-coloured mountains) → R highest, G-R ≤ 0 or slightly negative.
# Forests appear too but are rare in the early-game viewports we care
# about (Mediterranean / Atlantic / Indian Ocean), and a false-positive
# on forest just costs one wasted tap — the OCR-check is the final
# arbiter.
WATER_GR_DELTA = 5         # G > R + this → water
WATER_SAMPLE_RADIUS = 4    # px window radius (9×9 sample box)


# ── Affine ─────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _load_affine() -> Optional[dict]:
    """Returns the dict {a, b, c, d, e, f} or None if no calibration
    has been done yet.  Cached for the process lifetime.
    """
    try:
        data = json.loads(_AFFINE_PATH.read_text())
        t = data["transform"]
        # Validate shape.
        for k in ("a", "b", "c", "d", "e", "f"):
            float(t[k])
        return t
    except (FileNotFoundError, KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.debug(f"[latlon-localize] affine not loaded: {exc}")
        return None


def reset_affine_cache() -> None:
    """Forces re-read from disk on next access — used by tests."""
    _load_affine.cache_clear()


def affine_available() -> bool:
    return _load_affine() is not None


def latlon_to_catalogue(lat: float, lon: float) -> Optional[Tuple[float, float]]:
    """Apply the persisted affine: (lat, lon) → catalogue (x, y).

    Returns None if no affine has been calibrated yet.
    """
    t = _load_affine()
    if t is None:
        return None
    cat_x = t["a"] * lon + t["b"] * lat + t["c"]
    cat_y = t["d"] * lon + t["e"] * lat + t["f"]
    return cat_x, cat_y


def catalogue_to_latlon(cat_x: float, cat_y: float) -> Optional[Tuple[float, float]]:
    """Inverse: catalogue (x, y) → (lat, lon).  None if uncalibrated."""
    t = _load_affine()
    if t is None:
        return None
    # [a b] [lon]   [cat_x - c]
    # [d e] [lat] = [cat_y - f]
    a, b, d, e = t["a"], t["b"], t["d"], t["e"]
    det = a * e - b * d
    if abs(det) < 1e-9:
        return None
    rhs_x = cat_x - t["c"]
    rhs_y = cat_y - t["f"]
    lon = ( e * rhs_x - b * rhs_y) / det
    lat = (-d * rhs_x + a * rhs_y) / det
    return lat, lon


# ── Water detection ────────────────────────────────────────────────────────

def is_pixel_water(
    frame: Image.Image, px: int, py: int,
    sample_radius: int = WATER_SAMPLE_RADIUS,
) -> bool:
    """True iff the pixel (and its small neighbourhood) looks like
    navigable water on the world map.

    Heuristic: average RGB in a (2r+1)² window, water if G > R + delta
    (sea-green tint).  Returns False on out-of-bounds inputs.
    """
    w, h = frame.size
    if not (0 <= px < w and 0 <= py < h):
        return False
    r = sample_radius
    left, top = max(0, px - r), max(0, py - r)
    right, bottom = min(w, px + r + 1), min(h, py + r + 1)
    crop = np.array(frame.crop((left, top, right, bottom)).convert("RGB"))
    if crop.size == 0:
        return False
    avg_r = float(crop[..., 0].mean())
    avg_g = float(crop[..., 1].mean())
    avg_b = float(crop[..., 2].mean())
    is_water = avg_g > avg_r + WATER_GR_DELTA
    logger.debug(
        f"[water-detect] ({px},{py}) avg RGB=({avg_r:.0f},{avg_g:.0f},{avg_b:.0f}) "
        f"G-R={avg_g - avg_r:.1f} → {'water' if is_water else 'land'}"
    )
    return is_water


def filter_water_candidates(
    frame: Image.Image,
    candidates: Iterable[Tuple[int, int]],
) -> list[Tuple[int, int]]:
    """Return the subset of *candidates* whose pixel looks like water
    on the given frame.  Preserves input order — caller's priority is
    respected."""
    return [pt for pt in candidates if is_pixel_water(frame, *pt)]


# ── End-to-end localization ────────────────────────────────────────────────

def localize_screen_center(
    frame: Optional[Image.Image] = None,
) -> Optional[dict]:
    """Tap a water pixel near screen centre, read its lat/lon, return
    the catalogue coord of that tap plus context for the caller.

    Returns None when:
      • no affine has been calibrated yet (run tools/calibrate_latlon.py),
      • every candidate pixel is on land (rare — pick a candidate grid
        that includes ocean for any plausible view), OR
      • every tap that found water failed OCR (very rare).

    On success returns:
      {
        "tap_pixel":  (px, py),
        "latlon":     (lat, lon),
        "catalogue":  (cat_x, cat_y),
      }
    """
    # Lazy imports so this module doesn't drag in ADB at import time
    # (matters for unit tests).
    from capture.adb_capture import capture_screen
    from actions.water_tap import (
        WATER_TAP_CANDIDATES, tap_water_and_read_latlon,
    )

    if not affine_available():
        logger.warning(
            "[latlon-localize] no affine calibrated yet — "
            "run tools/calibrate_latlon.py first"
        )
        return None

    if frame is None:
        frame = capture_screen()

    water_pts = filter_water_candidates(frame, WATER_TAP_CANDIDATES)
    if not water_pts:
        # Heuristic struck out (rare colour-cast, atmospheric tint, etc).
        # Fall back to trying every candidate — the OCR check is the
        # final arbiter; tapping land just gets us no readout and we
        # move on.  Each failed tap costs ~1 s.
        logger.info(
            "[latlon-localize] no candidate passed the water heuristic — "
            "trying all candidates (OCR will sort water from land)"
        )
        try_pts = list(WATER_TAP_CANDIDATES)
    else:
        logger.info(
            f"[latlon-localize] {len(water_pts)}/{len(WATER_TAP_CANDIDATES)} "
            f"candidate(s) on water; trying first: {water_pts[0]}"
        )
        try_pts = water_pts

    for px, py in try_pts:
        latlon = tap_water_and_read_latlon(px, py)
        if latlon is None:
            logger.info(
                f"[latlon-localize] tap ({px},{py}) thought to be water but "
                f"no readout — trying next"
            )
            continue
        lat, lon = latlon
        cat = latlon_to_catalogue(lat, lon)
        if cat is None:
            return None
        cat_x, cat_y = cat
        logger.info(
            f"[latlon-localize] tap=({px},{py}) latlon=({lat:.2f},{lon:.2f}) "
            f"→ catalogue=({cat_x:.0f},{cat_y:.0f})"
        )
        return {
            "tap_pixel": (px, py),
            "latlon": (lat, lon),
            "catalogue": (cat_x, cat_y),
        }
    return None
