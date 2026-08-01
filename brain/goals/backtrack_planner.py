"""Backtrack planner — yields reverse-trajectory waypoints.

See `docs/destination_generator_design.md` § 5c for the design.
When the bot needs to backtrack from a dead-end (or from an
exhausted junction) to a previous junction, the trajectory of the
edge that connects them was recorded by `JunctionGraph.record_position()`.
This module walks that trajectory in reverse, yielding the next
override target for the picker on each tick.

The planner is *pure* — given the recorded waypoints and the bot's
current position, it picks the next waypoint at lookahead distance.
No perception, no I/O.  Caller advances `target_idx` implicitly by
calling `next_target()` repeatedly with current positions.

Termination:
- `arrived_at_end()` returns True when the bot has consumed the
  whole reverse trajectory.  Caller switches the picker back to
  normal mode (exit backtracking).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# Threshold for "I've reached this waypoint" — same scale as
# CoverageTracker's cell size.  Tighter would force over-precise
# tracking; looser would skip waypoints.
DEFAULT_REACH_RADIUS_DEG = 0.05


@dataclass
class BacktrackPlanner:
    """Walks a recorded trajectory in reverse.

    Construction:
        # Trajectory is in original traversal order — newest sample
        # last.  The planner walks it from last to first.
        planner = BacktrackPlanner(waypoints=edge.waypoints[::-1])

    Per-tick:
        target = planner.next_target(current_pos)
        if planner.arrived_at_end():
            # we've reached the parent junction; exit backtrack mode
            ...
    """
    waypoints:    List[Tuple[float, float]] = field(default_factory=list)
    cursor:       int = 0
    reach_radius: float = DEFAULT_REACH_RADIUS_DEG

    def next_target(
        self, current_pos: Tuple[float, float],
    ) -> Optional[Tuple[float, float]]:
        """Return the next waypoint to steer toward.

        Advances `cursor` past any waypoints the bot has already
        reached (within `reach_radius`).  Returns None when the
        planner has consumed the whole trajectory.
        """
        # Skip waypoints we've already passed within reach_radius.
        while (self.cursor < len(self.waypoints)
                and _euclid(self.waypoints[self.cursor], current_pos)
                    < self.reach_radius):
            self.cursor += 1
        if self.cursor >= len(self.waypoints):
            return None
        return self.waypoints[self.cursor]

    def arrived_at_end(self) -> bool:
        """True when the planner has consumed the whole trajectory.
        Caller treats this as "the backtrack reached its destination
        junction" — exit backtracking mode.
        """
        return self.cursor >= len(self.waypoints)


def _euclid(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])
