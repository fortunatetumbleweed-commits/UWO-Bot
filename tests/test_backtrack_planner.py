"""Tests for the BacktrackPlanner."""
import unittest

from brain.goals.backtrack_planner import BacktrackPlanner


class BasicTests(unittest.TestCase):
    def test_empty_trajectory_returns_none(self):
        p = BacktrackPlanner(waypoints=[])
        self.assertIsNone(p.next_target(current_pos=(0.0, 0.0)))
        self.assertTrue(p.arrived_at_end())

    def test_yields_first_waypoint_when_far(self):
        # Trajectory of three waypoints (already in reverse order).
        p = BacktrackPlanner(
            waypoints=[(1.0, 0.0), (2.0, 0.0), (3.0, 0.0)],
            reach_radius=0.05,
        )
        # Bot far from any waypoint — yields first.
        self.assertEqual(p.next_target((0.0, 0.0)), (1.0, 0.0))

    def test_advances_when_close_to_waypoint(self):
        p = BacktrackPlanner(
            waypoints=[(1.0, 0.0), (2.0, 0.0), (3.0, 0.0)],
            reach_radius=0.05,
        )
        # Bot already at first waypoint → advances to second.
        self.assertEqual(p.next_target((1.0, 0.0)), (2.0, 0.0))
        # Bot at second → third.
        self.assertEqual(p.next_target((2.0, 0.0)), (3.0, 0.0))

    def test_returns_none_at_end(self):
        p = BacktrackPlanner(
            waypoints=[(1.0, 0.0)],
            reach_radius=0.05,
        )
        # Bot reaches the only waypoint.
        self.assertEqual(p.next_target((1.0, 0.0)), None)
        self.assertTrue(p.arrived_at_end())

    def test_sequential_progression_through_waypoints(self):
        """Realistic usage: bot reaches each waypoint in turn and the
        planner advances one step at a time."""
        p = BacktrackPlanner(
            waypoints=[(1.0, 0.0), (2.0, 0.0), (3.0, 0.0), (4.0, 0.0)],
            reach_radius=0.05,
        )
        # Start.
        self.assertEqual(p.next_target((0.5, 0.0)), (1.0, 0.0))
        # Approach first.
        self.assertEqual(p.next_target((1.0, 0.0)), (2.0, 0.0))
        self.assertEqual(p.next_target((2.0, 0.0)), (3.0, 0.0))
        self.assertEqual(p.next_target((3.0, 0.0)), (4.0, 0.0))
        self.assertEqual(p.next_target((4.0, 0.0)), None)


class ReverseUsagePattern(unittest.TestCase):
    """The canonical caller pattern: trajectory is in original
    traversal order; planner is constructed with `[::-1]`."""

    def test_canonical_reverse_traversal(self):
        # Bot's outbound trajectory: A → B → C.
        outbound = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (3.0, 0.0)]
        # Construct planner with reversed waypoints.
        p = BacktrackPlanner(waypoints=outbound[::-1])
        # First call from "C" should aim for the previous sample
        # (next-to-last in outbound = second-to-first in reverse).
        self.assertEqual(p.next_target((3.0, 0.0)), (2.0, 0.0))
        # Continue.
        self.assertEqual(p.next_target((2.0, 0.0)), (1.0, 0.0))
        self.assertEqual(p.next_target((1.0, 0.0)), (0.0, 0.0))
        self.assertEqual(p.next_target((0.0, 0.0)), None)
        self.assertTrue(p.arrived_at_end())


if __name__ == "__main__":
    unittest.main()
