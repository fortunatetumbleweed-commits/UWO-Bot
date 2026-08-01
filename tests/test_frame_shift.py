"""Unit tests for vision.frame_shift.measure_frame_shift."""
from __future__ import annotations

import numpy as np
import pytest

from vision.frame_shift import measure_frame_shift, shore_edge_mask


def _synthetic_river(H=100, W=200, shore_col=80, width=60):
    """A vertical stripe of water, `width` px wide, starting at `shore_col`."""
    mask = np.zeros((H, W), dtype=bool)
    mask[:, shore_col:shore_col + width] = True
    return mask


def _synthetic_bay(H=100, W=200, shore_col=80, width=60,
                    bay_row=40, bay_depth=20):
    """Vertical stripe of water with a rectangular bay cutting into
    the LEFT shore.  The bay makes the shore geometry non-uniform,
    giving translation signal even along the river axis."""
    mask = _synthetic_river(H, W, shore_col, width)
    # Bay = extra water to the left of the main channel
    mask[bay_row:bay_row + 10, shore_col - bay_depth:shore_col] = True
    return mask


def test_zero_shift_when_masks_identical():
    m = _synthetic_bay()
    dy, dx, conf = measure_frame_shift(m, m)
    assert (dy, dx) == (0, 0)
    assert conf > 0.5


def test_pure_horizontal_shift():
    m = _synthetic_bay()
    shifted = np.roll(m, shift=(0, 3), axis=(0, 1))
    dy, dx, conf = measure_frame_shift(m, shifted)
    # prev[r,c] appears at curr[r+dy, c+dx].  Rolled by (0, 3) → dx=3.
    assert (dy, dx) == (0, 3)
    assert conf > 0.5


def test_pure_vertical_shift_with_bay():
    m = _synthetic_bay()
    shifted = np.roll(m, shift=(-5, 0), axis=(0, 1))
    dy, dx, conf = measure_frame_shift(m, shifted)
    assert (dy, dx) == (-5, 0)
    assert conf > 0.5


def test_shape_mismatch_returns_zero():
    a = _synthetic_river(H=100, W=200)
    b = _synthetic_river(H=100, W=180)
    dy, dx, conf = measure_frame_shift(a, b)
    assert (dy, dx, conf) == (0, 0, 0.0)


def test_empty_shore_returns_zero():
    m = np.zeros((100, 200), dtype=bool)
    dy, dx, conf = measure_frame_shift(m, m)
    assert (dy, dx, conf) == (0, 0, 0.0)


def test_full_water_low_confidence():
    """All-water frame has only a frame-border shore ring (no real
    shore info).  Registration may return arbitrary shift; caller
    should check confidence."""
    m = np.ones((100, 200), dtype=bool)
    dy, dx, conf = measure_frame_shift(m, m)
    # No real signal → conf is 0 (unreliable).  Shift value irrelevant.
    assert conf < 0.1


def test_sprite_exclusion_does_not_bias():
    """Sprite pixels are removed from both shore masks — no bias.
    Ship sprite sits at center; it's the same in both frames so the
    exclusion shouldn't change the recovered shift."""
    m1 = _synthetic_bay()
    m2 = np.roll(m1, shift=(-3, -2), axis=(0, 1))
    sprite_center = np.zeros_like(m1)
    sprite_center[45:55, 95:105] = True     # 10x10 sprite at center
    dy, dx, conf = measure_frame_shift(m1, m2, sprite_center, sprite_center)
    assert (dy, dx) == (-3, -2)
    assert conf > 0.4


def test_shore_edge_mask_produces_thin_ring():
    m = _synthetic_river()
    shore = shore_edge_mask(m)
    # For a 60-wide × 100-tall stripe, shore = left col + right col +
    # top row + bottom row - 4 corners = 100 + 100 + 60 + 60 - 4 = 316
    assert shore.sum() == pytest.approx(316, rel=0.05)
