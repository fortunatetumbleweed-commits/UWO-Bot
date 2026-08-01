"""Frontier picker — decides where to go when CoverageTracker reports STUCK.

See `docs/destination_generator_design.md` § "Phase 2 update" for the
design context.  In short: when the CoverageTracker has emitted a
STUCK verdict — the visited-cell set isn't growing — this module
picks a *frontier cell* (unvisited cell adjacent to a visited cell)
as the recovery target.  The goal layer overrides the destination-
generator's normal lookahead with this target for a commit window,
breaking the oscillation.

Pluggable backends:
  - `YamauchiPicker` (this file) — classical frontier-based
    exploration (Yamauchi 1997, Holz 2010 goal-biased variant).
    Pure heuristic, no model.
  - `AIPicker` (future) — consult Claude or a learned policy.
  - `HybridPicker` (future) — Yamauchi when confidence is high,
    AI for ambiguous junctions.

The picker is pure — no perception, no I/O.  The CoverageTracker is
passed in as state; the picker reads `frontiers()` and `cell_center()`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Tuple

from brain.goals.backtrack_planner import BacktrackPlanner
from brain.goals.coverage_tracker import CoverageTracker
from brain.goals.junction_detector import (
    JunctionDetector,
    TopologyKind,
)
from brain.goals.junction_graph import (
    JunctionGraph,
    TopologyState,
    TremauxAction,
)


# ── Tunables ────────────────────────────────────────────────────────────────

# Weight on distance-from-current in the score.  α = 1 corresponds
# to classical Yamauchi (pick nearest frontier).
DEFAULT_ALPHA = 1.0

# Weight on distance-to-endpoint in the score.  β > 0 introduces a
# goal bias (Holz 2010).  With β = 0 the picker is pure nearest;
# higher β means prefer frontiers that also bring us closer to the
# endpoint.  Endpoint must be supplied to `pick()` for β to matter.
DEFAULT_BETA = 1.0


# ── Pickers ─────────────────────────────────────────────────────────────────

@dataclass
class YamauchiPicker:
    """Frontier picker scored by linear combination of two terms.

        score(frontier) = α · dist(current, frontier_center)
                        + β · dist(frontier_center, endpoint)

    Lowest score wins.  α = 1, β = 0  →  pure-nearest Yamauchi 1997.
    α = 1, β > 0  →  goal-biased Holz 2010.

    Ties are broken deterministically by `(cell_i, cell_j)` order
    so the output is reproducible.
    """
    alpha: float = DEFAULT_ALPHA
    beta:  float = DEFAULT_BETA

    def pick(
        self,
        current_pos: Tuple[float, float],
        coverage:    CoverageTracker,
        endpoint:    Optional[Tuple[float, float]] = None,
        nav=None,
    ) -> Optional[Tuple[float, float]]:
        """Pick a frontier and return its lat/lon center.

        Returns None when no frontiers exist (no visited cells yet,
        or every neighbour is already visited — the latter shouldn't
        happen in practice but is handled).

        `nav` is accepted and ignored — present so that the picker
        protocol is uniform with `TremauxPicker`.
        """
        del nav
        frontiers = coverage.frontiers()
        if not frontiers:
            return None

        def score(frontier_cell):
            center = coverage.cell_center(frontier_cell)
            d_cur = _euclid(current_pos, center)
            d_end = _euclid(center, endpoint) if endpoint is not None else 0.0
            return (self.alpha * d_cur + self.beta * d_end, frontier_cell)

        best = min(frontiers, key=score)
        return coverage.cell_center(best)


def _euclid(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


# Lookahead distance for converting a Trémaux exit bearing into a
# target point.  Matches POINT_PURSUIT_L in `hug_shore.py` so the
# generator-replacing target is at the same scale as a normal
# destination.
DEFAULT_TREMAUX_LOOKAHEAD_DEG = 0.10

# Cell-size used to identify junctions.  Must match
# `CoverageTracker.cell_size_deg` so that the same lat/lon
# corresponds to the same junction id across multiple visits.
DEFAULT_JUNCTION_CELL_SIZE_DEG = 0.1

# How many trajectory samples to walk back in reverse when reversal
# is detected.  Bounds the backtrack so it just undoes the U-turn
# rather than re-traversing the whole voyage.  Tuning intuition:
# the bot's drift between firing reversal and the BacktrackPlanner
# acting is on the order of 2 × DEFAULT_REVERSAL_WINDOW (≈ 20 ticks);
# add headroom so the backtrack target is meaningfully behind us.
DEFAULT_REVERSAL_BACKTRACK_WINDOW = 30


@dataclass
class TremauxPicker:
    """Trémaux DFS state machine — junctions, dead-ends, backtracking,
    exhaustion.

    See `docs/destination_generator_design.md` § 5a for the full mode
    table.  Per-tick dispatch:

    | Perception | Picker action |
    | ---------- | ------------- |
    | CHANNEL    | Record position onto active edge; return None. |
    | JUNCTION   | `graph.visit()`; pick exit OR signal backtrack. |
    | DEAD_END   | Start a `BacktrackPlanner` over the active edge in reverse; return its first waypoint. |
    | (backtracking already in progress) | Continue yielding from the planner; switch back to normal when arrived_at_end. |
    | (exhausted) | Return None; caller decides what to do. |

    Stateful — holds the `JunctionGraph` and the in-flight
    `BacktrackPlanner` across the voyage.
    """
    detector:  JunctionDetector = field(default_factory=JunctionDetector)
    graph:     JunctionGraph = field(default_factory=JunctionGraph)
    cell_size_deg:  float = DEFAULT_JUNCTION_CELL_SIZE_DEG
    lookahead_deg:  float = DEFAULT_TREMAUX_LOOKAHEAD_DEG
    reversal_backtrack_window: int = DEFAULT_REVERSAL_BACKTRACK_WINDOW
    # In-flight backtrack: set when the bot is following a recorded
    # edge in reverse.  Cleared when the planner reports arrived.
    _backtracker: Optional[BacktrackPlanner] = field(default=None, init=False)
    # Most-recent mode for diagnostics / trace.
    last_mode: str = field(default="pre_topological", init=False)

    def pick(
        self,
        current_pos: Tuple[float, float],
        coverage:    CoverageTracker,
        endpoint:    Optional[Tuple[float, float]] = None,
        nav=None,
    ) -> Optional[Tuple[float, float]]:
        """Dispatch on topology and return a target (or None)."""
        # Backtracking in progress takes priority — finish before
        # responding to new perception.
        if self._backtracker is not None:
            target = self._backtracker.next_target(current_pos)
            if self._backtracker.arrived_at_end():
                # Arrived back at the start of the backtrack tail.
                self._backtracker = None
                self.last_mode = "arrived_from_backtrack"
                # The next perception read will either show a junction
                # (we're at the parent) and continue DFS, or show a
                # channel (we're between junctions) and we sit out.
            else:
                self.last_mode = "backtracking"
            # During backtracking we DON'T record position onto an
            # active edge — we're reversing a recorded edge, not
            # forging a new one.
            return target

        # Always log to trajectory.  Done before the reversal check so
        # the latest sample is included; safe even when an active
        # edge exists (it grows alongside trajectory in CHANNEL mode).
        self.graph.record_position(current_pos)

        # Reversal check — Dudek 1991 directional Trémaux marks,
        # generalised to the raw trajectory so it fires in
        # PRE_TOPOLOGICAL state (no junction yet).  When the recent
        # window's travel bearing has reversed >90° vs the prior
        # window's, the bot is walking back over already-traveled
        # path.  Fire a BacktrackPlanner over the last K trajectory
        # samples reversed to undo the U-turn; existing in-flight
        # backtrack will not be re-entered (the check above gates it).
        if self.graph.is_reversing():
            tail = list(reversed(
                self.graph.trajectory[-self.reversal_backtrack_window:]
            ))
            if len(tail) >= 2:
                self._backtracker = BacktrackPlanner(waypoints=tail)
                self.last_mode = "reversal_backtrack"
                return self._backtracker.next_target(current_pos)

        # No nav → can't perceive topology.
        if nav is None or not hasattr(nav, "sectors"):
            self.last_mode = "no_nav"
            return None

        # Classify the local geometry.
        # Pass nav.water_mask through when available so the detector
        # can run its skeleton hybrid (LAKE override + disagreement
        # logging).  Stub navs in tests don't expose water_mask;
        # getattr-with-None keeps backward compat.
        topo = self.detector.classify(
            nav.sectors,
            water_mask=getattr(nav, "water_mask", None),
        )
        heading = getattr(nav, "ship_heading_deg", None)

        if topo.kind == TopologyKind.CHANNEL:
            # Normal between-junctions navigation.  Trajectory already
            # logged above; let the destination-generator continue.
            self.last_mode = (
                "pre_topological"
                if self.graph.topology_state == TopologyState.PRE_TOPOLOGICAL
                else "between_junctions"
            )
            return None

        if heading is None:
            # Perception of a junction/dead-end without a confident
            # heading is too ambiguous to act on.
            self.last_mode = "no_heading"
            return None

        if topo.kind == TopologyKind.DEAD_END:
            # Bot has reached the end of the current branch.  Build a
            # reverse-trajectory planner over the active edge so we
            # can navigate back to the parent junction.
            edge = self.graph.active_edge
            if edge is None or not edge.waypoints:
                # No recorded edge to backtrack along — just signal
                # nothing and let the caller's normal mechanisms run
                # (UTurnRecovery may fire).
                self.last_mode = "dead_end_no_edge"
                return None
            self._backtracker = BacktrackPlanner(
                waypoints=list(reversed(edge.waypoints)),
            )
            self.last_mode = "dead_end_start_backtrack"
            return self._backtracker.next_target(current_pos)

        # JUNCTION.
        absolute_exits = tuple(
            (heading + rel) % 360.0 for rel in topo.exits
        )
        cell = (math.floor(current_pos[0] / self.cell_size_deg),
                math.floor(current_pos[1] / self.cell_size_deg))
        decision = self.graph.visit(
            cell=cell,
            exits=absolute_exits,
            arrived_via_bearing=heading,
        )
        if decision.action == TremauxAction.TAKE_EXIT:
            # Start recording the edge for the new exit so we can
            # backtrack later if needed.
            # Find the index of the chosen exit in the absolute_exits
            # tuple (matches state.explored bookkeeping).
            best_idx = min(
                range(len(absolute_exits)),
                key=lambda i: abs(
                    ((absolute_exits[i] - decision.exit_bearing + 180.0)
                     % 360.0) - 180.0
                ),
            )
            self.graph.start_edge(from_cell=cell, from_exit_idx=best_idx)
            self.last_mode = "at_junction_take_exit"
        else:
            # BACKTRACK from the junction — all exits exhausted.
            # Build a reverse planner from the active edge if any.
            self.last_mode = "at_junction_backtrack"
            edge = self.graph.active_edge
            if edge is not None and edge.waypoints:
                self._backtracker = BacktrackPlanner(
                    waypoints=list(reversed(edge.waypoints)),
                )
                return self._backtracker.next_target(current_pos)
            # No edge to backtrack along — fall through to bearing
            # target.

        rad = math.radians(decision.exit_bearing)
        return (current_pos[0] + self.lookahead_deg * math.cos(rad),
                current_pos[1] + self.lookahead_deg * math.sin(rad))
