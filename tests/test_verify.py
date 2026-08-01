"""
Layer 2 tests — verification primitives (light_check, heavy_check,
update_cue_catalog).

See docs/planner_architecture.md and brain/verify.py.

Coverage:
  - light_check Tier 0: signature delta + expected_progress evaluation
    for each kind (state_change, detail_match, screen_change, free_text)
  - light_check Tier 1: Moondream injection when Tier 0 says NO_PROGRESS
  - heavy_check with stub Claude response → status + cue parsing
  - heavy_check with empty / non-JSON response → UNCERTAIN
  - update_cue_catalog increments stats, promotes confidence,
    appends new cues with proper provenance
"""

import json
import unittest
from dataclasses import dataclass
from typing import Optional

from brain.plan import (
    Cue, CueCatalog, CueProvenance, CueStats,
    Goal, ProgressExpectation,
)
from brain.verify import (
    PROGRESS, NO_PROGRESS, CLARIFY,
    GOAL_ACHIEVED, NOT_YET, UNCERTAIN,
    LightResult, HeavyResult, CueObservation,
    light_check, heavy_check, update_cue_catalog,
)


@dataclass
class FakePerceive:
    """Stand-in for brain.perceive.PerceiveResult used in tests."""
    state:     Optional[str] = None
    detail:    str = ""
    flow:      Optional[str] = None
    flow_step: Optional[str] = None


# ── light_check ──────────────────────────────────────────────────────────────


class LightCheckTier0Tests(unittest.TestCase):

    def test_state_change_kind_matches_when_state_equals_target(self):
        before = FakePerceive(state="building", detail="building: harbor")
        after  = FakePerceive(state="port_overworld", detail="London harbor entrance")
        result = light_check(before, after, ProgressExpectation(
            kind="state_change", expected_state="port_overworld",
        ))
        self.assertEqual(result.status, PROGRESS)
        self.assertTrue(result.matched_expected)
        self.assertEqual(result.tier, 0)

    def test_state_change_kind_progresses_even_when_state_doesnt_match(self):
        """Wrong-direction progress is still progress at Tier 0; matched_expected=False."""
        before = FakePerceive(state="building", detail="building: harbor")
        after  = FakePerceive(state="building", detail="building: recruit crew")
        result = light_check(before, after, ProgressExpectation(
            kind="state_change", expected_state="port_overworld",
        ))
        self.assertEqual(result.status, PROGRESS)
        self.assertFalse(result.matched_expected)

    def test_detail_match_kind_evaluates_substring(self):
        before = FakePerceive(state="building", detail="building: harbor")
        after  = FakePerceive(state="building", detail="building: inn — recruit crew sub-screen")
        result = light_check(before, after, ProgressExpectation(
            kind="detail_match", expected_detail_substring="recruit crew",
        ))
        self.assertEqual(result.status, PROGRESS)
        self.assertTrue(result.matched_expected)

    def test_screen_change_kind_signature_unchanged_returns_no_progress(self):
        same = FakePerceive(state="building", detail="building: harbor")
        result = light_check(same, same, ProgressExpectation(kind="screen_change"))
        self.assertEqual(result.status, NO_PROGRESS)
        self.assertFalse(result.matched_expected)

    def test_free_text_kind_with_signature_change_progresses(self):
        before = FakePerceive(state="building", detail="building: inn")
        after  = FakePerceive(state="building", detail="building: recruit crew sub-screen")
        result = light_check(before, after, ProgressExpectation(
            kind="free_text", hint="recruit crew sub-screen should appear",
        ))
        self.assertEqual(result.status, PROGRESS)


class LightCheckTier1Tests(unittest.TestCase):

    def test_moondream_says_change_overrides_no_progress(self):
        same = FakePerceive(state="building", detail="building: harbor")
        moondream_yes = lambda b, a: True
        result = light_check(same, same, ProgressExpectation(),
                             moondream_progress_check=moondream_yes)
        self.assertEqual(result.status, PROGRESS)
        self.assertEqual(result.tier, 1)

    def test_moondream_says_no_change_confirms_no_progress(self):
        same = FakePerceive(state="building", detail="building: harbor")
        moondream_no = lambda b, a: False
        result = light_check(same, same, ProgressExpectation(),
                             moondream_progress_check=moondream_no)
        self.assertEqual(result.status, NO_PROGRESS)
        self.assertEqual(result.tier, 1)

    def test_moondream_unsure_falls_back_to_tier0_no_progress(self):
        same = FakePerceive(state="building", detail="building: harbor")
        moondream_unsure = lambda b, a: None
        result = light_check(same, same, ProgressExpectation(),
                             moondream_progress_check=moondream_unsure)
        self.assertEqual(result.status, NO_PROGRESS)
        self.assertEqual(result.tier, 0)


# ── heavy_check ──────────────────────────────────────────────────────────────


class HeavyCheckTests(unittest.TestCase):

    def _goal(self):
        return Goal(
            goal_id="has_enough_crew",
            description="Recruitment committed; fleet has crew sufficient for departure",
            predicate_text="fleet crew count strictly increased AND no warning",
        )

    def _stub_claude(self, response_obj: dict):
        """Return a callable that pretends to be a Claude API call."""
        def call(frame, prompt):
            return json.dumps(response_obj)
        return call

    def test_yes_response_returns_goal_achieved(self):
        stub = self._stub_claude({
            "goal_achieved": "yes",
            "confidence": 0.95,
            "cues_observed": [
                {"cue": "fleet crew count increased", "present": True, "confirms_goal": True},
            ],
            "new_cues": ["Depart Now button is gold"],
            "evidence_summary": "crew count went from 843 to 953; warning is gone",
        })
        result = heavy_check(frame=None, goal=self._goal(),
                             catalog=None, claude_call=stub)
        self.assertEqual(result.status, GOAL_ACHIEVED)
        self.assertAlmostEqual(result.confidence, 0.95)
        self.assertEqual(len(result.cues_observed), 1)
        self.assertEqual(result.new_cues, ["Depart Now button is gold"])

    def test_no_response_returns_not_yet(self):
        stub = self._stub_claude({
            "goal_achieved": "no",
            "confidence": 0.9,
            "cues_observed": [
                {"cue": "fleet crew count increased", "present": False, "confirms_goal": True},
            ],
            "new_cues": [],
            "evidence_summary": "crew count is unchanged from before",
        })
        result = heavy_check(frame=None, goal=self._goal(),
                             catalog=None, claude_call=stub)
        self.assertEqual(result.status, NOT_YET)
        self.assertFalse(result.cues_observed[0].present)

    def test_uncertain_response(self):
        stub = self._stub_claude({
            "goal_achieved": "uncertain",
            "confidence": 0.4,
            "evidence_summary": "screen partially obscured",
        })
        result = heavy_check(frame=None, goal=self._goal(),
                             catalog=None, claude_call=stub)
        self.assertEqual(result.status, UNCERTAIN)

    def test_non_json_response_falls_to_uncertain(self):
        stub = lambda f, p: "I think the goal is achieved!"
        result = heavy_check(frame=None, goal=self._goal(),
                             catalog=None, claude_call=stub)
        self.assertEqual(result.status, UNCERTAIN)

    def test_no_response_falls_to_uncertain(self):
        stub = lambda f, p: None
        result = heavy_check(frame=None, goal=self._goal(),
                             catalog=None, claude_call=stub)
        self.assertEqual(result.status, UNCERTAIN)

    def test_code_fenced_json_is_parsed(self):
        stub = lambda f, p: '```json\n{"goal_achieved": "yes", "confidence": 1.0}\n```'
        result = heavy_check(frame=None, goal=self._goal(),
                             catalog=None, claude_call=stub)
        self.assertEqual(result.status, GOAL_ACHIEVED)


# ── update_cue_catalog ───────────────────────────────────────────────────────


class CueCatalogUpdateTests(unittest.TestCase):

    def test_existing_cue_observed_present_confirmed(self):
        catalog = CueCatalog(
            goal="g1",
            cues=[Cue(cue="cue A", confidence="unverified",
                       stats=CueStats(observed=0, confirmed=0))],
        )
        result = HeavyResult(
            status=GOAL_ACHIEVED,
            cues_observed=[CueObservation(cue="cue A", present=True, confirms=True)],
        )
        update_cue_catalog(catalog, result, context="test_run")
        self.assertEqual(catalog.cues[0].stats.observed, 1)
        self.assertEqual(catalog.cues[0].stats.confirmed, 1)
        self.assertEqual(catalog.cues[0].stats.refuted, 0)

    def test_cue_promotion_lifecycle(self):
        """confirmed >= 5 → medium; confirmed >= 15 with no refutes → high."""
        catalog = CueCatalog(goal="g1", cues=[
            Cue(cue="cue A", confidence="unverified", stats=CueStats()),
        ])
        # 5 confirmed → medium
        for _ in range(5):
            update_cue_catalog(catalog, HeavyResult(
                status=GOAL_ACHIEVED,
                cues_observed=[CueObservation(cue="cue A", present=True, confirms=True)],
            ))
        self.assertEqual(catalog.cues[0].confidence, "medium")
        # Add 10 more (15 total) → high
        for _ in range(10):
            update_cue_catalog(catalog, HeavyResult(
                status=GOAL_ACHIEVED,
                cues_observed=[CueObservation(cue="cue A", present=True, confirms=True)],
            ))
        self.assertEqual(catalog.cues[0].confidence, "high")

    def test_cue_demotion_when_refuted_dominates(self):
        catalog = CueCatalog(goal="g1", cues=[
            Cue(cue="cue A", confidence="medium",
                 stats=CueStats(observed=2, confirmed=2)),
        ])
        # Three refutations with one confirmation → demote to low
        for _ in range(3):
            update_cue_catalog(catalog, HeavyResult(
                status=GOAL_ACHIEVED,
                cues_observed=[CueObservation(cue="cue A", present=True, confirms=False)],
            ))
        self.assertEqual(catalog.cues[0].confidence, "low")
        self.assertGreaterEqual(catalog.cues[0].stats.refuted, 3)

    def test_new_cue_is_appended_with_provenance(self):
        catalog = CueCatalog(goal="g1", cues=[])
        result = HeavyResult(
            status=GOAL_ACHIEVED,
            cues_observed=[],
            new_cues=["no red dot on Recruit Crew menu"],
        )
        update_cue_catalog(catalog, result, context="claude_guided_loop step 4")
        self.assertEqual(len(catalog.cues), 1)
        new = catalog.cues[0]
        self.assertEqual(new.cue, "no red dot on Recruit Crew menu")
        self.assertEqual(new.confidence, "unverified")
        self.assertEqual(new.provenance.source, "claude_observed")
        self.assertEqual(new.provenance.first_seen_in, "claude_guided_loop step 4")
        self.assertEqual(new.provenance.noted_during, "heavy_check")
        self.assertEqual(new.provenance.noted_by, "claude_vision")
        self.assertEqual(new.stats.observed, 1)
        self.assertEqual(new.stats.confirmed, 1)

    def test_observed_cue_not_in_catalog_added_as_new(self):
        """Claude can reference a cue that isn't in the catalog yet — it should be added."""
        catalog = CueCatalog(goal="g1", cues=[])
        result = HeavyResult(
            status=GOAL_ACHIEVED,
            cues_observed=[CueObservation(
                cue="harbor official says 'Ready to sail'",
                present=True, confirms=True,
            )],
        )
        update_cue_catalog(catalog, result, context="t")
        self.assertEqual(len(catalog.cues), 1)
        self.assertEqual(catalog.cues[0].cue, "harbor official says 'Ready to sail'")
        self.assertEqual(catalog.cues[0].provenance.source, "claude_observed")

    def test_duplicate_new_cue_not_added_twice(self):
        catalog = CueCatalog(goal="g1", cues=[
            Cue(cue="existing cue"),
        ])
        result = HeavyResult(
            status=GOAL_ACHIEVED,
            new_cues=["existing cue"],   # already known
        )
        update_cue_catalog(catalog, result)
        self.assertEqual(len(catalog.cues), 1)


if __name__ == "__main__":
    unittest.main()
