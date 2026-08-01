"""Regression tests for the yellow-wedge bow detector in
`vision/minimap_navigation_view._ship_heading`.

The wedge detector replaces a PCA + pixel-count heuristic that
intermittently flipped 180° on noise.  These tests pin two specific
behaviours:

  1. When a small yellow component sits at the bow of the green hull,
     the heading points toward the wedge — not 180° opposite.
  2. When no wedge is present, the function falls back to PCA so it
     still returns a value (the old behaviour).

Synthetic masks are used so the test is reproducible and doesn't
depend on any captured frames.
"""
import math

import numpy as np

from vision.minimap_navigation_view import (
    _ship_heading, _ship_heading_wedge, _ship_heading_pca,
    ShipHeadingDetector, HeadingReading,
)


def _make_ship(heading_compass_deg: float, with_wedge: bool = True):
    """Construct a green-hull mask + yellow-wedge mask for a ship
    pointing in `heading_compass_deg` (0=N, 90=E).  Image is 100×100
    with the ship at (50, 50)."""
    H = W = 100
    cx, cy = 50, 50
    green = np.zeros((H, W), dtype=bool)
    yellow = np.zeros((H, W), dtype=bool)

    # Hull: ellipse 8 long × 4 wide, rotated by heading.
    # In compass: 0° = north (up) = -y direction in image.
    rad = math.radians(heading_compass_deg)
    bow_dx, bow_dy = math.sin(rad), -math.cos(rad)
    # Stern direction = -bow direction.
    for t in np.linspace(-5, 5, 21):
        for r in np.linspace(-2, 2, 9):
            x = cx + t * bow_dx + r * (-bow_dy)
            y = cy + t * bow_dy + r * bow_dx
            ix, iy = int(round(x)), int(round(y))
            if 0 <= ix < W and 0 <= iy < H:
                green[iy, ix] = True

    if with_wedge:
        # Wedge: small triangular cluster ~5 px ahead of hull centre.
        wedge_dist = 6
        wx0 = cx + wedge_dist * bow_dx
        wy0 = cy + wedge_dist * bow_dy
        for dx in range(-2, 3):
            for dy in range(-2, 3):
                if abs(dx) + abs(dy) <= 2:
                    ix, iy = int(round(wx0 + dx)), int(round(wy0 + dy))
                    if 0 <= ix < W and 0 <= iy < H:
                        yellow[iy, ix] = True

    return green, yellow


def _heading_close(a: float, b: float, tol: float = 25.0) -> bool:
    diff = abs(((a - b + 540) % 360) - 180)
    return diff <= tol


def test_wedge_resolves_bow_north():
    g, y = _make_ship(0.0)
    h = _ship_heading_wedge(g, y)
    assert h is not None
    assert _heading_close(h, 0.0), f"expected ~0°, got {h:.1f}"


def test_wedge_resolves_bow_east():
    g, y = _make_ship(90.0)
    h = _ship_heading_wedge(g, y)
    assert h is not None
    assert _heading_close(h, 90.0), f"expected ~90°, got {h:.1f}"


def test_wedge_resolves_bow_south():
    g, y = _make_ship(180.0)
    h = _ship_heading_wedge(g, y)
    assert h is not None
    assert _heading_close(h, 180.0), f"expected ~180°, got {h:.1f}"


def test_wedge_resolves_bow_west():
    g, y = _make_ship(270.0)
    h = _ship_heading_wedge(g, y)
    assert h is not None
    assert _heading_close(h, 270.0), f"expected ~270°, got {h:.1f}"


def test_wedge_ignores_distant_yellow():
    """A yellow blob far from the ship (e.g. port anchor) must not
    pull the heading toward it."""
    g, y = _make_ship(0.0)  # ship pointing north, wedge at (~50, ~44)
    # Add a big yellow blob in the bottom-right (port anchor-like).
    y[80:88, 80:88] = True
    h = _ship_heading_wedge(g, y)
    assert h is not None
    assert _heading_close(h, 0.0), f"expected ~0°, got {h:.1f}"


def test_wedge_ignores_npc_adjacent_to_side_of_hull():
    """An NPC sitting right next to the ship's *side* (perpendicular
    to the bow direction) must not be picked as the wedge.  The wedge
    sits along the ship's long axis (bow-stern line), so off-axis
    yellow blobs adjacent to the hull are NPCs and must be filtered
    out via the axis-projection guard.

    This reproduces the live tick 9 failure on 2026-05-29_114939
    where a port-side NPC caused a 180° heading flip.
    """
    # Ship pointing north (heading 0°).  Long axis is vertical (along y).
    g, y = _make_ship(0.0, with_wedge=True)
    # Add a small NPC-sized yellow blob touching the LEFT side of the
    # hull (perpendicular to the bow direction).  ~3×3 pixels at (44, 50).
    for dx in range(-2, 3):
        for dy in range(-2, 3):
            if abs(dx) + abs(dy) <= 2:
                ix, iy = 43 + dx, 50 + dy
                if 0 <= ix < y.shape[1] and 0 <= iy < y.shape[0]:
                    y[iy, ix] = True
    h = _ship_heading_wedge(g, y)
    assert h is not None
    # Must still point north (toward the real wedge), not west
    # (toward the side NPC).
    assert _heading_close(h, 0.0), \
        f"expected ~0° (real wedge), got {h:.1f}° (side NPC fooled it)"


def test_wedge_ignores_large_yellow():
    """An NPC sprite (large yellow component) right next to the ship
    must not be picked as the wedge — its component is too big."""
    g, y = _make_ship(0.0)
    # Big yellow square south of the ship (stern side).
    y[58:78, 40:60] = True
    h = _ship_heading_wedge(g, y)
    assert h is not None
    assert _heading_close(h, 0.0), \
        f"expected ~0° (NPC ignored), got {h:.1f}"


def test_wedge_returns_none_with_no_yellow():
    g, y = _make_ship(90.0, with_wedge=False)
    h = _ship_heading_wedge(g, y)
    assert h is None


def test_combined_heading_falls_back_to_pca():
    """When no wedge is visible, the public `_ship_heading` must fall
    back to PCA rather than returning None."""
    g, y = _make_ship(45.0, with_wedge=False)
    h = _ship_heading(g, y)
    assert h is not None
    # PCA on a long hull won't 180°-flip on the simple synthetic ship,
    # so it should return roughly 45° or 225°.  Either is acceptable
    # — the point is that *some* heading comes back.


def test_combined_heading_uses_wedge_when_available():
    """When the wedge is available, public `_ship_heading` returns the
    wedge result (not PCA's potentially-flipped one)."""
    g, y = _make_ship(90.0, with_wedge=True)
    h_combined = _ship_heading(g, y)
    h_wedge = _ship_heading_wedge(g, y)
    assert h_combined == h_wedge
    assert _heading_close(h_combined, 90.0)


def test_combined_heading_no_yellow_arg():
    """Calling without `yellow` (backward compat) uses PCA only."""
    g, _ = _make_ship(0.0)
    h = _ship_heading(g)   # yellow defaults to None
    assert h is not None   # falls through to PCA


# ── Sail-pair strategy ──
# When the bow wedge degrades (live t=31 of 2026-05-29_205124), the
# detector falls back to the two yellow sail blobs on the ship's hull.
# Their angular bisector points at the bow regardless of wedge state.
# Empirical separation is 68° ± 7° (see tools/measure_sail_separation.py).

def _make_ship_with_sails(heading_compass_deg: float):
    """Larger hull + two yellow sail blobs at ±34° from bow, no bow wedge.

    The hull is sized closer to a real frame (~7 long × 4 wide ellipse,
    ~150 px) so the two sail blobs at distance 12 from centroid still
    touch the hull's 2-px dilation.  The synthetic ship in `_make_ship`
    is too small for this — a real captured frame ship is ~400-500 px.
    """
    H = W = 100
    cx, cy = 50, 50
    green = np.zeros((H, W), dtype=bool)
    yellow = np.zeros((H, W), dtype=bool)

    rad = math.radians(heading_compass_deg)
    bow_dx, bow_dy = math.sin(rad), -math.cos(rad)
    # Larger hull ellipse — 14 long × 8 wide.
    for t in np.linspace(-7, 7, 29):
        for r in np.linspace(-4, 4, 17):
            if (t / 7) ** 2 + (r / 4) ** 2 > 1.0:   # outside ellipse
                continue
            x = cx + t * bow_dx + r * (-bow_dy)
            y = cy + t * bow_dy + r * bow_dx
            ix, iy = int(round(x)), int(round(y))
            if 0 <= ix < W and 0 <= iy < H:
                green[iy, ix] = True

    # Two sails at heading ± 34°, distance 9 from centroid, ~29 px each
    # (radius-3 disk).  Distance 9 keeps the blobs disjoint AND lets their
    # edges reach within the 2-iteration cross-dilation of the hull.
    for offset_deg in (-34.0, +34.0):
        sail_rad = math.radians(heading_compass_deg + offset_deg)
        sx0 = cx + 9 * math.sin(sail_rad)
        sy0 = cy - 9 * math.cos(sail_rad)
        for dx in range(-3, 4):
            for dy in range(-3, 4):
                if dx * dx + dy * dy <= 9:    # ~29 px disk
                    ix, iy = int(round(sx0 + dx)), int(round(sy0 + dy))
                    if 0 <= ix < W and 0 <= iy < H:
                        yellow[iy, ix] = True
    return green, yellow


def test_sail_pair_resolves_bow_north():
    g, y = _make_ship_with_sails(0.0)
    r = ShipHeadingDetector().detect(g, y)
    assert r is not None
    assert r.strategy == "sail_pair"
    assert _heading_close(r.bearing_deg, 0.0), \
        f"expected ~0°, got {r.bearing_deg:.1f}°"
    assert r.confidence >= 0.7   # high-confidence strategy


def test_sail_pair_resolves_bow_east():
    g, y = _make_ship_with_sails(90.0)
    r = ShipHeadingDetector().detect(g, y)
    assert r is not None
    assert r.strategy == "sail_pair"
    assert _heading_close(r.bearing_deg, 90.0)


def test_sail_pair_does_not_fire_when_sails_too_far_apart():
    """Two yellow blobs at 110° apart are not a valid sail pair (way
    outside the 68° ± 7° tolerance) — strategy must return None and
    the detector should fall through to other strategies."""
    H = W = 100
    cx, cy = 50, 50
    green = np.zeros((H, W), dtype=bool)
    yellow = np.zeros((H, W), dtype=bool)
    # Vertical hull.
    for t in np.linspace(-5, 5, 21):
        for r_off in np.linspace(-2, 2, 9):
            ix, iy = int(round(cx + r_off)), int(round(cy + t))
            if 0 <= ix < W and 0 <= iy < H:
                green[iy, ix] = True
    # Two yellow blobs at ±55° from bow (110° apart, outside tolerance).
    for offset_deg in (-55.0, +55.0):
        rad = math.radians(0.0 + offset_deg)
        bx0 = cx + 6 * math.sin(rad)
        by0 = cy - 6 * math.cos(rad)
        for dx in range(-3, 4):
            for dy in range(-3, 4):
                if dx * dx + dy * dy <= 9:
                    ix, iy = int(round(bx0 + dx)), int(round(by0 + dy))
                    if 0 <= ix < W and 0 <= iy < H:
                        yellow[iy, ix] = True
    det = ShipHeadingDetector()
    gys, gxs = np.where(green)
    cx_f, cy_f = float(gxs.mean()), float(gys.mean())
    principal = det._principal_axis(gxs, gys, cx_f, cy_f)
    cands = det._collect_yellow_candidates(green, yellow, cx_f, cy_f, principal)
    assert det._sail_pair(cands) is None   # 110° != 68° ± 7° → no fire


def test_heading_reading_dataclass_fields():
    """HeadingReading exposes bearing_deg, strategy, confidence."""
    g, y = _make_ship_with_sails(45.0)
    r = ShipHeadingDetector().detect(g, y)
    assert isinstance(r, HeadingReading)
    assert 0.0 <= r.bearing_deg < 360.0
    assert r.strategy in ("sail_pair", "bow_wedge", "pca_fallback")
    assert 0.0 <= r.confidence <= 1.0
