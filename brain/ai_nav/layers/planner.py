"""Layer 3 — symbolic local planner with commit_heading.

Reads the heading (L1) + water mask (L2), runs the shore-walk to
find a waypoint, and emits a steering command.

The load-bearing addition over the legacy shore picker:
**commit_direction**.  The walk direction along the shore is
chosen by alignment with commit_direction, NOT with the per-frame
heading.  This prevents the failure pattern observed at t150→t151
in the 2026-06-17 session: bow rotates from a collision, port-side
flips to the other bank, picker happily traces the wrong shore.

commit_direction only changes when:
  - Layer 4 (tactical) overrides it (junction / dead-end / lake)
  - Layer 5 (strategic) overrides it (new mission target)
  - planner detects sustained no_shore (hard dead-end safety net)
  - explicit init (first tick, before any other source has fired)
"""
from __future__ import annotations

import math
import random
from typing import Optional, Protocol

from brain.ai_nav.state import CommitDirection, NavState, PlannerOutput
from brain.ai_nav.vision_input import VisionFrame


# Tuning knobs — match the legacy shore picker defaults so behavior
# is comparable across A/B runs.
DEFAULT_WALK_PX = 80
DEFAULT_SAFE_PX = 25
MIN_DELTA_DEG = 5.0       # don't command turns below this
RATE_DPS = 120.0          # measured deg/sec while holding rudder
                          # at the calibration speed (~8 kt)
RATE_CALIB_SPEED_KT = 8.0 # the speed RATE_DPS was measured at
MAX_HOLD_MS = 2500        # allows ~300° at 8 kt or ~90° at 27 kt.
                          # Raised from 1800 because the bot was
                          # steering the correct direction but not
                          # hard enough to avoid pre-Y-tip collisions
                          # in voyage 6.  At high speed the ship's
                          # momentum needs a longer commit to actually
                          # rotate enough before hitting shore.
# Anti-cheat / human-plausibility floor.  A 42 ms tap-and-release
# (what `5° / 120°/sec` works out to) isn't humanly possible and
# burst-tap patterns flag the game's anti-cheat (see
# memory/feedback_sea_steering_rudder_deflection.md).  Bump every
# nonzero hold to a jittered floor ~ 190-210 ms — squarely in the
# "normal deliberate tap" band and further from the anti-cheat
# suspicion zone.  Trade-off: min turn ≈ 24° at ~120°/sec, so fine
# corrections < 24° round up (voyage 14:26 had 53% of holds at the
# prior 160 ms floor — corrections are usually small anyway).
MIN_HOLD_MS = 200
MIN_HOLD_JITTER_MS = 10   # ± this many ms around MIN_HOLD_MS


def _speed_scale(speed_kt: Optional[float]) -> float:
    """Scale factor applied to the raw rotation budget given the
    ship's current speed.

    Empirical fit from fast-ship voyage 2026-07-23 (session
    ai_nav_2026-07-23T14-58-31): at 25 kt a 375 ms hold produced
    only ~28° of rotation (effective rate ~75°/sec, vs 120°/sec at
    the 8-kt calibration).  Rate scales roughly like
    1/sqrt(speed/CALIB), so hold_ms scale = sqrt(speed/CALIB).

    Prior linear scale (speed/CALIB = 3.375× at 27 kt) was too
    aggressive → over-rotation → shore collisions.  Prior cap at
    1.0 was under → shore collisions from too little rotation.
    sqrt is the empirical middle ground.  Capped at 2.5 to prevent
    runaway holds at absurd speeds (e.g., speed OCR misreads).
    """
    if speed_kt is None or speed_kt <= 0:
        return 1.0
    import math
    return min(2.5, math.sqrt(speed_kt / RATE_CALIB_SPEED_KT))


# Cap per-command angular change.  Even with correct hold length,
# attempting a 90° turn in one command means the ship rotates the
# full 90° arc without any chance to re-evaluate.  On fast ships in
# narrow water, that arc's lateral displacement hits shore before
# the rotation completes.  Splitting big corrections across multiple
# ticks lets the reflex re-pick a WP after each partial rotation.
#
# EXCEPTION for U-turns: deltas above U_TURN_THRESHOLD_DEG are NOT
# capped.  A 180° reversal split into 4 × 45° chunks takes 4 ticks
# (~12s) while the ship keeps moving forward — that's a full
# minute lost by the time the U-turn completes, and by then the
# picker has usually re-committed to yet another direction.
# Holding the full ~1500 ms at 8 kt to actually U-turn is worth
# the momentary loss of re-evaluate opportunity.  Observed
# 2026-07-24T15-29-48 t340-t360: ship needed to U-turn out of the
# Y-tip pocket but couldn't rotate fast enough with the 45° cap,
# ended up drifting NW instead.
MAX_PER_COMMAND_DEG = 45.0
U_TURN_THRESHOLD_DEG = 90.0     # above this, don't cap the hold


def _floor_hold_ms(hold_ms: int) -> int:
    """Round a small commanded hold up to a human-plausible
    jittered floor.  No-op when the requested hold is already
    above the floor."""
    if hold_ms <= 0:
        return hold_ms
    floor = MIN_HOLD_MS + random.uniform(-MIN_HOLD_JITTER_MS,
                                         MIN_HOLD_JITTER_MS)
    return max(int(round(floor)), hold_ms)


def _compute_hold_ms(delta_deg: float, speed_kt: Optional[float]) -> int:
    """Plan a single rudder hold for the given heading delta, taking
    current speed into account.  Returns 0 when delta is below the
    threshold (no command).  Otherwise applies the human-plausible
    floor and the MAX_HOLD_MS cap.

    Big corrections are capped at MAX_PER_COMMAND_DEG (≈ 45°) so a
    single command never attempts more than half a right-angle.  The
    reflex re-picks each tick, so multi-step corrections converge
    naturally without over-committing on any single arc.
    """
    if abs(delta_deg) < MIN_DELTA_DEG:
        return 0
    # U-turn escape: large deltas skip the per-command cap so a single
    # long hold can complete the rotation.  Below the threshold the
    # cap still applies for narrow-channel safety.
    if abs(delta_deg) >= U_TURN_THRESHOLD_DEG:
        capped_delta = abs(delta_deg)
    else:
        capped_delta = min(abs(delta_deg), MAX_PER_COMMAND_DEG)
    raw_ms = capped_delta / RATE_DPS * 1000.0
    scaled_ms = raw_ms * _speed_scale(speed_kt)
    capped = int(min(MAX_HOLD_MS, scaled_ms))
    return _floor_hold_ms(capped)
NO_SHORE_HARD_TICKS = 6   # consecutive no_shore before commit changes


class PlannerLayer(Protocol):
    name: str
    latency_budget_ms: float

    def plan(self, frame: VisionFrame, state: NavState) -> NavState: ...


class ShoreHugPlanner:
    """Shore-walk planner with commit_direction.

    Uses heading (L1) ONLY to define port/starboard side.
    Uses commit_direction to choose walk direction along the contour.
    These two were conflated in the legacy picker — separating them
    is the entire reason this layer exists.
    """
    name = "shore_hug"
    latency_budget_ms = 20.0

    def __init__(
        self,
        side: str = "port",
        walk_px: int = DEFAULT_WALK_PX,
        safe_px: int = DEFAULT_SAFE_PX,
    ):
        assert side in ("port", "starboard")
        self.side = side
        self.walk_px = walk_px
        self.safe_px = safe_px
        self._no_shore_streak = 0
        # Only allow the dead-end safety-net to fire after the planner
        # has actually found shore at least once.  Without this, a
        # parked-bot frame's persistent no_shore looks like a dead-end
        # and the safety-net rotates commit_direction uselessly.
        self._ever_found_shore = False

    def plan(self, frame: VisionFrame, state: NavState) -> NavState:
        # Lazy import — shore_waypoint_picker pulls skimage.
        from tools.shore_waypoint_picker import extract_shore_path

        if state.water_mask is None:
            state.planner_output = PlannerOutput(skip_reason="no_mask")
            return state
        if state.heading is None:
            state.planner_output = PlannerOutput(skip_reason="no_heading")
            return state

        mask = state.water_mask
        H, W = mask.shape
        ship_center = (W // 2, H // 2)

        # Side selection uses the *current* heading (per-tick).
        port_side_bearing = state.heading.bearing_deg

        # Walk-direction selection uses commit_direction (persistent).
        # On the first tick, fall back to current heading as commit.
        if state.commit_direction is None:
            state.commit_direction = CommitDirection(
                bearing_deg=state.heading.bearing_deg,
                reason="init_from_heading",
                set_at_tick=state.tick,
            )

        shore_pts, path_pts = extract_shore_path(
            mask, ship_center,
            heading_deg=port_side_bearing,
            side=self.side,
            walk_distance_px=self.walk_px,
            safe_distance_px=self.safe_px,
            walk_direction_deg=state.commit_direction.bearing_deg,
        )

        # No shore on the chosen side this tick.
        if not shore_pts or not path_pts:
            po = PlannerOutput(skip_reason="no_shore")
            # Only count no_shore as a dead-end vote when we've
            # actually been hugging a shore at some point — otherwise
            # parked-bot frames (perpetual no_shore from tick 1)
            # would trigger the safety-net before the bot ever
            # started sailing.
            if self._ever_found_shore:
                self._no_shore_streak += 1
                if self._no_shore_streak >= NO_SHORE_HARD_TICKS:
                    # Hard dead-end: nothing on port side for N ticks
                    # after we'd been following a shore.  Rotate 90°
                    # to try the other side.  Real recovery should
                    # come from Layer 4; this is the safety net that
                    # keeps the bot moving while tactical is asleep.
                    state.commit_direction = CommitDirection(
                        bearing_deg=(state.commit_direction.bearing_deg + 90) % 360,
                        reason="planner_dead_end_safety_net",
                        set_at_tick=state.tick,
                    )
                    self._no_shore_streak = 0
            state.planner_output = po
            return state

        # Successful shore find.
        self._no_shore_streak = 0
        self._ever_found_shore = True
        waypoint = path_pts[-1]
        wp_bearing = _bearing_of_waypoint(ship_center, waypoint)

        # Steer toward waypoint, but only if the delta exceeds MIN_DELTA.
        delta = _signed_delta(wp_bearing, state.heading.bearing_deg)
        cmd = None
        hold_ms = 0
        hold_ms = _compute_hold_ms(delta, state.speed_kt)
        if hold_ms > 0:
            cmd = "hold_right" if delta > 0 else "hold_left"

        state.planner_output = PlannerOutput(
            shore_pts=shore_pts,
            path_pts=path_pts,
            waypoint_px=waypoint,
            command=cmd,
            hold_ms=hold_ms,
        )
        return state


# ── Small helpers (copied from shore_waypoint_picker for now) ──────────


def _bearing_of_waypoint(ship_xy, dest_yx) -> float:
    sx, sy = ship_xy
    dy, dx = dest_yx
    return (math.degrees(math.atan2(dx - sx, -(dy - sy))) + 360.0) % 360.0


def _signed_delta(target_deg: float, current_deg: float) -> float:
    """Signed shortest-arc delta from current to target, in (-180, 180]."""
    d = (target_deg - current_deg + 540.0) % 360.0 - 180.0
    return d


def _bearing_to_image_vec(bearing_deg: float) -> tuple[float, float]:
    """Compass bearing → (dy, dx) unit vector in image coords (y-down).
    0° = up (-y).  90° = right (+x)."""
    r = math.radians(bearing_deg)
    return (-math.cos(r), math.sin(r))


def _image_vec_to_compass(dy: float, dx: float) -> float:
    """Inverse of _bearing_to_image_vec.  (dy, dx) → compass deg."""
    return (math.degrees(math.atan2(dx, -dy)) + 360.0) % 360.0


def _junction_exits_from_tree(
    water_mask, lookahead_px: int = 20,
) -> tuple[float, ...]:
    """Compass bearings of every outgoing edge at the nearest junction
    node to the ship (image center).  Bearings measured from the
    junction node outward along the edge for `lookahead_px` so curving
    arms report their *initial* heading, not their distant ends.

    Returns () when there's no junction node nearby (which can happen
    even when topology=junction if the ship is mid-channel approaching
    one — the L5 caller should treat this as a transient and re-check
    next tick).
    """
    from tools.centerline_waypoint_prototype import extract_tree_from_mask
    tree = extract_tree_from_mask(water_mask)
    if not tree.nodes:
        return ()
    H, W = water_mask.shape
    cy, cx = H // 2, W // 2
    junction_nodes = [n for n in tree.nodes.values() if n.kind == "junction"]
    if not junction_nodes:
        return ()
    near = min(junction_nodes,
               key=lambda n: (n.y - cy) ** 2 + (n.x - cx) ** 2)
    exits: list[float] = []
    for e in tree.edges:
        if e.a != near.id and e.b != near.id:
            continue
        # Orient polyline so it starts at the junction node.
        poly = e.points
        start_at_a = (int(poly[0, 0]), int(poly[0, 1])) == (near.y, near.x)
        if not start_at_a:
            poly = poly[::-1]
        # Sample first `lookahead_px` worth of the edge for the bearing.
        n = min(lookahead_px, len(poly) - 1)
        if n < 1:
            continue
        dy = float(poly[n, 0] - poly[0, 0])
        dx = float(poly[n, 1] - poly[0, 1])
        if dy == 0 and dx == 0:
            continue
        exits.append(round(_image_vec_to_compass(dy, dx), 1))
    return tuple(exits)


# ── L3 alternative: centerline planner ─────────────────────────────────

# Pixels ahead along the centerline tree to place the waypoint.
# Matches the prototype's LOOKAHEAD_PX.
DEFAULT_LOOKAHEAD_PX = 50

# Waypoint-inertia rule.  Centerline derived from a noisy water mask can
# pick wildly different tree branches tick-to-tick (a coastal feature
# appears, water_frac drops, skeleton recomputes), producing waypoint
# jumps that the ship cannot physically follow.  Mirror the heading
# physics-rejection: reject big jumps when motion bearing says the new
# waypoint contradicts where the ship is actually going.
#
# Empirical: t1→t2 in ai_nav_2026-06-29T17-34-39 jumped wp from
# (158,175) to (60,212) = 105 px while heading and lat/lon both said
# south.  This rule keeps the prior waypoint when the new one would
# yank the ship 90°+ off its actual motion direction.
WP_INERTIA_JUMP_PX     = 40.0    # min jump to trigger cross-check
WP_INERTIA_DISAGREE_DEG = 90.0   # min wp-vs-motion disagreement to reject
WP_INERTIA_LATLON_BUF  = 6       # rolling buffer of lat/lon for motion
WP_INERTIA_MOTION_WIN  = 5       # ticks back to derive motion bearing
# Same trust range as the pipeline's physics-rejection — degrees of
# total lat+lon motion per WP_INERTIA_MOTION_WIN ticks.
WP_INERTIA_MOTION_MIN  = 0.02
WP_INERTIA_MOTION_MAX  = 1.0


class CenterlinePlanner:
    """Walks the medial-axis tree of the water region and places the
    waypoint LOOKAHEAD_PX ahead of the ship along it.

    Useful in narrow channels (rivers) where shore-hug puts the
    waypoint too close to the bank.  In open water the tree degenerates
    and this planner returns no_shore — defer to `HybridPlanner`
    to route those to the shore-hug primitive instead.

    Wraps `tools.centerline_waypoint_prototype.pick_waypoint`.
    """
    name = "centerline"
    latency_budget_ms = 30.0

    def __init__(
        self,
        side: str = "port",
        lookahead_px: int = DEFAULT_LOOKAHEAD_PX,
    ):
        assert side in ("port", "starboard")
        self.side = side
        self.lookahead_px = lookahead_px
        # Waypoint-inertia state (see WP_INERTIA_* constants above).
        self._prev_waypoint: Optional[tuple[int, int]] = None
        self._prev_wp_brg: Optional[float] = None
        # Flipped True after the planner has issued its first non-zero
        # rudder command.  Until then, the wp_inertia rule (which
        # depends on a meaningful prev_cmd) is disabled so the initial
        # waypoint can be established freely.
        self._has_commanded: bool = False
        from collections import deque
        self._latlon_buf: deque = deque(maxlen=WP_INERTIA_LATLON_BUF)

    def _motion_bearing_deg(self) -> Optional[float]:
        """Bearing from lat/lon Δ over WP_INERTIA_MOTION_WIN ticks, or
        None when motion is outside the trust range (too slow / OCR
        garbage)."""
        if len(self._latlon_buf) <= WP_INERTIA_MOTION_WIN:
            return None
        cur = self._latlon_buf[-1]
        ref = self._latlon_buf[-1 - WP_INERTIA_MOTION_WIN]
        if cur is None or ref is None:
            return None
        dlat = cur[0] - ref[0]; dlon = cur[1] - ref[1]
        mag = math.hypot(dlat, dlon)
        if mag < WP_INERTIA_MOTION_MIN or mag > WP_INERTIA_MOTION_MAX:
            return None
        return math.degrees(math.atan2(dlon, dlat)) % 360.0

    def plan(self, frame: VisionFrame, state: NavState) -> NavState:
        from tools.centerline_waypoint_prototype import (
            extract_tree_from_mask, pick_waypoint,
        )

        if state.water_mask is None:
            state.planner_output = PlannerOutput(skip_reason="no_mask",
                                                 primitive=self.name)
            return state
        if state.heading is None:
            state.planner_output = PlannerOutput(skip_reason="no_heading",
                                                 primitive=self.name)
            return state

        # Seed commit_direction from heading on first tick if neither
        # the mission layer nor a runner flag set it.  Mirrors what
        # ShoreHugPlanner does so the centerline planner walks the
        # channel in the user's intended direction instead of an
        # arbitrary one when picked by HybridPlanner on t1.
        if state.commit_direction is None:
            state.commit_direction = CommitDirection(
                bearing_deg=state.heading.bearing_deg,
                reason="init_from_heading",
                set_at_tick=state.tick,
            )

        mask = state.water_mask
        H, W = mask.shape
        ship_xy = (W // 2, H // 2)
        bearing = state.heading.bearing_deg
        # Walk the channel in the direction the mission (or initial
        # seed) wants us to go — NOT the bow's current heading.  The
        # bow can be rotated by physics/collisions; commit is the
        # persistent goal.
        walk_bearing = state.commit_direction.bearing_deg
        walk_vec = _bearing_to_image_vec(walk_bearing)
        hug_side = "left" if self.side == "port" else "right"

        tree = extract_tree_from_mask(mask)
        if not tree.edges:
            # Open water or insufficient mask — no centerline to walk.
            state.planner_output = PlannerOutput(skip_reason="no_centerline",
                                                 primitive=self.name)
            return state

        dest, trace, _ = pick_waypoint(
            tree, ship_xy, walk_vec,
            hug_side=hug_side, lookahead=self.lookahead_px,
        )
        if dest is None:
            state.planner_output = PlannerOutput(skip_reason="no_waypoint",
                                                 primitive=self.name)
            return state

        # Waypoint-inertia cross-check.  Update lat/lon buffer first so
        # this tick contributes to the motion bearing for the NEXT call.
        if state.lat is not None and state.lon is not None:
            self._latlon_buf.append((state.lat, state.lon))
        else:
            self._latlon_buf.append(None)
        wp_inertia_note: Optional[str] = None
        # Waypoint-inertia (direction-based): the wp's BEARING from ship
        # is the right metric, not pixel distance.  A wp can move 90 px
        # in the same bearing (sliding along the path ahead — fine) or
        # 10 px crossing the bow centerline (true direction flip — bad).
        # Pixel-jump conflates these; bearing change separates them.
        #
        # Empirical (t202→t203 in ai_nav_2026-06-29T23-57-44):
        #   wp went from (y=55, x=236) bearing 42° (NE)
        #          to   (y=95, x=145) bearing 270° (W)
        #   Bearing change = 132° — wp jumped from east side of bow to
        #   west side.  Bot commanded only -13°, nowhere near enough.
        #
        # Gating mirrors the prior rule: skip when the bot commanded a
        # big turn or at a dead-end (legitimate reasons for the wp to
        # relocate).
        # Plausible bearing change ≤ |cmd| × 1.25 + 15° (same shape as
        # the physics_reject formula on heading.py).  The old "if
        # |cmd|>30° accept any change" gate had the same too-permissive
        # binary-gate bug: 2026-06-30 voyage 15:14 t229 had prev_cmd
        # just over 30° and waypoint bearing flipped 133°, easily
        # bypassed.  Now: |cmd|=31° → plausible=53.75°, reject.
        WP_BEARING_REJECT_DEG    = 60.0    # min bearing change to trigger
        WP_BEARING_TOLERANCE_REL = 0.25
        WP_BEARING_TOLERANCE_FLOOR = 15.0
        new_wp_brg = _bearing_of_waypoint(ship_xy, dest)
        # Latch guard: the reflex wp must be on the path toward the
        # tactical dest.  If the currently-latched wp bearing disagrees
        # with the tactical bearing by more than 90°, the latch is
        # pointing away from the goal and would deadlock every fresh
        # (goal-aligned) pick through the wp_inertia filter — drop it
        # so the fresh pick takes effect this tick.  Observed in
        # ai_nav_2026-07-24T12-52-08 t7-t34: latched wp bearing 338°,
        # tactical bearing 172°, disagreement 166° for 30+ ticks.
        wp_reject_thr = 90.0
        if (self._prev_wp_brg is not None
                and state.tactical_dest_px_offset is not None):
            tact_dy, tact_dx = state.tactical_dest_px_offset
            if tact_dy != 0 or tact_dx != 0:
                tact_brg = (math.degrees(math.atan2(tact_dx, -tact_dy))
                            + 360.0) % 360.0
                latch_off_tact = abs(((self._prev_wp_brg - tact_brg
                                       + 540.0) % 360.0) - 180.0)
                if latch_off_tact > wp_reject_thr:
                    import logging
                    logging.getLogger(__name__).warning(
                        "[ai_nav] wp_inertia latch dropped: "
                        "latched wp brg %.0f° is %.0f° off from "
                        "tactical brg %.0f° (thr=%.0f°) — reset "
                        "so fresh pick takes effect",
                        self._prev_wp_brg, latch_off_tact,
                        tact_brg, wp_reject_thr,
                    )
                    self._prev_wp_brg = None
                    self._prev_waypoint = None
        # Only apply wp_inertia AFTER the bot has issued at least one
        # meaningful command — at startup the planner needs to
        # establish a fresh waypoint, and a tight rule with prev_cmd=0
        # would deadlock the very first wp update.  We unlock the
        # filter once `_has_commanded` flips True (set below).
        if (self._prev_wp_brg is not None
                and self._prev_waypoint is not None
                and getattr(self, "_has_commanded", False)):
            diff = abs(((new_wp_brg - self._prev_wp_brg + 540.0) % 360.0) - 180.0)
            if diff > WP_BEARING_REJECT_DEG:
                prev_po = state.planner_output
                prev_cmd_deg = 0.0
                if prev_po and prev_po.command and prev_po.hold_ms:
                    mag = prev_po.hold_ms * RATE_DPS / 1000.0
                    prev_cmd_deg = (mag if prev_po.command == "hold_right"
                                    else -mag)
                abs_cmd = abs(prev_cmd_deg)
                plausible = (abs_cmd * (1.0 + WP_BEARING_TOLERANCE_REL)
                             + WP_BEARING_TOLERANCE_FLOOR)
                bearing_explained_by_cmd = diff <= plausible
                at_dead_end = (state.topology or "").lower() == "dead_end"
                # A commit stamped with the current tick came from a
                # mission/tactical update this tick (post-planner re-run
                # in pipeline.py) — the wp bearing SHOULD rotate to
                # follow it, so inertia must not block the fresh pick.
                commit_just_updated = (
                    state.commit_direction is not None
                    and state.commit_direction.set_at_tick == state.tick
                )
                if not (bearing_explained_by_cmd or at_dead_end
                        or commit_just_updated):
                    import logging
                    logging.getLogger(__name__).warning(
                        "[ai_nav] wp_bearing_reject: new_brg=%.0f° vs "
                        "prev=%.0f° (diff=%.0f°) prev_cmd=%.0f° "
                        "plausible=%.0f° topology=%s — holding prior wp %s",
                        new_wp_brg, self._prev_wp_brg, diff, prev_cmd_deg,
                        plausible, state.topology, self._prev_waypoint,
                    )
                    dest = self._prev_waypoint
                    trace = []   # prior trace doesn't apply
                    new_wp_brg = self._prev_wp_brg   # don't update prior on reject
        self._prev_waypoint = (int(dest[0]), int(dest[1]))
        self._prev_wp_brg = new_wp_brg

        wp_bearing = _bearing_of_waypoint(ship_xy, dest)
        delta = _signed_delta(wp_bearing, bearing)
        cmd = None
        hold_ms = 0
        hold_ms = _compute_hold_ms(delta, state.speed_kt)
        if hold_ms > 0:
            cmd = "hold_right" if delta > 0 else "hold_left"

        state.planner_output = PlannerOutput(
            path_pts=[(int(y), int(x)) for y, x in trace],
            waypoint_px=(int(dest[0]), int(dest[1])),
            command=cmd,
            hold_ms=hold_ms,
            primitive=self.name,
        )
        if cmd is not None and hold_ms > 0:
            self._has_commanded = True
        return state


# ── L3 hybrid: dispatch by water topology ──────────────────────────────


class HybridPlanner:
    """Dispatches between CenterlinePlanner and ShoreHugPlanner based
    on the local water topology (skeleton-derived).

      CHANNEL  → centerline   (narrow river — center is safer than bank)
      JUNCTION → centerline   (the picker handles branch selection)
      DEAD_END → centerline   (walks to the leaf — useful retreat info)
      LAKE     → shore_hug    (centerline degenerates in open water)

    Falls back to shore_hug on any centerline failure (no_centerline,
    no_waypoint) so the bot keeps moving even when topology
    classification disagrees with what the centerline planner can do.
    """
    name = "hybrid"
    latency_budget_ms = 50.0

    # The default lake_skeleton_ratio in vision/water_skeleton.py is
    # 0.05, calibrated for the place-graph / TremauxPicker which wants
    # to treat a 20-px-wide+ region as a lake terminal.  For our
    # dispatching purpose a 30–50 px channel (Cairo Nile gives
    # ratio 0.02–0.03) is exactly what centerline planning is built
    # for, so we lower the threshold here.  Centerline failures
    # (no_waypoint) fall back to shore_hug — under-classifying lake
    # is the safer error.
    DEFAULT_LAKE_THRESHOLD = 0.01

    def __init__(
        self,
        side: str = "port",
        walk_px: int = DEFAULT_WALK_PX,
        safe_px: int = DEFAULT_SAFE_PX,
        lookahead_px: int = DEFAULT_LOOKAHEAD_PX,
        lake_threshold: float = DEFAULT_LAKE_THRESHOLD,
    ):
        self.side = side
        self.lake_threshold = lake_threshold
        self._shore = ShoreHugPlanner(
            side=side, walk_px=walk_px, safe_px=safe_px,
        )
        self._center = CenterlinePlanner(
            side=side, lookahead_px=lookahead_px,
        )

    def plan(self, frame: VisionFrame, state: NavState) -> NavState:
        from vision.water_skeleton import (
            classify_topology, extract_skeleton,
        )
        from brain.goals.junction_detector import TopologyKind

        if state.water_mask is None:
            state.planner_output = PlannerOutput(skip_reason="no_mask",
                                                 primitive=self.name)
            return state

        analysis = extract_skeleton(state.water_mask)
        topology = classify_topology(
            analysis, lake_skeleton_ratio=self.lake_threshold,
        )
        # Expose topology + exit bearings to L5.  Junction exits are
        # derived from the centerline tree: at the ship's nearest
        # junction node, compute compass bearing along each outgoing
        # edge (taking the first ~20 px so curving edges report their
        # initial heading from the junction, not their distant end).
        state.topology = topology.value
        if topology == TopologyKind.JUNCTION:
            state.junction_exits_compass = _junction_exits_from_tree(
                state.water_mask
            )
        else:
            state.junction_exits_compass = ()
        prefer_centerline = topology in (
            TopologyKind.CHANNEL,
            TopologyKind.JUNCTION,
            TopologyKind.DEAD_END,
        )

        if prefer_centerline:
            self._center.plan(frame, state)
            out = state.planner_output
            # Fallback to shore_hug if centerline couldn't emit a
            # waypoint (open patch within a "channel" classification,
            # or sparse skeleton).
            if out is not None and out.skip_reason in (
                "no_centerline", "no_waypoint",
            ):
                self._shore.plan(frame, state)
                if state.planner_output is not None:
                    state.planner_output.topology = topology.value
                    state.planner_output.primitive = (
                        f"{self._shore.name}+fallback"
                    )
                return state
        else:
            self._shore.plan(frame, state)
            if state.planner_output is not None:
                state.planner_output.primitive = self._shore.name

        if state.planner_output is not None:
            state.planner_output.topology = topology.value
        return state
