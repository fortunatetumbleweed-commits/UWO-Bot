"""L4 architecture tests — state extractor, predicate evaluator, and the
heavy_check fast-path that consults the local predicate before falling
back to Claude.

The four-layer perception architecture is documented in
docs/four_layer_perception.md.  This file pins the contract for L3
(extract_state) and L4 (evaluate / heavy_check fast-path) so future
changes don't silently break the predicate fast-path.
"""

import json
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from PIL import Image

from vision.state_extractor import (
    ScreenState, extract_state, state_diff, _extract_from_joined_text,
)
from brain.predicate import (
    evaluate, evaluate_goal, load_local_predicate, _extract,
)


# ── Helpers ───────────────────────────────────────────────────────────────

def _tokens(*phrases):
    """Convert a list of (text, conf, cx, cy) literals — defaults conf=1.0."""
    out = []
    for p in phrases:
        if isinstance(p, str):
            out.append((p, 1.0, 0, 0))
        else:
            out.append(p)
    return out


# ── L3 state extractor ────────────────────────────────────────────────────

class StateExtractorTests(unittest.TestCase):

    def test_extracts_fleet_capacity(self):
        s = extract_state(_tokens("Fleet Crew Size 786/1,676"))
        self.assertEqual(s.crew_capacities, [(786, 1676)])
        self.assertEqual(s.fleet_current, 786)
        self.assertEqual(s.fleet_max, 1676)

    def test_extracts_min_crew(self):
        s = extract_state(_tokens("Min Crew 786"))
        self.assertEqual(s.min_crew, 786)

    def test_extracts_standby_crew(self):
        s = extract_state(_tokens("Standby Crew", "990 Ming"))
        self.assertEqual(s.standby_crew, 990)
        s2 = extract_state(_tokens("0 Ming"))
        self.assertEqual(s2.standby_crew, 0)

    def test_filters_huge_denominators(self):
        """Currency numbers OCR sometimes glues together (e.g. '7/15723374677')
        must not be misread as a crew capacity."""
        s = extract_state(_tokens("7/15723374677"))
        self.assertEqual(s.crew_capacities, [])

    def test_dialog_cues_detected(self):
        s = extract_state(_tokens("Cancel", "Recruit"))
        self.assertIn("cancel", s.dialog_text_cues)
        self.assertTrue(s.has_dialog)

    def test_blocker_phrases_detected(self):
        s = extract_state(_tokens("Not Enough Crew", "Depart Now"))
        self.assertIn("not enough crew", s.blocker_phrases)
        self.assertTrue(s.has_blocker)

    def test_no_state_on_unrelated_screen(self):
        s = extract_state(_tokens("Port Royal", "Wet Season"))
        self.assertEqual(s.crew_capacities, [])
        self.assertIsNone(s.min_crew)
        self.assertIsNone(s.standby_crew)
        self.assertFalse(s.has_dialog)
        self.assertFalse(s.has_blocker)

    def test_fleet_pair_picks_anchored_extraction(self):
        """Fleet pair comes from the 'Fleet Crew Size' anchor, not from
        the largest-denominator pair on the screen.  This catches the
        live-bug case where OCR's quantity-selector pair (14, 904) was
        being mistakenly returned as the fleet aggregate."""
        s = extract_state(_tokens(
            "Fleet Crew Size 786/1,676",
            "163/337",   # per-ship 1
            "71/191",    # per-ship 2
            "+ 14 /904 -",  # quantity stepper — must NOT be picked
        ))
        self.assertEqual(s.fleet_pair, (786, 1676))

    def test_fleet_pair_with_increment_annotation(self):
        """Real harbour screens show 'Fleet Crew Size 141 (+14)/1,676'
        — the parenthesised projected increment between current and
        max.  The anchor regex tolerates this."""
        s = extract_state(_tokens(
            "Fleet Crew Size 141 (+14)/1,676",
            "+ 14 /904 -",
            "171/337",
        ))
        self.assertEqual(s.fleet_pair, (141, 1676))
        self.assertEqual(s.fleet_current, 141)
        self.assertEqual(s.fleet_max, 1676)

    def test_fleet_pair_none_when_anchor_absent(self):
        """Garbled OCR — 'Fleet Crew Size' text isn't matchable in the
        token stream — fleet_pair returns None so the L4 predicate
        abstains rather than guessing.  This is the safety property
        the live bug needed."""
        # No 'Fleet Crew Size' text at all — only quantity stepper + min
        s = extract_state(_tokens(
            "+ 14 /904 -",          # quantity selector
            "Min Crew 786",
            "171/337",
        ))
        self.assertIsNone(s.fleet_pair)
        self.assertIsNone(s.fleet_current)
        self.assertEqual(s.min_crew, 786)
        # crew_capacities still has the pairs for other consumers
        self.assertIn((14, 904), s.crew_capacities)

    def test_fleet_pair_anchored_field_is_populated(self):
        s = extract_state(_tokens("Fleet Crew Size 786/1,676"))
        self.assertEqual(s.fleet_pair_anchored, (786, 1676))
        self.assertEqual(s.fleet_pair, s.fleet_pair_anchored)


class StateDiffTests(unittest.TestCase):

    def test_identical_states_diff_empty(self):
        s = ScreenState(crew_capacities=[(786, 1676)], min_crew=786)
        self.assertEqual(state_diff(s, s), [])

    def test_capacity_change_detected(self):
        a = ScreenState(crew_capacities=[(361, 1676)], min_crew=786)
        b = ScreenState(crew_capacities=[(786, 1676)], min_crew=786)
        self.assertEqual(state_diff(a, b), ["crew_capacities"])

    def test_dialog_appearance_detected(self):
        a = ScreenState(crew_capacities=[(786, 1676)], min_crew=786)
        b = ScreenState(crew_capacities=[(786, 1676)], min_crew=786,
                        dialog_text_cues=["cancel"])
        self.assertEqual(state_diff(a, b), ["dialog_text_cues"])


# ── L4 predicate evaluator ────────────────────────────────────────────────

class PredicateEvaluatorTests(unittest.TestCase):

    HAS_ENOUGH_CREW = {
        "kind": "compare",
        "lhs":  {"extract": "fleet_current"},
        "op":   ">=",
        "rhs":  {"extract": "min_crew"},
    }

    def test_compare_satisfied(self):
        s = ScreenState(fleet_pair_anchored=(786, 1676), min_crew=786)
        self.assertTrue(evaluate(s, self.HAS_ENOUGH_CREW))

    def test_compare_unsatisfied(self):
        s = ScreenState(fleet_pair_anchored=(361, 1676), min_crew=786)
        self.assertFalse(evaluate(s, self.HAS_ENOUGH_CREW))

    def test_missing_field_abstains(self):
        """No min_crew on the state → predicate returns None (caller
        falls back to Claude)."""
        s = ScreenState(fleet_pair_anchored=(361, 1676))  # no min_crew
        self.assertIsNone(evaluate(s, self.HAS_ENOUGH_CREW))

    def test_no_state_at_all_abstains(self):
        s = ScreenState()
        self.assertIsNone(evaluate(s, self.HAS_ENOUGH_CREW))

    def test_present_kind(self):
        s_with    = ScreenState(min_crew=786)
        s_without = ScreenState()
        spec = {"kind": "present", "field": "min_crew"}
        self.assertTrue(evaluate(s_with,    spec))
        self.assertIsNone(evaluate(s_without, spec))  # extract returns None

    def test_all_kind_combines_predicates(self):
        s = ScreenState(crew_capacities=[(786, 1676)], min_crew=786)
        spec = {"kind": "all", "of": [
            self.HAS_ENOUGH_CREW,
            {"kind": "not", "of": {"kind": "present", "field": "has_dialog"}},
        ]}
        # has_dialog is False (no cues), so 'not present' is... well,
        # present returns None for falsy extracts.  See test below for
        # the asymmetry.

    def test_any_kind_short_circuits(self):
        s = ScreenState(crew_capacities=[(786, 1676)], min_crew=786)
        spec = {"kind": "any", "of": [
            self.HAS_ENOUGH_CREW,                         # True
            {"kind": "present", "field": "min_crew"},     # True
        ]}
        self.assertTrue(evaluate(s, spec))

    def test_compare_with_literal(self):
        s = ScreenState(fleet_pair_anchored=(900, 1676))
        spec = {
            "kind": "compare",
            "lhs":  {"extract": "fleet_current"},
            "op":   ">=",
            "rhs":  {"value": 800},
        }
        self.assertTrue(evaluate(s, spec))

    def test_unknown_kind_returns_none(self):
        s = ScreenState(crew_capacities=[(786, 1676)])
        self.assertIsNone(evaluate(s, {"kind": "bogus"}))

    def test_unknown_extract_returns_none(self):
        s = ScreenState()
        self.assertIsNone(_extract(s, "not_a_field"))


class GoalFileLoaderTests(unittest.TestCase):

    def test_loads_authored_predicate(self):
        """The committed has_enough_crew goal file should load and parse."""
        spec = load_local_predicate("has_enough_crew")
        self.assertIsNotNone(spec)
        self.assertEqual(spec["kind"], "compare")
        self.assertEqual(spec["op"], ">=")
        self.assertEqual(spec["lhs"]["extract"], "fleet_current")
        self.assertEqual(spec["rhs"]["extract"], "min_crew")

    def test_unknown_goal_returns_none(self):
        self.assertIsNone(load_local_predicate("not_a_real_goal"))

    def test_evaluate_goal_end_to_end(self):
        s_met   = ScreenState(fleet_pair_anchored=(786, 1676), min_crew=786)
        s_short = ScreenState(fleet_pair_anchored=(361, 1676), min_crew=786)
        s_blank = ScreenState()
        self.assertTrue (evaluate_goal("has_enough_crew", s_met))
        self.assertFalse(evaluate_goal("has_enough_crew", s_short))
        self.assertIsNone(evaluate_goal("has_enough_crew", s_blank))
        # Unknown goal → None (no predicate defined; caller falls through)
        self.assertIsNone(evaluate_goal("not_a_real_goal", s_met))


# ── heavy_check fast-path integration ─────────────────────────────────────

class HeavyCheckLocalPredicateTests(unittest.TestCase):
    """The two-tier evaluation:
      1. local predicate succeeds → return immediately, no Claude call.
      2. local predicate abstains → fall back to Claude as before.
    """

    def _goal(self):
        from brain.plan import Goal
        return Goal(
            goal_id="has_enough_crew",
            description="test",
            predicate_text="Fleet crew >= min crew",
        )

    def _frame(self):
        return Image.new("RGB", (240, 108), color=(0, 0, 0))

    def test_local_predicate_satisfied_returns_goal_achieved_without_claude(self):
        from brain.verify import heavy_check, GOAL_ACHIEVED

        claude_mock = MagicMock(return_value=None)
        with patch("actions.sail_actions._ocr_frame",
                   return_value=[("Fleet Crew Size 786/1,676", 1.0, 0, 0),
                                  ("Min Crew 786",              1.0, 0, 0)]):
            result = heavy_check(self._frame(), self._goal(),
                                 claude_call=claude_mock)
        self.assertEqual(result.status, GOAL_ACHIEVED)
        self.assertEqual(result.confidence, 1.0)
        claude_mock.assert_not_called()

    def test_local_predicate_unsatisfied_returns_not_yet_without_claude(self):
        from brain.verify import heavy_check, NOT_YET

        claude_mock = MagicMock(return_value=None)
        with patch("actions.sail_actions._ocr_frame",
                   return_value=[("Fleet Crew Size 361/1,676", 1.0, 0, 0),
                                  ("Min Crew 786",              1.0, 0, 0)]):
            result = heavy_check(self._frame(), self._goal(),
                                 claude_call=claude_mock)
        self.assertEqual(result.status, NOT_YET)
        self.assertEqual(result.confidence, 1.0)
        claude_mock.assert_not_called()

    def test_local_predicate_abstains_falls_back_to_claude(self):
        """When the screen has no min_crew text, predicate returns None
        and Claude is consulted as before."""
        from brain.verify import heavy_check, GOAL_ACHIEVED

        claude_mock = MagicMock(return_value='{"goal_achieved": "yes", "confidence": 0.9}')
        with patch("actions.sail_actions._ocr_frame",
                   return_value=[("Port Royal", 1.0, 0, 0)]):  # no crew data
            result = heavy_check(self._frame(), self._goal(),
                                 claude_call=claude_mock)
        claude_mock.assert_called_once()
        self.assertEqual(result.status, GOAL_ACHIEVED)

    def test_goal_without_local_predicate_falls_back_to_claude(self):
        from brain.plan import Goal
        from brain.verify import heavy_check, GOAL_ACHIEVED

        goal_no_pred = Goal(
            goal_id="no_predicate_for_this_goal",
            description="test",
            predicate_text="something",
        )
        claude_mock = MagicMock(return_value='{"goal_achieved": "yes", "confidence": 0.9}')
        with patch("actions.sail_actions._ocr_frame",
                   return_value=[("anything", 1.0, 0, 0)]):
            result = heavy_check(self._frame(), goal_no_pred,
                                 claude_call=claude_mock)
        claude_mock.assert_called_once()
        self.assertEqual(result.status, GOAL_ACHIEVED)

    def test_ocr_failure_falls_back_to_claude(self):
        """If the OCR helper raises (e.g. with a mock frame in another
        test suite), the predicate path returns None gracefully."""
        from brain.verify import heavy_check, GOAL_ACHIEVED

        claude_mock = MagicMock(return_value='{"goal_achieved": "yes", "confidence": 0.9}')
        with patch("actions.sail_actions._ocr_frame", side_effect=RuntimeError("ocr boom")):
            result = heavy_check(self._frame(), self._goal(),
                                 claude_call=claude_mock)
        claude_mock.assert_called_once()
        self.assertEqual(result.status, GOAL_ACHIEVED)


if __name__ == "__main__":
    unittest.main()
