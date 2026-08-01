"""Scenario dataclass + runner for tactical regression tests."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable
import json
import math
import time

import numpy as np


@dataclass
class TickOutcome:
    """What the tactical layer produced at one tick."""
    tick: int
    wp_offset: Optional[tuple[int, int]]         # (row, col) offset from ship
    wp_edge: Optional[str]                        # top/bot/left/right
    wp_bearing: Optional[float]                   # compass bearing from ship
    reason: str
    polyline_endpoint: Optional[tuple[int, int]]  # (y, x) absolute in mask
    trace_start: Optional[tuple[int, int]]        # polyline[0] absolute
    commit_deg: Optional[float]
    # L3 reflex waypoint the planner emits (recorded every tick).
    reflex_wp: Optional[tuple[int, int]] = None      # (row, col) absolute px
    reflex_bearing: Optional[float] = None           # compass bearing from ship
    # Hug-side invariant: True when the commit puts the coast on the WRONG
    # (far) side — hug side is open water and the far side has the bank.
    hug_violation: bool = False

    @property
    def d_bot(self) -> Optional[int]:
        if self.polyline_endpoint is None:
            return None
        H = 187  # standard minimap H
        return H - 1 - self.polyline_endpoint[0]


@dataclass
class Scenario:
    """A tactical regression scenario.

    Runs against a specific session's tick images with an override
    on initial commit_deg (bootstrap the tactical layer to a known
    desired direction).  Lat/lon and frame_shift come from the
    recorded trace file — they're consistent with the images.
    """
    name: str
    session: str
    start_tick: int
    end_tick: int
    initial_commit_deg: float
    hug_side: str = "port"
    # Optional pre-seed of the sticky-anchor state (bypasses the
    # cold-start bootstrap at the first tick).  Useful when the
    # scenario tests specifically a mid-voyage transition rather
    # than the initial WP acquisition.
    initial_wp_offset: Optional[tuple[int, int]] = None   # (row, col) offset from ship
    initial_wp_edge: Optional[str] = None                  # top/bot/left/right

    # Assertions (all optional — set to check the specific
    # invariants this scenario cares about)
    max_flips_90deg: Optional[int] = None
    max_bootstrap_count: Optional[int] = None
    expected_dominant_edge: Optional[str] = None    # >50% of ticks
    expected_wp_bearing_range: Optional[tuple[float, float]] = None  # min, max deg
    # Reflex-WP check (L3 planner).  The runner always records the reflex
    # WP the planner emits each tick; max_reflex_misaligned caps the
    # number of ticks whose reflex bearing is > reflex_misalign_thr_deg
    # off the commit direction — i.e. the reflex pointing away from the
    # goal (the open-water regression class the tactical anchor asserts
    # can't see).
    max_reflex_misaligned: Optional[int] = None
    reflex_misalign_thr_deg: float = 90.0
    # Commit-stability check.  max_commit_rotations caps the number of
    # ticks whose commit direction rotates more than commit_rotation_thr_deg
    # from the previous tick — a large single-tick commit swing is the
    # signature of the anchor leaping (e.g. dead-end tip → far corner)
    # while the ship still has to traverse the pocket.
    max_commit_rotations: Optional[int] = None
    commit_rotation_thr_deg: float = 60.0
    # Hug-side invariant: cap the number of ticks whose commit puts the
    # coast on the WRONG side (land on starboard for a port-hug).
    max_hug_violations: Optional[int] = None
    # Cap the number of ticks the hug-side rule OVERRODE the anchor
    # (hug_repick / hold(hug_side)).  In a channel this must be 0 — the
    # anchor owns direction there and the hug-side rule must not fire.
    max_hug_overrides: Optional[int] = None
    # Cap the angular SPREAD of the commit over the window (smallest arc
    # containing all commit bearings).  A ship navigating a passage should
    # hold a consistent through-direction; a large spread means it's
    # oscillating / can't commit (e.g. going W and E in the same window).
    max_commit_spread_deg: Optional[float] = None
    # Known-failing case (redesign target).  When True the test expects the
    # assertions to FAIL (xfail); if they start passing the test flags an
    # XPASS so we remove the marker.
    xfail: bool = False
    xfail_reason: str = ""
    note: str = ""


# Use the SAME heading layer production runs (ShipPartsHeading, the region-
# parts U-Net) so the scenarios validate the real deployed path.  Load once
# and share across scenarios in a run.
_HEADING_LAYER = None


def _get_heading_layer():
    global _HEADING_LAYER
    if _HEADING_LAYER is None:
        from brain.ai_nav.layers.heading import ShipPartsHeading
        _HEADING_LAYER = ShipPartsHeading()
    return _HEADING_LAYER


_KM_PER_NM = 1.852
_KM_PER_DEG_LAT = 111.0


def _advance_latlon(lat, lon, heading_deg, speed_kt, dt_hours):
    """Dead-reckon next (lat, lon) — mirrors run_ai_nav_live._advance_latlon."""
    km = speed_kt * dt_hours * _KM_PER_NM
    r = math.radians(heading_deg)
    dlat = (km * math.cos(r)) / _KM_PER_DEG_LAT
    dlon = (km * math.sin(r)) / (_KM_PER_DEG_LAT * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def run_scenario(sc: Scenario, sessions_root: Optional[Path] = None
                 ) -> list[TickOutcome]:
    """Run the scenario as a live-run-style perception replay.

    Drives the real `AiNavPipeline` over the scenario's frame range via a
    `FileVisionSource`.  The water mask, heading (ensemble CNN),
    frame-shift motion and dead-reckoned position are all PERCEIVED from
    the PNG frames — nothing is injected from the recorded `trace.jsonl`
    except the first-frame position, used solely as the dead-reckon anchor
    (the "start of the trip").  This mirrors what the live bot computes
    from these frames, so a passing scenario reflects real behaviour, not
    a replay of the recorded (possibly buggy) trajectory.

    Seeds: first-frame position (anchor) + `initial_commit_deg` (the
    section's intended travel direction → tactical goal bearing + initial
    commit).  The ship's heading is perceived, not seeded.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from brain.ai_nav.pipeline import AiNavPipeline, PipelineConfig
    from brain.ai_nav.vision_input import FileVisionSource
    from brain.ai_nav.layers.hug_path_planner import HugPathPlanner
    from brain.ai_nav.layers.tactical import LookaheadTactical
    from brain.ai_nav.state import NavState, CommitDirection

    if sessions_root is None:
        sessions_root = Path(__file__).resolve().parents[2] / "data" / "sessions"
    session_dir = sessions_root / sc.session

    # Anchor = recorded first-frame position (the "starting place").  The
    # ONLY value taken from the recording; every later position is
    # dead-reckoned from perceived heading.
    anchor_lat = anchor_lon = None
    for line in open(session_dir / "trace.jsonl"):
        d = json.loads(line)
        if d['tick'] == sc.start_tick:
            anchor_lat, anchor_lon = d.get('lat'), d.get('lon')
            break

    tac = LookaheadTactical(hug_side=sc.hug_side,
                            goal_bearing_deg=sc.initial_commit_deg)
    cfg = PipelineConfig(
        heading=_get_heading_layer(),
        planner=HugPathPlanner(),
        tactical=tac,
    )
    pipe = AiNavPipeline(source=FileVisionSource(session_dir), config=cfg)

    state = NavState(tick=sc.start_tick - 1, lat=anchor_lat, lon=anchor_lon)
    state.commit_direction = CommitDirection(
        bearing_deg=sc.initial_commit_deg, reason="scenario_seed",
        set_at_tick=sc.start_tick - 1)
    # Optional mid-voyage anchor seed (some scenarios start already
    # committed to an edge and can't settle from cold in a few ticks).
    if sc.initial_wp_offset is not None:
        tac._current_dest_px_offset = sc.initial_wp_offset
        tac._prev_wp_edge = sc.initial_wp_edge

    outcomes: list[TickOutcome] = []
    for _ in range(sc.start_tick, sc.end_tick + 1):
        state = pipe.tick(state)
        H, W = (state.water_mask.shape
                if state.water_mask is not None else (187, 393))

        # Dead-reckon position from perceived heading (HUD OCR can't read
        # a minimap-only crop — matches run_ai_nav_live replay).
        h = state.heading
        if (h is not None and h.confidence > 0.05
                and state.lat is not None and state.lon is not None):
            state.lat, state.lon = _advance_latlon(
                state.lat, state.lon, h.bearing_deg, 8.0, 1.0 / 3600.0)

        # Hug-side invariant check: does the commit keep the coast on the
        # hug side?  (land on starboard for a port-hug ⇒ violation)
        hug_violation = False
        if (state.water_mask is not None
                and tac._last_commit_deg is not None):
            mh, mw = state.water_mask.shape
            _sr, _sc = mh // 2, mw // 2
            _hug_land = LookaheadTactical._hug_side_has_land(
                state.water_mask, _sr, _sc, tac._last_commit_deg, sc.hug_side)
            _far_open = LookaheadTactical._far_side_open(
                state.water_mask, _sr, _sc, tac._last_commit_deg, sc.hug_side)
            hug_violation = (not _hug_land) and (not _far_open)

        wp = tac._current_dest_px_offset
        wp_bearing = None
        if wp is not None:
            wp_bearing = (math.degrees(math.atan2(wp[1], -wp[0])) + 360.0) % 360.0
        pl = tac._prev_polyline_yx
        reflex_wp = reflex_bearing = None
        po = state.planner_output
        if po is not None and po.waypoint_px is not None:
            ry, rx = po.waypoint_px
            sr, sc_ = H // 2, W // 2
            reflex_wp = (int(ry), int(rx))
            reflex_bearing = (math.degrees(
                math.atan2(rx - sc_, -(ry - sr))) + 360.0) % 360.0

        outcomes.append(TickOutcome(
            tick=state.tick,
            wp_offset=tuple(wp) if wp else None,
            wp_edge=tac._prev_wp_edge,
            wp_bearing=wp_bearing,
            reason=tac._current_dest_reason or "",
            polyline_endpoint=tuple(pl[-1]) if pl else None,
            trace_start=tuple(pl[0]) if pl else None,
            commit_deg=tac._last_commit_deg,
            reflex_wp=reflex_wp,
            reflex_bearing=reflex_bearing,
            hug_violation=hug_violation,
        ))

    return outcomes


def assess(sc: Scenario, outcomes: list[TickOutcome]) -> tuple[bool, list[str]]:
    """Check scenario assertions.  Returns (ok, list of failure messages)."""
    failures = []

    # Count 90° WP flips
    flips = 0
    prev_bearing = None
    for o in outcomes:
        if o.wp_bearing is None:
            continue
        if prev_bearing is not None:
            diff = abs(((o.wp_bearing - prev_bearing + 540.0) % 360.0) - 180.0)
            if diff >= 90:
                flips += 1
        prev_bearing = o.wp_bearing
    if sc.max_flips_90deg is not None and flips > sc.max_flips_90deg:
        failures.append(f"WP flips ≥90°: {flips} > max {sc.max_flips_90deg}")

    # Count bootstrap/new_anchor
    boot = sum(1 for o in outcomes
               if 'bootstrap' in o.reason or 'new_anchor' in o.reason)
    if sc.max_bootstrap_count is not None and boot > sc.max_bootstrap_count:
        failures.append(f"bootstrap/new_anchor count: {boot} > max {sc.max_bootstrap_count}")

    # Dominant edge
    if sc.expected_dominant_edge is not None:
        from collections import Counter
        edge_counts = Counter(o.wp_edge for o in outcomes if o.wp_edge)
        if edge_counts:
            dominant = edge_counts.most_common(1)[0][0]
            if dominant != sc.expected_dominant_edge:
                failures.append(
                    f"dominant edge: {dominant} ({edge_counts[dominant]}/{len(outcomes)}) "
                    f"≠ expected {sc.expected_dominant_edge}"
                )

    # WP bearing range (majority in range)
    if sc.expected_wp_bearing_range is not None:
        lo, hi = sc.expected_wp_bearing_range
        in_range = 0
        for o in outcomes:
            if o.wp_bearing is None:
                continue
            # Bearing "in range" handles wrap: if lo < hi, in [lo, hi];
            # if lo > hi (wraps 0°), in [lo, 360) ∪ [0, hi]
            if lo < hi:
                ok = lo <= o.wp_bearing <= hi
            else:
                ok = o.wp_bearing >= lo or o.wp_bearing <= hi
            if ok:
                in_range += 1
        frac = in_range / max(1, len(outcomes))
        if frac < 0.5:
            failures.append(
                f"WP bearing in range [{lo}, {hi}]: {in_range}/{len(outcomes)} "
                f"({frac*100:.0f}%) < 50%"
            )

    # Reflex WP goalward check: count ticks whose reflex bearing is more
    # than reflex_misalign_thr_deg off the commit direction (reflex
    # pointing away from the goal — the open-water regression class).
    if sc.max_reflex_misaligned is not None:
        thr = sc.reflex_misalign_thr_deg
        misaligned = []
        for o in outcomes:
            if o.reflex_bearing is None or o.commit_deg is None:
                continue
            diff = abs(((o.reflex_bearing - o.commit_deg + 540.0) % 360.0) - 180.0)
            if diff > thr:
                misaligned.append((o.tick, round(o.reflex_bearing),
                                   round(o.commit_deg), round(diff)))
        if len(misaligned) > sc.max_reflex_misaligned:
            detail = ", ".join(f"t{t}(reflex={r}°,commit={c}°,off={dd}°)"
                               for t, r, c, dd in misaligned[:6])
            failures.append(
                f"reflex WP >{thr:.0f}° off commit: {len(misaligned)} ticks "
                f"> max {sc.max_reflex_misaligned}  [{detail}]"
            )

    # Commit-stability check: count ticks whose commit rotated more than
    # the threshold from the previous tick (anchor-leap signature).
    if sc.max_commit_rotations is not None:
        thr = sc.commit_rotation_thr_deg
        rotations = []
        prev_c = None
        for o in outcomes:
            if o.commit_deg is None:
                continue
            if prev_c is not None:
                diff = abs(((o.commit_deg - prev_c + 540.0) % 360.0) - 180.0)
                if diff > thr:
                    rotations.append((o.tick, round(prev_c),
                                      round(o.commit_deg), round(diff)))
            prev_c = o.commit_deg
        if len(rotations) > sc.max_commit_rotations:
            detail = ", ".join(f"t{t}({a}°→{b}°,Δ{dd}°)"
                               for t, a, b, dd in rotations[:6])
            failures.append(
                f"commit rotation >{thr:.0f}°: {len(rotations)} ticks "
                f"> max {sc.max_commit_rotations}  [{detail}]"
            )

    # Hug-side invariant check: cap ticks whose commit puts the coast on
    # the wrong side (land on starboard for a port-hug).
    if sc.max_hug_violations is not None:
        viol = [o.tick for o in outcomes if o.hug_violation]
        if len(viol) > sc.max_hug_violations:
            failures.append(
                f"hug-side violations (coast on wrong side): {len(viol)} "
                f"> max {sc.max_hug_violations}  [ticks {viol[:8]}]"
            )

    # Hug-side override check: cap ticks where the hug-side rule overrode
    # the anchor (must be 0 in a channel — the anchor owns direction there).
    if sc.max_hug_overrides is not None:
        ov = [o.tick for o in outcomes
              if 'hug_repick' in (o.reason or '')
              or 'hold(hug_side' in (o.reason or '')]
        if len(ov) > sc.max_hug_overrides:
            failures.append(
                f"hug-side overrides in a channel: {len(ov)} "
                f"> max {sc.max_hug_overrides}  [ticks {ov[:8]}]"
            )

    # Commit-spread check: the smallest arc containing all commit bearings.
    if sc.max_commit_spread_deg is not None:
        angs = sorted(o.commit_deg % 360.0 for o in outcomes
                      if o.commit_deg is not None)
        if len(angs) >= 2:
            gaps = [angs[i + 1] - angs[i] for i in range(len(angs) - 1)]
            gaps.append(angs[0] + 360.0 - angs[-1])
            spread = 360.0 - max(gaps)
            if spread > sc.max_commit_spread_deg:
                failures.append(
                    f"commit spread {spread:.0f}° > max "
                    f"{sc.max_commit_spread_deg:.0f}° (direction not held)"
                )

    return (len(failures) == 0, failures)
