"""Phase 3 tests: SEARCH tracker in pixel space + fork-emergence trigger.

See `docs/pixel_continuity_design.md` §5-§6.  Phase 3 migrates the
SEARCH-mode continuity tracker from lat/lon reprojection to pixel
offset, and adds a fork-emergence trigger that fires the walker with
hug-side when a new frame-edge water run appears (t358 fork mispick
fix from ai_nav_2026-07-22T20-33-50).
"""
from __future__ import annotations

import time
import numpy as np
import pytest
from PIL import Image

from brain.ai_nav.layers.tactical import LookaheadTactical
from brain.ai_nav.state import NavState
from brain.ai_nav.vision_input import VisionFrame


def _make_frame(tick: int = 100) -> VisionFrame:
    return VisionFrame(raw=Image.new("RGB", (2400, 1080)), tick=tick,
                       wall_ts=time.time())


def _mask_with_top_edge_water(H=193, W=405, top_water_cols=(50, 150)):
    """Simple mask: water only on top edge (one run from col a to b)."""
    m = np.zeros((H, W), dtype=bool)
    a, b = top_water_cols
    m[0:80, a:b] = True     # a vertical stripe reaching from top edge
    return m


def _mask_with_two_top_edge_runs(H=193, W=405):
    """Fork emergence: two separate top-edge water runs."""
    m = np.zeros((H, W), dtype=bool)
    m[0:80, 50:100] = True   # west branch
    m[0:80, 250:340] = True  # east branch
    return m


def test_count_frame_edge_runs_single_run():
    tac = LookaheadTactical(hug_side="port")
    mask = _mask_with_top_edge_water()
    assert tac._count_frame_edge_runs(mask) == 1


def test_count_frame_edge_runs_two_runs():
    tac = LookaheadTactical(hug_side="port")
    mask = _mask_with_two_top_edge_runs()
    assert tac._count_frame_edge_runs(mask) == 2


def test_fork_emergence_first_tick_no_prev():
    """First tick has no prev count → cannot detect emergence."""
    tac = LookaheadTactical(hug_side="port")
    state = NavState(tick=1, lat=8.0, lon=33.0,
                     water_mask=_mask_with_top_edge_water())
    assert tac._detect_fork_emergence(state) is False
    # Prev count now set for next call.
    assert tac._prev_frame_edge_run_count == 1


def test_fork_emergence_detected_when_count_increases():
    tac = LookaheadTactical(hug_side="port")
    # Seed prev count with single run
    single = NavState(tick=1, lat=8.0, lon=33.0,
                      water_mask=_mask_with_top_edge_water())
    tac._detect_fork_emergence(single)   # sets prev = 1
    # Next tick: two runs → fork emerged
    double = NavState(tick=2, lat=8.0, lon=33.0,
                      water_mask=_mask_with_two_top_edge_runs())
    assert tac._detect_fork_emergence(double) is True
    assert tac._prev_frame_edge_run_count == 2


def test_fork_emergence_not_when_count_stable_or_decreases():
    tac = LookaheadTactical(hug_side="port")
    # Seed with 2 runs
    tac._detect_fork_emergence(NavState(
        tick=1, lat=8.0, lon=33.0,
        water_mask=_mask_with_two_top_edge_runs(),
    ))
    # Next tick still 2 runs → not emergence
    assert tac._detect_fork_emergence(NavState(
        tick=2, lat=8.0, lon=33.0,
        water_mask=_mask_with_two_top_edge_runs(),
    )) is False
    # Then drop to 1 → not emergence (a branch closed off)
    assert tac._detect_fork_emergence(NavState(
        tick=3, lat=8.0, lon=33.0,
        water_mask=_mask_with_top_edge_water(),
    )) is False


def test_nearest_edge_run_midpoint_px_returns_pixel():
    """New pixel-space helper returns (row, col) of nearest edge midpoint."""
    tac = LookaheadTactical(hug_side="port")
    state = NavState(tick=1, lat=8.0, lon=33.0,
                     water_mask=_mask_with_top_edge_water(top_water_cols=(50, 150)))
    # Anchor near the top edge, close to the midpoint col ~100
    result = tac._nearest_edge_run_midpoint_px(state, 5, 100, radius_px=80)
    assert result is not None
    row, col = result
    assert row == 0             # top edge
    assert 95 <= col <= 105     # midpoint of (50, 149)


def test_search_tracker_updates_pixel_offset_phase3():
    """After tracker runs, _current_dest_px_offset reflects the new
    edge-midpoint pixel offset from ship centre."""
    tac = LookaheadTactical(hug_side="port")
    tac._current_dest = (8.9, 33.0)
    tac._current_dest_reason = "frame_edge"
    tac._current_dest_px_offset = (-90, 0)
    state = NavState(tick=100, lat=8.0, lon=33.0,
                     water_mask=_mask_with_top_edge_water(top_water_cols=(150, 250)))
    frame = _make_frame(100)
    tac.maybe_consult(frame, state)
    assert tac._current_dest_px_offset is not None
    row_off, col_off = tac._current_dest_px_offset
    assert row_off == -96
    assert -10 <= col_off <= 10
