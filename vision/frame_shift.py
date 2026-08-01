"""Frame-to-frame pixel translation measurement.

Computes the (dy, dx, confidence) shift between two consecutive
minimap water masks via phase correlation on shore-edge pixels only.

Why shore-edge and not full mask:
    A straight river reach (e.g. Nile at Nubia) is a nearly-uniform
    vertical stripe of water pixels.  Translating that stripe along
    its own axis produces a mask that's ~identical to the un-shifted
    stripe (aperture problem), so full-mask registration returns
    (0, 0) even when the ship is clearly moving.  Shore-edge pixels
    (the 1-px ring where water meets land) carry all the translation
    signal — small bays, notches, curvature — without the interior
    ambiguity.  Session `ai_nav_2026-07-22T22-35-04` t108-t115
    confirmed the aperture failure with full-mask IoU and the fix
    with shore-edge phase correlation.

Sign convention:
    Returns `(dy, dx)` such that a world point at `prev_frame[r, c]`
    appears at `curr_frame[r + dy, c + dx]` in the next tick.  This
    is opposite the ship's motion direction — ship moves south →
    world content moves north (up) in the frame → dy is negative.

See `docs/pixel_continuity_design.md` §3 for the wider design.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
from scipy.ndimage import binary_erosion


# Minimum shore-edge pixel count for a meaningful registration.
# Below this the phase correlation is dominated by noise.
_MIN_SHORE_PIXELS = 100


def shore_edge_mask(water_mask: np.ndarray,
                    sprite_mask: Optional[np.ndarray] = None,
                    ) -> np.ndarray:
    """Extract the water-side shore-edge ring from a water mask.

    The result is a 1-pixel-wide boolean band along the inner shore
    boundary (water pixels adjacent to land).  Sprite pixels (if
    provided) are removed so moving NPCs / ship-icon / sonar don't
    contribute false-motion noise.
    """
    eroded = binary_erosion(water_mask, iterations=1)
    shore = water_mask & ~eroded
    if sprite_mask is not None:
        shore = shore & ~sprite_mask
    return shore


def measure_frame_shift(
    prev_water: np.ndarray,
    curr_water: np.ndarray,
    prev_sprites: Optional[np.ndarray] = None,
    curr_sprites: Optional[np.ndarray] = None,
) -> tuple[int, int, float]:
    """Measure per-tick pixel translation between two water masks.

    Uses `cv2.phaseCorrelate` on shore-edge inputs — ~1.2 ms per call
    on 187×399 masks, well inside the tick budget.  cv2 applies a
    Hanning window internally to suppress edge-wrap artifacts.

    Returns `(dy, dx, confidence)` where:
        - `dy`, `dx` are integer pixel shifts (rounded from cv2's
          sub-pixel result).  Sign convention: a point at
          `prev[r, c]` appears at `curr[r+dy, c+dx]`.
        - `confidence` is cv2's response peak height in [0, 1].
          Values below ~0.3 typically mean registration failure
          (UI overlay, camera state change, or first-tick).
    """
    try:
        import cv2
    except ImportError:
        return 0, 0, 0.0
    if prev_water.shape != curr_water.shape:
        return 0, 0, 0.0
    prev_shore = shore_edge_mask(prev_water, prev_sprites)
    curr_shore = shore_edge_mask(curr_water, curr_sprites)
    if (prev_shore.sum() < _MIN_SHORE_PIXELS
            or curr_shore.sum() < _MIN_SHORE_PIXELS):
        return 0, 0, 0.0
    prev_f = prev_shore.astype(np.float32)
    curr_f = curr_shore.astype(np.float32)
    hann = cv2.createHanningWindow(prev_f.shape[::-1], cv2.CV_32F)
    (dx_f, dy_f), conf = cv2.phaseCorrelate(prev_f, curr_f, hann)
    # cv2 returns (dx, dy) as the shift OF b relative to a — i.e.
    # b[y, x] ≈ a[y - dy, x - dx].  Our convention:
    # prev[r, c] appears at curr[r+dy, c+dx], so cv2's (dx, dy)
    # equals our (dx, dy) directly.  (Ship moves south → world
    # content moves up in frame → cv2 dy is negative → our dy is
    # negative.  Consistent.)
    return int(round(dy_f)), int(round(dx_f)), float(conf)
