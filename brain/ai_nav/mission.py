"""Mission layer — the strategic decision-maker above L3.

Reads the planner's per-tick output (topology, position) and the
bot's dead-reckoned lat/lon, then writes `commit_direction` to
NavState.  L3 reads commit on the next tick.  Most ticks the
mission is a no-op (commit unchanged); mission only acts on
strategic events:

  - first tick after lat/lon is known (set initial commit)
  - significant bearing change toward destination (PointToPoint)
  - reached a new place (ExploreMission: pick next direction)
  - mission complete (arrived at destination / explored everything)

A Mission produces commit_direction; it does NOT pick waypoints.
That stays in L3.  Mission is the "what" (head south, explore the
Nile), L3 is the "how" (which pixel waypoint).

Three current implementations:

  - `NoOpMission`        — never updates commit (default).  Use
                           when the user is happy with whatever
                           commit_direction is set externally.
  - `PointToPointMission` — sets commit toward a fixed (lat, lon)
                           destination; declares complete on
                           arrival within `arrival_km`.
  - `ExploreMission`     — generic explore loop over a swappable
                           `place_extractor` + `decision_rule` +
                           `is_complete`.  See docstring for the
                           pluggable design that handles river /
                           coastal / open-sea exploration with
                           different configs (not classes).

Mission runs between L3 (planner) and L4 (tactical) in the
pipeline tick.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from brain.ai_nav.state import CommitDirection, NavState
from brain.ai_nav.vision_input import VisionFrame

log = logging.getLogger(__name__)


KM_PER_DEG_LAT = 111.0


class Mission(Protocol):
    """Mission protocol.  Implementations decide when (and whether)
    to update `state.commit_direction` based on per-tick context."""
    name: str

    def update(self, frame: VisionFrame, state: NavState) -> NavState: ...


# ── No-op ─────────────────────────────────────────────────────────────


class NoOpMission:
    """Default mission — never touches commit_direction.  Use when
    initial commit is set elsewhere (e.g. via `--commit-bearing`)
    and the bot just needs to maintain that direction.
    """
    name = "noop"

    def update(self, frame: VisionFrame, state: NavState) -> NavState:
        return state


# ── Dead-end memory ──────────────────────────────────────────────────


class DeadEndMemoryMission:
    """Watches the tactical layer's LOCK-mode decisions and remembers
    the (lat, lon) of every dead_end / narrow_choke the ship reaches.

    Runs BEFORE tactical each tick, so it observes the *previous*
    tick's tactical dest (unchanged since tactical hasn't run yet
    this tick).  When the previous dest was a LOCK'd target and this
    tick's state either (a) still has that same dest but the ship is
    close, or (b) the dest was reached in the prior tick (indicated
    by dest scrolling off minimap or the reflex passing it), record
    the dest before tactical picks a new one.

    Simpler heuristic used here: track prev_dest across ticks; when
    the tactical dest changes and the previous one was a LOCK reason,
    record the previous dest.

    Does NOT touch commit_direction.  Downstream (LookaheadTactical)
    reads `state.visited_dead_ends` to reject candidates near any
    recorded point — that plumbing is added separately.
    """
    name = "deadend_memory"

    DEDUP_KM = 1.0     # duplicate detection: same dead-end within 1 km

    _LOCK_TOKENS = ("turning_point", "narrow_choke")

    def __init__(self):
        self._prev_dest: Optional[tuple[float, float]] = None
        self._prev_reason: str = ""

    def update(self, frame: VisionFrame, state: NavState) -> NavState:
        cur_dest = state.tactical_dest_latlon
        cur_reason = (state.commit_direction.reason or ""
                      if state.commit_direction is not None else "")

        # Fire on tactical-dest change when the *previous* dest was a
        # LOCK'd target — that means the ship reached it (or the
        # closer-than-reflex invariant fired) and tactical is about
        # to re-pick.
        if (self._prev_dest is not None
                and cur_dest is not None
                and tuple(cur_dest) != tuple(self._prev_dest)
                and any(tok in self._prev_reason
                        for tok in self._LOCK_TOKENS)):
            already = any(
                _haversine_km(self._prev_dest[0], self._prev_dest[1],
                              lat, lon) < self.DEDUP_KM
                for (lat, lon) in state.visited_dead_ends
            )
            if not already:
                state.visited_dead_ends.append(tuple(self._prev_dest))
                log.info("[mission:deadend_memory] recorded dead_end #%d "
                         "at (%.3f, %.3f) prev_reason=%s",
                         len(state.visited_dead_ends),
                         self._prev_dest[0], self._prev_dest[1],
                         self._prev_reason)

        self._prev_dest = tuple(cur_dest) if cur_dest is not None else None
        self._prev_reason = cur_reason
        return state


# ── Point-to-point ────────────────────────────────────────────────────


class PointToPointMission:
    """Steer the bot toward a fixed destination (lat, lon).

    Each tick:
      - Compute great-circle bearing from current lat/lon to dest.
      - Write commit_direction if it differs from the current commit
        by more than `recompute_threshold_deg` (keeps trace logs
        readable — small wobbles don't produce a CommitDirection
        update every tick).
      - Mark mission complete when current lat/lon is within
        `arrival_km` of destination.

    Waits for lat/lon to be set (first tick after dead-reckoning
    begins) before issuing any commit.
    """
    name = "point_to_point"

    def __init__(
        self,
        dest_lat: float,
        dest_lon: float,
        arrival_km: float = 5.0,
        recompute_threshold_deg: float = 8.0,
        return_to_lat: float | None = None,
        return_to_lon: float | None = None,
    ):
        self.dest_lat = dest_lat
        self.dest_lon = dest_lon
        self.arrival_km = arrival_km
        self.recompute_threshold_deg = recompute_threshold_deg
        # Round-trip: on arrival at the first destination, swap in the
        # return waypoint and keep going.  Set on init.  When None, the
        # mission just completes on arrival (single-leg behaviour).
        self.return_to_lat = return_to_lat
        self.return_to_lon = return_to_lon
        self._completed = False
        self._phase = "outbound"

    def update(self, frame: VisionFrame, state: NavState) -> NavState:
        if self._completed:
            state.mission_dest_latlon = None
            return state
        # Expose the current mission destination so downstream layers
        # (LookaheadTactical) can compute a dynamic goal bearing from
        # ship→dest instead of relying on a static --commit-bearing.
        state.mission_dest_latlon = (self.dest_lat, self.dest_lon)
        if state.lat is None or state.lon is None:
            return state

        # Arrival check
        d_km = _haversine_km(
            state.lat, state.lon, self.dest_lat, self.dest_lon,
        )
        if d_km <= self.arrival_km:
            if (self._phase == "outbound"
                    and self.return_to_lat is not None
                    and self.return_to_lon is not None):
                # Turn around: swap destination for the return waypoint
                # and keep the mission alive.  Steering will pick up the
                # new bearing on the next tick's recomputation.
                self.dest_lat = self.return_to_lat
                self.dest_lon = self.return_to_lon
                self._phase = "return"
            else:
                self._completed = True
                return state

        bearing = _bearing_deg(
            state.lat, state.lon, self.dest_lat, self.dest_lon,
        )

        current = (state.commit_direction.bearing_deg
                   if state.commit_direction is not None else None)
        if current is None:
            reason = "point_to_point_init"
        else:
            delta = abs(((bearing - current + 540.0) % 360.0) - 180.0)
            if delta < self.recompute_threshold_deg:
                return state
            reason = "point_to_point_update"

        state.commit_direction = CommitDirection(
            bearing_deg=bearing,
            reason=reason,
            set_at_tick=state.tick,
        )
        return state


# ── Generic explore ───────────────────────────────────────────────────


PlaceId = tuple   # opaque hashable; concrete shape depends on extractor


@dataclass
class PlaceGraph:
    """Visited-places memory.  The mission's "where have I been."

    Generic over the place extractor: keys are whatever the extractor
    returns.  For a grid_cell extractor, keys are (lat_idx, lon_idx)
    tuples; for a junction extractor, keys are junction node ids.
    """
    visited: set = field(default_factory=set)
    visit_order: list = field(default_factory=list)
    # entry_bearing[place] = bearing the bot arrived from; used by
    # decision rules that need to know which exit was "incoming."
    entry_bearing: dict = field(default_factory=dict)

    def visit(self, place: PlaceId, entry_bearing_deg: Optional[float]):
        if place in self.visited:
            return
        self.visited.add(place)
        self.visit_order.append(place)
        if entry_bearing_deg is not None:
            self.entry_bearing[place] = entry_bearing_deg

    def __contains__(self, place):
        return place in self.visited


PlaceExtractor   = Callable[[NavState], Optional[PlaceId]]
DecisionRule     = Callable[[PlaceGraph, NavState], Optional[float]]
TerminationCheck = Callable[[PlaceGraph, NavState], bool]


class ExploreMission:
    """Generic exploration mission.  Composes three pluggable rules:

      - `place_extractor(state) -> PlaceId | None`
            Returns a hashable place ID, or None if "no new place
            this tick" (most ticks).  Examples:
              - grid_cell(km=10): (round(lat/0.09), round(lon/0.09))
                                  ≈ 10 km grid
              - junction_cell:    returns place ID only when L3 reports
                                  topology=junction or dead_end
              - shore_segment:    lat/lon cell where bot is hugging shore
      - `decision_rule(graph, state) -> bearing | None`
            Returns the new commit_direction bearing, or None to keep
            current.  Examples:
              - nearest_frontier: bearing toward closest unvisited cell
              - tremaux:          at junction, prefer untaken exit
              - follow_then_loop: stay on current heading until loop
                                  closes
      - `is_complete(graph, state) -> bool`
            Termination check.  Examples:
              - coverage_complete(area, threshold=0.95)
              - all_junction_edges_traversed
              - returned_to_start

    The mission is a thin orchestrator — all variability lives in
    the three callables.
    """
    name = "explore"

    def __init__(
        self,
        place_extractor: PlaceExtractor,
        decision_rule:   DecisionRule,
        is_complete:     TerminationCheck = lambda g, s: False,
        config_name: str = "explore",
    ):
        self.place_extractor = place_extractor
        self.decision_rule   = decision_rule
        self.is_complete     = is_complete
        self.graph = PlaceGraph()
        self.name = config_name
        self._completed = False

    def update(self, frame: VisionFrame, state: NavState) -> NavState:
        if self._completed:
            return state

        # 1. Did the bot reach a new place?
        place = self.place_extractor(state)
        if place is not None:
            entry = (state.heading.bearing_deg
                     if state.heading is not None else None)
            self.graph.visit(place, entry)

        # 2. Mission complete?
        if self.is_complete(self.graph, state):
            self._completed = True
            return state

        # 3. Decide next direction.
        new_bearing = self.decision_rule(self.graph, state)
        if new_bearing is None:
            return state

        current = (state.commit_direction.bearing_deg
                   if state.commit_direction is not None else None)
        if current is not None:
            delta = abs(((new_bearing - current + 540.0) % 360.0) - 180.0)
            if delta < 5.0:
                return state

        state.commit_direction = CommitDirection(
            bearing_deg=new_bearing,
            reason=self.name,
            set_at_tick=state.tick,
        )
        return state


# ── Default place extractor + decision rule (grid + frontier) ─────────


def grid_cell_extractor(cell_size_km: float = 10.0) -> PlaceExtractor:
    """Quantize lat/lon to fixed-size cells.  Returns None when the
    bot's lat/lon isn't known yet (first ticks)."""
    cell_deg = cell_size_km / KM_PER_DEG_LAT

    def extract(state: NavState) -> Optional[PlaceId]:
        if state.lat is None or state.lon is None:
            return None
        return (round(state.lat / cell_deg), round(state.lon / cell_deg))
    return extract


def keep_commit_rule(graph: PlaceGraph, state: NavState) -> Optional[float]:
    """Trivial decision rule: keep whatever commit_direction is set;
    if none, use current heading.  Useful as a 'just keep going'
    baseline before a richer rule is wired in.
    """
    if state.commit_direction is not None:
        return None   # don't update
    if state.heading is not None:
        return state.heading.bearing_deg
    return None


# ── Helpers ───────────────────────────────────────────────────────────


def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def _bearing_deg(lat1, lon1, lat2, lon2):
    """Initial great-circle bearing from (lat1, lon1) to (lat2, lon2).
    Compass degrees (0=N, CW)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    y = math.sin(dlam) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2)
         - math.sin(phi1) * math.cos(phi2) * math.cos(dlam))
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


# ── River explore (junction graph + Trémaux) ─────────────────────────


class RiverExploreMission:
    """Wraps `brain.goals.junction_graph.JunctionGraph` as a Mission.

    Uses the place-graph + Trémaux state machine the project has
    accumulated for river exploration.  At each junction (keyed by
    HUD lat/lon cell), the graph picks an unexplored exit; when all
    exits are explored, it returns BACKTRACK and the mission flips
    commit_direction to retreat to the prior junction.

    Pre-junction commit direction (the "default heading" when no
    junction is active) comes from `default_bearing_deg`.  Set this
    via the runner with --commit-bearing or computed from a
    destination.

    Operation:
      - Tick has `state.topology` and `state.junction_exits_compass`
        from L3, plus HUD `state.lat`, `state.lon`.
      - On topology=junction with exits AND new cell (or first time):
        consult JunctionGraph → set commit_direction to the chosen
        exit bearing, hold that commitment.
      - On topology=channel or dead_end: clear commitment, restore
        default bearing (or backtrack if the graph said so).
      - Bot's actual escape from junction is detected by cell change;
        the graph itself handles "we backtracked here, mark this
        branch explored."
    """
    name = "river_explore"

    # Junction cell granularity: HUD lat/lon truncated to this many
    # decimal places.  0.05° ≈ 5.5 km — small enough that distinct
    # junctions stay distinct, big enough that re-visits hit the same
    # cell despite OCR jitter.
    CELL_DEG = 0.05

    # Stuck-in-junction detection.  If the ship has been in junction
    # topology and the current cell for at least STUCK_WINDOW ticks AND
    # its net displacement over that window is below STUCK_NET_DEG,
    # the currently-committed exit is a dead-end — mark it failed and
    # force a re-visit so the graph picks a different exit.
    STUCK_WINDOW = 25
    STUCK_NET_DEG = 0.03            # ~3 km at mid-Nile latitudes

    def __init__(self, default_bearing_deg: float = 180.0):
        from collections import deque
        self.default_bearing_deg = default_bearing_deg
        # Lazy import — brain.goals.junction_graph is heavy on first load.
        from brain.goals.junction_graph import JunctionGraph
        self._graph = JunctionGraph()
        self._active_decision = None     # JunctionDecision
        self._active_cell = None         # CellIdx of the junction we're
                                         # currently committing to
        self._prev_topology = None       # for channel→junction edge
        self._recent_positions: deque = deque(maxlen=self.STUCK_WINDOW)
        self._force_revisit = False      # set on stuck-detect; consumed
                                         # by next tick's new_visit gate

    def _cell(self, lat: float, lon: float):
        return (round(lat / self.CELL_DEG), round(lon / self.CELL_DEG))

    def update(self, frame, state):
        from brain.goals.junction_graph import TremauxAction

        if state.lat is None or state.lon is None:
            return state
        cell = self._cell(state.lat, state.lon)
        # Record trajectory each tick (graph uses this for reversal
        # detection + edge waypoints).
        self._graph.record_position((state.lat, state.lon))
        self._recent_positions.append((state.lat, state.lon))

        topology = state.topology
        # Clear active commitment when topology leaves junction.
        # channel / dead_end / lake → we've cleared the junction.
        if topology != "junction":
            self._active_decision = None
            self._active_cell = None
            self._recent_positions.clear()   # reset stuck detector

        # Stuck-in-junction detection: committed to an exit, been in
        # this cell for STUCK_WINDOW ticks, but net displacement is
        # tiny → the exit is a dead-end the graph didn't know about.
        # Mark it failed and clear the decision so the next visit picks
        # a different exit.  See t369-t400 in 2026-07-16 live voyage.
        if (topology == "junction"
                and self._active_decision is not None
                and self._active_cell == cell
                and len(self._recent_positions) >= self.STUCK_WINDOW):
            first = self._recent_positions[0]
            last = self._recent_positions[-1]
            net = abs(last[0] - first[0]) + abs(last[1] - first[1])
            if net < self.STUCK_NET_DEG:
                exit_bearing = self._active_decision.exit_bearing
                marked = self._graph.mark_exit_failed(cell, exit_bearing)
                if marked:
                    import logging
                    logging.getLogger(__name__).warning(
                        "[river_explore] stuck at cell=%s for %d ticks "
                        "(net %.4f°); marking exit @%.0f° failed",
                        cell, self.STUCK_WINDOW, net, exit_bearing,
                    )
                self._active_decision = None
                self._force_revisit = True
                self._recent_positions.clear()

        # New junction event = topology TRANSITIONED from non-junction
        # to junction.  Keying off transitions (not cell changes) fixes
        # the bend-circling issue: bots moving fast cross many cells
        # without leaving junction topology; re-triggering the graph
        # on every cell change made the planner unable to commit to
        # any exit.  See 2026-06-24 voyage @t339-t368.  Exception: if
        # the stuck-detector fired, force a re-visit even in the same
        # cell.
        is_junction = (
            topology == "junction"
            and len(state.junction_exits_compass) >= 2
        )
        new_visit = (
            is_junction
            and (self._prev_topology != "junction" or self._force_revisit)
            and self._active_decision is None
        )
        self._force_revisit = False

        if new_visit:
            arrived_via = (state.heading.bearing_deg
                           if state.heading is not None else None)
            decision = self._graph.visit(
                cell,
                tuple(state.junction_exits_compass),
                arrived_via,
            )
            self._active_decision = decision
            self._active_cell = cell

        # Drive commit_direction from current state.
        if self._active_decision is not None:
            # Use the graph's chosen exit (TAKE_EXIT or BACKTRACK both
            # provide an exit_bearing — TAKE_EXIT for forward
            # progress, BACKTRACK for retreat via the entrance).
            target = self._active_decision.exit_bearing
        else:
            target = self.default_bearing_deg

        current = (state.commit_direction.bearing_deg
                   if state.commit_direction is not None else None)
        if current is None or abs(((target - current + 540.0) % 360.0)
                                  - 180.0) >= 5.0:
            reason = ("river_explore_junction"
                      if self._active_decision is not None
                      else "river_explore_default")
            state.commit_direction = CommitDirection(
                bearing_deg=target,
                reason=reason,
                set_at_tick=state.tick,
            )
        self._prev_topology = topology
        return state
