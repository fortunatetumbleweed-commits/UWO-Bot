"""Wrapper that validates and (if needed) repairs the waypoint a
planner emits, before the reflex loop acts on it.

Design rationale in `docs/agentic_navigation_harness.md` §"Item #2
verification step".

Three checks (cheap → structural):
  1. bounds       — waypoint pixel inside the mini-map
  2. water        — waypoint neighbourhood is mostly water
  3. reachability — straight-line ship→waypoint doesn't cross land

On rejection, a repair pass slides the waypoint toward the ship along
the same direction until it passes all three checks.  Sliding preserves
the *bearing* to the waypoint, so the inner planner's already-computed
steering command remains valid.  If no slide survives, the waypoint is
cleared and `skip_reason='wp_unreachable'` is set — the Watcher (future
layer) decides whether to trigger a big-think escape.

The validator is a *thin wrapper* around any existing PlannerLayer —
today's HybridPlanner, CenterlinePlanner, ShoreHugPlanner all get
validation for free by opting in via the runner's `--validate-planner`
flag.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

from brain.ai_nav.state import NavState, PlannerOutput
from brain.ai_nav.vision_input import VisionFrame

log = logging.getLogger(__name__)


DEFAULT_WATER_NEIGHBORHOOD = 3          # radius, so a 7×7 patch
DEFAULT_WATER_THRESHOLD = 0.70          # fraction of the patch that must be water
DEFAULT_PATH_LAND_TOLERANCE = 0.05      # fraction of pixels on the line
_SLIDE_STOPS = (0.9, 0.75, 0.6, 0.45, 0.3, 0.15)

# When the inner planner's proposal is unusable, we scan a fan of
# bearings around the ship to find real water directions.  16 rays →
# 22.5° angular resolution.  Cap ray-walk at MAX_RAY_PX to bound cost.
_RADIAL_SCAN_BEARINGS = 16
_MAX_RAY_PX = 80
# V11 marks all sprite pixels (including the bot's own ship + sonar
# fan + port markers overlaying the ship) as land.  On live frames the
# ship centre is therefore LAND in the mask, and every ray-walk stops
# at step 1 with reach=0.  Before scanning, force a small disc around
# the ship centre to be water — the ship is *actually* on water and
# the disc is smaller than any real land feature.  See
# memory/project_ship_sonar_punch_holes_in_water_mask.md.
_SHIP_HOLE_RADIUS_PX = 15
# The game renders a group of concentric sonar rings around the ship.
# Different rings brighten on different ticks (radar-sweep animation);
# when a ring is bright enough it flips to "land" in V11's brightness-
# based mask.  A ray from the ship walking outward will therefore hit
# a spurious land band 1-3 pixels thick, then real water resumes.
# Growing the ship-disc doesn't help (rings extend to ~35 px and their
# positions vary tick-to-tick); the fix is to let rays cross short
# land bands and keep walking.
_LAND_TOLERANCE_PX = 5
# Any candidate closer than this to the ship is dropped.  A 7-px
# waypoint gives only ~2° of steering resolution — the sim planner
# spent 200 ticks re-picking slid-down waypoints in that
# "useless reach" zone.  Unlike the earlier version, this is a HARD
# floor: if nothing reaches, noop and let a higher layer intervene.
MIN_REACH_PX = 10.0


@dataclass
class Verdict:
    ok: bool
    reason: str                          # "ok" | "out_of_bounds" | "on_land" | "path_crosses_land"


@dataclass
class Repair:
    wp: Optional[tuple[int, int]]        # None => give up
    mode: str                            # "kept" | "slid_to(0.5)" | "commit_fallback" | "unreachable"


class WaypointValidator:
    def __init__(
        self,
        water_radius: int = DEFAULT_WATER_NEIGHBORHOOD,
        water_threshold: float = DEFAULT_WATER_THRESHOLD,
        path_land_tolerance: float = DEFAULT_PATH_LAND_TOLERANCE,
    ):
        self.water_radius = water_radius
        self.water_threshold = water_threshold
        self.path_land_tolerance = path_land_tolerance

    def validate(
        self,
        wp: tuple[int, int],
        water_mask: np.ndarray,
        ship_px: tuple[int, int],
    ) -> Verdict:
        """wp and ship_px are `(row, col)` — matching the planner's
        `waypoint_px = (int(dest[0]), int(dest[1]))` where trace elements
        are `(y, x)`.  See tick_viewer.py:1611-1612 for the drawing
        convention this must match.
        """
        H, W = water_mask.shape
        y, x = int(wp[0]), int(wp[1])
        if not (0 <= x < W and 0 <= y < H):
            return Verdict(False, "out_of_bounds")
        if not water_mask[y, x]:                # center pixel must be water
            return Verdict(False, "on_land")
        r = self.water_radius
        y0, y1 = max(0, y - r), min(H, y + r + 1)
        x0, x1 = max(0, x - r), min(W, x + r + 1)
        patch = water_mask[y0:y1, x0:x1]
        if patch.size == 0 or patch.mean() < self.water_threshold:
            return Verdict(False, "on_land")
        if self._path_land_fraction(ship_px, wp, water_mask) > \
                self.path_land_tolerance:
            return Verdict(False, "path_crosses_land")
        return Verdict(True, "ok")

    def repair(
        self,
        wp: tuple[int, int],
        water_mask: np.ndarray,
        ship_px: tuple[int, int],
    ) -> Repair:
        sy, sx = ship_px
        wy, wx = int(wp[0]), int(wp[1])
        for t in _SLIDE_STOPS:
            cand = (int(round(sy + t * (wy - sy))),
                    int(round(sx + t * (wx - sx))))
            if self.validate(cand, water_mask, ship_px).ok:
                return Repair(cand, f"slid_to({t:.2f})")
        return Repair(None, "unreachable")

    @staticmethod
    def _path_land_fraction(
        ship_px: tuple[int, int],
        wp: tuple[int, int],
        water_mask: np.ndarray,
    ) -> float:
        H, W = water_mask.shape
        sy, sx = ship_px
        wy, wx = wp
        n = max(abs(wx - sx), abs(wy - sy)) + 1
        xs = np.clip(np.linspace(sx, wx, n).astype(np.int32), 0, W - 1)
        ys = np.clip(np.linspace(sy, wy, n).astype(np.int32), 0, H - 1)
        return float((~water_mask[ys, xs]).mean())


@dataclass
class _Candidate:
    label: str                            # "planner" | "commit@40" ...
    wp: tuple[int, int]                   # (row, col)
    reason: str                           # verdict.reason before repair
    mode: str                             # "ok" | "slid_to(0.6)" | ...
    distance_px: float                    # from ship centre


class ValidatedPlanner:
    """Propose-and-select planner wrapper.

    Each tick we consider multiple waypoint candidates:
      1. the inner planner's proposed waypoint (as today);
      2. one waypoint per distance stop along the current
         `commit_direction` bearing — captures "just go the way the
         mission wants us to" when the inner planner is stuck picking
         a dead-end branch.

    Each candidate is validated + (if needed) repaired via slide.
    Then we DROP any candidate closer than MIN_REACH_PX to the ship
    if any candidate is farther, and pick the highest-scoring survivor.
    Score prefers longer reach + alignment with commit_direction.

    Motivation — 2026-07-16 sim voyage got stuck for 200 ticks at a
    notch in the Nile canvas: inner planner kept picking east (into
    dead-end branch), validator slid the waypoint closer and closer
    (down to 7 px) until it noop'd.  A south candidate along
    commit_direction=180° would have been open water the whole time.
    """
    def __init__(self, inner, validator: Optional[WaypointValidator] = None):
        self._inner = inner
        self._val = validator or WaypointValidator()

    @property
    def name(self) -> str:
        inner_name = getattr(self._inner, "name", type(self._inner).__name__)
        return f"validated:{inner_name}"

    def plan(self, frame: VisionFrame, state: NavState) -> NavState:
        state = self._inner.plan(frame, state)
        po = state.planner_output
        if po is None or state.water_mask is None:
            return state
        H, W = state.water_mask.shape
        ship_px = (H // 2, W // 2)

        # Fast path: inner planner's waypoint is present, validates cleanly
        # (or repairs cleanly to ≥ MIN_REACH_PX), and no reason to look
        # further.  Trust it.  This preserves the current mission's
        # branch-exploration behaviour when the inner planner is doing
        # its job.
        inner_c = None
        if po.waypoint_px is not None:
            inner_c = self._resolve("planner", po.waypoint_px,
                                    state.water_mask, ship_px)
        if inner_c is not None and inner_c.distance_px >= MIN_REACH_PX:
            po.waypoint_px = inner_c.wp
            po.skip_reason = None
            po.wp_note = (None if inner_c.mode == "ok"
                          else f"rejected:{inner_c.reason}→{inner_c.mode}")
            return state

        # Slow path: inner planner is unreachable or slid-to <10 px.  Time
        # to consider commit-direction alternates.  This is the t720-notch
        # rescue: don't spend 200 ticks re-proposing a dead-end when the
        # mission's south bearing is wide open.
        candidates = [inner_c] if inner_c is not None else []
        candidates += self._commit_alternates(state, ship_px)

        if not candidates:
            po.waypoint_px = None
            po.command = None
            po.hold_ms = 0
            po.skip_reason = "wp_unreachable"
            po.wp_note = "no_candidate_valid"
            log.warning("[wp_validate] tick=%d no valid candidate",
                        state.tick)
            return state

        # Hard floor — drop everything shorter than MIN_REACH_PX,
        # even if that leaves nothing.  A 4-px waypoint is worse than
        # a noop because it commits steering to a useless direction.
        candidates = [c for c in candidates if c.distance_px >= MIN_REACH_PX]
        if not candidates:
            po.waypoint_px = None
            po.command = None
            po.hold_ms = 0
            po.skip_reason = "wp_unreachable"
            po.wp_note = "all_candidates_too_short"
            log.warning("[wp_validate] tick=%d no candidate reaches "
                        "MIN_REACH_PX=%d", state.tick, int(MIN_REACH_PX))
            return state

        winner = max(candidates, key=lambda c: self._score(c, state))

        # Detect whether we swapped the inner planner's proposal
        original_wp = po.waypoint_px
        po.waypoint_px = winner.wp
        po.skip_reason = None
        if winner.label != "planner":
            self._recompute_command(po, state, ship_px, winner.wp)
            po.wp_note = (f"swapped→{winner.label}({winner.mode},"
                          f"d={winner.distance_px:.0f}px)")
            log.warning("[wp_validate] tick=%d swapped inner-wp %s → "
                        "%s(%s,d=%.0f)  reason: %d candidate(s), "
                        "MIN_REACH filter applied",
                        state.tick, original_wp, winner.label,
                        winner.mode, winner.distance_px, len(candidates))
        elif winner.mode != "ok":
            po.wp_note = f"rejected:{winner.reason}→{winner.mode}"
        return state

    # ── helpers ────────────────────────────────────────────────────

    def _commit_alternates(
        self, state: NavState, ship_px: tuple[int, int],
    ) -> list[_Candidate]:
        """Radial scan — sample N bearings around the ship, walk each
        ray outward on the water mask to find its max reach, return
        one candidate per bearing (only those with reach ≥ MIN_REACH_PX).

        Score later picks the best via alignment with commit_direction.

        This replaces earlier "only sample commit direction" logic —
        that missed obvious escapes (SW, SE) when the exact commit
        bearing happened to be blocked at close range.
        """
        H, W = state.water_mask.shape
        sy, sx = ship_px
        # Force a small disc around the ship to be water — V11 marks
        # sprite pixels as land, but the ship itself is *on* water.
        yy, xx = np.mgrid[:H, :W]
        ship_disc = ((yy - sy) ** 2 + (xx - sx) ** 2) <= _SHIP_HOLE_RADIUS_PX ** 2
        water_mask = state.water_mask | ship_disc
        out: list[_Candidate] = []
        for i in range(_RADIAL_SCAN_BEARINGS):
            bearing = i * (360.0 / _RADIAL_SCAN_BEARINGS)
            r = math.radians(bearing)
            dr = -math.cos(r)               # image y-axis grows south
            dc = math.sin(r)
            reach = 0
            consecutive_land = 0
            for step in range(1, _MAX_RAY_PX + 1):
                py = int(round(sy + step * dr))
                px = int(round(sx + step * dc))
                if not (0 <= px < W and 0 <= py < H):
                    break
                if water_mask[py, px]:
                    reach = step                    # last-seen water
                    consecutive_land = 0
                else:
                    consecutive_land += 1
                    if consecutive_land > _LAND_TOLERANCE_PX:
                        break                        # too many in a row = real land
            if reach < MIN_REACH_PX:
                continue
            wp = (int(round(sy + reach * dr)),
                  int(round(sx + reach * dc)))
            # Validate the endpoint properly — ray-walk stopped 1 px
            # before land, but neighbourhood-water rule may reject.
            verdict = self._val.validate(wp, water_mask, ship_px)
            if not verdict.ok:
                # Try a slightly shorter waypoint (halfway up the ray).
                wp = (int(round(sy + reach // 2 * dr)),
                      int(round(sx + reach // 2 * dc)))
                verdict = self._val.validate(wp, water_mask, ship_px)
                if not verdict.ok:
                    continue
                actual_reach = reach // 2
            else:
                actual_reach = reach
            if actual_reach < MIN_REACH_PX:
                continue
            out.append(_Candidate(
                label=f"ray@{bearing:.0f}°",
                wp=wp,
                reason="ok",
                mode="ok",
                distance_px=float(actual_reach),
            ))
        return out

    def _resolve(
        self, label: str, wp: tuple[int, int],
        water_mask: np.ndarray, ship_px: tuple[int, int],
    ) -> Optional[_Candidate]:
        """Validate wp; if invalid, try slide-repair; return _Candidate or None."""
        verdict = self._val.validate(wp, water_mask, ship_px)
        if verdict.ok:
            dist = math.hypot(wp[0] - ship_px[0], wp[1] - ship_px[1])
            return _Candidate(label, wp, "ok", "ok", dist)
        repair = self._val.repair(wp, water_mask, ship_px)
        if repair.wp is None:
            return None
        dist = math.hypot(repair.wp[0] - ship_px[0],
                          repair.wp[1] - ship_px[1])
        return _Candidate(label, repair.wp, verdict.reason,
                          repair.mode, dist)

    @staticmethod
    def _score(c: _Candidate, state: NavState) -> float:
        """Larger score = preferred.  Rewards long reach + alignment
        with commit_direction."""
        s = c.distance_px
        if state.commit_direction is not None:
            H, W = state.water_mask.shape
            sy, sx = H // 2, W // 2
            dr = c.wp[0] - sy
            dc = c.wp[1] - sx
            # image dr,dc → compass bearing.  compass 0° = north (dr<0),
            # 90° = east (dc>0).
            bearing_deg = (math.degrees(math.atan2(dc, -dr)) + 360) % 360
            commit = state.commit_direction.bearing_deg
            misalign = abs(((bearing_deg - commit + 540) % 360) - 180)
            s -= misalign * 0.5             # 1 px penalty per 2° of misalignment
        return s

    @staticmethod
    def _recompute_command(
        po: PlannerOutput, state: NavState,
        ship_px: tuple[int, int], wp: tuple[int, int],
    ) -> None:
        """When we swap the inner planner's waypoint for a commit
        alternate, the inner planner's command/hold_ms were computed
        for the OLD waypoint direction — must recompute for the new one.
        """
        from brain.ai_nav.layers.planner import (
            _compute_hold_ms, _signed_delta,
        )
        if state.heading is None:
            po.command = None
            po.hold_ms = 0
            return
        dr = wp[0] - ship_px[0]
        dc = wp[1] - ship_px[1]
        wp_bearing = (math.degrees(math.atan2(dc, -dr)) + 360) % 360
        delta = _signed_delta(wp_bearing, state.heading.bearing_deg)
        hold_ms = _compute_hold_ms(delta, state.speed_kt)
        if hold_ms > 0:
            po.command = "hold_right" if delta > 0 else "hold_left"
            po.hold_ms = hold_ms
        else:
            po.command = None
            po.hold_ms = 0
