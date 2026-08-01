"""Tests for the junction graph + Trémaux algorithm."""
import unittest

from brain.goals.junction_graph import (
    JunctionGraph,
    TopologyState,
    TremauxAction,
)


class FirstVisitTests(unittest.TestCase):
    def test_first_visit_records_junction_and_picks_exit(self):
        g = JunctionGraph()
        # Three-way Y junction: aft (came from), front-left, front-right.
        # Bot arrived heading north (0°), so the entrance is the SOUTH
        # exit (180°).
        exits = (0.0, 60.0, 180.0)   # forward, right-of-forward, south (entrance)
        d = g.visit((0, 0), exits, arrived_via_bearing=0.0)
        self.assertEqual(d.action, TremauxAction.TAKE_EXIT)
        # Entrance (180°) should NOT be the picked exit.
        self.assertNotEqual(d.exit_bearing, 180.0)
        # Graph state recorded.
        self.assertIn((0, 0), g.junctions)
        # Stack has the junction.
        self.assertEqual(g.stack, [(0, 0)])

    def test_root_visit_with_no_arrival_direction(self):
        """Voyage start at a junction with no came-from."""
        g = JunctionGraph()
        exits = (0.0, 120.0, 240.0)
        d = g.visit((0, 0), exits, arrived_via_bearing=None)
        # No entrance recorded; all exits unexplored at first visit
        # except the one Trémaux picks.
        state = g.junctions[(0, 0)]
        self.assertIsNone(state.entrance_idx)


class HugLeftTests(unittest.TestCase):
    """Verify Trémaux picks the left-most unexplored exit relative
    to the entrance."""

    def test_picks_leftmost_relative_to_entrance(self):
        g = JunctionGraph()
        # Entrance at 180° (south).  Two front exits: 90° (east),
        # 270° (west).  Looking outward from the entrance (i.e. into
        # the junction), counterclockwise from south goes to east
        # first, then to west.  Hug-left rule:
        # _ccw_distance(entrance=180, 90)  = (180 - 90)  % 360 =  90
        # _ccw_distance(entrance=180, 270) = (180 - 270) % 360 = 270
        # Smaller CCW distance → picked.  90 < 270 → pick east (90°).
        exits = (90.0, 180.0, 270.0)
        d = g.visit((0, 0), exits, arrived_via_bearing=0.0)
        # arrived_via_bearing=0 means heading north, so entrance is
        # flipped to 180°.  Hug-left → 90°.
        self.assertEqual(d.action, TremauxAction.TAKE_EXIT)
        self.assertEqual(d.exit_bearing, 90.0)


class ReEntryTests(unittest.TestCase):
    def test_reentry_picks_remaining_exit(self):
        """Bot enters Y, picks left, hits dead-end, returns; picks right."""
        g = JunctionGraph()
        exits = (90.0, 180.0, 270.0)
        # First visit: arrived heading 0° (entrance=180°).
        d1 = g.visit((0, 0), exits, arrived_via_bearing=0.0)
        first_pick = d1.exit_bearing
        # Bot sails out the picked exit, hits a dead-end, sails back.
        # Returning means heading is opposite of first_pick.
        return_heading = (first_pick + 180.0) % 360.0
        d2 = g.visit((0, 0), exits, arrived_via_bearing=return_heading)
        # Should pick the remaining exit (not 180°, not first_pick).
        self.assertEqual(d2.action, TremauxAction.TAKE_EXIT)
        self.assertNotEqual(d2.exit_bearing, 180.0)
        self.assertNotEqual(d2.exit_bearing, first_pick)

    def test_all_exits_explored_backtracks(self):
        """After exploring both branches of a Y, the third visit
        (returning from the second branch) should report BACKTRACK
        with the entrance bearing."""
        g = JunctionGraph()
        exits = (90.0, 180.0, 270.0)
        d1 = g.visit((0, 0), exits, arrived_via_bearing=0.0)
        return1 = (d1.exit_bearing + 180.0) % 360.0
        d2 = g.visit((0, 0), exits, arrived_via_bearing=return1)
        return2 = (d2.exit_bearing + 180.0) % 360.0
        d3 = g.visit((0, 0), exits, arrived_via_bearing=return2)
        self.assertEqual(d3.action, TremauxAction.BACKTRACK)
        self.assertEqual(d3.exit_bearing, 180.0)   # back through entrance
        self.assertTrue(d3.exhausted)


class StackTests(unittest.TestCase):
    def test_stack_pops_on_full_backtrack(self):
        g = JunctionGraph()
        exits = (0.0, 180.0)   # 2 exits — straight channel (degenerate test
                                # case; the algorithm should still work)
        # First visit: arrived heading 0° → entrance idx = 1 (180°).
        d1 = g.visit((0, 0), exits, arrived_via_bearing=0.0)
        # Picks 0° (the only unexplored).
        self.assertEqual(d1.exit_bearing, 0.0)
        self.assertEqual(g.stack, [(0, 0)])
        # Bot sails out, returns (heading 180°).
        d2 = g.visit((0, 0), exits, arrived_via_bearing=180.0)
        self.assertEqual(d2.action, TremauxAction.BACKTRACK)
        self.assertTrue(d2.exhausted)
        # Stack popped.
        self.assertEqual(g.stack, [])


class MultiJunctionScenarioTests(unittest.TestCase):
    """End-to-end scenario: a chain of two Y junctions."""

    def test_explores_first_junction_then_second(self):
        g = JunctionGraph()
        # First junction at cell (0,0): entrance south, exits north and east.
        d1 = g.visit((0, 0), (0.0, 90.0, 180.0), arrived_via_bearing=0.0)
        # First visit picks east (90°) per hug-left rule
        # (CCW from entrance 180°: 0°=180°, 90°=90°; 90 < 180 so pick 90°).
        self.assertEqual(d1.exit_bearing, 90.0)
        # Bot sails east, reaches second junction at cell (0,1).
        d2 = g.visit((0, 1), (90.0, 0.0, 270.0), arrived_via_bearing=90.0)
        # Entrance to second junction = west (270°).
        # Both junctions are on the stack now.
        self.assertEqual(g.stack, [(0, 0), (0, 1)])
        # Pick some exit at the second junction; not the entrance.
        self.assertNotEqual(d2.exit_bearing, 270.0)


class TopologyStateTests(unittest.TestCase):
    def test_fresh_graph_is_pre_topological(self):
        g = JunctionGraph()
        self.assertEqual(g.topology_state, TopologyState.PRE_TOPOLOGICAL)

    def test_after_first_visit_is_exploring(self):
        g = JunctionGraph()
        g.visit((0, 0), (0.0, 120.0, 240.0), arrived_via_bearing=None)
        self.assertEqual(g.topology_state, TopologyState.EXPLORING)

    def test_after_all_exhausted_is_exhausted(self):
        g = JunctionGraph()
        exits = (90.0, 180.0, 270.0)
        d1 = g.visit((0, 0), exits, arrived_via_bearing=0.0)
        return1 = (d1.exit_bearing + 180.0) % 360.0
        d2 = g.visit((0, 0), exits, arrived_via_bearing=return1)
        return2 = (d2.exit_bearing + 180.0) % 360.0
        g.visit((0, 0), exits, arrived_via_bearing=return2)
        # All exits explored, stack popped — EXHAUSTED.
        self.assertEqual(g.topology_state, TopologyState.EXHAUSTED)


class EdgeRecordingTests(unittest.TestCase):
    def test_start_edge_creates_active_edge(self):
        g = JunctionGraph()
        g.visit((0, 0), (0.0, 90.0, 180.0), arrived_via_bearing=0.0)
        g.start_edge((0, 0), from_exit_idx=0)
        self.assertIsNotNone(g.active_edge)
        self.assertEqual(g.active_edge.from_junction, (0, 0))

    def test_record_position_appends_waypoint(self):
        g = JunctionGraph()
        g.visit((0, 0), (0.0, 90.0, 180.0), arrived_via_bearing=0.0)
        g.start_edge((0, 0), from_exit_idx=0)
        g.record_position((30.0, 30.0))
        g.record_position((30.05, 30.05))
        self.assertEqual(len(g.active_edge.waypoints), 2)

    def test_arriving_at_new_junction_closes_edge(self):
        g = JunctionGraph()
        # First junction.
        g.visit((0, 0), (0.0, 90.0, 180.0), arrived_via_bearing=0.0)
        g.start_edge((0, 0), from_exit_idx=0)
        g.record_position((30.0, 30.0))
        g.record_position((30.05, 30.05))
        # Arrive at second junction.
        g.visit((0, 1), (0.0, 90.0, 180.0), arrived_via_bearing=90.0)
        # Active edge should be closed and pushed onto edges list.
        self.assertIsNone(g.active_edge)
        self.assertEqual(len(g.edges), 1)
        self.assertEqual(g.edges[0].from_junction, (0, 0))
        self.assertEqual(g.edges[0].to_junction, (0, 1))
        self.assertEqual(len(g.edges[0].waypoints), 2)


class ReversalDetectionTests(unittest.TestCase):
    """Dudek-1991 directional Trémaux marks generalised to the
    trajectory.  The check fires when the recent window's travel
    bearing is >90° off the prior window's."""

    def _walk(self, g, points):
        for p in points:
            g.record_position(p)

    def test_insufficient_samples_returns_false(self):
        g = JunctionGraph()
        # 10 samples, window=10 → need 20.
        self._walk(g, [(i * 0.1, 0.0) for i in range(10)])
        self.assertFalse(g.is_reversing())

    def test_straight_trajectory_not_reversing(self):
        g = JunctionGraph()
        # 25 samples moving east (lon increases).
        self._walk(g, [(0.0, i * 0.1) for i in range(25)])
        self.assertFalse(g.is_reversing())

    def test_uturn_detected_as_reversing(self):
        g = JunctionGraph()
        # 20 samples east — prior window (indices 10..19) is clean east.
        self._walk(g, [(0.0, i * 0.1) for i in range(20)])
        # 10 samples west — recent window (indices 20..29) is clean west.
        self._walk(g, [(0.0, 1.9 - i * 0.1) for i in range(1, 11)])
        self.assertTrue(g.is_reversing())

    def test_stationary_window_returns_false(self):
        """Position locked (bounce, occlusion) → bearing undefined,
        no false positive."""
        g = JunctionGraph()
        # 20 samples on top of each other (no displacement).
        self._walk(g, [(5.0, 5.0)] * 20)
        self.assertFalse(g.is_reversing())

    def test_threshold_below_90_does_not_fire(self):
        g = JunctionGraph()
        # First 15 samples east, then a 45° turn (still net forward).
        self._walk(g, [(0.0, i * 0.1) for i in range(15)])
        # NE direction (45°) — within 90° of east.
        self._walk(g, [(i * 0.07, 1.5 + i * 0.07) for i in range(1, 16)])
        self.assertFalse(g.is_reversing())


class TrajectoryGrowsUnconditionallyTests(unittest.TestCase):
    def test_record_position_appends_to_trajectory_even_without_edge(self):
        g = JunctionGraph()
        # No active edge — but trajectory still grows.
        g.record_position((1.0, 1.0))
        g.record_position((2.0, 2.0))
        self.assertEqual(len(g.trajectory), 2)

    def test_record_position_appends_to_both_when_edge_active(self):
        g = JunctionGraph()
        g.visit((0, 0), (0.0, 90.0, 180.0), arrived_via_bearing=0.0)
        g.start_edge((0, 0), from_exit_idx=0)
        g.record_position((30.0, 30.0))
        self.assertEqual(len(g.trajectory), 1)
        self.assertEqual(len(g.active_edge.waypoints), 1)


class MarkExitFailedTests(unittest.TestCase):
    """Stuck-in-junction rescue: when a committed exit turns out to be a
    dead-end the graph didn't know about, marking it failed excludes it
    from the next visit's unexplored list.
    """

    def test_marked_exit_is_excluded_from_next_visit(self):
        g = JunctionGraph()
        # 4-way junction; arrived from south (180°).
        exits = (0.0, 90.0, 180.0, 270.0)   # N, E, S(entrance), W
        d1 = g.visit((0, 0), exits, arrived_via_bearing=0.0)
        first_pick = d1.exit_bearing
        self.assertEqual(d1.action, TremauxAction.TAKE_EXIT)

        # Bot goes out, discovers first_pick is a dead-end.  Mark failed
        # while still keyed on the same junction cell (i.e. bot never
        # actually left the cell — stuck-in-junction pathology).
        self.assertTrue(g.mark_exit_failed((0, 0), first_pick))

        # Re-visit — should NOT pick first_pick again.
        d2 = g.visit((0, 0), exits, arrived_via_bearing=0.0)
        self.assertEqual(d2.action, TremauxAction.TAKE_EXIT)
        self.assertNotEqual(d2.exit_bearing, first_pick,
                            "marked-failed exit was picked again")

    def test_mark_unknown_cell_is_noop(self):
        g = JunctionGraph()
        # No cell recorded; mark should silently return False.
        self.assertFalse(g.mark_exit_failed((99, 99), 0.0))

    def test_mark_all_exits_forces_backtrack(self):
        g = JunctionGraph()
        exits = (0.0, 90.0, 180.0)          # N, E, S(entrance)
        g.visit((0, 0), exits, arrived_via_bearing=0.0)
        g.mark_exit_failed((0, 0), 0.0)     # kill N
        g.mark_exit_failed((0, 0), 90.0)    # kill E
        # Now only entrance (180°) is unexplored — but entrance is
        # already in explored on first visit, so all are explored.
        # Next visit → BACKTRACK.
        d = g.visit((0, 0), exits, arrived_via_bearing=0.0)
        self.assertEqual(d.action, TremauxAction.BACKTRACK)


if __name__ == "__main__":
    unittest.main()
