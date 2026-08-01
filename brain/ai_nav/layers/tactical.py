"""Layer 4 — mid-frequency tactical consult.

Fires every ~5-10 seconds, or on stuck-detector / sustained
no_shore / low-confidence-heading streak.  Asks a VLM a structured
question and converts the answer into a `TacticalDecision` that
can update `commit_direction`.

Cadence is the key: this layer MUST NOT fire per tick.  Each call
costs ~1-3 sec wall time on a Mac mini (Moondream via Ollama).
The pipeline calls `maybe_consult` every tick; the layer's
`_should_fire` decides whether to actually run inference.

Pluggable impls (swap via `PipelineConfig`):
  - `NoOpTactical` (default)        — does nothing
  - `MoondreamTactical`             — Moondream via Ollama (works today)
  - `QwenVLTactical`                — Qwen2-VL via MLX (stub)
  - `ClaudeTactical`                — Claude API (stub)

See `docs/ai_navigation_landscape.md` §5.4 and §3 for the model
choice rationale.
"""
from __future__ import annotations

import logging
import math
import re
import time
from typing import Optional, Protocol

from brain.ai_nav.state import CommitDirection, NavState, TacticalDecision
from brain.ai_nav.vision_input import VisionFrame

log = logging.getLogger(__name__)


# Direction tokens the consult layers emit, mapped to bearing offsets
# from current commit_direction.
DIRECTION_OFFSETS_DEG = {
    "forward":  0.0,
    "left":   -90.0,
    "right":  +90.0,
    "back":  +180.0,
}


class TacticalLayer(Protocol):
    name: str

    def maybe_consult(self, frame: VisionFrame, state: NavState
                      ) -> NavState: ...


class NoOpTactical:
    """Default — does nothing.  Pipeline runs end-to-end with this."""
    name = "noop"

    def maybe_consult(self, frame, state):
        return state


# ── Base mixin: cadence + trigger gate ────────────────────────────────


class BaseTactical:
    """Provides the cadence + trigger gate.  Subclasses implement
    `_consult(frame, state) -> TacticalDecision | None`.

    Subclasses should be cheap to construct (Moondream/Qwen model
    loading should happen lazily on the first `_consult`).
    """

    def __init__(
        self,
        heartbeat_sec: float = 60.0,
        consecutive_no_shore_trigger: int = 5,
        consecutive_low_conf_trigger: int = 8,
        low_conf_floor: float = 0.25,
    ):
        self.heartbeat_sec = heartbeat_sec
        self.consecutive_no_shore_trigger = consecutive_no_shore_trigger
        self.consecutive_low_conf_trigger = consecutive_low_conf_trigger
        self.low_conf_floor = low_conf_floor
        self._last_fire_ts: float = 0.0
        self._no_shore_streak = 0
        self._low_conf_streak = 0

    def _should_fire(self, frame: VisionFrame, state: NavState) -> Optional[str]:
        # Heartbeat — minimum cadence regardless of state.
        if (frame.wall_ts - self._last_fire_ts) >= self.heartbeat_sec:
            return "heartbeat"

        # Stuck — repeated no_shore from planner.
        po = state.planner_output
        if po and po.skip_reason == "no_shore":
            self._no_shore_streak += 1
        else:
            self._no_shore_streak = 0
        if self._no_shore_streak >= self.consecutive_no_shore_trigger:
            return "no_shore_streak"

        # Low-confidence heading streak (perception unreliable).
        if state.heading and state.heading.confidence < self.low_conf_floor:
            self._low_conf_streak += 1
        else:
            self._low_conf_streak = 0
        if self._low_conf_streak >= self.consecutive_low_conf_trigger:
            return "low_heading_conf_streak"

        return None

    def maybe_consult(self, frame: VisionFrame, state: NavState) -> NavState:
        reason = self._should_fire(frame, state)
        if reason is None:
            return state
        self._last_fire_ts = frame.wall_ts
        log.info("[tactical] firing — trigger=%s tick=%d", reason, state.tick)
        decision = self._consult(frame, state)
        if decision is None:
            return state
        state.tactical = decision
        state.tactical_history.append(decision)
        if decision.new_commit_heading is not None:
            state.commit_direction = CommitDirection(
                bearing_deg=decision.new_commit_heading,
                reason=f"tactical({decision.classification})",
                set_at_tick=state.tick,
            )
            # Reset the no_shore streak — we've just acted on the dead-end.
            self._no_shore_streak = 0
        return state

    def _consult(self, frame: VisionFrame, state: NavState
                 ) -> Optional[TacticalDecision]:
        raise NotImplementedError


# ── Helper: parse a VLM's free-text answer into a direction token ─────


_FORWARD_PAT = re.compile(r"\b(forward|ahead|straight|continue|keep going)\b", re.I)
_LEFT_PAT    = re.compile(r"\b(left|port|west|counter[- ]?clockwise|ccw)\b", re.I)
_RIGHT_PAT   = re.compile(r"\b(right|starboard|east|clockwise|cw)\b", re.I)
_BACK_PAT    = re.compile(r"\b(back|reverse|turn around|behind|u[- ]?turn)\b", re.I)


def parse_direction(answer: str) -> Optional[str]:
    """Return one of forward|left|right|back, or None on indeterminate.

    Order matters: 'back' / 'reverse' beats 'left'/'right' (since
    "turn around" overlaps with "turn").
    """
    if not answer:
        return None
    a = answer.strip().lower()
    # Exact single-word answers first.
    if a in ("forward", "left", "right", "back"):
        return a
    if _BACK_PAT.search(a):
        return "back"
    if _FORWARD_PAT.search(a):
        return "forward"
    if _LEFT_PAT.search(a) and not _RIGHT_PAT.search(a):
        return "left"
    if _RIGHT_PAT.search(a) and not _LEFT_PAT.search(a):
        return "right"
    return None


def direction_to_commit_heading(
    direction: str,
    current_commit_deg: float,
) -> Optional[float]:
    """Convert a direction token into an absolute commit_direction.

    'forward' means "no change" — return None so the caller knows not
    to update commit_direction.
    """
    if direction == "forward":
        return None
    offset = DIRECTION_OFFSETS_DEG.get(direction)
    if offset is None:
        return None
    return (current_commit_deg + offset) % 360.0


# ── Moondream impl — works today via ollama ──────────────────────────


_MOONDREAM_PROMPT_TEMPLATE = (
    "This is the top-down minimap from a sailing game.  The green "
    "ship icon is in the center of the map.\n\n"
    "Water is TRANSLUCENT and has WAVY DECORATIVE LINES on it.  "
    "Because the water layer is see-through, colors from the "
    "overworld scene behind the minimap bleed through, so water "
    "can appear bluish, brownish, tan, or yellowish depending on "
    "what's behind it.  The reliable signature of water is the "
    "wavy line decoration plus the see-through quality.\n\n"
    "Land is SOLID and OPAQUE — nothing bleeds through it.  Land "
    "has NO wavy lines and is typically a flat whitish-gray "
    "(occasionally faintly tinted by the ship's translucent "
    "light-blue radar circle near the center).\n\n"
    "Ignore: yellow circular sprites (other ships and NPCs), "
    "white diamond markers (port indicators), and any text "
    "labels — these are overlays, not land.\n\n"
    "The ship is currently heading roughly {heading_dir} (compass "
    "{heading_deg:.0f}°).  It is trying to keep land on its "
    "{side} side and follow the shore.  Looking at the minimap, "
    "where should the ship steer next?  Reply with ONLY one "
    "word: forward, left, right, or back."
)


def _bearing_to_compass_dir(deg: float) -> str:
    """Convert 0-360° to a coarse compass word (N, NE, E, ...)."""
    dirs = ["north", "northeast", "east", "southeast",
            "south", "southwest", "west", "northwest"]
    idx = int((deg + 22.5) // 45) % 8
    return dirs[idx]


class MoondreamTactical(BaseTactical):
    """First concrete L4 impl, backed by Moondream via Ollama.

    Uses the minimap (frame.minimap()).  When the full-screen
    swap-in is ready, change `frame.minimap()` → `frame.full_screen()`
    and adjust the prompt; no other layer or pipeline change needed.

    On consult:
      1. Take the minimap (thumbnailed to ~400x190 → ~800x400 for
         Moondream input).
      2. Ask "where should the ship steer: forward, left, right, back?"
      3. Parse the answer to a direction token.
      4. Map to a new commit_direction (relative to current commit).
      5. Return a TacticalDecision with classification="moondream_<dir>".

    Returns None on:
      - Moondream unavailable (Ollama not running, model not pulled)
      - Indeterminate answer ("I'm not sure", empty, etc.)
      - Answer parses to 'forward' (no commit change needed)
    """
    name = "moondream_tactical"

    def __init__(
        self,
        side: str = "port",
        model_name: Optional[str] = None,
        **kwargs,
    ):
        """When `model_name` is given (e.g. "moondream", "llava:7b",
        "qwen2.5vl:7b"), this layer constructs a per-instance
        LocalVision against that exact Ollama model rather than
        deferring to `get_vision()`'s autodetection.  Use this to
        A/B different VLM backends without changing the system's
        default."""
        super().__init__(**kwargs)
        self.side = side
        self.model_name = model_name
        self._vision = None   # lazy

    def _ensure_vision(self):
        if self._vision is None:
            if self.model_name is None:
                from vision.local_vision import get_vision
                self._vision = get_vision()
            else:
                from vision.local_vision import LocalVision
                self._vision = LocalVision(model=self.model_name)
        return self._vision

    def _consult(self, frame: VisionFrame, state: NavState
                 ) -> Optional[TacticalDecision]:
        vision = self._ensure_vision()
        if not vision.check_available():
            log.warning("[moondream_tactical] Moondream unavailable — "
                        "skipping consult")
            return None

        heading_deg = state.heading.bearing_deg if state.heading else 0.0
        commit_deg = (state.commit_direction.bearing_deg
                     if state.commit_direction else heading_deg)
        prompt = _MOONDREAM_PROMPT_TEMPLATE.format(
            heading_dir=_bearing_to_compass_dir(heading_deg),
            heading_deg=heading_deg,
            side=self.side,
        )
        img = frame.minimap().copy()
        img.thumbnail((800, 400))
        try:
            raw = vision.ask(prompt, frame=img)
        except Exception as e:
            log.warning("[moondream_tactical] ask() failed: %s", e)
            return None

        direction = parse_direction(raw)
        log.info("[moondream_tactical] answer=%r → direction=%s",
                 raw, direction)
        if direction is None:
            return TacticalDecision(
                classification="moondream_unparsed",
                new_commit_heading=None,
                rationale=f"raw={raw!r}",
                decided_at_tick=state.tick,
            )
        new_bearing = direction_to_commit_heading(direction, commit_deg)
        return TacticalDecision(
            classification=f"moondream_{direction}",
            new_commit_heading=new_bearing,
            rationale=f"direction={direction}; raw={raw!r}",
            decided_at_tick=state.tick,
        )


# ── Future impls — kept as stubs ──────────────────────────────────────


class QwenVLTactical(BaseTactical):
    """Qwen2-VL-7B in MLX — sweet spot for grounding on Mac mini.

    Calls `frame.full_screen()` (default) or `frame.minimap()`.
    Output parsed same way as MoondreamTactical.

    Not implemented yet — pending MLX install + Qwen weights download.
    Recommended once MoondreamTactical proves the architecture.
    """
    name = "qwen_vl_tactical"

    def __init__(self, model_path, **kwargs):
        super().__init__(**kwargs)
        self.model_path = model_path
        raise NotImplementedError(
            "QwenVLTactical not implemented yet — see "
            "docs/ai_navigation_landscape.md §5.4"
        )

    def _consult(self, frame, state):
        raise NotImplementedError


class ClaudeTactical(BaseTactical):
    """Claude API tactical consult.  Smartest option, slowest tick,
    actual $$ per call.  Use sparingly — better suited to L5
    strategic.  Stub for now."""
    name = "claude_tactical"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        raise NotImplementedError(
            "ClaudeTactical not implemented yet — use ClaudeStrategic "
            "in layer 5 instead."
        )

    def _consult(self, frame, state):
        raise NotImplementedError


# ── Lookahead tactical (bank-tracer substrate, per-tick, no VLM) ──────


class LookaheadTactical:
    """Per-tick tactical that maintains a **destination anchor** (lat, lon)
    on a frame-edge exit.  Commit_direction each tick = bearing from the
    current ship position to that anchor.

    Substrate: the **bank tracer** (`tools/bank_tracer.py`
    `trace_bank_to_edge`) — walk the ship's water-CC contour on `hug_side`
    to the first frame-edge exit, yielding one polyline per tick (no
    skeleton, no spurs, no sprite artifacts).  The class name and
    `--tactical lookahead` flag are retained from the retired
    skeleton-walk substrate; the skeleton-lookahead / edge-anchored-
    centerline designs are OBSOLETE — see the "Tactical anchor substrate
    — CURRENT" pointer in CLAUDE.md.

    Anchor policy over the polyline endpoint (see `maybe_consult`):
      - **sticky** — smooth tracking of the same edge's water-run midpoint;
      - **migrate** — adopt a *validated* (persistent + streak +
        route-plausible) cross-edge exit (e.g. Nubia bot→right bend);
      - **new_anchor / bootstrap** — cold pick when no sticky option;
      - **hold(pocket_ahead)** — dead-end pocket: when the previous anchor
        is still on the trace but the endpoint has leapt far beyond it and
        the ship hasn't reached it, hold the anchor at the tip (blocks the
        ~100° commit leap from a detaching dead-end tip);
      - **reach-gate** — defer a >90° reversal-migrate until the ship has
        reached the tip (within `TURNING_APPROACH_PX`), so it fully enters
        a dead-end before turning around.

    HUD OCR requirement: this layer needs state.lat/lon each tick.
    When OCR fails, hold the prior commit_direction unchanged.
    """
    name = "lookahead_tactical"

    LOOKAHEAD_PX          = 50
    SAFE_APPROACH_DIST_PX = 25
    ARRIVAL_KM            = 3.0
    PX_PER_DEG            = 100.0     # sim canvas + live measured ~100 px/°
    HUG_OFFSET_PX         = 10        # target distance from hug-side shore
                                      # in wide channels; centerline fallback
                                      # when the offset lands on land.
    # A channel narrower than 2*NARROW_DT_MIN px is treated as a
    # blocked deadend — walker stops there.  User-chosen 2026-07-21
    # after the pocket-entry pathology; ships physically cannot thread
    # sub-5px chokes in the sim.
    NARROW_DT_MIN         = 2.5       # DT<2.5 ⇔ channel width < 5 px
    # Turning-point approach threshold.  When ship is within this many px
    # of a turning-point dest (narrow_choke / river-bend pivot / leaf
    # terminus), fire the walker unguarded to pick the next leg.  40 px
    # ≈ 40 km — wide enough that ships bouncing near a Y-tip pocket
    # (own hull ≈ 20 px) still trip arrival and get a new dest.
    TURNING_APPROACH_PX   = 40
    # No-revisit rejection radius.  If a turning-point candidate is
    # within this many km of any lat/lon in state.visited_dead_ends,
    # the pick is rejected and the walker's result is downgraded to
    # `frame_edge` so we don't loop back to the same target we already
    # explored.  Populated by the Mission layer (DeadEndMemoryMission).
    VISITED_REJECT_KM     = 2.0

    # Reasons that mean "dest is a turning point — ship approaches it,
    # then the walker picks the next leg unguarded on arrival."
    #   - narrow_choke:  sub-5px channel width from walker (physical
    #                    filter — ship can't thread it)
    #   - turning_point: (a) reclassified frame_edge whose tracker
    #                        failed (river bend — dest scrolled off
    #                        frame edge), OR
    #                    (b) walker terminated at a leaf (Y-tip / pocket
    #                        end); folded from the old `dead_end` reason.
    # Anything else (frame_edge, mid_walk, visited_downgrade,
    # not_newly_visible) is SEARCH: tracker re-derives every tick.
    _TURNING_POINT_REASONS = frozenset(("narrow_choke", "turning_point"))

    def __init__(self, hug_side: str = "port",
                 goal_bearing_deg: Optional[float] = None):
        """
        hug_side: 'port' or 'starboard' — which shore to prefer at forks
        goal_bearing_deg: mission's overall goal bearing (compass deg).
            Used as the STABLE reference for skeleton lookahead's initial
            walk direction — prevents drift when local terrain rotates
            the commit away from the mission goal.  Pass the same value
            as --commit-bearing.  Default None → uses ship heading fallback.
        """
        assert hug_side in ("port", "starboard")
        self.hug_side = hug_side
        self.goal_bearing_deg = goal_bearing_deg
        self._current_dest: Optional[tuple[float, float]] = None  # (lat, lon)
        self._current_dest_reason: str = "init"
        self._current_walked_path_latlon: list = []      # last accepted walk
        self._last_walked_path_latlon: list = []         # scratch during pick
        self._last_commit_deg: Optional[float] = None
        # Ship's (lat, lon) at the end of the previous consult — used
        # to test whether a candidate LOCK dest was already visible
        # in last tick's mini-map (spurious junction flip if so).
        self._prev_ship_ll: Optional[tuple[float, float]] = None
        # Pixel offset (row, col) from ship centre to the current dest.
        # Phase 2 covered TURNING-POINT dests; Phase 3 extends to SEARCH.
        # Shift-integrated each tick via state.frame_shift_px so the
        # dest's identity is world-locked and decoupled from HUD OCR.
        self._current_dest_px_offset: Optional[tuple[int, int]] = None
        # Phase 3: frame-edge water-run count from the previous tick.
        # Used to detect fork emergence — when the count increases, a
        # new candidate exit has appeared; hand off to the walker with
        # hug-side rather than let the tracker smoothly drift to
        # whichever midpoint happens to be nearer.  Missing this signal
        # caused the t358 fork mispick in ai_nav_2026-07-22T20-33-50.
        self._prev_frame_edge_run_count: Optional[int] = None
        # Corner-split detection state (2026-07-25).  For each of the 4
        # corners, remember whether the previous tick's mask had water
        # continuous across both adjacent edges through the corner
        # region.  Used by _corner_split_since_last_tick.
        #   {"TL", "TR", "BL", "BR"} — corners connected LAST tick.
        self._prev_connected_corners: set[str] = set()
        # Bank-tracer-primary state (branch 2026-07-25): last tick's
        # polyline in mini-map pixel coords.
        self._prev_polyline_yx: Optional[list[tuple[int, int]]] = None
        # Persistent trace-start pixel (2026-07-27).  World-fixed
        # shore point where the trace begins.  Shift-integrated each
        # tick.  When still valid (in reachable water, close to ship,
        # near a contour), used as start_yx for trace_bank_to_edge —
        # bypasses _project_ship's cliff sensitivity on approach dir.
        self._trace_start_yx: Optional[tuple[int, int]] = None
        # Sticky-anchor state (Fix B, 2026-07-27): which frame edge
        # (top/bot/left/right) the current tactical WP sits on.  Used
        # to look for the same-edge water-run midpoint each tick and
        # keep the anchor pinned to that specific exit until it
        # genuinely disappears.
        self._prev_wp_edge: Optional[str] = None
        # Per-edge water-run existence from prev tick — used by
        # migration rule: migrate anchor to trace-endpoint's edge
        # ONLY when that edge was NOT present in prev tick (a
        # genuinely new exit came into view).  If the edge existed
        # before, the trace picking it is likely a port-hug-cliff
        # perception noise, not a real topology change.
        #
        # Default all True (unknown history — assume edges were
        # there).  This suppresses spurious first-tick migration
        # from a pre-seeded sticky state to trace endpoint on a
        # different edge.  Real migrations happen when we observe
        # an edge transition from absent→present.
        self._prev_edges_had_water: dict[str, bool] = {
            "top": True, "bot": True, "left": True, "right": True,
        }
        # Per-edge "consecutive ticks with water present" counter.
        # Resets to 0 when the edge is absent for a tick.  An edge
        # with a SMALL counter (say <10) is "recently new" — likely
        # a legitimate topology reveal.  A large counter means the
        # edge has always been there (any migration to it is
        # port-hug perception noise).
        self._edge_present_streak: dict[str, int] = {
            "top": 999, "bot": 999, "left": 999, "right": 999,
        }
        # Count of consecutive ticks where the trace endpoint has
        # been on a specific different-from-prev edge.  Reset when
        # trace endpoint edge changes or matches prev_wp_edge.
        # Used to filter single-tick perception noise from
        # persistent topology-change migrations.
        self._trace_diff_edge: Optional[str] = None
        self._trace_diff_edge_ticks: int = 0
        # Count of consecutive ticks a `new_anchor` wants a discontinuous
        # commit REVERSAL (see the artifact-reversal guard in the cascade).
        # A transient perception artifact (e.g. a bright glare band severing
        # the channel, t721) clears in 1-2 ticks; a genuine reversal persists.
        self._reversal_artifact_ticks: int = 0
        # Hug-side persistence state.  `_last_valid_hug_wp_offset` is the
        # (row, col) offset of the last commit that kept the coast on the
        # HUG side (land on port).  When a candidate commit would flip the
        # coast to the far side, hold this instead of doubling back.
        # `_hug_violation_ticks` bounds how long we hold before releasing
        # into free navigation (coast genuinely gone).
        self._last_valid_hug_wp_offset: Optional[tuple[int, int]] = None
        self._hug_violation_ticks: int = 0

    def reset(self) -> None:
        """Clear all per-voyage anchor state back to cold start (keeps
        `hug_side` + `goal_bearing_deg`).

        The live runner warms up the perception models with a throwaway
        `pipe.tick(warm_state)` before the real loop — but the pipeline
        layers are SHARED objects, so that warm-up runs THIS tactical on an
        UNSEEDED warm_state (planner default commit = 180° south) and leaves
        the anchor bootstrapped SOUTH (`_last_commit_deg≈180`,
        `_prev_wp_edge="bot"`, a southward `_current_dest_px_offset`).  The
        real, `--commit-bearing`-seeded run then inherited that stale anchor
        and sailed the wrong way (Cairo runs 2026-07-30 went south despite
        `--commit-bearing 0`).  The runner calls this after the warm-up so
        the real first tick bootstraps fresh from the seeded commit."""
        self.__init__(self.hug_side, self.goal_bearing_deg)

    # Minimum registration confidence for shift-integration to trust
    # the measurement.  Below this (UI overlay, camera change, popup),
    # we hold the previous pixel offset unchanged for one tick.
    # 0.15 chosen after fast-ship (27 kt) observation: shifts of
    # 30 px on a 190-px-tall frame inherently produce lower peak
    # confidence than 7-px shifts do; a 0.30 gate rejected 54% of
    # legitimate fast-ship measurements.
    SHIFT_CONF_MIN = 0.15

    # Max consecutive ticks to hold the last hug-valid commit while the
    # coast is on the wrong side, before releasing into free navigation
    # (the coast has genuinely ended — open ocean).
    HUG_VIOLATION_MAX_HOLD = 20


    # Adaptive scaling: baseline constants below were calibrated on
    # 8.5-kt sailing (~7 px shift/tick).  On a 27-kt fast ship
    # (~30 px/tick) the same absolute pixel budgets translate to
    # under 2 ticks of motion — tracker fails, ship overshoots
    # turning points, reflex WPs land inside the ship's own arc.
    # We scale the following pixel thresholds by
    # `state.expected_shift_px` (an EMA of recent frame-shift
    # magnitudes populated by the pipeline).
    #
    #   scaled = max(baseline, k × expected_shift_px)
    #
    # `k` = "ticks of motion this threshold should cover".  Chosen
    # so that at baseline shift ≈ 7, the scaled value equals the
    # baseline (backwards-compat), and at fast-ship shift ≈ 30,
    # thresholds grow ~3-4×.
    # Conservative multipliers: at 27 kt (shift ≈ 30 px) these give
    # radius=90 and approach=90 — only marginal expansion over baseline
    # 80/40.  Bigger multipliers (voyage 2026-07-23T14:58) pushed the
    # tracker to 360 px, which made it accept over-land midpoints and
    # compounded shore collisions.
    _SCALE_TRACKER_RADIUS   = 3    # tracker searches ~3 ticks of motion
    _SCALE_TURNING_APPROACH = 3    # 3 ticks to decelerate into a turning pt

    def _scaled_tracker_radius_px(self, state: NavState) -> int:
        exp = state.expected_shift_px or 0.0
        return max(self.TRACKER_SEARCH_RADIUS_PX,
                   int(round(self._SCALE_TRACKER_RADIUS * exp)))

    def _scaled_turning_approach_px(self, state: NavState) -> int:
        exp = state.expected_shift_px or 0.0
        return max(self.TURNING_APPROACH_PX,
                   int(round(self._SCALE_TURNING_APPROACH * exp)))

    @staticmethod
    def _classify_frame_edge(y: int, x: int, H: int, W: int, margin: int
                             ) -> Optional[str]:
        """Which frame edge a point sits on — the CLOSEST one, or None if the
        point is interior (all four distances > margin).

        Closest-edge (not a fixed top>bot>left>right priority) so a CORNER
        point resolves to the edge it's actually on: the port bank curving
        west exits the top-LEFT corner at (3, 0) — y=3 and x=0 are both within
        the margin, but x=0 is closer, so it's a "left" exit, not "top".  The
        old priority chain always called such corners "top", so a coast turning
        onto the left edge was invisible to the migration logic and the anchor
        clung to sticky(top) forever (Nile→Med exit, t796+)."""
        dist = {"top": y, "bot": H - 1 - y, "left": x, "right": W - 1 - x}
        edge, dmin = min(dist.items(), key=lambda kv: kv[1])
        return edge if dmin <= margin else None

    def maybe_consult(self, frame: VisionFrame, state: NavState) -> NavState:
        """Bank-tracer-primary flow — tactical-WP-driven variant
        (branch bank-tracer-primary, 2026-07-26).

        Terminology:
          - anchor    = any frame-edge water-run midpoint (an exit)
          - tactical WP = the specific anchor we've committed to
          - reflex WP  = farthest straight-line-water-reachable point on
                         the tactical polyline (picked by HugPathPlanner)

        Every tick:
          1. Shift prev tactical WP by frame_shift → shifted_wp
          2. Validity: is shifted_wp still an anchor?
              - on reachable water in this frame's ship-CC
              - near a frame edge (within SNAP_RADIUS_PX × 2)
              → YES: reuse it; trace_bank_to_edge from ship-projection to
                     wp-projection along the shorter contour arc.
              → NO:  topology change (or arrival); re-pick a new anchor
                     from the frame-edge enumeration (bootstrap).
          3. Snap tactical WP to nearest frame-edge run midpoint if close.
          4. Expose polyline as tactical_walked_path (HugPathPlanner
             reflex picks the WP for steering).

        Removed vs. earlier bank_tracer variants:
          - approach-direction chain (no longer needed once we have a WP)
          - trial-walk cliff sensitivity (shorter-arc from projection to
            projection is direction-unambiguous)
          - sticky anchor-held override (WP validity check handles it)
          - corner-split rule (topology changes → WP invalid → re-pick)

        Bootstrap (no prev WP or WP invalidated) still uses trace_bank_to_edge
        with hug_side + approach direction — that's the only place we
        need a heuristic to pick which anchor to commit to.
        """
        import math
        if state.lat is None or state.lon is None:
            return self._hold_prior_commit(state)
        if state.water_mask is None:
            return self._hold_prior_commit(state)

        try:
            from tools.bank_tracer import trace_bank_to_edge
            from scipy.ndimage import label as cc_label
        except ImportError:
            return self._hold_prior_commit(state)

        H, W = state.water_mask.shape
        ship_row, ship_col = H // 2, W // 2

        labeled, _ = cc_label(state.water_mask)
        ship_cc = labeled[ship_row, ship_col]
        if ship_cc == 0:
            reachable = state.water_mask.astype(bool)
        else:
            reachable = (labeled == ship_cc)

        # Step 1: shift prev tactical WP.
        shifted_wp: Optional[tuple[int, int]] = None   # (row, col) absolute
        if self._current_dest_px_offset is not None:
            row_off, col_off = self._current_dest_px_offset
            if state.frame_shift_px is not None:
                dy, dx, conf = state.frame_shift_px
                if conf >= self.SHIFT_CONF_MIN:
                    row_off, col_off = row_off + dy, col_off + dx
            shifted_wp = (ship_row + row_off, ship_col + col_off)

        # Step 2: validity check.
        wp_valid = (shifted_wp is not None
                    and self._anchor_still_valid(reachable, *shifted_wp))

        polyline: Optional[list[tuple[int, int]]] = None
        source: str
        wp_yx: Optional[tuple[int, int]] = None

        # New unified flow (2026-07-27): traceline production is
        # INDEPENDENT of the tactical WP.  We always run
        # trace_bank_to_edge with hug_side + a stable approach direction
        # (last commit = ship-to-prev-WP bearing, world-fixed).
        # Then we cross-check against the shifted-prev-WP:
        #   - if shifted_wp is close to any point on the traceline
        #     → traceline is legit, tactical WP stays at shifted_wp
        #       (unless prev WP has scrolled off-frame, then new WP =
        #        traceline endpoint)
        #   - if shifted_wp is NOT on the traceline → suspect traceline;
        #     hold prior tactical WP but log divergence
        # PRIMARY: ship→reflex_WP direction (from state.planner_output —
        # planner runs before tactical, so this is CURRENT tick's reflex
        # WP based on prev tick's tactical walked path).  This direction
        # is the LOCAL bank the ship is following, unlike commit_deg
        # (ship→tactical_WP) which points at the far-away mission
        # destination and can be misaligned with the immediate river
        # curvature.  Session ai_nav_2026-07-27T20-20-44 t805: commit=258°
        # WSW → port perp = SSE → ray shoots across the S-bend to hit
        # far bank at 51px.  ship→reflex=315° NW → port perp = SW → ray
        # hits the near bank at ~10px, walker walks the same bank in the
        # ship's actual travel direction.
        approach_for_projection: Optional[float] = None
        approach_src: str = ""
        # Priority 1: ship→reflex_WP direction (planner runs before
        # tactical, so state.planner_output.waypoint_px is CURRENT tick's
        # reflex WP based on prev tick's tactical walked path).  This is
        # the LOCAL bank the ship is following.  Prior scheme used commit
        # direction (ship→tactical_WP) which points at the far mission
        # destination and can be misaligned with the immediate river
        # topology.  Session ai_nav_2026-07-27T20-20-44 t805: commit=258°
        # WSW gave port perp SSE → ray crossed the S-bend to hit far bank
        # at 51 px.  ship→reflex=315° NW gives port perp SW → ray hits
        # near bank at ~10 px, walker walks the same bank the ship is on.
        po = state.planner_output
        if po is not None and po.waypoint_px is not None:
            r, c = po.waypoint_px
            rdy = r - ship_row; rdx = c - ship_col
            if math.hypot(rdy, rdx) > 5.0:
                approach_for_projection = (
                    math.degrees(math.atan2(rdx, -rdy)) + 360.0) % 360.0
                approach_src = "ship_to_reflex"
        # Priority 2: last commit direction (world-anchored ship→tactical)
        if approach_for_projection is None and self._last_commit_deg is not None:
            approach_for_projection = self._last_commit_deg
            approach_src = "last_commit"
        # Priority 3: bearing to shifted-prev-WP (world-anchored)
        if approach_for_projection is None and shifted_wp is not None:
            pdy = shifted_wp[0] - ship_row
            pdx = shifted_wp[1] - ship_col
            if math.hypot(pdy, pdx) > 5.0:
                approach_for_projection = (
                    math.degrees(math.atan2(pdx, -pdy)) + 360.0) % 360.0
                approach_src = "prev_wp_bearing"

        if approach_for_projection is None:
            # Bootstrap chain (no anchor, no commit yet)
            shift_brg = getattr(state, "shift_motion_bearing_deg", None)
            motion_brg = getattr(state, "motion_bearing_deg", None)
            if shift_brg is not None and motion_brg is not None:
                diff = abs(((shift_brg - motion_brg + 540.0) % 360.0) - 180.0)
                if diff > 90.0:
                    shift_brg = None
            if shift_brg is not None:
                approach_for_projection = shift_brg
                approach_src = "shift_motion"
            elif motion_brg is not None:
                approach_for_projection = motion_brg
                approach_src = "motion"
            elif state.heading is not None:
                approach_for_projection = state.heading.bearing_deg
                approach_src = "heading"
            elif self.goal_bearing_deg is not None:
                approach_for_projection = self.goal_bearing_deg
                approach_src = "goal"
            else:
                approach_for_projection = 180.0
                approach_src = "default"

        # Fresh start each tick (2026-07-27): _project_ship picks the
        # nearest port-side contour point from ship, perpendicular to
        # the desired direction.  Never shift-integrate the start —
        # accumulated per-tick frame_shift errors (couple px each)
        # add up to ~200 px drift over 170 ticks and put the start
        # on the wrong bank (session ai_nav_2026-07-27T15-53-21 t174).
        # Fresh pick each tick eliminates that drift entirely.
        #
        # Sanity check: log if fresh pick diverges from shifted-prev
        # pick by more than SHIFT_JUMP_TOLERANCE_PX (marker of
        # perception noise or genuine topology change), but adopt
        # the fresh pick anyway.
        polyline = trace_bank_to_edge(
            reachable, (ship_row, ship_col),
            approach_for_projection, hug_side=self.hug_side,
            offset_px=10, start_yx=None,
        )
        # Sanity-check pick vs prev+shift.
        if (polyline and self._trace_start_yx is not None
                and state.frame_shift_px is not None):
            dy, dx, conf = state.frame_shift_px
            if conf >= self.SHIFT_CONF_MIN:
                expected_y = self._trace_start_yx[0] + dy
                expected_x = self._trace_start_yx[1] + dx
                actual_y, actual_x = polyline[0]
                jump = math.hypot(actual_y - expected_y,
                                  actual_x - expected_x)
                if jump > 30:  # more than ~3 ticks worth of motion
                    log.info("[bank_tracer_primary] t=%d start jump=%.0fpx "
                             "(prev+shift=(%d,%d) fresh=(%d,%d)) — perception "
                             "noise or topology change",
                             state.tick, jump,
                             expected_y, expected_x, actual_y, actual_x)

        # Cache the actual start for next tick (polyline[0]).
        if polyline and len(polyline) >= 1:
            self._trace_start_yx = polyline[0]

        wp_yx: Optional[tuple[int, int]] = None
        source: str

        if not polyline or len(polyline) < 2:
            log.warning("[bank_tracer_primary] t=%d trace_bank_to_edge "
                        "failed, holding prior", state.tick)
            return self._maybe_commit_prior(state)

        # Snap traceline endpoint to nearest edge midpoint.
        snap_end = self._snap_to_edge_midpoint(reachable, *polyline[-1])
        if snap_end is not None:
            polyline[-1] = snap_end

        # TRACE ENDPOINT decides the anchor edge; sticky smooths within
        # the same edge (2026-07-27 refinement).
        #
        # Rule:
        #   - Classify polyline endpoint's frame edge (top/bot/left/right)
        #   - If SAME as prev_wp_edge:
        #     - sticky path — use _find_run_midpoint_on_edge to smooth
        #       against shifted-prev-WP position (WP moves along the edge
        #       as the water-run midpoint shifts with mask topology)
        #   - If DIFFERENT edge (or no prev):
        #     - anchor migration — adopt polyline endpoint directly
        #     - covers: bot exit closed & tracer found new right exit
        #       (Nubia bend); left-bot exit emerged past dead-end
        #       (Y-tip return); cold-start bootstrap
        #
        # This makes the trace geometry authoritative for which edge is
        # the anchor, while sticky preserves smooth motion within an
        # edge.  No arbitrary distance thresholds — the trace itself
        # verifies reachability (it walked from ship to that endpoint).
        H_r, W_r = reachable.shape
        end_y, end_x = polyline[-1]
        EDGE_MARGIN = self.SNAP_RADIUS_PX * 2   # 24 px
        trace_end_edge = self._classify_frame_edge(
            end_y, end_x, H_r, W_r, EDGE_MARGIN)

        # Compute current per-edge water-run existence for the
        # "new edge" migration check.
        cur_edges_had_water = {
            "top":   any(reachable[0, :]),
            "bot":   any(reachable[H_r-1, :]),
            "left":  any(reachable[:, 0]),
            "right": any(reachable[:, W_r-1]),
        }
        # Update per-edge streak counters (consecutive ticks present).
        for e, present in cur_edges_had_water.items():
            if present:
                self._edge_present_streak[e] = min(
                    999, self._edge_present_streak[e] + 1)
            else:
                self._edge_present_streak[e] = 0
        ANCHOR_SEARCH_RADIUS_PX = 60

        # Sticky-anchor first: try to find the prev edge's water-run
        # midpoint near shifted-prev-WP.  If found, that's where the
        # WP should be (smoothed tracking of the same edge exit).
        sticky_mid = None
        if self._prev_wp_edge is not None and shifted_wp is not None:
            search_y = max(0, min(H_r - 1, shifted_wp[0]))
            search_x = max(0, min(W_r - 1, shifted_wp[1]))
            sticky_mid = self._find_run_midpoint_on_edge(
                reachable, self._prev_wp_edge,
                search_y, search_x, ANCHOR_SEARCH_RADIUS_PX,
            )

        # Track how long the trace has consistently pointed at a
        # different edge (filters single-tick perception noise).
        if (trace_end_edge is not None
                and trace_end_edge != self._prev_wp_edge):
            if trace_end_edge == self._trace_diff_edge:
                self._trace_diff_edge_ticks += 1
            else:
                self._trace_diff_edge = trace_end_edge
                self._trace_diff_edge_ticks = 1
        else:
            self._trace_diff_edge = None
            self._trace_diff_edge_ticks = 0

        # Decision order:
        # 1. NEW edge migration (fires either when the edge appeared
        #    this tick OR trace has consistently pointed at it for
        #    MIGRATE_PERSISTENCE_TICKS+ ticks — filters perception
        #    noise while allowing delayed migration when trace picks
        #    up a new exit a few ticks after it appears).
        # 2. Sticky same-edge: trace still on prev edge, use sticky
        #    midpoint for smooth tracking.
        # 3. Sticky against trace noise: trace on different edge but
        #    inconsistently (not yet persistent enough to migrate).
        # 4. Bootstrap / new_anchor: no sticky option — adopt trace
        #    endpoint as the anchor.
        MIGRATE_PERSISTENCE_TICKS = 3
        MIGRATE_EDGE_MAX_STREAK = 10   # edge is "recently new" if streak ≤ this
        edge_is_recently_new = (
            trace_end_edge is not None
            and self._edge_present_streak.get(trace_end_edge, 999)
            <= MIGRATE_EDGE_MAX_STREAK
        )
        trace_persistent_diff = (
            self._trace_diff_edge_ticks >= MIGRATE_PERSISTENCE_TICKS
        )
        # Migrate ONLY when all:
        #   - trace has persistently pointed at the different edge
        #     (filters single-tick perception noise)
        #   - the target edge is recently new (filters port-hug
        #     cliff where trace keeps pointing at an edge that's
        #     always been there)
        #   - the new traceline passes CLOSE TO the shifted-prev-WP
        #     (physically-plausible route: ship still traverses past
        #     the old anchor location to reach the new one)
        MIGRATE_PREV_WP_TOLERANCE_PX = 60
        migration_route_ok = True
        if shifted_wp is not None and trace_end_edge is not None:
            min_prev_wp_dist = min(
                math.hypot(p[0] - shifted_wp[0], p[1] - shifted_wp[1])
                for p in polyline)
            migration_route_ok = min_prev_wp_dist <= MIGRATE_PREV_WP_TOLERANCE_PX
        # Endpoint-continuity relaxation.  A COAST BEND slides the trace
        # endpoint smoothly to the new edge tick-over-tick; an artifact
        # severing the channel makes it LEAP discontinuously (handled by the
        # artifact_reversal guard below).  In a bend the ship follows the bank
        # away from the old anchor, so the endpoint no longer passes near the
        # left-behind old WP and the old-WP route guard permanently blocks a
        # real turn (Nile→Med exit t798: old top-centre exit 64 px from the
        # left-hugging trace, yet the endpoint slid (11,0)→(18,0) at ~6 px/tick).
        # So also accept the migration when the endpoint arrived continuously.
        MIGRATE_ENDPOINT_CONTINUITY_PX = 40
        if (not migration_route_ok and self._prev_polyline_yx
                and state.frame_shift_px is not None):
            _pdy, _pdx, _pconf = state.frame_shift_px
            if _pconf >= self.SHIFT_CONF_MIN:
                _pe = self._prev_polyline_yx[-1]
                _exp = (_pe[0] + _pdy, _pe[1] + _pdx)
                if (math.hypot(polyline[-1][0] - _exp[0],
                               polyline[-1][1] - _exp[1])
                        <= MIGRATE_ENDPOINT_CONTINUITY_PX):
                    migration_route_ok = True
        should_migrate = (
            trace_end_edge is not None
            and self._prev_wp_edge is not None
            and trace_end_edge != self._prev_wp_edge
            and trace_persistent_diff
            and edge_is_recently_new
            and migration_route_ok
        )

        # OPEN-WATER COAST-FOLLOW.  `edge_is_recently_new` (above) rejects a
        # PERSISTENT new edge as "port-hug cliff" — correct in a BOUNDED
        # channel (a side edge that's always been there is the cliff you're
        # hugging, not a turn).  But in OPEN WATER — a bank on the hug side,
        # open sea on the other — a persistent side edge is the COAST you must
        # follow.  Same signal, opposite meaning.  So when the far (non-hug)
        # side is open sea all the way to the frame edge, adopt a persistent
        # bank-tracer exit on a different edge instead of clinging to the
        # open-sea edge as trace_noise (t930 ai_nav_2026-07-30T13-29-08:
        # sticky(left,trace_noise) held the sea edge while the coast ran along
        # the bottom, and the ship sailed off into open water).  The far-side-
        # open check keeps this OUT of the bounded Nile channel / lake, where
        # both sides have a bank.
        far_open = (
            approach_for_projection is not None
            and self._far_side_open(reachable, ship_row, ship_col,
                                    approach_for_projection, self.hug_side)
        )
        coast_follow = (
            far_open
            and not should_migrate      # migrate already handles fresh edges
            and trace_end_edge is not None
            and self._prev_wp_edge is not None
            and trace_end_edge != self._prev_wp_edge
            and trace_persistent_diff
        )

        # DEAD-END POCKET HOLD.  When the previous anchor is still ON the
        # current trace (the ship has to sail to it) but the trace
        # ENDPOINT has leapt far beyond it, and the ship hasn't reached
        # the old anchor yet, hold the anchor at the trace point nearest
        # the dead-reckoned old WP.  This blocks the within-edge
        # `new_anchor` branch (which — unlike `migrate` — has no route
        # guard) from jumping the anchor from a dead-end tip to the far
        # exit corner, flipping commit ~100° mid-traverse (session
        # 2026-07-28T00-27-25 t356).  Once the ship reaches the old
        # anchor the hold releases and the trace endpoint is adopted.
        #
        # Yields to a validated `migrate` (checked first in the cascade
        # below): a genuine new exit that has persisted for
        # MIGRATE_PERSISTENCE_TICKS+ is a real course change (e.g. Nubia
        # bot→right bend) and must NOT be held.  The hold only intercepts
        # the abrupt, unvalidated `new_anchor` leap (should_migrate False)
        # — the dead-end-tip-detach case.
        HOLD_ROUTE_TOL_PX = 40   # old anchor still "on" the trace
        HOLD_LEAP_MIN_PX = 80    # endpoint far beyond the old anchor
        HOLD_ARRIVAL_PX = 25     # ship reached old anchor → release
        hold_old_anchor = False
        hold_yx = None
        if self._prev_wp_edge is not None and shifted_wp is not None:
            _dists = [math.hypot(p[0] - shifted_wp[0], p[1] - shifted_wp[1])
                      for p in polyline]
            _i_near = min(range(len(_dists)), key=lambda i: _dists[i])
            _end_dist = math.hypot(polyline[-1][0] - shifted_wp[0],
                                   polyline[-1][1] - shifted_wp[1])
            _ship_dist = math.hypot(shifted_wp[0] - ship_row,
                                    shifted_wp[1] - ship_col)
            if (_dists[_i_near] <= HOLD_ROUTE_TOL_PX
                    and _end_dist > HOLD_LEAP_MIN_PX
                    and _ship_dist > HOLD_ARRIVAL_PX):
                hold_old_anchor = True
                hold_yx = polyline[_i_near]

        # ARTIFACT-REVERSAL GUARD.  A `new_anchor` (no sticky/migrate/hold
        # option) that would REVERSE the commit (>90°) AND whose trace does
        # NOT pass near the old WP is almost always a transient perception
        # artifact severing the channel — e.g. a bright sun-glare band
        # misclassified as land, which cut the northern channel and flipped
        # commit 348°→179° in ONE tick, permanently reversing the return leg
        # (t721 ai_nav_2026-07-29T15-42-01).  Unlike `migrate`, `new_anchor`
        # has no route-continuity check, so hold the old anchor for a few
        # ticks: a transient artifact clears in 1-2 ticks (channel returns →
        # sticky resumes), while a genuine severance persists and is adopted.
        NEW_ANCHOR_REVERSAL_PERSIST = 3
        artifact_reversal = False
        would_new_anchor = (
            not should_migrate and not hold_old_anchor
            and sticky_mid is None
            and trace_end_edge is not None
            and self._prev_wp_edge is not None
            and trace_end_edge != self._prev_wp_edge
        )
        if (would_new_anchor and shifted_wp is not None
                and self._last_commit_deg is not None):
            _ndy = polyline[-1][0] - ship_row
            _ndx = polyline[-1][1] - ship_col
            _new_brg = (math.degrees(math.atan2(_ndx, -_ndy)) + 360.0) % 360.0
            _turn = abs(((_new_brg - self._last_commit_deg + 540.0)
                         % 360.0) - 180.0)
            _route_dist = min(
                math.hypot(p[0] - shifted_wp[0], p[1] - shifted_wp[1])
                for p in polyline)
            if _turn > 90.0 and _route_dist > MIGRATE_PREV_WP_TOLERANCE_PX:
                self._reversal_artifact_ticks += 1
                if self._reversal_artifact_ticks < NEW_ANCHOR_REVERSAL_PERSIST:
                    artifact_reversal = True
            else:
                self._reversal_artifact_ticks = 0
        else:
            self._reversal_artifact_ticks = 0

        # DEAD-END REACH GATE.  A validated migrate that requires the ship
        # to REVERSE (>90° from the current commit) is a dead-end
        # turn-around, not forward progress.  While a hold is active (the
        # ship is still approaching a reachable dead-end tip), defer that
        # reversal until the ship has actually REACHED the tip (within
        # TURNING_APPROACH_PX), so it fully enters the pocket before
        # turning around instead of cutting the corner ~2× that distance
        # short (live session 2026-07-28T15-45-44 t377: turned at 87 px on
        # a 126° reversal).  A moderate-angle migrate (bend/fork, e.g.
        # Nubia bot→right ~40°) is forward progress and fires immediately.
        defer_migrate_reach = False
        if (should_migrate and hold_old_anchor
                and self._last_commit_deg is not None):
            _mdy = polyline[-1][0] - ship_row
            _mdx = polyline[-1][1] - ship_col
            _mig_bearing = (math.degrees(math.atan2(_mdx, -_mdy)) + 360.0) % 360.0
            _turn = abs(((_mig_bearing - self._last_commit_deg + 540.0)
                         % 360.0) - 180.0)
            _anchor_dist = math.hypot(hold_yx[0] - ship_row,
                                      hold_yx[1] - ship_col)
            if _turn > 90.0 and _anchor_dist > self.TURNING_APPROACH_PX:
                defer_migrate_reach = True

        if should_migrate and not defer_migrate_reach:
            # Migrate to trace endpoint's edge.  A validated (persistent)
            # new exit takes priority over the pocket hold — unless it is
            # a dead-end reversal the ship hasn't reached yet (see above).
            wp_yx = polyline[-1]
            source = (f"migrate({self._prev_wp_edge}→{trace_end_edge},"
                      f"streak={self._edge_present_streak[trace_end_edge]},"
                      f"persist={self._trace_diff_edge_ticks})")
        elif hold_old_anchor:
            # Keep the anchor at the dead-reckoned old position (nearest
            # trace point); preserve _prev_wp_edge so the sticky/hold
            # chain continues next tick (see edge-reclassify guard below).
            wp_yx = hold_yx
            source = f"hold(pocket_ahead,{self._prev_wp_edge})"
        elif (sticky_mid is not None
                and trace_end_edge == self._prev_wp_edge):
            # Trace agrees with prev edge → sticky-smooth.
            #
            # Special case: single water-run has split into two on
            # this edge (e.g. Y-fork emerges at bot).  Sticky-mid
            # picks the run NEAREST shifted-prev-WP, which can be
            # the WRONG branch when the walker has already committed
            # to the other one.  Since the port-hug walker in the
            # forward direction is authoritative on "which branch is
            # downstream", prefer the water-run that contains the
            # trace endpoint.  In the single-run case both anchors
            # resolve to the same midpoint (no-op).
            trace_end_run_mid = self._find_run_midpoint_on_edge(
                reachable, self._prev_wp_edge,
                polyline[-1][0], polyline[-1][1],
                ANCHOR_SEARCH_RADIUS_PX,
            )
            if (trace_end_run_mid is not None
                    and trace_end_run_mid != sticky_mid):
                wp_yx = trace_end_run_mid
                source = f"sticky({self._prev_wp_edge},trace_run)"
            else:
                wp_yx = sticky_mid
                source = f"sticky({self._prev_wp_edge})"
        elif coast_follow:
            # Open water: the far side is sea, so a persistent different-edge
            # exit is the COAST — follow it, don't cling to the sea edge.
            wp_yx = polyline[-1]
            source = (f"coast_follow({self._prev_wp_edge}→{trace_end_edge},"
                      f"persist={self._trace_diff_edge_ticks})")
        elif sticky_mid is not None:
            # Trace ends on a different edge but that edge existed
            # before too → likely port-hug noise, keep sticky.
            wp_yx = sticky_mid
            source = f"sticky({self._prev_wp_edge},trace_noise)"
        elif artifact_reversal:
            # Suspected transient artifact severed the channel and the trace
            # now reverses — hold the dead-reckoned old anchor, don't flip.
            wp_yx = (max(0, min(H_r - 1, shifted_wp[0])),
                     max(0, min(W_r - 1, shifted_wp[1])))
            source = f"hold(artifact_reversal,{self._prev_wp_edge})"
        elif trace_end_edge is not None:
            # No sticky anchor (no prev), or prev-edge run genuinely
            # gone → adopt trace endpoint.
            wp_yx = polyline[-1]
            if self._prev_wp_edge is None:
                source = f"bootstrap({trace_end_edge})"
            else:
                source = f"new_anchor({self._prev_wp_edge}→{trace_end_edge})"
        else:
            # Trace endpoint not on any frame edge (interior loop-close).
            wp_yx = polyline[-1]
            source = f"interior_end({approach_src})"

        # Cache edge existence for next tick's migration check.
        self._prev_edges_had_water = cur_edges_had_water

        if polyline is None or len(polyline) < 2 or wp_yx is None:
            log.warning("[bank_tracer_primary] t=%d no valid trace, "
                        "holding prior", state.tick)
            return self._maybe_commit_prior(state)

        # ── HUG-SIDE PERSISTENCE INVARIANT ─────────────────────────────
        # For a port-hug the coast must stay on the PORT side of travel.
        # If a candidate commit puts the land on the FAR (starboard) side —
        # the hug-side ray finds only open water while the far side has the
        # bank — the tracer has FLIPPED the hug side, which is illegal.
        # Hold the last hug-valid anchor (which keeps the ship travelling
        # the way that kept land on port, e.g. continuing WEST along an
        # E–W coast) instead of doubling back with the coast on the wrong
        # side.  Open-Med run 2026-07-30 t114-132: the commit flipped to
        # E/NE with land on starboard and the ship zig-zagged instead of
        # following the coast west.  A channel keeps land on the hug side
        # every tick, so this never fires there.
        hug_hold = False
        _cdy, _cdx = wp_yx[0] - ship_row, wp_yx[1] - ship_col
        _valid_hug = False
        _violation = False
        if math.hypot(_cdy, _cdx) > 5.0:
            _cand_deg = (math.degrees(math.atan2(_cdx, -_cdy)) + 360.0) % 360.0
            _hug_land = self._hug_side_has_land(
                reachable, ship_row, ship_col, _cand_deg, self.hug_side)
            _far_open = self._far_side_open(
                reachable, ship_row, ship_col, _cand_deg, self.hug_side)
            _valid_hug = _hug_land                       # coast on the hug side
            # Only correct when the ANCHOR isn't giving a reliable travel
            # direction — i.e. the cascade re-picked from scratch
            # (new_anchor / bootstrap).  When it produced a TRACKED anchor
            # (sticky / migrate / hold / coast_follow) that direction IS the
            # desired one (river, narrow part, or island gap — all rely on
            # the anchor), so trust it; overriding it with the hug-side rule
            # misfires at a meander bend where the inner bank transiently
            # opens one side (Nile return-leg t582-589, 2026-07-30).
            _anchor_repick = ("new_anchor" in source) or ("bootstrap" in source)
            _violation = (_anchor_repick
                          and (not _hug_land) and (not _far_open))
        if (_violation and self._last_valid_hug_wp_offset is not None
                and self._hug_violation_ticks < self.HUG_VIOLATION_MAX_HOLD):
            # The naive re-pick grabbed the BACKWARD exit (coast on the wrong
            # side).  Don't override it downstream — pick the RIGHT anchor:
            # re-trace the hug-side bank toward the last hug-valid direction
            # so the anchor is a real frame-edge exit that keeps land on the
            # hug side.  The anchor itself then always gives the desired
            # direction, and next tick's sticky can track it (open-Med:
            # continue west along the coast instead of doubling back east).
            self._hug_violation_ticks += 1
            _fwd = self._last_valid_hug_wp_offset
            _fwd_deg = (math.degrees(math.atan2(_fwd[1], -_fwd[0]))
                        + 360.0) % 360.0
            _rp = trace_bank_to_edge(
                reachable, (ship_row, ship_col), _fwd_deg,
                hug_side=self.hug_side, offset_px=10)
            if _rp and len(_rp) >= 2:
                _snap = self._snap_to_edge_midpoint(reachable, *_rp[-1])
                if _snap is not None:
                    _rp[-1] = _snap
                wp_yx = _rp[-1]
                polyline = _rp
                source = f"hug_repick({self.hug_side})"
            else:
                # no forward exit found — hold the last hug-valid direction
                _ho = self._last_valid_hug_wp_offset
                wp_yx = (ship_row + _ho[0], ship_col + _ho[1])
                source = f"hold(hug_side,{self.hug_side})"
                hug_hold = True
        else:
            if not _violation:
                self._hug_violation_ticks = 0
            if _valid_hug:
                self._last_valid_hug_wp_offset = (_cdy, _cdx)

        end_y, end_x = wp_yx
        end_offset = (end_y - ship_row, end_x - ship_col)

        # Cache polyline in pixel coords for next-tick cross-tick check.
        self._prev_polyline_yx = list(polyline)

        # Store.
        walked_ll = []
        for py, px in polyline:
            dy_px = py - ship_row
            dx_px = px - ship_col
            plat = state.lat + (-dy_px / self.PX_PER_DEG)
            plon = state.lon + (dx_px / self.PX_PER_DEG)
            walked_ll.append((plat, plon))

        self._current_dest_px_offset = end_offset
        dlat_end = -end_offset[0] / self.PX_PER_DEG
        dlon_end = end_offset[1] / self.PX_PER_DEG
        self._current_dest = (state.lat + dlat_end, state.lon + dlon_end)
        self._current_walked_path_latlon = walked_ll
        prev_reason = self._current_dest_reason
        self._current_dest_reason = f"bank_tracer:{source}"

        # Cache which edge the WP sits on for next tick's sticky-anchor
        # lookup.  wp_yx is always in-frame (polyline endpoint or edge
        # midpoint).  Use a generous margin so a slightly-inset midpoint
        # still classifies to its edge.
        #
        # Skip while holding: a held anchor sits at the dead-reckoned old
        # position (often interior, since the dead-end tip has detached
        # from the frame edge), which would classify to None and break
        # the hold/sticky chain.  Keep the previous edge instead.
        if not hold_old_anchor and not artifact_reversal and not hug_hold:
            H, W = reachable.shape
            EDGE_MARGIN = self.SNAP_RADIUS_PX * 2   # 24 px
            self._prev_wp_edge = self._classify_frame_edge(
                wp_yx[0], wp_yx[1], H, W, EDGE_MARGIN)

        bearing = self._bearing_to(state.lat, state.lon, *self._current_dest)
        state.commit_direction = CommitDirection(
            bearing_deg=bearing,
            reason=f"bank_tracer({source})",
            set_at_tick=state.tick,
        )
        self._last_commit_deg = bearing

        state.tactical_dest_latlon = self._current_dest
        state.tactical_walked_path = list(self._current_walked_path_latlon)
        state.tactical_dest_px_offset = self._current_dest_px_offset

        lvl = logging.INFO if self._current_dest_reason != prev_reason \
            else logging.DEBUG
        log.log(lvl, "[bank_tracer_primary] t=%d %s polyline=%dpts "
                "wp=(%+d,%+d) commit=%.1f°",
                state.tick, source, len(polyline),
                end_offset[0], end_offset[1], bearing)

        self._prev_ship_ll = (state.lat, state.lon)
        return state

    def _bootstrap_wp(self, reachable, state, ship_row: int, ship_col: int
                      ) -> tuple[Optional[tuple[int, int]],
                                 Optional[list[tuple[int, int]]], str]:
        """Pick a fresh tactical WP by running trace_bank_to_edge with an
        external-signal approach direction.  Returns (wp_yx, polyline,
        source).  Used at first tick and when prev tactical WP has been
        invalidated (arrival, off-frame, topology change).

        With trace_bank_to_edge (walker direction fixed by hug_side),
        there is no trial-walk cliff — the trace deterministically
        walks the hug_side bank to the first frame-edge exit.
        """
        from tools.bank_tracer import trace_bank_to_edge
        import math
        # External approach direction (cross-checked shift vs motion).
        shift_brg = getattr(state, "shift_motion_bearing_deg", None)
        motion_brg = getattr(state, "motion_bearing_deg", None)
        if shift_brg is not None and motion_brg is not None:
            diff = abs(((shift_brg - motion_brg + 540.0) % 360.0) - 180.0)
            if diff > 90.0:
                shift_brg = None
        if shift_brg is not None:
            approach = shift_brg
            src = "bootstrap:shift_motion"
        elif motion_brg is not None:
            approach = motion_brg
            src = "bootstrap:motion"
        elif state.heading is not None:
            approach = state.heading.bearing_deg
            src = "bootstrap:heading"
        elif self._last_commit_deg is not None:
            approach = self._last_commit_deg
            src = "bootstrap:last_commit"
        elif self.goal_bearing_deg is not None:
            approach = self.goal_bearing_deg
            src = "bootstrap:goal"
        else:
            approach = 180.0
            src = "bootstrap:default"

        polyline = trace_bank_to_edge(
            reachable, (ship_row, ship_col), approach,
            hug_side=self.hug_side, offset_px=10,
        )
        if not polyline or len(polyline) < 2:
            return None, None, src + "(failed)"
        end_y, end_x = polyline[-1]
        snap = self._snap_to_edge_midpoint(reachable, end_y, end_x)
        if snap is not None:
            end_y, end_x = snap
            polyline[-1] = (end_y, end_x)
        return (end_y, end_x), polyline, src

    # Tolerance for cross-tick endpoint verification (Step 5).  20 px ≈
    # 2 ticks of motion at 7 kt.
    VERIFICATION_TOLERANCE_PX = 20
    # Anchor-snap radius for Step 4 — endpoint within this many pixels
    # of a frame-edge water-run midpoint snaps to that midpoint.
    SNAP_RADIUS_PX = 12

    def _maybe_commit_prior(self, state: NavState) -> NavState:
        """Hold-prior helper: if we already have a dest, keep the commit
        direction from last tick.  Used when the bank tracer can't run."""
        if self._last_commit_deg is not None:
            state.commit_direction = CommitDirection(
                bearing_deg=self._last_commit_deg,
                reason="bank_tracer_hold_prior",
                set_at_tick=state.tick,
            )
        state.tactical_dest_latlon = self._current_dest
        state.tactical_walked_path = (list(self._current_walked_path_latlon)
                                       if self._current_walked_path_latlon
                                       else None)
        state.tactical_dest_px_offset = self._current_dest_px_offset
        return state

    # Very lenient distance cap for cached trace-start (2026-07-27
    # relaxation after t7 live-voyage flip diagnosis).  A shifted
    # cached start moves in sync with the ship via frame_shift and is
    # world-consistent regardless of ship distance.  Only reject when
    # off-frame or on land.  The 120 px cap is just a runaway guard.
    TRACE_START_MAX_SHIP_DIST_PX = 120

    def _is_valid_trace_start(self, mask, y: int, x: int,
                              ship_row: int, ship_col: int) -> bool:
        """Valid if the (possibly off-frame) start is world-consistent
        AND its in-frame projection lands on reachable water.

        Off-frame positions (e.g., row < 0 after shift-integration) are
        NOT invalid — the world-fixed shore pixel is still world-
        consistent; it just went outside the visible frame.  Off-frame
        is treated conceptually as "on land above/below/beside the
        frame".  trace_bank_to_edge clamps off-frame to in-frame for
        contour projection and skips termination on that frame edge.
        """
        H, W = mask.shape
        import math as _m
        # Clamp for the water/distance check.
        y_c = max(0, min(H - 1, y))
        x_c = max(0, min(W - 1, x))
        if not mask[y_c, x_c]:
            return False
        if _m.hypot(y_c - ship_row, x_c - ship_col) > \
                self.TRACE_START_MAX_SHIP_DIST_PX:
            return False
        return True

    def _anchor_still_valid(self, mask, anchor_y: int, anchor_x: int) -> bool:
        """Anchor valid when its shifted-forward position is still on
        reachable water (or WOULD be if we clamp it back to in-frame).

        Off-frame shifted anchors are NOT invalid — the world-fixed
        WP pixel is still world-consistent; it just went outside the
        visible frame (ship's motion pushed it past the frame edge).
        The corresponding water-run is still on that edge in the
        current mask, and sticky-anchor lookup will find it.
        Only reject when the CLAMPED in-frame position is on land
        (genuine topology change).

        2026-07-27: added clamping after t140 diagnosis — the
        rejection-on-off-frame rule invalidated a shifted WP that
        went 25 px below the visible frame, forcing bootstrap when
        the bot-edge water-run was clearly present."""
        H, W = mask.shape
        y_c = max(0, min(H - 1, anchor_y))
        x_c = max(0, min(W - 1, anchor_x))
        if not mask[y_c, x_c]:
            return False
        return True

    def _snap_to_edge_midpoint(self, mask, end_y: int, end_x: int
                               ) -> Optional[tuple[int, int]]:
        """If (end_y, end_x) is within SNAP_RADIUS_PX of a frame-edge
        water-run midpoint (in the given reachable mask), return that
        midpoint.  Otherwise None.  Same "world-lock to a physical exit"
        role the old SEARCH tracker played."""
        H, W = mask.shape
        best = None
        best_dist = float("inf")
        for edge in ("top", "bot", "left", "right"):
            mid = self._find_run_midpoint_on_edge(
                mask, edge, end_y, end_x, self.SNAP_RADIUS_PX,
            )
            if mid is None:
                continue
            import math as _m
            d = _m.hypot(mid[0] - end_y, mid[1] - end_x)
            if d < best_dist:
                best_dist = d
                best = mid
        return best

    # ── helpers ────────────────────────────────────────────────────

    # ── SEARCH-mode continuity tracker (docs/tactical_search_tracker.md)
    #
    # Skips the (expensive) skeleton walker while a SEARCH-mode dest is
    # still on a frame-edge water-run midpoint.  Cheaper (~2ms vs ~15ms)
    # and avoids walker Y-junction tie-break instability.
    #
    # Algorithm:
    #   1. Project current dest lat/lon to current-frame pixels.
    #   2. Find the nearest frame-edge water-run midpoint within
    #      TRACKER_SEARCH_RADIUS_PX of that projected pixel; midpoint
    #      of that run becomes the new dest.
    #   3. If none is within radius, return False → caller reclassifies
    #      the dest as `turning_point` (the river has bent; dest is now
    #      the pivot, ship approaches it).
    TRACKER_SEARCH_RADIUS_PX    = 80        # ~89 km max shift per tick;
                                            # bigger jumps mean the frame-
                                            # edge exit has scrolled off
                                            # → treat as turning point.
    TRACKER_MIN_EDGE_RUN_PX     = 6

    # Corner region size for corner-split detection (px).
    _CORNER_REGION_PX = 20
    _CORNER_EDGES = {
        "TL": ("top", "left"),
        "TR": ("top", "right"),
        "BL": ("bottom", "left"),
        "BR": ("bottom", "right"),
    }

    def _connected_corners(self, mask) -> set:
        """Return the set of corner names {'TL','TR','BL','BR'} where
        water is CONTINUOUS across the two adjacent edges through the
        corner region.  Continuous means: within the corner region, the
        water pixels touching both edges are in the same connected
        component."""
        try:
            from scipy.ndimage import label as cc_label
        except ImportError:
            return set()
        import numpy as np
        H, W = mask.shape
        R = self._CORNER_REGION_PX
        out = set()
        for name, edges in self._CORNER_EDGES.items():
            # Extract corner region
            if name == "TL":
                region = mask[:R, :R]
                edge1 = region[0, :]      # top row within region
                edge2 = region[:, 0]      # left col within region
                e1_idx = (0, np.where(edge1)[0]) if edge1.any() else None
                e2_idx = (np.where(edge2)[0], 0) if edge2.any() else None
            elif name == "TR":
                region = mask[:R, -R:]
                edge1 = region[0, :]      # top row
                edge2 = region[:, -1]     # right col within region
                e1_idx = (0, np.where(edge1)[0]) if edge1.any() else None
                e2_idx = (np.where(edge2)[0], region.shape[1]-1) if edge2.any() else None
            elif name == "BL":
                region = mask[-R:, :R]
                edge1 = region[-1, :]     # bottom row
                edge2 = region[:, 0]      # left col
                e1_idx = (region.shape[0]-1, np.where(edge1)[0]) if edge1.any() else None
                e2_idx = (np.where(edge2)[0], 0) if edge2.any() else None
            else:  # BR
                region = mask[-R:, -R:]
                edge1 = region[-1, :]
                edge2 = region[:, -1]
                e1_idx = (region.shape[0]-1, np.where(edge1)[0]) if edge1.any() else None
                e2_idx = (np.where(edge2)[0], region.shape[1]-1) if edge2.any() else None
            if e1_idx is None or e2_idx is None:
                continue
            # Label CCs in the region
            lbl, _ = cc_label(region)
            # Pick any water pixel on each edge; check same CC.
            e1_cc = lbl[e1_idx[0], e1_idx[1][0]]
            e2_cc = lbl[e2_idx[0][0], e2_idx[1]]
            if e1_cc != 0 and e1_cc == e2_cc:
                out.add(name)
        return out

    def _corner_split_since_last_tick(self, state) -> bool:
        """Detect if a corner that was CONNECTED last tick has become
        SPLIT (land emerged separating the water into disjoint edge
        branches) AND the currently-tracked dest sits IN that specific
        corner region.  Fresh-pick only fires when the bank continuation
        at the dest itself has topologically changed.

        Original rule (2026-07-25) fired whenever the dest was on ANY
        edge touched by ANY split corner.  That was too aggressive:
        session ai_nav_2026-07-25T15-54-02 t401 had a BL corner split
        while the dest was on the BOT edge at col 264 — ~245 px from
        the BL corner region.  The bank continuation at col 264 was
        completely unchanged; forcing a fresh pick there flipped the
        dest to the top edge and stalled the voyage for 300+ ticks.

        Correct semantics: the dest must actually be IN the affected
        corner's region for the split to be relevant.  A dest sitting
        mid-edge, far from any corner, doesn't care about corner
        topology changes."""
        if state.water_mask is None or self._current_dest_px_offset is None:
            return False
        new_conn = self._connected_corners(state.water_mask)
        prev_conn = self._prev_connected_corners
        # Update prev for next tick's check.  Do this AFTER checking
        # so the current tick sees last tick's state.
        self._prev_connected_corners = new_conn
        # Corners that transitioned from CONNECTED → SPLIT this tick.
        split = prev_conn - new_conn
        if not split:
            return False
        # Which corner region (if any) does the tracked dest sit in?
        H, W = state.water_mask.shape
        R = self._CORNER_REGION_PX
        row = H // 2 + self._current_dest_px_offset[0]
        col = W // 2 + self._current_dest_px_offset[1]
        dest_corner: Optional[str] = None
        if 0 <= row < R and 0 <= col < R:
            dest_corner = "TL"
        elif 0 <= row < R and W - R <= col < W:
            dest_corner = "TR"
        elif H - R <= row < H and 0 <= col < R:
            dest_corner = "BL"
        elif H - R <= row < H and W - R <= col < W:
            dest_corner = "BR"
        if dest_corner is None:
            return False
        return dest_corner in split

    def _track_search_dest(self, state: NavState) -> bool:
        """SEARCH-mode continuity tracker — pure edge continuity.

        The tracker asks: "where did the water exit go this tick, given
        where it was last tick + how much the frame moved?"  Ship
        orientation, commit direction, and tactical goal are IRRELEVANT
        to that question — those pollute the answer with stale
        directional bias (t367 stale-SW-commit, t13 post-bounce N-spin).

        Rules:
          1. Classify prev dest's edge (top / bot / left / right).
          2. Apply frame_shift to get expected new position.
          3. Search water-run midpoints on the SAME edge first, within
             a radius = 3 × |frame_shift| (falls back to a floor when
             frame_shift is unknown).
          4. If same-edge has no candidate within radius, check the
             ADJACENT (perpendicular) edges.  Never consider the
             opposite edge — physically impossible for a world-fixed
             exit to jump from one side of the frame to the other
             in a single tick.
          5. On no hit → tracker fails → caller reclassifies as
             turning_point.
        """
        if self._current_dest is None or state.water_mask is None:
            return False
        mask = state.water_mask
        H, W = mask.shape
        ship_row, ship_col = H // 2, W // 2

        if self._current_dest_px_offset is not None:
            row = ship_row + self._current_dest_px_offset[0]
            col = ship_col + self._current_dest_px_offset[1]
        else:
            dlat = self._current_dest[0] - state.lat
            dlon = self._current_dest[1] - state.lon
            row = int(round(ship_row - dlat * self.PX_PER_DEG))
            col = int(round(ship_col + dlon * self.PX_PER_DEG))

        # Classify which edge the prev dest was closest to.  Ignore
        # interior positions (dist to nearest edge > 40 px) — those are
        # already off-frame; caller should have reclassified.  Adjacent
        # rules by edge:
        #   top  ↔ left, right   (never bot)
        #   bot  ↔ left, right   (never top)
        #   left ↔ top, bot      (never right)
        #   right↔ top, bot      (never left)
        ADJACENT = {
            "top":   ("left", "right"),
            "bot":   ("left", "right"),
            "left":  ("top", "bot"),
            "right": ("top", "bot"),
        }
        dist_top   = abs(row - 0)
        dist_bot   = abs(row - (H - 1))
        dist_left  = abs(col - 0)
        dist_right = abs(col - (W - 1))
        edge = min(("top", "bot", "left", "right"),
                   key=lambda e: {"top": dist_top, "bot": dist_bot,
                                  "left": dist_left, "right": dist_right}[e])

        # Radius scales with recent frame-shift magnitude.  Baseline 30
        # px prevents runaway when shift signal is missing.
        exp_shift = state.expected_shift_px or 10.0
        radius = max(30, int(round(3 * exp_shift)))

        # Gather run midpoints on SAME + ADJACENT edges (never opposite).
        # Then pick the one CLOSEST to (row, col) among all candidates.
        # Corner cases: when water is continuous across two edges (e.g.,
        # t444 bottom-right corner in ai_nav_2026-07-23T17-01-57), the
        # nearest midpoint may sit on an adjacent edge — a strict
        # "same edge first" rule would spuriously prefer a far same-
        # edge midpoint over a close adjacent one.
        #
        # Same-CC contiguity check on ALL candidates (same + adjacent
        # edges).  Reference is SHIP's water CC — the water we can
        # actually reach.  When a topology split occurs (t374→t375 in
        # ai_nav_2026-07-23T17-47-26: land sliver appears at top-left
        # corner, top-edge pool disconnects from left-edge pool
        # containing the ship), a same-edge candidate in the split-off
        # pool is unreachable and must be rejected — even though it's
        # on the "same edge" as prev.  If no candidate is in ship's
        # CC, tracker fails → dest reclassifies as turning_point and
        # walker picks next leg on arrival.
        from scipy.ndimage import label as cc_label
        labeled, _ = cc_label(mask)
        ship_cc = labeled[ship_row, ship_col] if mask[ship_row, ship_col] else 0
        candidates = []
        for candidate_edge in (edge,) + ADJACENT[edge]:
            near_px = self._find_run_midpoint_on_edge(
                mask, candidate_edge, row, col, radius,
            )
            if near_px is None:
                continue
            # Reachability: candidate must be in the same water CC as
            # the ship.  If ship_cc is 0 (ship on land in mask —
            # sprite occlusion edge case), skip the check to avoid
            # rejecting everything.
            if ship_cc != 0:
                cand_cc = labeled[near_px[0], near_px[1]]
                if cand_cc != ship_cc:
                    continue
            dist = math.hypot(near_px[0] - row, near_px[1] - col)
            candidates.append((dist, near_px, candidate_edge))

        if not candidates:
            log.debug("[lookahead_tactical] t=%d tracker: no run within "
                      "%dpx of anchor (%d,%d) on edge=%s or adjacent",
                      state.tick, radius, row, col, edge)
            return False

        candidates.sort()
        _, near_px, chosen_edge = candidates[0]
        if chosen_edge != edge:
            log.debug("[lookahead_tactical] t=%d tracker: nearest "
                      "midpoint on adjacent edge=%s (was on %s)",
                      state.tick, chosen_edge, edge)
        near_row, near_col = near_px
        self._current_dest_px_offset = (near_row - ship_row,
                                         near_col - ship_col)
        dlat = -(near_row - ship_row) / self.PX_PER_DEG
        dlon = (near_col - ship_col) / self.PX_PER_DEG
        self._current_dest = (state.lat + dlat, state.lon + dlon)
        if self._current_walked_path_latlon:
            self._current_walked_path_latlon.append(self._current_dest)
        else:
            self._current_walked_path_latlon = [
                (state.lat, state.lon), self._current_dest,
            ]
        return True

    @staticmethod
    def _far_side_open(mask, sr: int, sc: int, approach_deg: float,
                       hug_side: str) -> bool:
        """True if the NON-hug side is open water all the way to the frame
        edge — a bank on the hug side, open sea on the other, i.e. the ship
        is coast-following in OPEN WATER rather than inside a bounded channel
        (where both sides have a bank).  Cast a ray perpendicular to the
        approach on the far side; open ⇒ it reaches the frame edge without
        hitting land."""
        H, W = mask.shape
        # far side = starboard for a port-hug, port for a starboard-hug
        perp = approach_deg + (90.0 if hug_side == "port" else -90.0)
        r = math.radians(perp)
        uy, ux = -math.cos(r), math.sin(r)
        for d in range(1, H + W):
            y = int(round(sr + uy * d))
            x = int(round(sc + ux * d))
            if not (0 <= y < H and 0 <= x < W):
                return True            # reached the frame edge as water ⇒ open sea
            if not mask[y, x]:
                return False           # hit a far bank ⇒ bounded (channel/lake)
        return True

    @staticmethod
    def _hug_side_has_land(mask, sr: int, sc: int, commit_deg: float,
                           hug_side: str) -> bool:
        """True if the HUG side has a bank (land) — the coast the ship is
        supposed to keep on its hug side is actually there.  Casts a ray
        perpendicular to `commit_deg` toward the hug side (port = LEFT of
        travel); hitting land before the frame edge ⇒ the invariant holds.
        Reaching the edge as water ⇒ the hug side is open water and the
        land (if any) is on the WRONG side → port-hug flipped.  Mirror of
        `_far_side_open`."""
        H, W = mask.shape
        # hug side = port for a port-hug (LEFT = commit − 90)
        perp = commit_deg + (-90.0 if hug_side == "port" else 90.0)
        r = math.radians(perp)
        uy, ux = -math.cos(r), math.sin(r)
        for d in range(1, H + W):
            y = int(round(sr + uy * d))
            x = int(round(sc + ux * d))
            if not (0 <= y < H and 0 <= x < W):
                return False           # hug side open to the edge ⇒ no bank
            if not mask[y, x]:
                return True            # hit the hug-side bank ⇒ invariant holds
        return False

    def _find_run_midpoint_on_edge(self, mask, edge: str,
                                    near_row: int, near_col: int,
                                    radius: int):
        """Nearest water-run midpoint on a specific frame edge to
        (near_row, near_col), within radius.  Returns (row, col) or None."""
        H, W = mask.shape
        if edge == "top":
            edge_arr = mask[0, :]; fixed_axis = ("row", 0)
        elif edge == "bot":
            edge_arr = mask[H - 1, :]; fixed_axis = ("row", H - 1)
        elif edge == "left":
            edge_arr = mask[:, 0]; fixed_axis = ("col", 0)
        elif edge == "right":
            edge_arr = mask[:, W - 1]; fixed_axis = ("col", W - 1)
        else:
            return None
        runs = []
        i = 0
        while i < len(edge_arr):
            if edge_arr[i]:
                j = i
                while j < len(edge_arr) and edge_arr[j]:
                    j += 1
                if j - i >= self.TRACKER_MIN_EDGE_RUN_PX:
                    runs.append((i, j - 1))
                i = j
            else:
                i += 1
        if not runs:
            return None
        # Score candidates by TRUE 2D distance to (near_row, near_col).
        # Earlier version compared only the axis-varying coordinate
        # (col for top/bot, row for left/right), which admitted far
        # candidates whose fixed axis happened to align with the
        # anchor.  Session ai_nav_2026-07-25T15-54-02 t206: anchor at
        # (row=23, col=286) → LEFT edge midpoint at (22, 0) passed
        # the axis-only gate with `abs(22-23)=1` even though the
        # true 2D distance was 286 px.  Tracker then jumped the
        # dest from top-right corner to the left frame edge.  With
        # true-2D distance, that midpoint is correctly rejected and
        # the tracker fails cleanly → fresh pick fires the bank
        # tracer, which produces the correct NE continuation.
        if fixed_axis[0] == "row":
            # top/bot: midpoint col varies, row is fixed
            candidates = [(fixed_axis[1], (a + b) // 2) for a, b in runs]
        else:
            # left/right: midpoint row varies, col is fixed
            candidates = [((a + b) // 2, fixed_axis[1]) for a, b in runs]
        best = min(candidates,
                   key=lambda p: math.hypot(p[0] - near_row, p[1] - near_col))
        dist = math.hypot(best[0] - near_row, best[1] - near_col)
        return best if dist <= radius else None

    def _detect_fork_emergence(self, state: NavState) -> bool:
        """True if the count of frame-edge water runs increased since
        the previous tick — a new candidate exit has appeared and
        the walker should re-pick with hug-side rather than let the
        tracker smoothly drift to the nearer midpoint (t358 fork
        mispick in ai_nav_2026-07-22T20-33-50).

        Updates `_prev_frame_edge_run_count` for next tick.  Always
        called on SEARCH-mode ticks; returns False on the first tick
        when no prev count exists.
        """
        if state.water_mask is None:
            return False
        count = self._count_frame_edge_runs(state.water_mask)
        prev = self._prev_frame_edge_run_count
        self._prev_frame_edge_run_count = count
        if prev is None:
            return False
        if count > prev:
            log.info("[lookahead_tactical] t=%d fork emergence: edge "
                     "runs %d → %d → walker fires with hug-side",
                     state.tick, prev, count)
            return True
        return False

    def _count_frame_edge_runs(self, mask) -> int:
        """Number of contiguous water runs on the 4 frame edges (each
        of length >= TRACKER_MIN_EDGE_RUN_PX)."""
        H, W = mask.shape
        total = 0
        for edge in (mask[0, :], mask[H - 1, :],
                     mask[:, 0], mask[:, W - 1]):
            i = 0
            while i < len(edge):
                if edge[i]:
                    j = i
                    while j < len(edge) and edge[j]:
                        j += 1
                    if j - i >= self.TRACKER_MIN_EDGE_RUN_PX:
                        total += 1
                    i = j
                else:
                    i += 1
        return total

    def _nearest_edge_run_midpoint_px(self, state, near_row, near_col,
                                       radius_px):
        """Pixel-space cousin of `_nearest_edge_run_midpoint_ll` — same
        algorithm, returns (row, col) instead of (lat, lon)."""
        mask = state.water_mask
        H, W = mask.shape

        def runs(edge_arr):
            out = []; i = 0
            while i < len(edge_arr):
                if edge_arr[i]:
                    j = i
                    while j < len(edge_arr) and edge_arr[j]:
                        j += 1
                    if j - i >= self.TRACKER_MIN_EDGE_RUN_PX:
                        out.append((i, j - 1))
                    i = j
                else:
                    i += 1
            return out

        candidates = []
        for a, b in runs(mask[0, :]):
            candidates.append((0, (a + b) // 2))
        for a, b in runs(mask[H - 1, :]):
            candidates.append((H - 1, (a + b) // 2))
        for a, b in runs(mask[:, 0]):
            candidates.append(((a + b) // 2, 0))
        for a, b in runs(mask[:, W - 1]):
            candidates.append(((a + b) // 2, W - 1))
        if not candidates:
            return None
        best = min(candidates,
                   key=lambda p: (p[0] - near_row) ** 2
                                  + (p[1] - near_col) ** 2)
        d2 = (best[0] - near_row) ** 2 + (best[1] - near_col) ** 2
        if d2 > radius_px * radius_px:
            return None
        return best

    def _nearest_edge_run_midpoint_ll(self, state, near_row, near_col,
                                       radius_px):
        """Scan all 4 frame edges for contiguous water runs of at least
        TRACKER_MIN_EDGE_RUN_PX.  Return the (lat, lon) midpoint of the
        run nearest to (near_row, near_col), or None if none within
        radius_px."""
        mask = state.water_mask
        H, W = mask.shape

        def runs(edge_arr):
            out = []; i = 0
            while i < len(edge_arr):
                if edge_arr[i]:
                    j = i
                    while j < len(edge_arr) and edge_arr[j]:
                        j += 1
                    if j - i >= self.TRACKER_MIN_EDGE_RUN_PX:
                        out.append((i, j - 1))
                    i = j
                else:
                    i += 1
            return out

        candidates = []                      # list of (row, col)
        for a, b in runs(mask[0, :]):
            candidates.append((0, (a + b) // 2))
        for a, b in runs(mask[H - 1, :]):
            candidates.append((H - 1, (a + b) // 2))
        for a, b in runs(mask[:, 0]):
            candidates.append(((a + b) // 2, 0))
        for a, b in runs(mask[:, W - 1]):
            candidates.append(((a + b) // 2, W - 1))
        if not candidates:
            return None
        best = min(candidates,
                   key=lambda p: (p[0] - near_row) ** 2
                                  + (p[1] - near_col) ** 2)
        d2 = (best[0] - near_row) ** 2 + (best[1] - near_col) ** 2
        if d2 > radius_px * radius_px:
            return None
        dy_px = best[0] - H // 2
        dx_px = best[1] - W // 2
        plat = state.lat + (-dy_px / self.PX_PER_DEG)
        plon = state.lon + (dx_px / self.PX_PER_DEG)
        return (plat, plon)

    def _px_offset_from_ll(self, dest_ll: tuple[float, float],
                           state: NavState) -> tuple[int, int]:
        """Convert a lat/lon dest into pixel offset (row, col) from
        ship centre.  Used when seeding `_current_dest_px_offset` at
        Phase 2 dest transitions (walker pick, SEARCH → turning_point
        reclassify)."""
        dlat = dest_ll[0] - state.lat
        dlon = dest_ll[1] - state.lon
        row_off = int(round(-dlat * self.PX_PER_DEG))
        col_off = int(round(dlon * self.PX_PER_DEG))
        return (row_off, col_off)

    def _hold_prior_commit(self, state: NavState) -> NavState:
        if self._last_commit_deg is not None:
            state.commit_direction = CommitDirection(
                bearing_deg=self._last_commit_deg,
                reason="lookahead_no_ocr_hold",
                set_at_tick=state.tick,
            )
        return state

    def _pick_new_dest(self, state: NavState
                       ) -> tuple[Optional[tuple[float, float]], str]:
        """Pick a NEW tactical destination.

        Walks the skeleton from ship with hug-side at junctions until
        it either (a) hits a LEAF within the minimap → dest = leaf,
        kind='leaf'; or (b) reaches the minimap EDGE → dest = edge
        intersection, kind='edge'.

        The tactical dest is naturally FAR (edge is ~half the mini-map
        away, ~1° or more of world at PX_PER_DEG=100).  This means it
        persists across many ticks without update.

        Reachability check: rejects dests that land in a water
        connected component different from the ship's — those are
        physically unreachable (land between ship and dest).  Root
        cause is extract_tree_from_mask spanning multiple CCs; the
        safety check here is belt-and-suspenders on top of a
        pre-mask to ship's CC.
        """
        # Bank-tracer picker (2026-07-24 redesign after voyage
        # 22-01-22 t395 diagnosis).  Instead of enumerating frame-edge
        # openings and bucketing them by body-frame angle, we trace
        # the ship's LEFT bank forward and return the traced polyline
        # as the tactical route.
        #
        # Semantic: "follow the left bank forward in the approach
        # direction".  Handles both river navigation (bank curves
        # through the channel) and dead-end pockets (bank curls
        # around the terminal and reverses back out).  No opening
        # enumeration, no bucket priority, no turning-point vs
        # frame-edge distinction.
        #
        # Design in tools/bank_tracer.py.  Wired 2026-07-24.
        import math
        if state.water_mask is None:
            return None, "no_mask"
        try:
            from tools.bank_tracer import trace_bank_to_edge
            from scipy.ndimage import label as cc_label
        except ImportError:
            return None, "no_module"

        H, W = state.water_mask.shape
        ship_row, ship_col = H // 2, W // 2

        # Restrict water to the ship's connected component.
        labeled, _ = cc_label(state.water_mask)
        ship_cc = labeled[ship_row, ship_col]
        if ship_cc == 0:
            reachable_mask = state.water_mask.astype(bool)
        else:
            reachable_mask = (labeled == ship_cc)

        # Approach direction — how the ship has been traveling recently.
        # This is the ONLY signal the picker uses to score openings.
        # Rationale: dyn_goal (bearing to mission_dest) is naive — it
        # assumes straight-line water to the destination and misleads
        # in enclosed pockets (e.g. lake exploration requires going
        # south to a dead-end then reversing north).  Approach direction
        # + wall-follower hug rule is topology-aware without needing a
        # global map.
        #
        # Priority chain, most reliable first:
        #   1. shift_motion_bearing_deg — per-tick, vision-based
        #      (frame_shift_px averaged over recent ticks).  Most
        #      definitive ship-motion signal, immune to HUD OCR + CNN.
        #   2. motion_bearing_deg — lat/lon Δ over recent ticks.
        #   3. heading.bearing_deg — CNN ship bow orientation.
        #   4. _last_commit_deg — tactical dest direction (continuity
        #      when nothing else is available).
        #   5. self.goal_bearing_deg — CLI --commit-bearing fallback.
        #   6. 180° — absolute last resort.
        # Cross-check safety net (added 2026-07-25 after t210-t213 +
        # t371-t374 diagnosis): shift_motion_bearing_deg can go stale
        # under low frame-shift confidence, and the pipeline gate may
        # not always catch it.  motion_bearing_deg (lat/lon Δ) is a
        # slower but self-refreshing channel — if the two disagree by
        # more than 90° (they should point roughly the same way when
        # the ship is under way), the shift signal is almost certainly
        # stale and motion is the safer choice.
        shift_brg = getattr(state, "shift_motion_bearing_deg", None)
        motion_brg = getattr(state, "motion_bearing_deg", None)
        if shift_brg is not None and motion_brg is not None:
            diff = abs(((shift_brg - motion_brg + 540.0) % 360.0) - 180.0)
            if diff > 90.0:
                shift_brg = None   # force fallback to motion below
        if shift_brg is not None:
            approach_bearing = shift_brg
        elif motion_brg is not None:
            approach_bearing = motion_brg
        elif state.heading is not None:
            approach_bearing = state.heading.bearing_deg
        elif self._last_commit_deg is not None:
            approach_bearing = self._last_commit_deg
        elif self.goal_bearing_deg is not None:
            approach_bearing = self.goal_bearing_deg
        else:
            approach_bearing = 180.0

        # Trace the port-side bank forward until frame edge.
        polyline = trace_bank_to_edge(
            reachable_mask, (ship_row, ship_col), approach_bearing,
            hug_side=self.hug_side, offset_px=10,
        )
        if not polyline:
            return None, "bank_trace_failed"

        # Convert polyline pixels → world lat/lon for the reflex planner.
        self._last_walked_path_latlon = []
        for py, px in polyline:
            dy_px = py - ship_row
            dx_px = px - ship_col
            plat = state.lat + (-dy_px / self.PX_PER_DEG)
            plon = state.lon + (dx_px / self.PX_PER_DEG)
            self._last_walked_path_latlon.append((plat, plon))

        # Destination = the far end of the traced bank polyline.
        far_y, far_x = polyline[-1]
        dy_px = far_y - ship_row
        dx_px = far_x - ship_col
        dest_latlon = (state.lat + (-dy_px / self.PX_PER_DEG),
                       state.lon + (dx_px / self.PX_PER_DEG))

        log.info("[lookahead_tactical] t=%d bank-traced %d pts, "
                 "approach=%.0f°, far=(y=%d, x=%d) → (%.3f, %.3f)",
                 state.tick, len(polyline), approach_bearing,
                 far_y, far_x, dest_latlon[0], dest_latlon[1])

        return dest_latlon, "frame_edge"

    def _near_visited(self, state, dest_latlon) -> bool:
        """True if `dest_latlon` is within VISITED_REJECT_KM of any
        entry in state.visited_dead_ends."""
        if not state.visited_dead_ends:
            return False
        for (lat, lon) in state.visited_dead_ends:
            if self._haversine_km(dest_latlon[0], dest_latlon[1],
                                  lat, lon) < self.VISITED_REJECT_KM:
                return True
        return False

    def _dest_visible_last_tick(self, dest_latlon) -> bool:
        """True if dest_latlon would have fit inside the mini-map
        centered on the ship's previous-tick position.  Uses fixed
        193×405 mini-map size and PX_PER_DEG scaling."""
        if self._prev_ship_ll is None:
            return False
        prev_lat, prev_lon = self._prev_ship_ll
        d_lat = dest_latlon[0] - prev_lat
        d_lon = dest_latlon[1] - prev_lon
        py = -d_lat * self.PX_PER_DEG   # image y-down: south → +py
        px = d_lon * self.PX_PER_DEG
        H, W = 193, 405
        return abs(py) < H / 2 and abs(px) < W / 2

    def _at_turning_point(self, state: NavState,
                          dest: tuple[float, float]) -> bool:
        """Turning-point arrival test.

        Only turning-point dests (`narrow_choke`, `dead_end`,
        `turning_point`) route through this — SEARCH-mode continuity is
        handled by `_track_search_dest` directly.  Returns True when the
        ship has arrived at the pivot by one of:
          - within TURNING_APPROACH_PX of ship in current mini-map
          - dest scrolled off the mini-map (ship overshot)
          - this tick's reflex waypoint is farther from ship than dest
        """
        if self._current_dest_reason not in self._TURNING_POINT_REASONS:
            return True

        import math
        dlat = dest[0] - state.lat
        dlon = dest[1] - state.lon
        dy_px = -dlat * self.PX_PER_DEG
        dx_px = dlon * self.PX_PER_DEG
        H, W = 193, 405
        wp_y = H // 2 + dy_px
        wp_x = W // 2 + dx_px
        if not (0 <= wp_y < H and 0 <= wp_x < W):
            return True
        tact_dist = math.hypot(dy_px, dx_px)
        if tact_dist < self._scaled_turning_approach_px(state):
            return True
        # "Reflex closer than tactical → reached" — semantic is
        # "ship has passed the tactical target".  Only meaningful at
        # close range; at 100+ px away nothing about reflex being
        # slightly farther indicates arrival.  Gate this rule to only
        # fire when tact_dist is within a small multiple of the approach
        # threshold (2× — same range where "close enough to matter").
        # ai_nav_2026-07-23T18-25-33 t845 fired arrival at tact=100.4px
        # / reflex=109.2px, both way above the 40-px approach; walker
        # then picked a bottom-edge dest despite ship heading north.
        approach = self._scaled_turning_approach_px(state)
        po = state.planner_output
        if (po is not None and po.waypoint_px is not None
                and tact_dist < 2.0 * approach):
            ship_row, ship_col = H // 2, W // 2
            r, c = po.waypoint_px
            reflex_dist = math.hypot(r - ship_row, c - ship_col)
            if reflex_dist > tact_dist:
                log.info("[lookahead_tactical] t=%d turning-point tactical "
                         "(%.1fpx) closer than reflex (%.1fpx) → reached",
                         state.tick, tact_dist, reflex_dist)
                return True
        return False

    @staticmethod
    def _bearing_to(from_lat: float, from_lon: float,
                    to_lat: float, to_lon: float) -> float:
        """Great-circle bearing (compass) from (from) to (to)."""
        import math
        phi1, phi2 = math.radians(from_lat), math.radians(to_lat)
        dlam = math.radians(to_lon - from_lon)
        y = math.sin(dlam) * math.cos(phi2)
        x = (math.cos(phi1) * math.sin(phi2)
             - math.sin(phi1) * math.cos(phi2) * math.cos(dlam))
        return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0

    @staticmethod
    def _haversine_km(lat1: float, lon1: float,
                      lat2: float, lon2: float) -> float:
        import math
        R = 6371.0
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlam = math.radians(lon2 - lon1)
        a = (math.sin(dphi/2)**2
             + math.cos(phi1) * math.cos(phi2) * math.sin(dlam/2)**2)
        return 2 * R * math.asin(math.sqrt(a))

    def _truncate_at_choke(self, trace_px, mask):
        """Scan the trace; stop at the first point that is BOTH narrow
        (DT < NARROW_DT_MIN) AND has in-frame land on both perpendicular
        sides.  Returns (trace_up_to_choke, dest_px, choked_bool).

        The land-on-both-sides guard rejects false narrows at the frame
        edge: a channel that exits the crop has DT < 2.5 near the exit
        (frame boundary counts as background for DT) but only one bank
        is in-frame — the other side is off-frame water that just
        isn't in view yet.  Firing on those was the source of the
        t186 → t199 LOCK cascade in session ai_nav_2026-07-22T08-28-30.

        Returns (_, None, _) when the ship is already at a choke."""
        if not trace_px:
            return trace_px, None, False
        from scipy.ndimage import distance_transform_edt
        dt = distance_transform_edt(mask)
        H, W = mask.shape
        for i, (r, c) in enumerate(trace_px):
            if not (0 <= r < H and 0 <= c < W):
                continue
            if dt[r, c] >= self.NARROW_DT_MIN:
                continue
            if not self._land_on_both_sides(trace_px, i, mask):
                continue
            if i == 0:
                return trace_px[:1], None, False
            trunc = trace_px[:i]
            return trunc, trunc[-1], True
        return trace_px, trace_px[-1], False

    @staticmethod
    def _land_on_both_sides(trace_px, i, mask, max_walk_px: int = 15) -> bool:
        """At `trace_px[i]`, sample perpendicular to the local tangent in
        both directions.  Return True only if BOTH sides hit an in-frame
        land pixel within `max_walk_px` steps.  Off-frame counts as
        "not confirmed land" — the water body may continue past the
        crop, so we can't call it a bank."""
        n = len(trace_px)
        H, W = mask.shape
        r, c = trace_px[i]
        if n < 2:
            return True                     # can't compute tangent
        i0 = max(0, i - 1)
        i1 = min(n - 1, i + 1)
        r0, c0 = trace_px[i0]
        r1, c1 = trace_px[i1]
        dr = float(r1 - r0)
        dc = float(c1 - c0)
        norm = (dr * dr + dc * dc) ** 0.5
        if norm == 0:
            return True                     # degenerate — conservative
        dr /= norm; dc /= norm
        # Perpendiculars in image coords: (-dc, dr) and (dc, -dr)
        for perp_r, perp_c in ((-dc, dr), (dc, -dr)):
            found_land = False
            for k in range(1, max_walk_px + 1):
                yy = int(round(r + k * perp_r))
                xx = int(round(c + k * perp_c))
                if not (0 <= yy < H and 0 <= xx < W):
                    break                   # off-frame → this side unconfirmed
                if not mask[yy, xx]:
                    found_land = True
                    break
            if not found_land:
                return False
        return True

    def _offset_toward_shore(self, trace_px, water_mask):
        """Shift each trace point HUG_OFFSET_PX perpendicular toward
        the hug-side shore.  If the offset lands on land (narrow
        channel), fall back to the centerline point.

        Local tangent = central difference on the trace.  In image
        coords (y-down, compass 0 = up):
          port  (left of forward)  = (-dx,  dy)
          starboard (right)        = ( dx, -dy)
        """
        if not trace_px or water_mask is None:
            return list(trace_px)
        H, W = water_mask.shape
        d = float(self.HUG_OFFSET_PX)
        sign = +1 if self.hug_side == "starboard" else -1  # port = -1

        n = len(trace_px)
        out = []
        for i, (r, c) in enumerate(trace_px):
            # Central-diff tangent (forward/backward at ends)
            if n == 1:
                out.append((r, c))
                continue
            i0 = max(0, i - 1)
            i1 = min(n - 1, i + 1)
            r0, c0 = trace_px[i0]
            r1, c1 = trace_px[i1]
            dr = float(r1 - r0)
            dc = float(c1 - c0)
            norm = (dr * dr + dc * dc) ** 0.5
            if norm == 0:
                out.append((r, c))
                continue
            dr /= norm; dc /= norm
            # 90° rotation in image coords: starboard = (dc, -dr).
            # Port = (-dc, dr).  Sign folded above.
            perp_r = sign * dc
            perp_c = sign * (-dr)
            or_ = int(round(r + d * perp_r))
            oc_ = int(round(c + d * perp_c))
            if 0 <= or_ < H and 0 <= oc_ < W and bool(water_mask[or_, oc_]):
                out.append((or_, oc_))
            else:
                out.append((r, c))     # narrow choke: stay on centerline
        return out
