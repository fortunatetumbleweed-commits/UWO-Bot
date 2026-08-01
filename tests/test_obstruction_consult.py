"""Phase B2a — Claude consult for novel obstructions.

The Claude API call itself is mocked.  Tests pin:

  - Cache hit short-circuits the API call
  - Goal-context goes into the prompt
  - Successful analyses are saved to memory/knowledge/obstruction_analyses/
  - Successful analyses + failed parses both append to the training log
  - The cache key includes (kind, structural_hash, goal_intent) so the
    same obstruction under different goals records separately
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from vision.obstruction_classifier import KIND_OVERLAY, ObstructionResult
import vision.obstruction_consult as oc


def _frame():
    return Image.new("RGB", (2400, 1080), color=(0, 0, 0))


def _tokens():
    """OCR tokens that fall inside the Harbor crew-hired overlay bbox."""
    return [
        ("Harbor Official",    0.9, 909, 704),
        ("The Crew is ready!", 0.9, 929, 769),
        # One outside the bbox — gets filtered out
        ("Recruit Crew", 0.9, 328, 55),
    ]


def _obstruction():
    return ObstructionResult(
        kind=KIND_OVERLAY,
        bbox=(700, 600, 1300, 800),
        confidence="high",
        signals=["test"],
    )


def _good_claude_response():
    return {
        "raw_text": json.dumps({
            "purpose": "Harbor Official confirms crew was recruited",
            "full_text": "Harbor Official: The Crew is ready!",
            "dismissal": "tap_anywhere",
            "relates_to_goal": True,
            "outcome_for_goal": "likely_resolved",
            "confidence": "high",
        }),
        "parsed": {
            "purpose": "Harbor Official confirms crew was recruited",
            "full_text": "Harbor Official: The Crew is ready!",
            "dismissal": "tap_anywhere",
            "relates_to_goal": True,
            "outcome_for_goal": "likely_resolved",
            "confidence": "high",
        },
    }


class ConsultObstructionTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self._cache_patch = patch.object(oc, "_CACHE_DIR", tmp / "cache")
        self._log_patch   = patch.object(oc, "_TRAINING_LOG", tmp / "training" / "log.jsonl")
        self._cache_patch.start()
        self._log_patch.start()

    def tearDown(self):
        self._cache_patch.stop()
        self._log_patch.stop()
        self._tmp.cleanup()

    def test_cache_miss_calls_claude_and_persists(self):
        with patch.object(oc, "_call_claude", return_value=_good_claude_response()) as fake:
            result = oc.consult_obstruction(
                frame=_frame(), obstruction=_obstruction(),
                ocr_tokens=_tokens(), goal_context=None,
            )
        self.assertEqual(fake.call_count, 1)
        self.assertIsNotNone(result)
        self.assertEqual(result.dismissal, "tap_anywhere")
        self.assertTrue(result.relates_to_goal)
        self.assertEqual(result.outcome_for_goal, "likely_resolved")
        # Cached at the right path
        cache_files = list((Path(self._tmp.name) / "cache").glob("*.json"))
        self.assertEqual(len(cache_files), 1)
        # Training log written
        log_path = Path(self._tmp.name) / "training" / "log.jsonl"
        self.assertTrue(log_path.exists())
        records = [json.loads(line) for line in log_path.read_text().splitlines()]
        self.assertEqual(records[0]["claude_status"], "ok")
        self.assertEqual(records[0]["obstruction_kind"], "overlay")

    def test_cache_hit_skips_claude(self):
        # First call populates cache
        with patch.object(oc, "_call_claude", return_value=_good_claude_response()):
            oc.consult_obstruction(_frame(), _obstruction(), _tokens(), None)
        # Second call should hit cache → no Claude call
        with patch.object(oc, "_call_claude", return_value=None) as fake:
            result = oc.consult_obstruction(_frame(), _obstruction(), _tokens(), None)
        self.assertEqual(fake.call_count, 0, "cache hit should skip Claude")
        self.assertIsNotNone(result)
        self.assertEqual(result.visit_count, 2)

    def test_goal_context_in_prompt(self):
        from brain.goal_context import GoalContext
        captured = {}

        def fake_call(prompt, frame, **kw):
            captured["prompt"] = prompt
            return _good_claude_response()

        goal = GoalContext(
            intent="set_sail",
            target={"destination": "Lisbon"},
        )
        with patch.object(oc, "_call_claude", side_effect=fake_call):
            oc.consult_obstruction(
                _frame(), _obstruction(), _tokens(), goal_context=goal,
            )
        self.assertIn("set_sail", captured["prompt"])
        self.assertIn("Lisbon", captured["prompt"])

    def test_different_goals_get_separate_cache(self):
        from brain.goal_context import GoalContext
        goal_a = GoalContext(intent="set_sail", target={})
        goal_b = GoalContext(intent="recruit_crew", target={})
        with patch.object(oc, "_call_claude", return_value=_good_claude_response()) as fake:
            oc.consult_obstruction(_frame(), _obstruction(), _tokens(), goal_a)
            oc.consult_obstruction(_frame(), _obstruction(), _tokens(), goal_b)
        # Two distinct cache entries, two Claude calls
        cache_files = list((Path(self._tmp.name) / "cache").glob("*.json"))
        self.assertEqual(len(cache_files), 2)
        self.assertEqual(fake.call_count, 2)

    def test_claude_unavailable_returns_none_and_logs_failure(self):
        with patch.object(oc, "_call_claude", return_value=None):
            result = oc.consult_obstruction(
                _frame(), _obstruction(), _tokens(), None,
            )
        self.assertIsNone(result)
        log_path = Path(self._tmp.name) / "training" / "log.jsonl"
        self.assertTrue(log_path.exists())
        records = [json.loads(line) for line in log_path.read_text().splitlines()]
        self.assertEqual(records[0]["claude_status"], "no_parse")

    def test_no_tokens_in_bbox_returns_none_no_call(self):
        # Bbox far from any token
        obs = ObstructionResult(
            kind=KIND_OVERLAY,
            bbox=(0, 0, 200, 200),
            confidence="high",
        )
        with patch.object(oc, "_call_claude") as fake:
            result = oc.consult_obstruction(
                _frame(), obs, _tokens(), None,
            )
        self.assertIsNone(result)
        self.assertEqual(fake.call_count, 0)

    def test_structural_hash_stable_across_token_order(self):
        toks_a = [
            ("Harbor Official", 909, 704),
            ("The Crew is ready!", 929, 769),
        ]
        toks_b = [
            ("The Crew is ready!", 929, 769),
            ("Harbor Official", 909, 704),
        ]
        self.assertEqual(oc._structural_hash(toks_a), oc._structural_hash(toks_b))


if __name__ == "__main__":
    unittest.main()
