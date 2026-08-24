"""Tests for the company-growth strategy + ducat guardrail (#30)."""
import unittest

from brain.growth_strategy import (
    Activity, GrowthDecision, guardrail_ok, choose_growth_activity,
)


def _acts():
    return [
        Activity("barter", est_xp=100, est_ducat_delta=+3_000_000),
        Activity("explore", est_xp=250, est_ducat_delta=-500_000),
        Activity("combat", est_xp=400, est_ducat_delta=-2_000_000),
    ]


class GrowthStrategyTests(unittest.TestCase):
    def test_maximizes_xp_above_floor(self):
        # Plenty of ducats → pick the highest-XP activity (combat).
        d = choose_growth_activity(_acts(), ducats=50_000_000, ducat_floor=1_000_000)
        self.assertEqual(d.activity.name, "combat")

    def test_excludes_activity_that_breaks_floor(self):
        # ducats 2.5M, floor 1M: combat (−2M) would drop to 0.5M < floor → excluded;
        # explore (−0.5M → 2.0M) is the best XP that stays safe.
        d = choose_growth_activity(_acts(), ducats=2_500_000, ducat_floor=1_000_000)
        self.assertEqual(d.activity.name, "explore")

    def test_recovers_when_below_floor(self):
        # Below floor → ignore XP, pick most ducat-positive (barter).
        d = choose_growth_activity(_acts(), ducats=500_000, ducat_floor=1_000_000)
        self.assertEqual(d.activity.name, "barter")
        self.assertIn("recover", d.reason)

    def test_recovers_when_nothing_stays_safe(self):
        acts = [Activity("explore", 250, -500_000), Activity("combat", 400, -2_000_000)]
        # ducats exactly at floor; any spend breaks it → recover with the least-negative.
        d = choose_growth_activity(acts, ducats=1_000_000, ducat_floor=1_000_000)
        self.assertEqual(d.activity.name, "explore")   # -500k is the most ducat-positive

    def test_guardrail_ok(self):
        self.assertTrue(guardrail_ok(Activity("x", 0, -1_000_000), 5_000_000, 1_000_000))
        self.assertFalse(guardrail_ok(Activity("x", 0, -1_000_000), 1_500_000, 1_000_000))

    def test_empty_activities(self):
        self.assertIsNone(choose_growth_activity([], 5_000_000, 1_000_000))

    def test_barter_positive_delta_always_safe(self):
        # A cash-positive activity is always guardrail-safe.
        d = choose_growth_activity([Activity("barter", 100, +3_000_000)],
                                   ducats=1_000_000, ducat_floor=1_000_000)
        self.assertEqual(d.activity.name, "barter")


if __name__ == "__main__":
    unittest.main()
