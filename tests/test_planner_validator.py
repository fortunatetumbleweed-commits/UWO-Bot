"""Tests for the WaypointValidator + ValidatedPlanner wrapper.

Covers the three checks (bounds / water / reachability) individually,
the slide-repair pass, and the wrapper's behavior around an inner
planner.  Synthetic water masks — no game frames needed.

Convention: waypoint_px and ship_px are BOTH (row, col) — matching
the planner's `waypoint_px = (int(dest[0]), int(dest[1]))` where dest
is drawn from `path_pts=[(y, x) for y, x in trace]`.  See
tick_viewer.py:1611-1612 for how this is drawn.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
from PIL import Image

from brain.ai_nav.layers.planner_validator import (
    ValidatedPlanner, WaypointValidator,
)
from brain.ai_nav.state import (
    CommitDirection, NavState, PlannerOutput,
)
from brain.ai_nav.vision_input import VisionFrame


# ── mask helpers ─────────────────────────────────────────────────────

def all_water(H=190, W=400):
    return np.ones((H, W), dtype=bool)


def half_land_below(y_thresh, H=190, W=400):
    m = np.ones((H, W), dtype=bool)
    m[y_thresh:, :] = False
    return m


def _blank_frame():
    return VisionFrame(raw=Image.new("RGB", (2400, 1080)), tick=0)


def _state_with_mask(mask, wp=None, tick=1, commit_deg=None):
    st = NavState(tick=tick, water_mask=mask)
    if commit_deg is not None:
        st.commit_direction = CommitDirection(
            bearing_deg=commit_deg, reason="test", set_at_tick=0,
        )
    if wp is not None:
        st.planner_output = PlannerOutput(waypoint_px=wp, command="hold_right",
                                          hold_ms=200)
    return st


# ── validator: individual checks ─────────────────────────────────────

def test_validator_accepts_center_water_waypoint():
    v = WaypointValidator()
    mask = all_water()
    ship = (95, 200)                              # (row, col)
    verdict = v.validate((95, 250), mask, ship)   # (row, col)
    assert verdict.ok
    assert verdict.reason == "ok"


def test_validator_rejects_out_of_bounds_below():
    # The real-world t324 pattern: row=220 in a 190-tall mask.
    v = WaypointValidator()
    mask = all_water(H=190, W=400)
    verdict = v.validate((220, 147), mask, (95, 200))
    assert not verdict.ok
    assert verdict.reason == "out_of_bounds"


def test_validator_rejects_out_of_bounds_right():
    v = WaypointValidator()
    verdict = v.validate((95, 500), all_water(), (95, 200))
    assert not verdict.ok
    assert verdict.reason == "out_of_bounds"


def test_validator_rejects_on_land():
    # Mask: top half water, bottom half land.  wp in land region.
    v = WaypointValidator()
    mask = half_land_below(100)
    verdict = v.validate((150, 200), mask, (50, 200))
    assert not verdict.ok
    assert verdict.reason == "on_land"


def test_validator_rejects_center_land_even_if_neighborhood_water():
    """The tick-966 case that broke real voyages: center pixel is a
    tiny land island; 48/49 neighbourhood pixels are water so the
    old ratio-only rule passed.  Center-pixel gate rejects it."""
    v = WaypointValidator()
    mask = all_water()
    mask[95, 250] = False                         # single pixel of land
    verdict = v.validate((95, 250), mask, (95, 200))
    assert not verdict.ok
    assert verdict.reason == "on_land"


def test_validator_rejects_path_crosses_land():
    # Water at ship, water at wp, but a land band in between.
    v = WaypointValidator()
    mask = np.ones((190, 400), dtype=bool)
    mask[80:120, :] = False        # horizontal land band
    verdict = v.validate((170, 200), mask, (30, 200))
    assert not verdict.ok
    assert verdict.reason == "path_crosses_land"


def test_validator_tolerates_two_bad_pixels_in_neighborhood():
    # 7×7 patch is 49 px; two bad pixels off-center = 47/49 = 0.96
    # >> 0.70 threshold.  Center is water so it passes.
    v = WaypointValidator()
    mask = all_water()
    mask[93, 248] = False
    mask[97, 253] = False
    assert v.validate((95, 250), mask, (95, 200)).ok


# ── repair: slide behavior ───────────────────────────────────────────

def test_repair_slides_out_of_bounds_wp_into_map():
    # wp is off the bottom edge; slide toward ship (mid-map) recovers.
    v = WaypointValidator()
    mask = all_water(H=190, W=400)
    ship = (95, 200)
    repair = v.repair((220, 147), mask, ship)
    assert repair.wp is not None
    y, x = repair.wp
    assert 0 <= y < 190 and 0 <= x < 400
    assert repair.mode.startswith("slid_to")


def test_repair_returns_unreachable_when_ship_stuck():
    v = WaypointValidator()
    mask = np.zeros((190, 400), dtype=bool)   # all land
    repair = v.repair((100, 250), mask, (95, 200))
    assert repair.wp is None
    assert repair.mode == "unreachable"


# ── wrapper: end-to-end ──────────────────────────────────────────────

def _fake_inner(waypoint_px):
    """Mock PlannerLayer that always returns a fixed waypoint."""
    def plan(frame, state):
        state.planner_output = PlannerOutput(
            waypoint_px=waypoint_px,
            command="hold_right",
            hold_ms=200,
        )
        return state
    return SimpleNamespace(plan=plan, name="fake")


def test_wrapper_passes_valid_waypoint_through():
    inner = _fake_inner((95, 250))                # (row, col), on water
    wrap = ValidatedPlanner(inner)
    state = _state_with_mask(all_water())
    state = wrap.plan(_blank_frame(), state)
    assert state.planner_output.waypoint_px == (95, 250)
    assert state.planner_output.wp_note is None


def test_wrapper_repairs_off_crop_waypoint():
    # t324-style: waypoint below the map (row=220 in a 190-tall mask).
    inner = _fake_inner((220, 147))
    wrap = ValidatedPlanner(inner)
    state = _state_with_mask(all_water())
    state = wrap.plan(_blank_frame(), state)
    y, x = state.planner_output.waypoint_px
    assert 0 <= y < 190 and 0 <= x < 400
    assert state.planner_output.wp_note is not None
    assert "out_of_bounds" in state.planner_output.wp_note
    assert "slid_to" in state.planner_output.wp_note


def test_wrapper_noops_when_unreachable():
    inner = _fake_inner((100, 250))
    wrap = ValidatedPlanner(inner)
    state = _state_with_mask(np.zeros((190, 400), dtype=bool))
    state = wrap.plan(_blank_frame(), state)
    assert state.planner_output.waypoint_px is None
    assert state.planner_output.command is None
    assert state.planner_output.skip_reason == "wp_unreachable"


def test_wrapper_passes_when_no_mask():
    inner = _fake_inner((220, 147))
    wrap = ValidatedPlanner(inner)
    state = NavState(tick=1)
    state = wrap.plan(_blank_frame(), state)
    assert state.planner_output.waypoint_px == (220, 147)
    assert state.planner_output.wp_note is None


# ── propose-and-select ───────────────────────────────────────────────

def test_wrapper_prefers_commit_alternate_when_planner_wp_is_tiny():
    """The t720-notch pathology: inner planner keeps proposing a
    waypoint deep in an unreachable branch, validator slides it down
    to <10 px.  A commit-direction alternate in the open water should
    win because it's longer than 10 px."""
    # Mask: open water everywhere EXCEPT a narrow band at cols 210-405 is
    # land past col 210 (so the inner planner's east-going wp fails).
    # Commit-direction 180° (south) points into open water — should win.
    mask = np.ones((190, 400), dtype=bool)
    mask[:, 210:] = False               # land to the east
    inner = _fake_inner((95, 350))      # planner picks deep east → unreachable
    wrap = ValidatedPlanner(inner)
    state = _state_with_mask(mask, commit_deg=180.0)
    state.heading = _mk_heading(180.0)
    state = wrap.plan(_blank_frame(), state)
    y, x = state.planner_output.waypoint_px
    # Winner should be south of ship (row > 95, near col 200)
    assert y > 100 and abs(x - 200) < 20, \
        f"expected south waypoint, got ({y}, {x})"
    assert state.planner_output.wp_note is not None
    assert "swapped→ray@" in state.planner_output.wp_note


def test_wrapper_keeps_planner_wp_when_it_wins_alignment():
    """When the planner's waypoint is long AND matches commit direction,
    don't swap it out for a commit alternate."""
    mask = all_water()
    inner = _fake_inner((150, 200))     # 55 px south, along commit
    wrap = ValidatedPlanner(inner)
    state = _state_with_mask(mask, commit_deg=180.0)
    state.heading = _mk_heading(180.0)
    state = wrap.plan(_blank_frame(), state)
    assert state.planner_output.waypoint_px == (150, 200)
    assert state.planner_output.wp_note is None       # no swap


def test_wrapper_noops_when_no_candidate_valid():
    """Ship in a fully-enclosed land pocket: every candidate fails."""
    inner = _fake_inner((150, 200))
    wrap = ValidatedPlanner(inner)
    mask = np.zeros((190, 400), dtype=bool)
    state = _state_with_mask(mask, commit_deg=180.0)
    state.heading = _mk_heading(180.0)
    state = wrap.plan(_blank_frame(), state)
    assert state.planner_output.waypoint_px is None
    assert state.planner_output.skip_reason == "wp_unreachable"
    assert state.planner_output.wp_note == "no_candidate_valid"


def _mk_heading(deg):
    from brain.ai_nav.layers.heading import Heading
    return Heading(bearing_deg=deg, confidence=1.0, source="test")
