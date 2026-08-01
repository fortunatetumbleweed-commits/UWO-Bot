"""Classify the sail toggle button — rudder (stopped) vs anchor (sailing).

The button at frame coords (201, 741) on the sea HUD displays one of
two icons depending on ship state:

    RUDDER (ship's wheel)  → ship is STOPPED.  Tapping starts sailing.
    ANCHOR                 → ship is actively SAILING.  Tapping drops
                             the anchor and stops.

`sail_start` / `sail_stop` in `actions/sea_actions.py` were previously
blind taps — they assumed the right state without checking and would
silently do the opposite of what we wanted if the state was off.  Live
calibration run 2026-05-30_16:34 caught this: Phase A's sail_stop ran
while the ship was still in deceleration; Phase B's sail_start then
arrived while the button was still ANCHOR, so the tap dropped the
anchor instead of raising it.  Result: ship sat at 0.0 kt for 12
seconds of Phase B when it should have been accelerating.

This classifier reads the icon directly so the caller can:
  - skip the tap when already in the desired state
  - verify the post-tap transition (tap retry on failure)

Two reference crops live in `vision/assets/`:
    sail_button_rudder.png  — sample 001 of calibration run
    sail_button_anchor.png  — sample 016 of calibration run

Distance metric: sum-of-squared-differences on the resized crop.  The
two reference icons are visually distinct (wheel-with-spokes vs
chunky anchor), so even a tiny pixel-wise comparison gives confident
results.  Cost: <5 ms per call.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import numpy as np
from PIL import Image

# Button toggle location and crop box.  Same as the tap target in
# actions/sea_actions.RUDDER_TOGGLE_XY.
BUTTON_XY = (201, 741)
BUTTON_CROP = (BUTTON_XY[0] - 35, BUTTON_XY[1] - 35,
               BUTTON_XY[0] + 35, BUTTON_XY[1] + 35)

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
_REF_PATHS = {
    "rudder": _ASSETS_DIR / "sail_button_rudder.png",
    "anchor": _ASSETS_DIR / "sail_button_anchor.png",
}

ButtonState = Literal["rudder", "anchor"]

_refs_cache: dict[str, np.ndarray] | None = None


def _load_refs() -> dict[str, np.ndarray]:
    """Lazy-load reference crops as small grayscale numpy arrays."""
    global _refs_cache
    if _refs_cache is None:
        cache: dict[str, np.ndarray] = {}
        for label, path in _REF_PATHS.items():
            if not path.exists():
                raise FileNotFoundError(f"missing reference crop: {path}")
            img = Image.open(path).convert("L")    # gray scale
            cache[label] = np.array(img, dtype=np.float32)
        _refs_cache = cache
    return _refs_cache


def classify_sail_button(frame) -> tuple[Optional[ButtonState], float]:
    """Read the toggle button state from a 2400×1080 sea-HUD frame.

    Returns (state, confidence):
        state       — 'rudder' | 'anchor' | None
        confidence  — 0..1, separation between best and second-best
                      reference SSD; >0.2 is a clean read.

    None when the frame can't be sized/read or both references are
    unexpectedly far.
    """
    refs = _load_refs()
    try:
        button = frame.crop(BUTTON_CROP).convert("L")
        arr = np.array(button, dtype=np.float32)
    except Exception:
        return None, 0.0
    # Resize references on first call if shapes differ from current crop.
    expected_shape = arr.shape
    distances: dict[ButtonState, float] = {}
    for label, ref in refs.items():
        if ref.shape != expected_shape:
            ref_img = Image.fromarray(ref.astype(np.uint8))
            ref_img = ref_img.resize(
                (expected_shape[1], expected_shape[0]), Image.LANCZOS,
            )
            ref = np.array(ref_img, dtype=np.float32)
        diff = arr - ref
        distances[label] = float(np.mean(diff * diff))  # MSE
    best = min(distances, key=distances.get)
    other = "anchor" if best == "rudder" else "rudder"
    # Confidence = relative separation.  If best=10 and other=200,
    # separation = (200-10)/200 = 0.95 → very confident.  If best=100
    # and other=110, separation = 0.09 → low confidence.
    if distances[other] < 1e-6:
        confidence = 0.0
    else:
        confidence = max(
            0.0,
            (distances[other] - distances[best]) / distances[other],
        )
    return best, confidence


def is_sailing(frame) -> Optional[bool]:
    """Convenience: True if the ship is currently sailing (anchor icon),
    False if stopped (rudder icon), None if uncertain."""
    state, conf = classify_sail_button(frame)
    if state is None or conf < 0.10:
        return None
    return state == "anchor"
