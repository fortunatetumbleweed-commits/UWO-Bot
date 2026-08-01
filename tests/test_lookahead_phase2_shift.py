"""Phase 2 shift-integration tests for LookaheadTactical.

Covers `docs/pixel_continuity_design.md` §5: turning-point dests
are tracked by pixel offset from ship centre, shifted each tick by
`state.frame_shift_px` (when confidence ≥ SHIFT_CONF_MIN).

Skipped 2026-07-25: SEARCH tracker + shift-integrated dest tracking
retired for the bank-trace picker (P12/P13 fix).  maybe_consult now
runs _pick_new_dest every tick, so the stored pixel offset reflects
the fresh pick — not shift-integrated re-anchoring.  Tests kept
skipped rather than deleted so the historical intent is documented.
"""
from __future__ import annotations

import time
import numpy as np
import pytest
from PIL import Image

# SEARCH tracker restored 2026-07-25 with corner-split detection,
# but the turning-point-mode branch (which these tests exercise via
# _current_dest_reason='turning_point') was NOT restored — the
# bank-tracer picker doesn't produce turning_point dests.  The
# shift-integrated pixel offset still works (it's independent of
# the tracker branch), but these tests seed turning_point reasons
# that no longer take a special path.
pytestmark = pytest.mark.skip(
    reason="turning_point mode not restored with bank-tracer picker "
           "(P12/P13 rework 2026-07-25).  Shift-integration still runs; "
           "tests need reason='frame_edge' + a mask the tracker can "
           "hold, not just all-water.",
)

from brain.ai_nav.layers.tactical import LookaheadTactical
from brain.ai_nav.state import NavState
from brain.ai_nav.vision_input import VisionFrame


def _make_frame(tick: int = 100) -> VisionFrame:
    return VisionFrame(raw=Image.new("RGB", (2400, 1080)), tick=tick,
                       wall_ts=time.time())


def _make_state(tick: int, lat: float, lon: float,
                frame_shift_px=None) -> NavState:
    s = NavState(tick=tick, lat=lat, lon=lon)
    s.water_mask = np.ones((193, 405), dtype=bool)
    s.frame_shift_px = frame_shift_px
    return s


def test_high_conf_shift_updates_turning_point_offset():
    """A high-conf frame shift moves the stored pixel offset by (dy, dx)."""
    tac = LookaheadTactical(hug_side="port", goal_bearing_deg=180.0)
    tac._current_dest = (7.77, 33.75)
    tac._current_dest_reason = "turning_point"
    tac._current_dest_px_offset = (60, 20)     # far enough not to trip arrival

    state = _make_state(100, 8.00, 33.60, frame_shift_px=(-5, -3, 0.85))
    tac.maybe_consult(_make_frame(100), state)

    assert tac._current_dest_px_offset == (55, 17)


def test_low_conf_shift_holds_offset_unchanged():
    """When registration confidence is below SHIFT_CONF_MIN, hold offset."""
    tac = LookaheadTactical(hug_side="port", goal_bearing_deg=180.0)
    tac._current_dest = (7.77, 33.75)
    tac._current_dest_reason = "turning_point"
    tac._current_dest_px_offset = (60, 20)

    state = _make_state(101, 8.01, 33.61, frame_shift_px=(-8, -2, 0.05))
    tac.maybe_consult(_make_frame(101), state)

    assert tac._current_dest_px_offset == (60, 20)


def test_search_mode_tracks_pixel_offset_phase3():
    """Phase 3: SEARCH-mode dests now also track pixel offset — the
    tracker finds the nearest frame-edge water-run midpoint each tick
    (in pixel space) and updates the offset."""
    tac = LookaheadTactical(hug_side="port", goal_bearing_deg=180.0)
    tac._current_dest = (7.77, 33.75)
    tac._current_dest_reason = "frame_edge"    # SEARCH
    tac._current_dest_px_offset = None

    state = _make_state(100, 8.00, 33.60, frame_shift_px=(-5, -3, 0.85))
    tac.maybe_consult(_make_frame(100), state)

    # All-water mask → tracker finds a frame-edge midpoint on some side.
    # Just assert that offset is populated (non-None) under Phase 3.
    assert tac._current_dest_px_offset is not None


def test_missing_frame_shift_leaves_offset_unchanged():
    """No frame_shift_px this tick (e.g., first tick) → hold offset."""
    tac = LookaheadTactical(hug_side="port", goal_bearing_deg=180.0)
    # Dest far enough from ship that _at_turning_point doesn't trigger
    # arrival + walker refire (which would reset offset).
    tac._current_dest = (7.20, 33.75)      # ~80 px SE of ship
    tac._current_dest_reason = "narrow_choke"
    tac._current_dest_px_offset = (80, 15)

    state = _make_state(1, 8.00, 33.60, frame_shift_px=None)
    tac.maybe_consult(_make_frame(1), state)

    assert tac._current_dest_px_offset == (80, 15)


def test_shifted_offset_reconstructs_dest_latlon():
    """After shift, _current_dest lat/lon is recomputed from new offset
    + ship position."""
    tac = LookaheadTactical(hug_side="port", goal_bearing_deg=180.0)
    tac._current_dest = (7.77, 33.75)
    tac._current_dest_reason = "turning_point"
    tac._current_dest_px_offset = (60, 20)

    state = _make_state(100, 8.00, 33.60, frame_shift_px=(-5, -3, 0.85))
    tac.maybe_consult(_make_frame(100), state)

    # After shift: offset (55, 17).  Reconstruct: dlat = -55/100 = -0.55,
    # dlon = 17/100 = 0.17.  Dest = (8.00 - 0.55, 33.60 + 0.17).
    assert tac._current_dest[0] == 7.45
    assert abs(tac._current_dest[1] - 33.77) < 0.001
