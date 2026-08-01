"""Tests for the M-line progress monitor.

The monitor is a pure dataclass with one method.  Tests cover:
  - First-tick (no prior sample) returns KEEP_FOLLOWING.
  - Same-side ticks return KEEP_FOLLOWING (between crossings).
  - Sign-flipping ticks return MAKING_PROGRESS when closer to endpoint,
    REGRESSING_AT_HIT otherwise.
  - `d_at_last_hit` updates only on progress crossings.
"""
import unittest

from brain.goals.m_line_monitor import (
    MLineMonitor,
    Verdict,
)


class FirstTickTests(unittest.TestCase):
    def test_first_tick_returns_keep_following(self):
        mon = MLineMonitor(start=(0.0, 0.0), endpoint=(10.0, 0.0))
        # No prior signed-perp sample; can't detect a crossing yet.
        self.assertEqual(
            mon.record(current=(1.0, 0.5)),
            Verdict.KEEP_FOLLOWING,
        )

    def test_d_at_last_hit_initialized_to_m_line_length(self):
        mon = MLineMonitor(start=(0.0, 0.0), endpoint=(3.0, 4.0))
        # Initial d_at_last_hit is the length of the M-line itself.
        self.assertAlmostEqual(mon.d_at_last_hit, 5.0)


class SameSideTests(unittest.TestCase):
    def test_consecutive_same_side_ticks_keep_following(self):
        mon = MLineMonitor(start=(0.0, 0.0), endpoint=(10.0, 0.0))
        mon.record(current=(1.0, 0.5))    # north of the M-line
        # Still north — no crossing.
        self.assertEqual(
            mon.record(current=(2.0, 0.7)),
            Verdict.KEEP_FOLLOWING,
        )
        self.assertEqual(
            mon.record(current=(3.0, 0.3)),
            Verdict.KEEP_FOLLOWING,
        )


class CrossingTests(unittest.TestCase):
    """M-line is the x-axis from (0,0) to (10,0).  Positive y is one
    side; negative y is the other."""

    def setUp(self):
        self.mon = MLineMonitor(start=(0.0, 0.0), endpoint=(10.0, 0.0))
        # Establish prior sign with a single sample above the line.
        self.mon.record(current=(1.0, 0.5))

    def test_crossing_closer_to_endpoint_is_progress(self):
        # Now cross to the south side, much closer to the endpoint
        # (10, 0) — d = ~3, vs initial d_at_last_hit = 10.
        verdict = self.mon.record(current=(7.0, -0.5))
        self.assertEqual(verdict, Verdict.MAKING_PROGRESS)
        # Reference updated to the new (smaller) distance.
        self.assertLess(self.mon.d_at_last_hit, 10.0)

    def test_crossing_farther_from_endpoint_is_regression(self):
        # Cross south but at a point FARTHER from endpoint than the
        # initial M-line length would allow.
        verdict = self.mon.record(current=(-2.0, -0.5))
        self.assertEqual(verdict, Verdict.REGRESSING_AT_HIT)

    def test_regression_does_not_update_reference(self):
        before = self.mon.d_at_last_hit
        self.mon.record(current=(-2.0, -0.5))    # regression crossing
        # The reference must stay frozen so the next crossing still
        # compares against the same hit point.
        self.assertEqual(self.mon.d_at_last_hit, before)

    def test_two_progress_crossings_tighten_the_reference(self):
        # First crossing: south at x=7, d ≈ 3.
        self.mon.record(current=(7.0, -0.5))
        ref_after_first = self.mon.d_at_last_hit
        # Second crossing: back north at x=9, d ≈ 1.
        verdict = self.mon.record(current=(9.0, 0.5))
        self.assertEqual(verdict, Verdict.MAKING_PROGRESS)
        self.assertLess(self.mon.d_at_last_hit, ref_after_first)

    def test_progress_then_regression(self):
        # Progress crossing to south.
        self.mon.record(current=(7.0, -0.5))
        d_after_progress = self.mon.d_at_last_hit
        # Cross back north but farther — regression.
        verdict = self.mon.record(current=(2.0, 0.5))
        self.assertEqual(verdict, Verdict.REGRESSING_AT_HIT)
        # Reference still the tighter value from the progress crossing.
        self.assertEqual(self.mon.d_at_last_hit, d_after_progress)


class DegenerateMLineTests(unittest.TestCase):
    def test_zero_length_m_line_does_not_crash(self):
        """When start == endpoint the M-line is degenerate.  The
        helper returns 0 for the signed-perp distance, so every
        tick reads as 'on the line' — but the monitor must not
        crash and the verdict must be sensible (KEEP_FOLLOWING is
        fine since no real progress concept applies)."""
        mon = MLineMonitor(start=(5.0, 5.0), endpoint=(5.0, 5.0))
        # First call returns KEEP_FOLLOWING (no prior).
        self.assertEqual(
            mon.record(current=(5.0, 5.0)),
            Verdict.KEEP_FOLLOWING,
        )
        # Second call — both prev and cur are 0; treated as same-side.
        self.assertEqual(
            mon.record(current=(5.0, 5.0)),
            Verdict.KEEP_FOLLOWING,
        )


if __name__ == "__main__":
    unittest.main()
