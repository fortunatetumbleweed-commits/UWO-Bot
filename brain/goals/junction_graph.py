"""Junction graph — topological memory + Trémaux's algorithm.

See `docs/destination_generator_design.md` § 3b for the design
context.  Trémaux (1882) is the canonical solution for exploring a
finite branching environment when junctions can be observed
directly.  Provably complete, O(junctions) memory, deterministic
branch choice.

This module is the pure data + algorithm layer.  Junction *detection*
is in `junction_detector.py`; the picker that combines this graph
with bot state is `TremauxPicker` in `frontier_picker.py`.

The algorithm at a junction:
- First visit:    record the exits, push onto the stack, mark the
                  entrance as explored, return the first unexplored
                  exit.
- Re-entry:       look at unexplored exits; if any, return the next
                  one; if none, the junction is exhausted — return
                  a backtrack signal so the picker steers back along
                  the entrance edge to the parent junction.

Each junction is identified by the grid cell it sits in (matching
`CoverageTracker`'s cell scheme).  Two visits to nearby (same-cell)
positions are treated as the same junction.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple


CellIdx = Tuple[int, int]    # matches CoverageTracker's cell id


# Trajectory window for reversal detection (Dudek et al. 1991
# directional Trémaux marks, generalised to the pre-topological
# trajectory).  Number of recent samples to compare against the prior
# window of the same size.  Larger window = more stable but slower to
# detect; smaller = noisier.
DEFAULT_REVERSAL_WINDOW = 10

# Minimum displacement (degrees) over a window for its bearing to be
# considered well-defined.  Below this the bot is essentially in place
# (bounce, occlusion, mid-pivot) and bearing is unreliable.
MIN_WINDOW_DISPLACEMENT_DEG = 0.01

# Reversal threshold — Dudek 1991 directional marks fire when travel
# direction reverses by more than 90° vs the established direction.
REVERSAL_THRESHOLD_DEG = 90.0


class TremauxAction(str, Enum):
    """What Trémaux says to do at the current junction."""
    TAKE_EXIT       = "take_exit"        # take the chosen unexplored exit
    BACKTRACK       = "backtrack"        # all exits exhausted, go back
                                          # through the entrance


@dataclass
class JunctionDecision:
    """Result of consulting Trémaux at a junction visit."""
    action:          TremauxAction
    exit_bearing:    float       # absolute compass bearing to steer toward
    exhausted:       bool        # True iff this junction has no more
                                  # unexplored exits (caller may want
                                  # to pop the stack on backtrack)


@dataclass
class JunctionState:
    """Per-junction memory.

    `exits` is the absolute compass bearings of each branch.
    `explored` tracks which exits have been taken (by index).
    `entrance_idx` is the exit through which we first arrived
    (the parent edge).  None for the root junction.
    """
    exits:         Tuple[float, ...]
    explored:      set = field(default_factory=set)
    entrance_idx:  Optional[int] = None


class TopologyState(str, Enum):
    """Voyage-level topological state for the Trémaux algorithm.

    `PRE_TOPOLOGICAL` — no junction has been encountered yet.
    `EXPLORING`       — at least one unfinished junction on the stack.
    `EXHAUSTED`       — all known junctions explored, stack is empty.
    """
    PRE_TOPOLOGICAL = "pre_topological"
    EXPLORING       = "exploring"
    EXHAUSTED       = "exhausted"


@dataclass
class Edge:
    """A topological edge — the path between two junctions.

    `from_junction` — the cell the bot left from.
    `to_junction`   — the cell the bot arrived at; None while the
                       edge is being traversed (active edge).
    `from_exit_idx` — which exit of `from_junction` this edge uses.
    `waypoints`     — list of (lat, lon) samples along the trajectory,
                       in traversal order.  Used by BacktrackPlanner
                       to follow the edge in reverse.
    """
    from_junction:  "CellIdx"
    to_junction:    Optional["CellIdx"]
    from_exit_idx:  int
    waypoints:      List[Tuple[float, float]] = field(default_factory=list)


@dataclass
class JunctionGraph:
    """Topological graph + Trémaux state machine.

    Usage:
        graph = JunctionGraph()
        decision = graph.visit(cell, exits, arrived_via_bearing)
        # caller steers toward decision.exit_bearing
        # on next junction or dead-end, call visit() again

    Phase 5 additions:
    - `ever_visited` counter (drives `topology_state`).
    - `edges` — list of recorded edges between junctions.
    - `active_edge` — the edge currently being traversed.
    - `record_position()` — caller pushes position samples each tick
      to populate the active edge's waypoints.

    Reversal-detection additions (Dudek 1991 directional Trémaux marks,
    generalised to the pre-topological trajectory):
    - `trajectory` — every position sample, regardless of whether an
      edge is active.  Used by `is_reversing()` so reversal detection
      works before the first junction is encountered.
    """
    junctions:    Dict[CellIdx, JunctionState] = field(default_factory=dict)
    stack:        List[CellIdx] = field(default_factory=list)
    ever_visited: int = 0
    edges:        List[Edge] = field(default_factory=list)
    active_edge:  Optional[Edge] = field(default=None)
    trajectory:   List[Tuple[float, float]] = field(default_factory=list)

    def visit(
        self,
        cell:                CellIdx,
        exits:               Tuple[float, ...],
        arrived_via_bearing: Optional[float],
    ) -> JunctionDecision:
        """Process a junction visit; return the decision.

        `cell` identifies the junction.  `exits` are the absolute
        compass bearings of each branch.  `arrived_via_bearing` is
        the compass bearing the bot was heading WHILE entering the
        junction (used to identify which exit is the entrance).
        Pass `None` for the root visit (voyage start).
        """
        first_visit = cell not in self.junctions
        if first_visit:
            entrance_idx = (
                _closest_exit_index(exits, _flip(arrived_via_bearing))
                if arrived_via_bearing is not None else None
            )
            state = JunctionState(exits=exits, entrance_idx=entrance_idx)
            if entrance_idx is not None:
                state.explored.add(entrance_idx)
            self.junctions[cell] = state
            self.stack.append(cell)
            self.ever_visited += 1
            # Close any active edge — we've arrived at a new junction.
            if self.active_edge is not None:
                self.active_edge.to_junction = cell
                self.edges.append(self.active_edge)
                self.active_edge = None
        else:
            state = self.junctions[cell]
            # On re-entry: mark the exit we came back through as
            # explored.  We just sailed in via `arrived_via_bearing`;
            # the exit we used (looking from the junction outward) has
            # the flipped bearing.
            if arrived_via_bearing is not None:
                idx = _closest_exit_index(exits, _flip(arrived_via_bearing))
                state.explored.add(idx)

        unexplored = [
            i for i in range(len(state.exits)) if i not in state.explored
        ]
        if unexplored:
            # Hug-left rule: pick the exit with the smallest relative
            # bearing measured counterclockwise from the entrance.
            # "Smallest counterclockwise distance from the entrance"
            # means most-leftward exit if you imagine standing at the
            # entrance and turning counterclockwise.
            entrance = state.exits[state.entrance_idx] \
                if state.entrance_idx is not None else 0.0
            best = min(unexplored, key=lambda i: _ccw_distance(
                entrance, state.exits[i]))
            state.explored.add(best)
            return JunctionDecision(
                action=TremauxAction.TAKE_EXIT,
                exit_bearing=state.exits[best],
                exhausted=False,
            )
        # All exits explored — backtrack via the entrance.
        if state.entrance_idx is not None:
            # Pop from stack if this is the top.
            if self.stack and self.stack[-1] == cell:
                self.stack.pop()
            return JunctionDecision(
                action=TremauxAction.BACKTRACK,
                exit_bearing=state.exits[state.entrance_idx],
                exhausted=True,
            )
        # Root junction with no entrance — all branches walked, voyage
        # complete from the topology's perspective.
        return JunctionDecision(
            action=TremauxAction.BACKTRACK,
            exit_bearing=0.0,
            exhausted=True,
        )

    @property
    def topology_state(self) -> TopologyState:
        """Voyage-level topology state for picker dispatch.

        See `TopologyState` for the three values.  Drives whether the
        picker should act at all (PRE_TOPOLOGICAL → no), continue
        exploring (EXPLORING), or signal completion (EXHAUSTED).
        """
        if self.ever_visited == 0:
            return TopologyState.PRE_TOPOLOGICAL
        if self.stack:
            return TopologyState.EXPLORING
        return TopologyState.EXHAUSTED

    def mark_exit_failed(
        self,
        cell: CellIdx,
        exit_bearing: float,
    ) -> bool:
        """Mark an exit at `cell` as tried-and-failed.

        Adds the closest-matching exit index to `explored`, so the next
        `visit()` call excludes it from `unexplored` and picks a
        different branch.

        Returns True if the mark was applied; False if the cell is
        unknown or the exit couldn't be identified.  Used by
        RiverExploreMission when the ship has been stuck in a junction
        cell with no net progress — the committed exit is a dead-end
        the graph didn't know about yet.
        """
        state = self.junctions.get(cell)
        if state is None:
            return False
        idx = _closest_exit_index(state.exits, exit_bearing)
        state.explored.add(idx)
        return True

    def start_edge(self, from_cell: CellIdx, from_exit_idx: int) -> None:
        """Begin recording a new edge from this junction through the
        given exit.  Called by the picker when it picks a new exit
        to traverse.
        """
        self.active_edge = Edge(
            from_junction=from_cell,
            to_junction=None,
            from_exit_idx=from_exit_idx,
        )

    def record_position(self, pos: Tuple[float, float]) -> None:
        """Push a position sample onto the trajectory log and (if any)
        the active edge's waypoints.

        Trajectory is appended unconditionally — it drives the
        Dudek-1991 reversal check, which must work in PRE_TOPOLOGICAL
        state (before any junction has been encountered and an edge
        has been opened).
        """
        self.trajectory.append(pos)
        if self.active_edge is not None:
            self.active_edge.waypoints.append(pos)

    def is_reversing(
        self,
        window: int = DEFAULT_REVERSAL_WINDOW,
        threshold_deg: float = REVERSAL_THRESHOLD_DEG,
    ) -> bool:
        """True iff the recent travel bearing has reversed vs the
        prior window's travel bearing.

        Implements Dudek-Jenkin-Milios-Wilkes 1991 directional Trémaux
        edge marks, generalised to the raw trajectory so the check
        works in PRE_TOPOLOGICAL state.  Compares the bearing from
        `trajectory[-2*window]` → `trajectory[-window-1]` (prior) with
        `trajectory[-window]` → `trajectory[-1]` (recent).  Bearings
        are derived from position deltas — i.e. velocity — not from
        the noisier ship-heading estimate.

        Returns False when there aren't enough samples or when either
        window has insufficient displacement to define a bearing
        (mid-pivot / occluded position).
        """
        if len(self.trajectory) < 2 * window:
            return False
        prior_bearing  = _bearing(
            self.trajectory[-2 * window], self.trajectory[-window - 1])
        recent_bearing = _bearing(
            self.trajectory[-window], self.trajectory[-1])
        if prior_bearing is None or recent_bearing is None:
            return False
        delta = abs(((recent_bearing - prior_bearing + 180.0) % 360.0) - 180.0)
        return delta > threshold_deg


# ── Helpers ─────────────────────────────────────────────────────────────────

def _closest_exit_index(
    exits: Tuple[float, ...], bearing: float,
) -> int:
    """Index of the exit whose bearing is closest to `bearing`."""
    def diff(b):
        return abs(((b - bearing + 180.0) % 360.0) - 180.0)
    return min(range(len(exits)), key=lambda i: diff(exits[i]))


def _flip(bearing: float) -> float:
    """Compass bearing 180° away (the reciprocal direction)."""
    return (bearing + 180.0) % 360.0


def _ccw_distance(a: float, b: float) -> float:
    """Counterclockwise angular distance from compass bearing `a`
    to `b`, in [0, 360).  Used by the hug-left rule: smaller =
    more leftward when standing at `a` looking outward."""
    return (a - b) % 360.0


def _bearing(
    a: Tuple[float, float], b: Tuple[float, float],
) -> Optional[float]:
    """Compass-like bearing from `a` to `b`, derived from a position
    delta.  Returns None when the points are closer than
    `MIN_WINDOW_DISPLACEMENT_DEG` — at that scale the bearing is
    swamped by position noise."""
    dlat = b[0] - a[0]
    dlon = b[1] - a[1]
    if math.hypot(dlat, dlon) < MIN_WINDOW_DISPLACEMENT_DEG:
        return None
    return math.degrees(math.atan2(dlat, dlon))
