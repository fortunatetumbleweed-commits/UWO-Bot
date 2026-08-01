"""Tests for the frontier picker (Yamauchi backend)."""
import unittest

from brain.goals.coverage_tracker import CoverageTracker
from brain.goals.frontier_picker import YamauchiPicker


def _approx(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) < tol


class EmptyAndDegenerateTests(unittest.TestCase):
    def test_no_visited_cells_returns_none(self):
        coverage = CoverageTracker(cell_size_deg=0.1)
        picker = YamauchiPicker()
        result = picker.pick(
            current_pos=(30.0, 30.0), coverage=coverage,
        )
        self.assertIsNone(result)


class PureNearestTests(unittest.TestCase):
    """β = 0 → pick the frontier closest to current position."""

    def test_picks_closest_neighbour_to_current(self):
        coverage = CoverageTracker(cell_size_deg=0.1)
        # Visit a single cell at (30.05, 30.05).
        coverage.record(0, 30.05, 30.05)
        # Frontier candidates: (30.15, 30.05), (29.95, 30.05),
        #                      (30.05, 30.15), (30.05, 29.95).
        # Position bot just north of the visited cell.
        picker = YamauchiPicker(alpha=1.0, beta=0.0)
        target = picker.pick(
            current_pos=(30.20, 30.05), coverage=coverage,
        )
        # Closest frontier is (30.15, 30.05).
        self.assertIsNotNone(target)
        self.assertTrue(_approx(target[0], 30.15, tol=1e-6))
        self.assertTrue(_approx(target[1], 30.05, tol=1e-6))


class GoalBiasedTests(unittest.TestCase):
    """β > 0 → prefer frontiers closer to the endpoint."""

    def test_goal_bias_overrides_nearest(self):
        coverage = CoverageTracker(cell_size_deg=0.1)
        coverage.record(0, 30.05, 30.05)
        # Current pos equidistant from all four frontiers.
        # Without goal bias, ties would resolve by frontier-cell order.
        # With endpoint to the SOUTH, the south frontier (29.95, 30.05)
        # should win.
        picker = YamauchiPicker(alpha=1.0, beta=10.0)
        target = picker.pick(
            current_pos=(30.05, 30.05), coverage=coverage,
            endpoint=(0.0, 30.05),    # far south
        )
        # South-frontier center: (29.95, 30.05).
        self.assertIsNotNone(target)
        self.assertTrue(_approx(target[0], 29.95, tol=1e-6))
        self.assertTrue(_approx(target[1], 30.05, tol=1e-6))

    def test_strong_goal_bias_can_override_nearby(self):
        """A frontier farther from current but much closer to
        endpoint wins under strong goal bias."""
        coverage = CoverageTracker(cell_size_deg=0.1)
        # Long horizontal corridor of visited cells.
        for lat in [30.05, 30.05, 30.05, 30.05]:
            for lon_off in range(0, 4):
                coverage.record(lon_off, lat, 30.05 + lon_off * 0.1)
        # Frontiers at both ends of the corridor + north/south of
        # each visited cell.  Endpoint far south.
        picker = YamauchiPicker(alpha=1.0, beta=100.0)
        target = picker.pick(
            current_pos=(30.05, 30.05), coverage=coverage,
            endpoint=(0.0, 30.05),    # far south, aligned with start
        )
        # The frontier directly south of (30.05, 30.05): center
        # (29.95, 30.05).  All other frontiers are farther from
        # endpoint (eastern corridor frontiers are northward or
        # to the east where it's even farther south distance).
        self.assertIsNotNone(target)
        self.assertTrue(_approx(target[0], 29.95, tol=1e-6))


class YBranchScenarioTests(unittest.TestCase):
    """Smoke-test of the Y-tip scenario that motivated the picker:
    a column of visited cells (the wrong branch) and the bot stuck
    at the bottom of the column; a frontier exists to the side that
    would lead to the right branch."""

    def test_picks_side_frontier_when_wrong_branch_explored(self):
        coverage = CoverageTracker(cell_size_deg=0.1)
        # Wrong branch: 4 cells stacked south.
        for i, lat in enumerate([30.05, 29.95, 29.85, 29.75]):
            coverage.record(i, lat, 30.05)
        # Bot stuck at the bottom of the column.
        picker = YamauchiPicker(alpha=1.0, beta=1.0)
        target = picker.pick(
            current_pos=(29.75, 30.05), coverage=coverage,
            endpoint=(20.0, 30.20),    # far south-east
        )
        # Many frontier candidates.  We don't pin the exact pick
        # here — just confirm it's an unvisited cell adjacent to
        # the visited column.  In practice the algorithm should
        # pick something east-of-current (toward endpoint).
        self.assertIsNotNone(target)
        # Frontier must be 4-neighbour of one of the visited cells
        # AND not in the visited set itself.
        # We'll verify it's outside the visited set by checking that
        # its cell index isn't in coverage.visited.
        cell = (int(target[0] / coverage.cell_size_deg),
                int(target[1] / coverage.cell_size_deg))
        # Picker returns a center, so the integer floor may differ
        # by 1 due to floating-point — accept either.  The simpler
        # check: target's lat/lon don't fall in the visited cells.
        for lat_v, lon_v in [(30.05, 30.05), (29.95, 30.05),
                              (29.85, 30.05), (29.75, 30.05)]:
            self.assertFalse(
                abs(target[0] - lat_v) < 1e-9 and
                abs(target[1] - lon_v) < 1e-9,
                f"frontier {target} should not be a visited cell"
            )


class DeterminismTests(unittest.TestCase):
    def test_repeated_picks_with_same_state_are_identical(self):
        coverage = CoverageTracker(cell_size_deg=0.1)
        coverage.record(0, 30.05, 30.05)
        picker = YamauchiPicker()
        r1 = picker.pick(current_pos=(30.05, 30.05), coverage=coverage)
        r2 = picker.pick(current_pos=(30.05, 30.05), coverage=coverage)
        self.assertEqual(r1, r2)


class TremauxNoJunctionTests(unittest.TestCase):
    """TremauxPicker returns None when no junction is detected.

    The earlier design fell back to YamauchiPicker; that turned out
    to harm bend traversal (voyage explore_port_20260604_141023).
    Non-junction STUCK is a perception/decision instability that the
    destination-generator self-corrects when perception stabilises;
    the picker should sit out.
    """

    def test_no_nav_returns_none(self):
        from brain.goals.frontier_picker import TremauxPicker
        coverage = CoverageTracker(cell_size_deg=0.1)
        coverage.record(0, 30.05, 30.05)
        picker = TremauxPicker()
        target = picker.pick(
            current_pos=(30.05, 30.05), coverage=coverage,
            endpoint=(0.0, 30.05), nav=None,
        )
        self.assertIsNone(target)

    def test_nav_without_junction_returns_none(self):
        """Straight channel (2 arcs) — detector returns None;
        picker should return None too (no override)."""
        from dataclasses import dataclass
        from brain.goals.frontier_picker import TremauxPicker

        @dataclass
        class StubSector:
            land_fraction: float
            is_observed:   bool = True
        @dataclass
        class StubNav:
            sectors:          tuple
            ship_heading_deg: float

        # Straight-channel pattern (2 arcs only — not a junction).
        land_pattern = "WLLLLLLWWWLLLLLW"
        sectors = tuple(
            StubSector(land_fraction=(0.1 if ch == "W" else 0.9))
            for ch in land_pattern
        )
        nav = StubNav(sectors=sectors, ship_heading_deg=0.0)
        coverage = CoverageTracker(cell_size_deg=0.1)
        coverage.record(0, 30.0, 30.0)
        picker = TremauxPicker()
        target = picker.pick(
            current_pos=(30.0, 30.0), coverage=coverage,
            endpoint=None, nav=nav,
        )
        self.assertIsNone(target)


class TremauxJunctionTests(unittest.TestCase):
    """When perception confirms a junction, Trémaux is invoked and
    its target point reflects an unexplored exit."""

    def test_junction_perception_triggers_tremaux(self):
        import math as _m
        from brain.goals.frontier_picker import TremauxPicker
        from brain.goals.junction_detector import JunctionDetector

        # Build synthetic nav with 16 sectors matching the Y-junction
        # pattern used in junction_detector tests.
        from dataclasses import dataclass
        @dataclass
        class StubSector:
            land_fraction: float
            is_observed:   bool = True
        @dataclass
        class StubNav:
            sectors:           tuple
            ship_heading_deg:  float

        land_pattern = "LWWWLLLWWWLLLWWW"   # 3-arc Y junction
        sectors = tuple(
            StubSector(land_fraction=(0.1 if ch == "W" else 0.9))
            for ch in land_pattern
        )
        nav = StubNav(sectors=sectors, ship_heading_deg=0.0)
        coverage = CoverageTracker(cell_size_deg=0.1)
        coverage.record(0, 30.0, 30.0)

        picker = TremauxPicker()
        target = picker.pick(
            current_pos=(30.0, 30.0), coverage=coverage,
            endpoint=None, nav=nav,
        )
        # Target must exist and be a (lat, lon) tuple.
        self.assertIsNotNone(target)
        self.assertEqual(len(target), 2)
        # The picker should have recorded the junction in its graph.
        self.assertEqual(len(picker.graph.junctions), 1)


class TremauxStateMachineTests(unittest.TestCase):
    """End-to-end mode dispatch in TremauxPicker."""

    def _make_nav(self, land_pattern: str, heading_deg: float = 0.0):
        from dataclasses import dataclass
        @dataclass
        class StubSector:
            land_fraction: float
            is_observed:   bool = True
        @dataclass
        class StubNav:
            sectors:          tuple
            ship_heading_deg: float
        sectors = tuple(
            StubSector(land_fraction=(0.1 if ch == "W" else 0.9))
            for ch in land_pattern
        )
        return StubNav(sectors=sectors, ship_heading_deg=heading_deg)

    def test_channel_records_position(self):
        from brain.goals.frontier_picker import TremauxPicker
        picker = TremauxPicker()
        coverage = CoverageTracker(cell_size_deg=0.1)
        coverage.record(0, 30.0, 30.0)
        # First visit a junction to start recording an edge.
        picker.pick(
            current_pos=(30.0, 30.0), coverage=coverage,
            nav=self._make_nav("LWWWLLLWWWLLLWWW"),    # 3-arc Y
        )
        # Then a channel — should record position and return None.
        target = picker.pick(
            current_pos=(30.05, 30.05), coverage=coverage,
            nav=self._make_nav("WLLLLLLWWWLLLLLW"),    # 2-arc straight
        )
        self.assertIsNone(target)
        # The active edge should have at least one waypoint recorded.
        self.assertIsNotNone(picker.graph.active_edge)
        self.assertGreater(len(picker.graph.active_edge.waypoints), 0)

    def test_dead_end_starts_backtrack(self):
        from brain.goals.frontier_picker import TremauxPicker
        picker = TremauxPicker()
        coverage = CoverageTracker(cell_size_deg=0.1)
        # Junction → channel → dead-end.
        picker.pick(
            current_pos=(30.0, 30.0), coverage=coverage,
            nav=self._make_nav("LWWWLLLWWWLLLWWW"),
        )
        picker.pick(
            current_pos=(30.05, 30.05), coverage=coverage,
            nav=self._make_nav("WLLLLLLWWWLLLLLW"),
        )
        picker.pick(
            current_pos=(30.10, 30.10), coverage=coverage,
            nav=self._make_nav("WLLLLLLWWWLLLLLW"),
        )
        # Dead-end signal (1 arc).
        target = picker.pick(
            current_pos=(30.15, 30.15), coverage=coverage,
            nav=self._make_nav("LLLLLLLWWWLLLLLL"),
        )
        # Should now be in backtracking mode and yield a waypoint.
        self.assertIsNotNone(target)
        self.assertIsNotNone(picker._backtracker)

    def test_pre_topological_returns_none(self):
        from brain.goals.frontier_picker import TremauxPicker
        from brain.goals.junction_graph import TopologyState
        picker = TremauxPicker()
        coverage = CoverageTracker(cell_size_deg=0.1)
        coverage.record(0, 30.0, 30.0)
        # Open water — channel, no junction yet seen.
        target = picker.pick(
            current_pos=(30.0, 30.0), coverage=coverage,
            nav=self._make_nav("WLLLLLLWWWLLLLLW"),
        )
        self.assertIsNone(target)
        self.assertEqual(
            picker.graph.topology_state, TopologyState.PRE_TOPOLOGICAL
        )


class TremauxReversalTests(unittest.TestCase):
    """Reversal detection — Dudek 1991 directional Trémaux marks
    generalised to the raw trajectory (works in PRE_TOPOLOGICAL state).
    When the bot reverses direction by >90°, the picker fires a
    BacktrackPlanner over the recent trajectory."""

    def _make_nav(self, land_pattern="WLLLLLLWWWLLLLLW", heading=0.0):
        from dataclasses import dataclass
        @dataclass
        class StubSector:
            land_fraction: float
            is_observed:   bool = True
        @dataclass
        class StubNav:
            sectors:          tuple
            ship_heading_deg: float
        sectors = tuple(
            StubSector(land_fraction=(0.1 if ch == "W" else 0.9))
            for ch in land_pattern
        )
        return StubNav(sectors=sectors, ship_heading_deg=heading)

    def test_reversal_fires_backtracker_in_pre_topological(self):
        """The very situation the trace failure exhibited: hugging
        shore (PRE_TOPOLOGICAL state, no junction encountered), bounce
        flips heading, bot starts walking back over its own trajectory.
        Reversal check fires and starts a BacktrackPlanner."""
        from brain.goals.frontier_picker import TremauxPicker
        picker = TremauxPicker()
        coverage = CoverageTracker(cell_size_deg=0.1)
        # Walk east for 20 ticks.
        for i in range(20):
            picker.pick(
                current_pos=(0.0, i * 0.1), coverage=coverage,
                nav=self._make_nav(),
            )
        # Then reverse — west for 10 ticks.
        target = None
        for i in range(1, 11):
            target = picker.pick(
                current_pos=(0.0, 1.9 - i * 0.1), coverage=coverage,
                nav=self._make_nav(),
            )
        # Backtracker should have been armed by the reversal check;
        # picker returns a target rather than None.
        self.assertIsNotNone(picker._backtracker)
        self.assertEqual(picker.last_mode, "backtracking")
        self.assertIsNotNone(target)

    def test_no_reversal_on_straight_trajectory(self):
        from brain.goals.frontier_picker import TremauxPicker
        picker = TremauxPicker()
        coverage = CoverageTracker(cell_size_deg=0.1)
        target = None
        for i in range(30):
            target = picker.pick(
                current_pos=(0.0, i * 0.1), coverage=coverage,
                nav=self._make_nav(),
            )
        self.assertIsNone(picker._backtracker)
        self.assertIsNone(target)


if __name__ == "__main__":
    unittest.main()
