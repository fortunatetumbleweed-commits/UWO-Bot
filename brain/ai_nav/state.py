"""NavState — the running state passed through the layer stack.

Every layer reads it, returns an updated copy.  The pipeline owns
the mutation discipline: a layer's output replaces only the fields
that layer is responsible for.

`commit_heading` is the load-bearing addition over the legacy
shore picker: it survives bow rotations from collisions and only
changes at *deliberate decision points* (tactical layer fires,
sustained no-shore, loop closure, target acquired).  Concept
discussed in the 2026-06-16 session and in
`docs/ai_navigation_landscape.md` §5.3.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass
class Heading:
    """A heading measurement with provenance + confidence.

    Layers down-weight low-confidence headings; the planner's
    Kalman / smoother gates outliers using `confidence`.
    """
    bearing_deg: float                    # 0 = N, CW; world frame
    confidence: float                     # 0..1
    source: str = "unknown"               # "pca", "cnn", "vlm", "filtered"
    # Raw perception output *before* any tiebreak, physics reject, or
    # smoothing corrections were applied.  Populated by CNNHeading (and
    # future learned detectors) so offline analysis can score the raw
    # CNN independently of the runtime correction stack.  None means
    # "same as bearing_deg" (no correction happened).
    raw_bearing_deg: Optional[float] = None
    # Optional second-CNN output (for A/B comparison).  When
    # CNNHeading is constructed with a shadow checkpoint, the shadow
    # model's raw prediction is stashed here for every tick.  It does
    # NOT affect the runtime decision; it's logged so offline analysis
    # can compare two models on the same voyage frames.
    shadow_bearing_deg: Optional[float] = None
    shadow_confidence: Optional[float] = None


@dataclass
class CommitDirection:
    """The bot's persistent desired heading.

    Survives single-tick perception jitter and bow-rotation-by-
    collision.  Updated only by Layer 4 (tactical) or Layer 5
    (strategic), or by the planner on hard structural events.
    """
    bearing_deg: float
    reason: str                           # why this value was set
    set_at_tick: int = 0


@dataclass
class PlannerOutput:
    """Layer 3's tick output."""
    shore_pts: list[tuple[int, int]] = field(default_factory=list)
    path_pts: list[tuple[int, int]] = field(default_factory=list)
    waypoint_px: Optional[tuple[int, int]] = None
    command: Optional[str] = None         # "hold_left" | "hold_right" | None
    hold_ms: int = 0
    skip_reason: Optional[str] = None     # "no_shore" | "no_heading" | …
    # HybridPlanner records which primitive ran this tick so traces
    # show "channel→centerline" vs "lake→shore_hug".  None when only
    # a single primitive is in use.
    topology: Optional[str] = None        # "channel" | "junction" | …
    primitive: Optional[str] = None       # "shore_hug" | "centerline"
    wp_note: Optional[str] = None         # ValidatedPlanner audit trail
                                          # e.g. "rejected:out_of_bounds→slid_to(0.5)"


@dataclass
class TacticalDecision:
    """Layer 4's output when it fires.  None on the ticks it skips."""
    classification: str                   # "channel" | "junction" | "lake" | …
    new_commit_heading: Optional[float]   # if the consult overrode
    rationale: str
    decided_at_tick: int


@dataclass
class StrategicDirective:
    """Layer 5's output when it fires."""
    kind: str                             # "new_skill" | "new_target" | "abort"
    payload: dict
    decided_at_tick: int


@dataclass
class NavState:
    """Per-tick navigation state, passed by reference through the
    layer stack.  Fields are populated by the layer that owns them.
    """
    tick: int = 0

    # Layer 1 — heading
    heading: Optional[Heading] = None
    heading_history: deque = field(default_factory=lambda: deque(maxlen=16))

    # Layer 2 — segmentation
    water_mask: Optional[np.ndarray] = None  # bool, same shape as minimap
    seg_meta: dict = field(default_factory=dict)
    # Frame-to-frame translation (dy, dx) measured by shore-edge phase
    # correlation between last tick's water mask and this tick's.
    # `confidence` is cv2.phaseCorrelate's response peak height (0..1);
    # below ~0.3 = registration failure.  Sign: a point at
    # prev_frame[r, c] appears at curr_frame[r + dy, c + dx] this tick.
    # Populated by AiNavPipeline; Phase 1 is measurement-only (no
    # consumers), Phase 2+ wire into tactical + reflex per
    # docs/pixel_continuity_design.md.
    frame_shift_px: Optional[tuple[int, int, float]] = None
    # EMA of `hypot(dy, dx)` over recent high-confidence frame shifts.
    # Used to scale speed-sensitive pixel thresholds (tracker radius,
    # turning-approach, reflex min-reach, radial-scan range) so a
    # 27-kt fast ship doesn't overshoot 8.5-kt-calibrated thresholds.
    # None until we have enough history; consumers fall back to their
    # baseline constants.
    expected_shift_px: Optional[float] = None
    # Rolling maximum speed seen over the last ~30 ticks.  Used as a
    # proxy for the ship's top cruising speed — different for slow
    # (~8.5 kt) vs fast (~27 kt) ships.  Consumers (e.g., the
    # heading physics-reject filter) scale bounce-recovery trust by
    # `curr_speed / top_speed_est`: at low ratios the ship is likely
    # in/just-after a bounce, so big Δheading should get more weight.
    top_speed_est: Optional[float] = None
    # Bearing (compass deg) inferred from lat/lon Δ over recent ticks.
    # Reflects the ship's ACTUAL direction of travel — used by the
    # tactical walker as `init_bearing` at fork picks so hug-port
    # always picks the same physical shore regardless of whether ship
    # is on outbound (south) or return (north) leg.  None when motion
    # magnitude is outside the trust range (ship stopped or teleported).
    motion_bearing_deg: Optional[float] = None
    # Bearing (compass deg) derived directly from `frame_shift_px`
    # signal — weighted average of recent high-conf shifts.
    # `bearing = atan2(-dx, dy)` because world content moves OPPOSITE
    # to ship motion.  This is the most definitive direction reference
    # available: per-tick (no OCR latency), independent of HUD OCR
    # jitter, and grounded in what actually happened in the world.
    # Preferred over `motion_bearing_deg` (lat/lon-based) and
    # `heading.bearing_deg` (CNN spin corruption).  None when we don't
    # have enough high-conf shift history yet.
    shift_motion_bearing_deg: Optional[float] = None

    # Layer 3 — planner
    commit_direction: Optional[CommitDirection] = None
    planner_output: Optional[PlannerOutput] = None

    # Layer 4 — tactical (the latest decision; older ones go to history)
    tactical: Optional[TacticalDecision] = None
    tactical_history: list = field(default_factory=list)
    # Persistent tactical waypoint in world coords (lat, lon).  Written
    # by LookaheadTactical; read by HugPathPlanner as the far target.
    tactical_dest_latlon: Optional[tuple] = None
    # Mission's ultimate destination in world coords (lat, lon).
    # Written by PointToPointMission on every tick where the mission is
    # active; read by LookaheadTactical to compute a *dynamic* goal
    # bearing from ship→dest each pick, replacing the static
    # `--commit-bearing` CLI arg.  None when no destination mission is
    # active (NoOpMission, ExploreMission).
    mission_dest_latlon: Optional[tuple] = None
    # Sequence of (lat, lon) points along the water path from ship to
    # the tactical destination — the "curved route".  Reflex picks a
    # local waypoint by walking backward along this and finding the
    # farthest point with straight-line water reach.
    tactical_walked_path: Optional[list] = None
    # Phase 2 (docs/pixel_continuity_design.md §5): pixel offset
    # (row, col) from ship centre to the tactical dest.  Set only for
    # TURNING-POINT dests; shift-integrated each tick via
    # `state.frame_shift_px`.  None for SEARCH-mode dests (which keep
    # lat/lon reprojection for now).
    tactical_dest_px_offset: Optional[tuple[int, int]] = None

    # Layer 5 — strategic
    strategic: Optional[StrategicDirective] = None
    strategic_history: list = field(default_factory=list)

    # Mission-owned dead-end memory.  Each entry is (lat, lon) of a
    # dead-end the tactical layer LOCK'd onto and the ship then
    # reached.  Written by the Mission layer; read by
    # LookaheadTactical to reject candidate dests within a
    # rejection radius of any visited dead-end.
    visited_dead_ends: list = field(default_factory=list)

    # Dead-reckoned position + breadcrumbs (carried by the planner)
    lat: Optional[float] = None
    lon: Optional[float] = None
    # Source of (lat, lon): "hud_ocr" when read from the sea HUD, else
    # "dead_reckon" (runner advances based on commanded heading + speed).
    # HUD reads are authoritative when present; dead-reckoning is the
    # fallback when OCR fails (sail-stopped, occlusion, etc.).
    latlon_source: Optional[str] = None
    # Current sailing speed in knots, read from sea-HUD OCR.  None when
    # OCR fails (text occluded, not on sea HUD).  Used by reward shape
    # for collision detection (sudden speed drop = bounce) and by the
    # learned controller as an input feature.
    speed_kt: Optional[float] = None
    # Side the bot is hugging — "port" (default) or "starboard".  For
    # phase 1 of the learned controller, always "port"; runtime
    # mirror-and-flip handles starboard inference.  Reserved here for
    # future goal-layer cases (islands requiring side switches).
    hug_side: str = "port"
    # Topology + exits exposed by L3 (HybridPlanner) for L5 missions
    # that need junction-aware decisions.  None on non-junction ticks.
    topology: Optional[str] = None
    junction_exits_compass: tuple = ()
    breadcrumbs: list = field(default_factory=list)
    cumulative_km: float = 0.0

    def snapshot(self) -> dict:
        """Plain-dict view for trace.jsonl.  No numpy arrays — those
        get summarized, not dumped."""
        out = {
            "tick": self.tick,
            "lat": self.lat,
            "lon": self.lon,
            "latlon_source": self.latlon_source,
            "speed_kt": self.speed_kt,
            "hug_side": self.hug_side,
            "cumulative_km": self.cumulative_km,
        }
        if self.heading is not None:
            out["heading_deg"] = self.heading.bearing_deg
            out["heading_conf"] = self.heading.confidence
            out["heading_source"] = self.heading.source
            if self.heading.raw_bearing_deg is not None:
                out["cnn_raw_heading_deg"] = self.heading.raw_bearing_deg
            if self.heading.shadow_bearing_deg is not None:
                out["shadow_cnn_heading_deg"] = self.heading.shadow_bearing_deg
                out["shadow_cnn_confidence"] = self.heading.shadow_confidence
        if self.commit_direction is not None:
            out["commit_deg"] = self.commit_direction.bearing_deg
            out["commit_reason"] = self.commit_direction.reason
            out["commit_set_at"] = self.commit_direction.set_at_tick
        if self.planner_output is not None:
            po = self.planner_output
            out["shore_pts"] = [list(p) for p in po.shore_pts]
            out["path_pts"] = [list(p) for p in po.path_pts]
            out["waypoint_px"] = list(po.waypoint_px) if po.waypoint_px else None
            out["action"] = po.command or "noop"
            out["hold_ms"] = po.hold_ms
            out["skip_reason"] = po.skip_reason
            out["topology"] = po.topology
            out["primitive"] = po.primitive
            if po.wp_note is not None:
                out["wp_note"] = po.wp_note
        if self.water_mask is not None:
            out["water_frac"] = float(self.water_mask.mean())
        if self.frame_shift_px is not None:
            dy, dx, conf = self.frame_shift_px
            out["frame_shift_px"] = [dy, dx, round(conf, 3)]
        if self.expected_shift_px is not None:
            out["expected_shift_px"] = round(self.expected_shift_px, 1)
        if self.top_speed_est is not None:
            out["top_speed_est"] = round(self.top_speed_est, 1)
        if self.motion_bearing_deg is not None:
            out["motion_bearing_deg"] = round(self.motion_bearing_deg, 1)
        if self.shift_motion_bearing_deg is not None:
            out["shift_motion_bearing_deg"] = round(self.shift_motion_bearing_deg, 1)
        if self.tactical is not None:
            out["tactical_class"] = self.tactical.classification
            out["tactical_decided_at"] = self.tactical.decided_at_tick
        if self.tactical_dest_latlon is not None:
            out["tactical_dest_latlon"] = list(self.tactical_dest_latlon)
        if self.tactical_dest_px_offset is not None:
            out["tactical_dest_px_offset"] = list(self.tactical_dest_px_offset)
        if self.tactical_walked_path:
            out["tactical_walked_path_len"] = len(self.tactical_walked_path)
        if self.strategic is not None:
            out["strategic_kind"] = self.strategic.kind
            out["strategic_decided_at"] = self.strategic.decided_at_tick
        if self.visited_dead_ends:
            out["visited_dead_ends"] = [list(x) for x in self.visited_dead_ends]
        return out
