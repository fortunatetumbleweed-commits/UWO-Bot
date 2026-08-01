"""
Tests for the Plan demotion-policy fix (Fix C, 2026-05-04 livefix).

Old rule: demote on `failure_count >= THRESHOLD AND success_count == 0`.
That punished the whole plan for transient tail-step failures even when
the plan's transaction step had succeeded — so a single post-recruit
miss disabled the inn_recruit_route plan permanently.

New rule (in addition to the old preconditions):
  - When a failed_step_id is supplied, require the SAME step to have
    failed FAILURE_DEMOTION_THRESHOLD times in a row.  Different-step
    misses don't accumulate toward demotion.
  - A plan whose transaction step (kind=commit_via_positive_taps) has
    succeeded at least once is exempt.  Capability-demonstrated.
  - record_step_success resets the consecutive-failure tracker.
"""

import unittest

from brain.plan import (
    Plan, PlanStep, FAILURE_DEMOTION_THRESHOLD,
    PROVENANCE_HAND_AUTHORED,
)


CONFIDENCE_UNVERIFIED = "unverified"
CONFIDENCE_LOW = "low"


def _make_plan(*, with_commit_step: bool = True) -> Plan:
    """Construct a minimal inn-recruit-style plan for tests."""
    steps = [
        PlanStep(step_id="step_1_exit", action={"kind": "exit_to_port_overworld"}),
        PlanStep(step_id="step_2_navigate", action={"kind": "navigate_to", "target": "inn"}),
        PlanStep(step_id="step_3_open_recruit",
                 action={"kind": "tap_primary_action", "building": "inn",
                         "sub_menu": "recruit_crew"}),
    ]
    if with_commit_step:
        steps.append(PlanStep(
            step_id="step_3b_commit",
            action={"kind": "commit_via_positive_taps", "max_taps": 6},
        ))
    steps.append(PlanStep(step_id="step_4_exit_after",
                          action={"kind": "exit_to_port_overworld"}))
    return Plan(
        plan_id="inn_recruit_test",
        goal_id="has_enough_crew",
        steps=steps,
        provenance=PROVENANCE_HAND_AUTHORED,
        confidence=CONFIDENCE_UNVERIFIED,
    )


class DemotionPolicyTests(unittest.TestCase):

    def test_three_failures_no_step_id_demotes_legacy_behaviour(self):
        """Legacy callers without failed_step_id still get the old rule."""
        plan = _make_plan(with_commit_step=False)
        plan.record_failure(); plan.record_failure(); plan.record_failure()
        self.assertEqual(plan.confidence, CONFIDENCE_LOW)

    def test_three_failures_on_different_steps_does_not_demote(self):
        """Fix C: different-step misses don't accumulate."""
        plan = _make_plan(with_commit_step=False)
        plan.record_failure(failed_step_id="step_2_navigate")
        plan.record_failure(failed_step_id="step_3_open_recruit")
        plan.record_failure(failed_step_id="step_4_exit_after")
        self.assertEqual(
            plan.confidence, CONFIDENCE_UNVERIFIED,
            "different failed steps shouldn't hit the same-step streak",
        )

    def test_three_failures_on_same_step_demotes(self):
        """Fix C: same-step streak does demote (legacy behaviour preserved
        for the genuinely-broken-step case)."""
        plan = _make_plan(with_commit_step=False)
        plan.record_failure(failed_step_id="step_4_exit_after")
        plan.record_failure(failed_step_id="step_4_exit_after")
        plan.record_failure(failed_step_id="step_4_exit_after")
        self.assertEqual(plan.confidence, CONFIDENCE_LOW)

    def test_successful_commit_step_exempts_plan_from_demotion(self):
        """Fix C: a plan whose transaction step has worked is exempt."""
        plan = _make_plan(with_commit_step=True)
        # Mark the commit step as having succeeded once
        commit = next(s for s in plan.steps if s.step_id == "step_3b_commit")
        commit.success_count = 1
        # Now try to demote with a same-step streak
        plan.record_failure(failed_step_id="step_4_exit_after")
        plan.record_failure(failed_step_id="step_4_exit_after")
        plan.record_failure(failed_step_id="step_4_exit_after")
        self.assertEqual(
            plan.confidence, CONFIDENCE_UNVERIFIED,
            "transaction-demonstrated plan should not be demoted",
        )

    def test_record_step_success_resets_consecutive_streak(self):
        """A successful intervening step resets the same-step streak."""
        plan = _make_plan(with_commit_step=False)
        plan.record_failure(failed_step_id="step_2_navigate")
        plan.record_failure(failed_step_id="step_2_navigate")
        plan.record_step_success("step_3_open_recruit")
        plan.record_failure(failed_step_id="step_2_navigate")
        # Streak was: 2 -> reset to 0 -> 1.  Below threshold.
        self.assertEqual(plan.confidence, CONFIDENCE_UNVERIFIED)

    def test_streak_resets_when_a_different_step_fails(self):
        plan = _make_plan(with_commit_step=False)
        plan.record_failure(failed_step_id="step_2_navigate")
        plan.record_failure(failed_step_id="step_2_navigate")
        plan.record_failure(failed_step_id="step_4_exit_after")  # resets streak
        plan.record_failure(failed_step_id="step_2_navigate")    # new streak: 1
        self.assertEqual(plan.confidence, CONFIDENCE_UNVERIFIED)

    def test_plan_with_any_success_is_exempt(self):
        """Pre-existing rule: success_count > 0 → no demotion."""
        plan = _make_plan(with_commit_step=False)
        plan.record_success(duration_secs=1.0)
        plan.record_failure(failed_step_id="step_2_navigate")
        plan.record_failure(failed_step_id="step_2_navigate")
        plan.record_failure(failed_step_id="step_2_navigate")
        self.assertNotEqual(plan.confidence, CONFIDENCE_LOW)


class SkipExemptionAtLookupTests(unittest.TestCase):
    """Fix C lookup-time extension (2026-05-12 livefix).

    A plan demoted on disk (confidence='low', failure_count >= threshold)
    can be rehabilitated automatically when its transaction step has
    ever succeeded.  Without this, PlanRuntime.lookup() returns "no
    viable plan" even though the plan is structurally fine — what we
    saw in the live log when inn_recruit_route was stranded.
    """

    def test_demoted_plan_with_transaction_success_is_not_skipped(self):
        """The live inn_recruit_route case: confidence='low',
        failure_count=4, but step_3b_commit has 3 successes.  Fix C
        lookup-time exemption means should_skip() returns False."""
        plan = _make_plan(with_commit_step=True)
        commit = next(s for s in plan.steps if s.step_id == "step_3b_commit")
        commit.success_count = 3
        # Manually put the plan into the "previously demoted" state
        plan.confidence    = CONFIDENCE_LOW
        plan.failure_count = FAILURE_DEMOTION_THRESHOLD + 1
        self.assertFalse(plan.should_skip(),
                         "demoted plan with successful transaction step must NOT be skipped")

    def test_demoted_plan_without_transaction_success_is_still_skipped(self):
        """Genuinely broken plans (no transaction step has ever
        succeeded) keep being skipped — the exemption is about
        capability, not unconditional rehabilitation."""
        plan = _make_plan(with_commit_step=True)
        # commit step has never succeeded
        plan.confidence    = CONFIDENCE_LOW
        plan.failure_count = FAILURE_DEMOTION_THRESHOLD + 1
        self.assertTrue(plan.should_skip())

    def test_demoted_plan_without_commit_step_is_still_skipped(self):
        """Plans with no transaction-class step at all stay skipped
        when demoted."""
        plan = _make_plan(with_commit_step=False)
        plan.confidence    = CONFIDENCE_LOW
        plan.failure_count = FAILURE_DEMOTION_THRESHOLD + 1
        self.assertTrue(plan.should_skip())

    def test_high_confidence_plan_never_skipped(self):
        """Unrelated to Fix C: anything not in CONFIDENCE_LOW is
        always usable."""
        plan = _make_plan(with_commit_step=True)
        plan.confidence = "high"
        plan.failure_count = 100
        self.assertFalse(plan.should_skip())


if __name__ == "__main__":
    unittest.main()
