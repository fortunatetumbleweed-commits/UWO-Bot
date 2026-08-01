"""Tests for the coverage tracker.

Phase 3a — the STUCK predicate is net displacement over time
window, not "no new cells in N ticks."  Tests cover both axes:

- Cell tracking (drives MAKING_PROGRESS + frontier identification).
- Net-displacement STUCK detection (the canonical Nav2 pattern).
"""
import unittest

from brain.goals.coverage_tracker import (
    CoverageTracker,
    Verdict,
)


# ── Cell tracking + MAKING_PROGRESS ─────────────────────────────────────────

class CellGriddingTests(unittest.TestCase):
    def test_consecutive_positions_same_cell(self):
        t = CoverageTracker(cell_size_deg=0.1)
        # Same 0.1° × 0.1° cell.
        v1 = t.record(0, 30.10, 30.10)
        v2 = t.record(1, 30.15, 30.15)
        self.assertEqual(v1, Verdict.MAKING_PROGRESS)    # new cell
        self.assertEqual(v2, Verdict.KEEP_FOLLOWING)     # same cell
        self.assertEqual(t.visited_count, 1)

    def test_adjacent_cell_recognized_as_new(self):
        t = CoverageTracker(cell_size_deg=0.1)
        v1 = t.record(0, 30.05, 30.05)
        v2 = t.record(1, 30.15, 30.05)    # one cell north
        self.assertEqual(v1, Verdict.MAKING_PROGRESS)
        self.assertEqual(v2, Verdict.MAKING_PROGRESS)
        self.assertEqual(t.visited_count, 2)

    def test_negative_lat_lon_cells(self):
        t = CoverageTracker(cell_size_deg=0.1)
        v1 = t.record(0, -30.05, -30.05)
        v2 = t.record(1, -30.05, -30.05)
        self.assertEqual(v1, Verdict.MAKING_PROGRESS)
        self.assertEqual(v2, Verdict.KEEP_FOLLOWING)


# ── Net-displacement STUCK ──────────────────────────────────────────────────

class StuckDetectionTests(unittest.TestCase):
    def test_partial_window_defers_stuck(self):
        """Until we have a full window of samples we can't say
        anything about displacement; STUCK never fires."""
        t = CoverageTracker(stuck_window_ticks=10, min_displacement_deg=0.3)
        # 5 ticks all at the same position — should be KEEP_FOLLOWING,
        # not STUCK, because the window isn't full.
        t.record(0, 30.0, 30.0)
        for tick in range(1, 6):
            v = t.record(tick, 30.0, 30.0)
            self.assertEqual(v, Verdict.KEEP_FOLLOWING)

    def test_stuck_fires_after_full_window_low_displacement(self):
        t = CoverageTracker(stuck_window_ticks=10, min_displacement_deg=0.3)
        # 11 ticks all at the same position.  At tick 10 the window
        # is full and net displacement is 0.
        t.record(0, 30.0, 30.0)
        for tick in range(1, 10):
            t.record(tick, 30.0, 30.0)
        v = t.record(10, 30.0, 30.0)
        self.assertEqual(v, Verdict.STUCK)

    def test_in_transit_not_stuck_even_in_old_cell(self):
        """Backtracking through a known region en route to new ground:
        bot is in old cells, but it's net-displaced from where it was
        a window ago, so STUCK does NOT fire."""
        t = CoverageTracker(
            cell_size_deg=0.1, stuck_window_ticks=10, min_displacement_deg=0.3
        )
        # Establish a corridor of cells going east, then back west,
        # then onward south.  After 11 ticks the window's oldest
        # sample is east of the current position by more than the
        # threshold.
        positions = [
            (30.0, 30.0),  # t=0
            (30.0, 30.5),  # t=1 — far east, NEW
            (30.0, 30.4),
            (30.0, 30.3),
            (30.0, 30.2),
            (30.0, 30.1),
            (30.0, 30.0),  # t=6 — back at the start cell (revisit)
            (29.9, 30.0),  # t=7 — south, NEW
            (29.8, 30.0),
            (29.7, 30.0),
            (29.6, 30.0),  # t=10 — fully south
        ]
        for i, (lat, lon) in enumerate(positions):
            v = t.record(i, lat, lon)
        # Window now covers t=0..t=10.  Oldest sample is (30.0, 30.0).
        # Current sample is (29.6, 30.0).  Net displacement is 0.4°
        # (> 0.3° threshold) — NOT stuck.
        self.assertNotEqual(v, Verdict.STUCK)

    def test_return_to_window_start_fires_stuck(self):
        """If the bot returns to exactly where it was a window ago,
        net displacement is zero and STUCK should fire — even if
        the bot traveled far in between.  Net displacement is the
        correct signal: the bot has made no progress in the time."""
        t = CoverageTracker(
            cell_size_deg=0.1, stuck_window_ticks=10, min_displacement_deg=0.3
        )
        t.record(0, 30.0, 30.0)
        # March north into new cells.
        for i in range(1, 11):
            t.record(i, 30.0 + i * 0.1, 30.0)
        # Now revisit the origin cell.  Net displacement from t=1's
        # sample is tiny (~0.1°), which is < threshold.
        v = t.record(11, 30.0, 30.0)
        self.assertEqual(v, Verdict.STUCK)

    def test_legitimate_exploration_not_stuck(self):
        """Bot enters a new cell every few ticks but covers ground;
        coverage tracker should never fire STUCK."""
        t = CoverageTracker(
            cell_size_deg=0.1, stuck_window_ticks=30, min_displacement_deg=0.3
        )
        # Move south by 0.05° per tick — slow but covering ground.
        for i in range(50):
            v = t.record(i, 30.0 - i * 0.05, 30.0)
            self.assertNotEqual(v, Verdict.STUCK)

    def test_oscillation_in_known_region_fires_stuck(self):
        """Bot bouncing between two adjacent cells with no net
        progress over the window — STUCK fires."""
        t = CoverageTracker(
            cell_size_deg=0.1, stuck_window_ticks=10, min_displacement_deg=0.3
        )
        # Establish both cells.
        t.record(0, 30.05, 30.05)
        t.record(1, 30.05, 30.15)
        # Bounce.  We should NOT see STUCK while window is partial.
        verdicts = []
        for tick in range(2, 20):
            lon = 30.05 if tick % 2 == 0 else 30.15
            verdicts.append(t.record(tick, 30.05, lon))
        # After the window fills, STUCK should appear.
        self.assertIn(Verdict.STUCK, verdicts)

    def test_window_slides_correctly(self):
        """When fresh samples enter the window, old samples are
        dropped — verifying that an earlier high-displacement event
        doesn't permanently hide STUCK later."""
        t = CoverageTracker(
            cell_size_deg=0.1, stuck_window_ticks=10, min_displacement_deg=0.3
        )
        # Big motion at the start.
        for i in range(11):
            t.record(i, 30.0 + i * 0.1, 30.0)
        # Now stop moving — but stay in an old cell.
        t.record(11, 30.0 + 10 * 0.1, 30.0)
        # Sample tick 12-21: same position, an old cell.  The window
        # initially still contains some of the prior motion, so the
        # earliest stuck verdict may be a few ticks later.
        results = []
        for tick in range(12, 30):
            results.append(t.record(tick, 30.0 + 10 * 0.1, 30.0))
        # At some point STUCK fires after the window slides past the
        # earlier motion samples.
        self.assertIn(Verdict.STUCK, results)


# ── Frontier identification (unchanged from Phase 2) ────────────────────────

class FrontierIdentificationTests(unittest.TestCase):
    def test_no_frontiers_with_empty_visited(self):
        t = CoverageTracker()
        self.assertEqual(t.frontiers(), set())
        self.assertEqual(t.frontier_count, 0)

    def test_single_cell_has_four_frontier_neighbours(self):
        t = CoverageTracker(cell_size_deg=0.1)
        t.record(0, 30.05, 30.05)
        self.assertEqual(len(t.frontiers()), 4)

    def test_two_adjacent_cells_share_a_neighbour_set(self):
        t = CoverageTracker(cell_size_deg=0.1)
        t.record(0, 30.05, 30.05)
        t.record(1, 30.15, 30.05)
        self.assertEqual(len(t.frontiers()), 6)


# ── Cell center helper ──────────────────────────────────────────────────────

class CellCenterTests(unittest.TestCase):
    def test_cell_center_returns_midpoint(self):
        t = CoverageTracker(cell_size_deg=0.1)
        t.record(0, 30.05, 30.05)
        cell = next(iter(t.visited))
        center = t.cell_center(cell)
        self.assertAlmostEqual(center[0], 30.05, places=6)
        self.assertAlmostEqual(center[1], 30.05, places=6)


if __name__ == "__main__":
    unittest.main()
