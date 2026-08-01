"""Reflex planner that follows the tactical layer's walked water path.

Replaces `HybridPlanner` + `CenterlinePlanner` for voyages using
`LookaheadTactical`.  Design 2026-07-20 after visualization of the
sim voyage showed the old planner's skeleton walk crossing land at
mid-Nile junctions.

Algorithm per tick:

  1. Read `state.tactical_walked_path` — list of (lat, lon) points
     along water from ship to the tactical destination.  Empty ⇒
     fall through to a radial-scan fallback (same as ValidatedPlanner).
  2. Convert each path point back to a mini-map pixel using the
     ship's current (lat, lon) and PX_PER_DEG.
  3. Walk the path FROM the far end TOWARD the ship.  For each point,
     test whether the straight line from ship to that point is
     entirely on water (Bresenham + water_mask lookup).
  4. The FIRST such point (farthest from ship, still reachable in a
     straight line) becomes `waypoint_px`.
  5. Compute steering command as usual (bearing delta → hold_left/right
     with `_compute_hold_ms`).

Why walk backward: we want the FARTHEST reachable point (more useful
for steering — gives clearer heading target).  The near-end of the
path is always reachable by construction; the far-end may not be if
the path curves around a bend.  Walking backward stops at the first
"visible from ship" point.

Fallback: when no path is set, or when NO path point has straight
water reach (unusual — path should have at least the first point
right next to ship), we fall through to a radial-scan around
`commit_direction` similar to ValidatedPlanner's slow path.
"""
from __future__ import annotations

import logging
import math
from typing import Optional

import numpy as np

from brain.ai_nav.state import NavState, PlannerOutput
from brain.ai_nav.vision_input import VisionFrame

log = logging.getLogger(__name__)


DEFAULT_PX_PER_DEG = 100.0
MIN_REACH_PX = 10.0
_RADIAL_BEARINGS = 16
_MAX_RAY_PX = 80
_SHIP_HOLE_RADIUS_PX = 15
_LAND_TOLERANCE_PX = 5
# Bank clearance for the reflex-WP line.  Keep the ship→WP straight line
# IDEAL_CLEARANCE_PX from land where possible, never below MIN_CLEARANCE_PX
# by choice.  A pinch below MIN is almost always a perception artifact
# (the game has no channels that narrow), so we still steer through it
# rather than stall — but we log it.  The WP is also positioned
# min(IDEAL, width/2) from the hugging bank (10px off it in a wide
# channel, centred when narrower than 2×IDEAL) so the ship keeps margin
# from the hugged bank without crowding the far bank.
IDEAL_CLEARANCE_PX = 10.0
MIN_CLEARANCE_PX = 5.0
_CLEARANCE_SKIP_NEAR_PX = 15   # ignore near-ship pixels (hull/sonar holes)
# Escape-route check: a reflex WP is a TRAP if, once reached, the route
# from it toward the tactical anchor dead-ends.  Require a contiguous
# water run from the WP toward the anchor of at least ESCAPE_MIN_PX
# (session 2026-07-28T18-27-37 t94: a 10px-clear, reachable WP had only
# 19px of water toward the tactical dest before land → bounce).
_ESCAPE_RAY_PX = 35
_ESCAPE_MIN_PX = 25
# Dead-end HOLD coupling: while the tactical anchor is holding (the ship is
# committed to reaching a dead-end tip), constrain reflex candidates to
# within _HOLD_ALIGN_DEG of the (pixel-space, frame-shift-maintained) held-
# anchor direction, so the reflex can't flip to the far shore of a wide
# lake pocket (session 2026-07-28T18-27-37 t422/t431 fishtail).
_HOLD_ALIGN_DEG = 70.0
# When holding and the tip is within this range and directly reachable,
# the reflex IS the tip — don't pick a lookahead that sidesteps/overshoots
# it (session 2026-07-28T21-01-31 t396: reflex jumped WSW past the held
# anchor → the ship swung away and bounced).
_HOLD_ANCHOR_CAP_PX = 60.0
# Wrong-direction check: reject a reflex wp whose bearing from ship is
# more than this many degrees away from the ship→tactical bearing.  120°
# means "reflex is going strongly opposite to tactical" — beyond a
# generous curved-route allowance.
_WRONG_DIR_THR_DEG = 120.0
# _HULL_HALF_PX converts a ray's ship-CENTRE distance to a hull-EDGE
# clearance (the ship marker is ~26px wide).
_HULL_HALF_PX = 13.0
# Reactive collision avoidance (VFH-lite, forward-arc, avoidance-ONLY —
# it nudges steering, it does NOT pick the waypoint; the planner still does).
# Sample clearance across the FORWARD arc only and, when a bank falls inside
# the ship's speed-scaled reaction distance ahead, nudge toward the openest
# forward bearing.  Abeam/rear banks are IGNORED — the ship is moving away
# from those, they are not hazards.  (t100 in ai_nav_2026-07-29T09-08-11:
# a ±90° beam avoider steered toward port off a starboard-REAR bank the ship
# was already leaving, driving it INTO the bank it was approaching.)
# Speed-aware: the danger band scales with per-tick travel (expected_shift_px)
# so a fast ship reacts earlier — a 10px gap it crosses in one tick is still
# a hazard.
_AVOID_ARC_RAYS_DEG = (-60.0, -40.0, -20.0, 0.0, 20.0, 40.0, 60.0)
# Reaction distance ≈ ONE tick of travel: this is a last-resort collision
# nudge for banks the ship is about to hit NEXT tick, NOT a continuous
# centring force.  Route-level decisions (which way to go, incl. U-turn
# reversal) stay in the planner; only imminent-collision avoidance lives
# here.  (At 2 ticks it fired ~56% of the time in narrow water — too much;
# 1.25 halves that and the weave while still catching the t18 swipe.)
_AVOID_LOOKAHEAD_TICKS = 1.25
_AVOID_BASE_MARGIN_PX = 10.0    # danger band floor for slow ships
_AVOID_MAX_BIAS_DEG = 25.0
# Repulsion weight is NONLINEAR (squared) so only genuinely-close banks drive
# the bias — a moderate bank at 0.7×danger contributes ~0.5, at 0.3×danger
# only ~0.1 — keeping this a collision guard, not a constant centring force
# in narrow water.  A deadzone drops sub-threshold nudges (near-field
# perception is jittery: heading ±7° + ~5px ship-centre offset).  NOTE: the
# durable fix for bank-hugging is route-level polyline centring, not this
# reactive guard — see project_polyline_centerline_offset_todo.
_AVOID_WEIGHT_POWER = 2.0
_AVOID_DEADZONE_DEG = 5.0
# Head-on escape: a bank in the near-bow (|rel| ≤ _AVOID_HEADON_ARC_DEG) is a
# DIRECT collision threat, but it contributes little/no LATERAL repulsion — a
# dead-ahead bank (rel=0) has no side, so it contributes exactly zero and the
# ship rams it (t38 in ai_nav_2026-07-29T11-39-16: 6px dead-ahead → 0° bias →
# speed 9.2→0).  So react to near-bow banks in a LARGER band (a head-on hit is
# worse than a glancing one) and steer decisively toward the openest forward
# bearing, on top of the lateral repulsion.
_AVOID_HEADON_TICKS = 2.0
_AVOID_HEADON_ARC_DEG = 20.0
# U-turn rotation-direction resolution.  For a near-reversal (|delta| ≥
# _UTURN_DELTA_DEG) both CW and CCW reach the same target heading, so the
# shortest-angle sign is an arbitrary tie-break.  Pick the rotation that
# sweeps the bow into the MORE OPEN beam (a right turn sweeps the bow
# through starboard, a left turn through port) so the reactive avoider
# never fights the planner into a stall.  Only override when the other
# side is clearer by more than _UTURN_CLR_MARGIN_PX (avoid flip-flop on
# near-equal clearances).  See t277 in session ai_nav_2026-07-28T22-18-45.
_UTURN_DELTA_DEG = 135.0
_UTURN_CLR_MARGIN_PX = 8.0
# Adaptive scaling: baseline constants were tuned on 8.5-kt sailing
# (~7 px/tick shift).  Fast ships at 27+ kt push per-tick motion to
# ~30 px, at which point _MAX_RAY_PX=80 covers <3 ticks of reach and
# MIN_REACH_PX=10 lands the wp behind the ship's forward motion.
# Scale by state.expected_shift_px.  scaled = max(baseline, k × exp).
# Conservative multipliers: at 27 kt (shift ≈ 30 px) these give
# min_reach=30 and max_ray=90 — modest expansion over baseline 10/80.
# Larger multipliers (voyage 2026-07-23T14:58 tried 1.5×/10×) pushed
# max_ray to 300 px, letting the radial scan pick land-adjacent
# waypoints that ships couldn't reach without shore hits.
_SCALE_MIN_REACH = 1.0   # WP ≥ 1 tick ahead
_SCALE_MAX_RAY   = 3     # radial scan covers ~3 ticks


def _scaled_min_reach(state) -> float:
    exp = getattr(state, "expected_shift_px", None) or 0.0
    return max(MIN_REACH_PX, _SCALE_MIN_REACH * exp)


def _scaled_max_ray(state) -> int:
    exp = getattr(state, "expected_shift_px", None) or 0.0
    return max(_MAX_RAY_PX, int(round(_SCALE_MAX_RAY * exp)))


class HugPathPlanner:
    """Reflex layer that follows tactical's walked water path."""
    name = "hug_path"

    def __init__(self, px_per_deg: float = DEFAULT_PX_PER_DEG,
                 use_pid: bool = False):
        self.px_per_deg = px_per_deg
        # Closed-loop PID heading control (opt-in).  When off, steering
        # uses the legacy feed-forward `_compute_hold_ms`.  The PID
        # inverts the calibrated nonlinear hold→turn curve — it must NOT
        # be used against the sim's idealised 120°/s turn model.
        if use_pid:
            from brain.ai_nav.steering_pid import HeadingPID
            self.pid = HeadingPID()
        else:
            self.pid = None

    def plan(self, frame: VisionFrame, state: NavState) -> NavState:
        if state.water_mask is None or state.heading is None:
            state.planner_output = PlannerOutput(
                skip_reason="no_mask_or_heading", primitive=self.name,
            )
            return state
        H, W = state.water_mask.shape
        ship_row, ship_col = H // 2, W // 2

        # 1. Try tactical walked path first
        wp = self._pick_from_path(state, ship_row, ship_col)
        wp_source = "path" if wp is not None else None

        # 2a. Dead-end HOLD fallback: if no path candidate is reachable while
        # the ship is committed to a dead-end tip, aim AT the tip — never let
        # the (hold-unaware) radial fallback pick a ray away from it.  Closes
        # the gap that flipped the reflex WP ~180° off the tip at t68 in
        # ai_nav_2026-07-29T11-39-16 (bad lake mask → path picker returns
        # None mid-hold → radial chose the long open ray behind the ship).
        if wp is None:
            wp = self._hold_aim_at_tip(state, ship_row, ship_col)
            if wp is not None:
                wp_source = "hold_tip"

        # 2b. Fallback: radial scan around commit_direction
        if wp is None:
            wp = self._radial_scan_fallback(state, ship_row, ship_col)
            if wp is not None:
                wp_source = "radial"

        if wp is None:
            state.planner_output = PlannerOutput(
                skip_reason="no_reachable_waypoint",
                wp_note="no_path_no_ray", primitive=self.name,
            )
            return state

        # 3. Wrong-direction guard.  If the picked wp is going > 120°
        # opposite of ship→tactical, log + attempt an aligned re-pick.
        # See t215 in session ai_nav_2026-07-22T08-28-30 — reflex went
        # NW while tactical dest was NE (~180° opposite).
        wp_note = None
        misalign = self._wp_misalignment_deg(wp, state, ship_row, ship_col)
        if misalign is not None and misalign > _WRONG_DIR_THR_DEG:
            log.warning("[hug_path] wp=%s (src=%s) is %.0f° opposite to "
                        "tactical %s — trying aligned re-pick",
                        wp, wp_source, misalign, state.tactical_dest_latlon)
            aligned = self._pick_aligned_from_path(
                state, ship_row, ship_col, wp_source=wp_source,
            )
            if aligned is not None:
                new_mis = self._wp_misalignment_deg(
                    aligned, state, ship_row, ship_col,
                )
                log.warning("[hug_path] aligned pick %s (%.0f° misalign)",
                            aligned, (new_mis if new_mis is not None else 0.0))
                wp = aligned
                wp_note = f"wrong_dir_repick(mis={misalign:.0f}→{new_mis:.0f})"
            else:
                wp_note = f"wrong_dir_kept(mis={misalign:.0f})"

        # 4. Route decision (planner-owned): choose the turn direction to
        # reach the WP.  For a reversal, prefer the rotation that sweeps the
        # bow through open water even if it's a bigger turn — a navigable
        # route beats a shorter one that drives into a bank.  One beam-
        # clearance read feeds both this and the steering layer's reactive
        # bias, so they can't oppose each other.  See t277 in session
        # ai_nav_2026-07-28T22-18-45.
        heading = state.heading.bearing_deg
        base_delta = self._wp_signed_delta(wp, ship_row, ship_col, heading)
        port_clr, star_clr = self._beam_clearances(
            state.water_mask, ship_row, ship_col, heading)
        route_delta, route_note = self._resolve_reversal_route(
            base_delta, port_clr, star_clr)
        if route_note:
            wp_note = route_note if wp_note is None else f"{wp_note};{route_note}"

        self._emit_command(state, wp, ship_row, ship_col,
                            route_delta, wp_note=wp_note)
        return state

    # ── helpers ────────────────────────────────────────────────────

    def _pick_from_path(self, state, ship_row, ship_col):
        """Pick the FARTHEST straight-line-water-reachable path point
        that makes progress toward the tactical dest.

        Rule (2026-07-22): a path point is a valid reflex-wp candidate
        only if its displacement from ship has POSITIVE projection on
        the ship→tactical vector (i.e., moving toward tactical, not
        away).

        Why ship→tactical rather than ship-heading:
          - **Bounce-invariant.** After a bounce the ship's heading can
            swing 90-180° in one tick.  The tactical dest is a world
            lat/lon anchored in the environment, so ship→tactical stays
            stable through the bounce and the reflex still picks a
            useful wp.
          - **Reversal-aware.** When the mission decides to trace back
            (e.g., dead-end reached, DeadEndMemoryMission rejects the
            revisit), tactical flips to point back the way we came.
            The filter automatically re-admits the previously-rejected
            "behind" path points because they now project positively
            onto the *new* ship→tactical.

        Walk direction: FORWARD (ship-end → dest-end).  We track the
        farthest still-reachable point and return it — never picking
        a "behind-tactical" point.  Diagnosed at t27/t33 in session
        ai_nav_2026-07-22T14-17-47 where the old reversed-walk logic
        picked NORTH-of-ship points as tactical pointed south, because
        stale head-of-path skeleton entries survived to the filter.
        """
        path = state.tactical_walked_path
        if not path or state.lat is None or state.lon is None:
            return None
        H, W = state.water_mask.shape
        # Ship → tactical direction (image pixel space).  If tactical
        # is undefined or on top of ship, skip the toward-tactical
        # filter (rare — only at first tick before tactical initialises).
        tact_dy = tact_dx = 0.0
        tact_norm = 0.0
        if state.tactical_dest_latlon is not None:
            tlat, tlon = state.tactical_dest_latlon
            tact_dy = -(tlat - state.lat) * self.px_per_deg
            tact_dx = (tlon - state.lon) * self.px_per_deg
            tact_norm = math.hypot(tact_dy, tact_dx)
        have_tact = tact_norm > 1.0

        # Dead-end HOLD coupling: while the tactical anchor is holding, the
        # ship is committed to reaching it — constrain reflex candidates to
        # be well-aligned with the (pixel-space) held-anchor direction so the
        # reflex can't flip to the far shore of a wide lake pocket.
        holding = False
        anchor_uy = anchor_ux = 0.0
        if (state.commit_direction is not None
                and "hold(pocket_ahead" in (state.commit_direction.reason or "")
                and state.tactical_dest_px_offset is not None):
            _ay, _ax = state.tactical_dest_px_offset
            _an = math.hypot(_ay, _ax)
            if _an > 1.0:
                holding = True
                anchor_uy, anchor_ux = _ay / _an, _ax / _an
        _hold_cos = math.cos(math.radians(_HOLD_ALIGN_DEG))

        # HOLD: the ship is committed to the dead-end tip — aim straight AT it
        # (last water pixel along the line to it), ALWAYS.  Previously this was
        # gated on tip ≤60px AND a fully-clear line; when the line was blocked
        # (pocket wall) or the tip far, it fell through to the path pick, whose
        # hold filter still allows the WP up to ±70° off the tip — so the reflex
        # jumped ~67-74° away mid-hold (t409, ai_nav_2026-07-29T15-42-01; same
        # class as t68).  During a hold there is no valid "lookahead" other than
        # the tip, so commit to it unconditionally.
        if holding:
            _ay, _ax = state.tactical_dest_px_offset
            _ar = max(0, min(H - 1, int(round(ship_row + _ay))))
            _ac = max(0, min(W - 1, int(round(ship_col + _ax))))
            return self._last_water_on_line(
                state.water_mask, ship_row, ship_col, _ar, _ac)

        # Direct-to-anchor: when the straight line to the tactical anchor is
        # open water, aim STRAIGHT at it instead of the farthest winding-
        # contour point.  The reflex normally follows the bank-tracer contour
        # (the walked path) — correct in a channel, but in an OPEN LAKE the
        # contour hugs the shoreline, so the farthest reachable contour point
        # veers into the shore while clear water sits straight toward the goal
        # (t32-38 ai_nav_2026-07-29T11-39-16: 109px open toward the south
        # anchor, reflex went 50-70° east into land → ship rammed the shore
        # t39).  Guarded by _straight_line_all_water: at a real bend the line
        # crosses land → fall through to contour-following (channel unchanged).
        if not holding and state.tactical_dest_px_offset is not None:
            _ay, _ax = state.tactical_dest_px_offset
            if math.hypot(_ay, _ax) >= _scaled_min_reach(state):
                _ar = max(0, min(H - 1, int(round(ship_row + _ay))))
                _ac = max(0, min(W - 1, int(round(ship_col + _ax))))
                if self._straight_line_all_water(
                        state.water_mask, ship_row, ship_col, _ar, _ac):
                    return self._last_water_on_line(
                        state.water_mask, ship_row, ship_col, _ar, _ac)

        px_path = []
        for pt_lat, pt_lon in path:
            dlat = pt_lat - state.lat
            dlon = pt_lon - state.lon
            dy = -dlat * self.px_per_deg
            dx = dlon * self.px_per_deg
            r = int(round(ship_row + dy))
            c = int(round(ship_col + dx))
            if not (0 <= r < H and 0 <= c < W):
                continue
            # TOWARD-TACTICAL filter: reject points whose displacement
            # from ship has non-positive projection onto ship→tactical.
            # Ship-heading-independent (survives bounces + reversals).
            if have_tact:
                proj = (dy * tact_dy + dx * tact_dx) / tact_norm
                if proj <= 0:
                    continue
            # HOLD alignment: during a dead-end hold, also require the
            # candidate to point roughly at the held anchor (no far-shore flip).
            if holding:
                dn = math.hypot(dy, dx)
                if dn < 1.0 or (dy * anchor_uy + dx * anchor_ux) / dn < _hold_cos:
                    continue
            px_path.append((r, c))

        if not px_path:
            return None

        # Distance-to-land field for the clearance guard.  Fill the ship's
        # own hull hole (a sprite punch-out) so it doesn't zero out the
        # field right where every reflex line starts.
        from scipy.ndimage import distance_transform_edt
        mask = state.water_mask
        yy, xx = np.ogrid[:H, :W]
        filled = mask | (((yy - ship_row) ** 2 + (xx - ship_col) ** 2)
                         <= _SHIP_HOLE_RADIUS_PX ** 2)
        dt = distance_transform_edt(filled)

        # Tactical anchor in pixel space (frame-shift-maintained by the
        # tactical layer, so reliable even when lat/lon glitches) — the
        # escape-route check aims for it.
        anchor = None
        if state.tactical_dest_px_offset is not None:
            ao = state.tactical_dest_px_offset
            anchor = (ship_row + ao[0], ship_col + ao[1])

        # Walk path FORWARD (ship-side first).  Track the farthest
        # straight-line-water point at each clearance tier: prefer the
        # farthest whose line stays IDEAL_CLEARANCE_PX from land, else the
        # farthest ≥ MIN, else any all-water point.  Preferring clearance
        # naturally shortens the reflex horizon at bends (a nearer point's
        # line doesn't cut the corner) so the ship keeps margin.  The good
        # tiers additionally require an ESCAPE ROUTE — a water run from the
        # WP toward the anchor — so we never commit to a dead-end pocket.
        best_ideal = best_min = best_any = None
        for (r, c) in px_path:
            dist = math.hypot(r - ship_row, c - ship_col)
            if dist < _scaled_min_reach(state):
                continue
            if not self._straight_line_all_water(mask, ship_row, ship_col,
                                                 r, c):
                continue
            best_any = (r, c)
            clr = self._line_min_clearance(dt, ship_row, ship_col, r, c)
            escape_ok = self._has_escape_route(mask, r, c, anchor)
            if clr >= MIN_CLEARANCE_PX and escape_ok:
                best_min = (r, c)
            if clr >= IDEAL_CLEARANCE_PX and escape_ok:
                best_ideal = (r, c)

        best = best_ideal or best_min or best_any
        if best is None:
            return None
        if best_ideal is None and best_min is None:
            log.debug("[hug_path] no reflex WP with ≥%.0fpx clearance AND an "
                      "escape route toward tactical — falling back to farthest "
                      "reachable (likely a perception pinch/pocket)",
                      MIN_CLEARANCE_PX)
        # Position the WP min(IDEAL, width/2) from the hugging (nearer)
        # bank: keep IDEAL_CLEARANCE_PX off the hugged bank in a wide
        # channel, but centre it when the channel is narrower than 2×IDEAL
        # (so it doesn't crowd the far bank).  Skip during a dead-end HOLD
        # (already returned above).
        if holding:
            return best
        return self._nudge_to_bank_offset(mask, ship_row, ship_col, best,
                                          IDEAL_CLEARANCE_PX)

    def _hold_aim_at_tip(self, state, ship_row, ship_col):
        """Dead-end HOLD fallback: aim at the held tip (last water toward it)
        when no path candidate is reachable.  Unlike the in-`_pick_from_path`
        aim-at-tip early return, this has NO distance cap and tolerates a
        partly-blocked line (stops at the last water pixel) — it exists purely
        so a bad mask can't drop the ship to the hold-unaware radial fallback
        and flip it ~180° off the tip (t68)."""
        cd = state.commit_direction
        if (cd is None or "hold(pocket_ahead" not in (cd.reason or "")
                or state.tactical_dest_px_offset is None
                or state.water_mask is None):
            return None
        ay, ax = state.tactical_dest_px_offset
        if math.hypot(ay, ax) < 1.0:
            return None
        H, W = state.water_mask.shape
        ar = max(0, min(H - 1, int(round(ship_row + ay))))
        ac = max(0, min(W - 1, int(round(ship_col + ax))))
        return self._last_water_on_line(state.water_mask, ship_row, ship_col,
                                        ar, ac)

    @staticmethod
    def _last_water_on_line(mask, sy, sx, ey, ex):
        """The farthest water pixel contiguously reachable from (sy,sx)
        along the line to (ey,ex) — aims at the endpoint but stops before
        land (the dead-end tip pixel is often land)."""
        n = max(abs(ey - sy), abs(ex - sx)) + 1
        ys = np.clip(np.linspace(sy, ey, n).astype(np.int32), 0, mask.shape[0] - 1)
        xs = np.clip(np.linspace(sx, ex, n).astype(np.int32), 0, mask.shape[1] - 1)
        last = (int(sy), int(sx))
        for i in range(n):
            if mask[ys[i], xs[i]]:
                last = (int(ys[i]), int(xs[i]))
            else:
                break
        return last

    @staticmethod
    def _has_escape_route(mask, wy, wx, anchor):
        """True if there's a contiguous water run of ≥ _ESCAPE_MIN_PX from
        the WP toward the tactical anchor — i.e. reaching the WP leaves a
        way to keep progressing toward the goal (not a dead-end pocket).
        No anchor ⇒ pass (can't judge)."""
        if anchor is None:
            return True
        dy = anchor[0] - wy
        dx = anchor[1] - wx
        norm = math.hypot(dy, dx)
        if norm < 1.0:
            return True
        uy, ux = dy / norm, dx / norm
        H, W = mask.shape
        run = 0
        for step in range(1, _ESCAPE_RAY_PX + 1):
            py = int(round(wy + uy * step))
            px = int(round(wx + ux * step))
            if not (0 <= py < H and 0 <= px < W) or not mask[py, px]:
                break
            run = step
        return run >= _ESCAPE_MIN_PX

    @staticmethod
    def _line_min_clearance(dt, sy, sx, ey, ex):
        """Min distance-to-land along the ship→endpoint line, skipping the
        near-ship segment (hull/sonar sprite holes)."""
        n = max(abs(ey - sy), abs(ex - sx)) + 1
        ys = np.clip(np.linspace(sy, ey, n).astype(np.int32), 0, dt.shape[0] - 1)
        xs = np.clip(np.linspace(sx, ex, n).astype(np.int32), 0, dt.shape[1] - 1)
        start = min(_CLEARANCE_SKIP_NEAR_PX, max(0, n - 1))
        seg = dt[ys[start:], xs[start:]]
        if seg.size == 0:
            seg = dt[ys, xs]
        return float(seg.min())

    @staticmethod
    def _nudge_to_bank_offset(mask, sy, sx, wp, hug_offset):
        """Shift wp perpendicular so it sits min(hug_offset, width/2) from
        the hugging (nearer) bank: hug_offset px off the hugged bank in a
        wide channel, but centred when the channel is narrower than
        2*hug_offset (so it doesn't crowd the far bank)."""
        wy, wx = wp
        dy = wy - sy; dx = wx - sx
        norm = math.hypot(dy, dx)
        if norm < 1e-6:
            return wp
        py, px = -dx / norm, dy / norm            # unit perpendicular
        H, W = mask.shape

        def _bank(sign, lim=70):
            for t in range(1, lim + 1):
                ny = int(round(wy + sign * t * py))
                nx = int(round(wx + sign * t * px))
                if not (0 <= ny < H and 0 <= nx < W) or not mask[ny, nx]:
                    return t - 1
            return lim
        dp = _bank(+1)
        dm = _bank(-1)
        width = dp + dm + 1
        hug_sign = 1 if dp <= dm else -1          # toward the nearer bank
        d_hug = min(dp, dm)
        target = min(float(hug_offset), width / 2.0)
        shift = hug_sign * (d_hug - target)       # +→toward hug bank
        ny = int(round(wy + shift * py))
        nx = int(round(wx + shift * px))
        if 0 <= ny < H and 0 <= nx < W and mask[ny, nx]:
            return (ny, nx)
        return wp

    def _radial_scan_fallback(self, state, ship_row, ship_col):
        """When no path point is reachable, sample 16 bearings around
        commit_direction and pick the longest ray toward that goal."""
        commit = (state.commit_direction.bearing_deg
                  if state.commit_direction is not None else 180.0)
        # Force ship disc = water
        mask = state.water_mask.copy()
        H, W = mask.shape
        yy, xx = np.mgrid[:H, :W]
        ship_disc = ((yy - ship_row) ** 2
                     + (xx - ship_col) ** 2) <= _SHIP_HOLE_RADIUS_PX ** 2
        mask = mask | ship_disc

        best = None
        best_score = -1e9
        max_ray = _scaled_max_ray(state)
        min_reach = _scaled_min_reach(state)
        for i in range(_RADIAL_BEARINGS):
            bearing = i * (360.0 / _RADIAL_BEARINGS)
            r = math.radians(bearing)
            dr = -math.cos(r); dc = math.sin(r)
            reach = 0
            consec_land = 0
            for step in range(1, max_ray + 1):
                py = int(round(ship_row + step * dr))
                px = int(round(ship_col + step * dc))
                if not (0 <= px < W and 0 <= py < H):
                    break
                if mask[py, px]:
                    reach = step
                    consec_land = 0
                else:
                    consec_land += 1
                    if consec_land > _LAND_TOLERANCE_PX:
                        break
            if reach < min_reach:
                continue
            # Score: reach + alignment with commit
            wp_y = ship_row + reach * dr
            wp_x = ship_col + reach * dc
            wp_bearing = (math.degrees(math.atan2(wp_x - ship_col,
                                                   -(wp_y - ship_row)))
                          + 360.0) % 360.0
            misalign = abs(((wp_bearing - commit + 540) % 360) - 180)
            score = reach - 0.5 * misalign
            if score > best_score:
                best_score = score
                best = (int(round(wp_y)), int(round(wp_x)))
        return best

    @staticmethod
    def _straight_line_all_water(mask, sy, sx, ey, ex,
                                  land_tolerance_frac=0.05):
        """True if straight line ship→endpoint is (nearly) all water."""
        n = max(abs(ey - sy), abs(ex - sx)) + 1
        ys = np.clip(np.linspace(sy, ey, n).astype(np.int32),
                     0, mask.shape[0] - 1)
        xs = np.clip(np.linspace(sx, ex, n).astype(np.int32),
                     0, mask.shape[1] - 1)
        land_frac = float((~mask[ys, xs]).mean())
        return land_frac <= land_tolerance_frac

    def _wp_misalignment_deg(self, wp, state, ship_row, ship_col
                              ) -> Optional[float]:
        """Absolute angle (0-180°) between (ship→wp) and (ship→tactical
        dest).  Returns None when either bearing is undefined."""
        if state.tactical_dest_latlon is None:
            return None
        if state.lat is None or state.lon is None:
            return None
        tlat, tlon = state.tactical_dest_latlon
        tact_dy = -(tlat - state.lat) * self.px_per_deg
        tact_dx = (tlon - state.lon) * self.px_per_deg
        if math.hypot(tact_dy, tact_dx) < 1.0:
            return None                    # tactical on top of ship
        tact_bearing = (math.degrees(math.atan2(tact_dx, -tact_dy))
                         + 360.0) % 360.0
        wy, wx = wp
        wp_dy = wy - ship_row
        wp_dx = wx - ship_col
        if math.hypot(wp_dy, wp_dx) < 1.0:
            return None
        wp_bearing = (math.degrees(math.atan2(wp_dx, -wp_dy))
                       + 360.0) % 360.0
        diff = abs(((wp_bearing - tact_bearing + 540.0) % 360.0) - 180.0)
        return diff

    def _pick_aligned_from_path(self, state, ship_row, ship_col,
                                 wp_source=None):
        """Re-scan the walked path for a straight-line-water-reachable
        point that is at least somewhat aligned with the ship→tactical
        bearing (misalign ≤ 90°).  Returns None if no aligned point
        passes."""
        path = state.tactical_walked_path
        if not path or state.lat is None or state.lon is None:
            return None
        H, W = state.water_mask.shape
        px_path = []
        for pt_lat, pt_lon in path:
            dlat = pt_lat - state.lat
            dlon = pt_lon - state.lon
            r = int(round(ship_row - dlat * self.px_per_deg))
            c = int(round(ship_col + dlon * self.px_per_deg))
            if 0 <= r < H and 0 <= c < W:
                px_path.append((r, c))
        if not px_path:
            return None
        # Walk far-end backward — pick first point that is BOTH
        # straight-line water-reachable AND well-aligned (misalign ≤ 90°).
        for (r, c) in reversed(px_path):
            dist = math.hypot(r - ship_row, c - ship_col)
            if dist < _scaled_min_reach(state):
                continue
            if not self._straight_line_all_water(state.water_mask,
                                                  ship_row, ship_col, r, c):
                continue
            mis = self._wp_misalignment_deg(
                (r, c), state, ship_row, ship_col,
            )
            if mis is None or mis <= 90.0:
                return (r, c)
        return None

    @staticmethod
    def _bank_mask(mask):
        """Cleaned water mask for beam-casting: True everywhere EXCEPT real
        banks.  A real bank is land connected to the frame border; interior
        land islands (the sonar fan, ship marker, white-diamond NPC markers,
        NPC icons) are sprite artifacts that V11 marks as land and would stop
        a beam ~16-20px out on every tick — treat them as pass-through water
        so the beam reaches the true bank.  (Genuine mid-river islands are
        large; the route layer, not this reactive beam, handles them.)"""
        from scipy.ndimage import label
        land = ~mask
        lbl, n = label(land)
        if n == 0:
            return mask.copy()
        border = (set(lbl[0, :]) | set(lbl[-1, :])
                  | set(lbl[:, 0]) | set(lbl[:, -1]))
        border.discard(0)
        if not border:
            return np.ones_like(mask)
        bank = np.isin(lbl, list(border))      # frame-connected land only
        return ~bank

    @staticmethod
    def _cast_edge(water, sy, sx, ang):
        """Hull-EDGE clearance along a ray at absolute bearing `ang`, against
        a sprite-cleaned water mask.  Pixels within the filled hull radius
        count as water; the first run of ≥3 consecutive non-water pixels is
        the bank (3-run tolerates 1-2px sprite holes).  Returns ≥0 px."""
        H, W = water.shape
        r = math.radians(ang)
        uy, ux = -math.cos(r), math.sin(r)
        gap = None
        miss = 0
        for d in range(1, _MAX_RAY_PX):
            y = int(round(sy + uy * d))
            x = int(round(sx + ux * d))
            if not (0 <= y < H and 0 <= x < W):
                return max(0.0, float(d) - _HULL_HALF_PX)   # frame edge ⇒ open
            if water[y, x] or d <= _SHIP_HOLE_RADIUS_PX:
                miss = 0
                gap = None
            else:
                if gap is None:
                    gap = d
                miss += 1
                if miss >= 3:
                    return max(0.0, float(gap) - _HULL_HALF_PX)
        return max(0.0, float(_MAX_RAY_PX) - _HULL_HALF_PX)

    @staticmethod
    def _beam_clearances(mask, sy, sx, heading_deg):
        """Hull-edge water clearance to port and starboard (±90° beams).
        Used only by the reversal route resolver, where the shared sprite
        floor cancels in the port-vs-starboard comparison.  Returns
        (port_px, starboard_px)."""
        water = HugPathPlanner._bank_mask(mask)
        port = HugPathPlanner._cast_edge(water, sy, sx, heading_deg - 90.0)
        star = HugPathPlanner._cast_edge(water, sy, sx, heading_deg + 90.0)
        return port, star

    @staticmethod
    def _forward_arc_bias(mask, sy, sx, heading, expected_shift_px):
        """Speed-aware forward-arc collision nudge (VFH-lite, repulsive).

        Sample hull-edge clearance across the forward arc.  EVERY bank inside
        the ship's reaction distance (`_AVOID_LOOKAHEAD_TICKS × per-tick
        travel`, floored at `_AVOID_BASE_MARGIN_PX`) pushes the steering AWAY
        from its bearing, weighted by proximity `(danger−clr)/danger` and by
        how head-on it is `cos(rel)`.  Summing over the arc means a close bank
        off the bow still deflects the ship even when dead-ahead is open — the
        failure that swiped t18 (a 6px bank at +20° sat inside the hull's
        swept width while straight-ahead read 67px).  Returns (bias_deg,note);
        +ve ⇒ steer right."""
        water = HugPathPlanner._bank_mask(mask)
        shift = expected_shift_px or 0.0
        danger = max(_AVOID_BASE_MARGIN_PX, _AVOID_LOOKAHEAD_TICKS * shift)
        clr = {rel: HugPathPlanner._cast_edge(water, sy, sx, heading + rel)
               for rel in _AVOID_ARC_RAYS_DEG}
        near_rel = min(clr, key=lambda k: clr[k])
        if clr[near_rel] >= danger:
            return 0.0, ""
        # (1) Lateral repulsion: side banks inside `danger` push away from
        # their bearing, weighted by proximity² × cos(rel).
        push = 0.0
        for rel, c in clr.items():
            if c < danger and rel != 0.0:
                w = ((danger - c) / danger) ** _AVOID_WEIGHT_POWER \
                    * math.cos(math.radians(rel))
                push += math.copysign(w, -rel)   # bank on +rel ⇒ steer left(−)

        # (2) Head-on escape: a near-bow bank contributes little/no lateral
        # repulsion (dead-ahead = zero), so (1) alone lets the ship ram it.
        # React to near-bow banks in a LARGER band and steer decisively toward
        # the openest forward bearing.
        headon_danger = max(_AVOID_BASE_MARGIN_PX,
                            _AVOID_HEADON_TICKS * shift)
        bow = min(clr[r] for r in clr
                  if abs(r) <= _AVOID_HEADON_ARC_DEG)
        if bow < headon_danger:
            open_rel = max(clr, key=lambda k: clr[k])
            if open_rel != 0.0:
                push += math.copysign((headon_danger - bow) / headon_danger,
                                      open_rel)

        bias = max(-1.0, min(1.0, push)) * _AVOID_MAX_BIAS_DEG
        if abs(bias) < _AVOID_DEADZONE_DEG:
            return 0.0, ""
        note = (f"favoid{bias:+.0f}(near{clr[near_rel]:.0f}@{near_rel:+.0f}"
                f",bow{bow:.0f},dngr{danger:.0f})")
        return bias, note

    @staticmethod
    def _wp_signed_delta(wp, ship_row, ship_col, heading):
        """Shortest-angle heading error (deg) from bow to the WP.
        +ve ⇒ turn right."""
        from brain.ai_nav.layers.planner import _signed_delta
        wy, wx = wp
        dy = wy - ship_row; dx = wx - ship_col
        wp_bearing = (math.degrees(math.atan2(dx, -dy)) + 360.0) % 360.0
        return _signed_delta(wp_bearing, heading)

    @staticmethod
    def _resolve_reversal_route(delta, port_clr, star_clr):
        """PLANNER-owned turn-direction decision for a reversal.

        The reflex WP fixes WHERE to go; the shortest-angle turn to it fixes
        the DEFAULT rotation.  For a near-reversal (|delta| ≥ _UTURN_DELTA_DEG)
        both rotations reach the same heading, so the shortest-angle sign is
        an arbitrary tie-break.  If that rotation sweeps the bow into the near
        bank (a right turn sweeps starboard, a left turn sweeps port), take
        the ALTERNATIVE legit route — the other way around, a bigger turn —
        when that side is clearer by more than _UTURN_CLR_MARGIN_PX.

        This is the "prefer a navigable route even if longer" principle at
        the route layer.  Because the route now sweeps into the open side,
        the steering layer's reactive collision bias reinforces it instead of
        stalling against it.  For a non-reversal (|delta| < threshold) there
        is only one viable rotation — going the long way would sweep almost
        all the way around — so we never reroute.  Returns (route_delta, note).
        """
        if abs(delta) < _UTURN_DELTA_DEG:
            return delta, ""
        turning_right = delta > 0
        if turning_right and (port_clr - star_clr) > _UTURN_CLR_MARGIN_PX:
            return delta - 360.0, f"reroute→L(p{port_clr:.0f}>s{star_clr:.0f})"
        if (not turning_right
                and (star_clr - port_clr) > _UTURN_CLR_MARGIN_PX):
            return delta + 360.0, f"reroute→R(s{star_clr:.0f}>p{port_clr:.0f})"
        return delta, ""

    def _emit_command(self, state, wp, ship_row, ship_col,
                      route_delta, wp_note=None):
        from brain.ai_nav.layers.planner import _compute_hold_ms
        heading = state.heading.bearing_deg
        delta = route_delta   # route (incl. reversal direction) chosen upstream

        # Reactive collision avoidance (VFH-lite, avoidance ONLY).  Skip it
        # mid-reversal: the route resolver already owns the turn direction and
        # the forward arc points at the dead-end the ship is turning away from.
        if abs(route_delta) < _UTURN_DELTA_DEG:
            fbias, fnote = self._forward_arc_bias(
                state.water_mask, ship_row, ship_col, heading,
                getattr(state, "expected_shift_px", None))
            if fbias:
                delta += fbias
                wp_note = fnote if wp_note is None else f"{wp_note};{fnote}"

        wy, wx = wp
        if self.pid is not None:
            # Closed-loop: PID on the heading error, hold_ms from the
            # calibrated hold→turn curve inverse.
            cmd, hold_ms, pid_note = self.pid.step(delta, heading)
            wp_note = pid_note if wp_note is None else f"{wp_note};{pid_note}"
        else:
            # Legacy feed-forward hold estimate.
            hold_ms = _compute_hold_ms(delta, state.speed_kt)
            cmd = None
            if hold_ms > 0:
                cmd = "hold_right" if delta > 0 else "hold_left"
        state.planner_output = PlannerOutput(
            waypoint_px=(int(wy), int(wx)),
            command=cmd, hold_ms=hold_ms,
            primitive="hug_path",
            wp_note=wp_note,
        )
