"""Tests for the Phase 4a family classifier.

Covers:
  • The SCREEN_TYPE_TO_FAMILY mapping covers every screen type the
    labeler currently produces, with no ambiguous duplicates.
  • The inference module's "no model" fallback returns
    FamilyVerdict("unknown", 0.0) without crashing.

Model accuracy is verified by the training script itself (it prints
per-class val acc); we don't re-test the trained weights here.
"""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from tools.train_family_classifier import (
    CLASSES,
    CLASS_TO_IDX,
    SCREEN_TYPE_TO_FAMILY,
)
from vision.family_classifier import (
    FamilyVerdict,
    classify_family,
    _reset_for_test,
)


class FamilyMappingTests(unittest.TestCase):

    def test_the_classes_the_bot_routes_on_are_present(self):
        """Pin the NAMES the routing depends on, not the COUNT.

        This asserted `len(CLASSES) == 5` and broke when the model legitimately gained a
        sixth. A count is a restatement of a constant: it fails whenever the classifier grows
        and it catches nothing, since adding a class breaks nothing while REMOVING one that
        `brain/perceive.py` gates on breaks routing outright.
        """
        for name in ("chromed", "sea"):
            with self.subTest(family=name):
                self.assertIn(name, CLASSES)
        self.assertIn("port_overworld", CLASSES)
        self.assertIn("sea", CLASSES)
        self.assertIn("world_map", CLASSES)
        self.assertIn("chromed", CLASSES)
        self.assertIn("transient", CLASSES)

    def test_port_family_aggregation(self):
        # All port-family screen types map to port_overworld
        for st in ("port_overworld", "port_arrival_overlay",
                   "port_loading", "port_map"):
            self.assertEqual(SCREEN_TYPE_TO_FAMILY[st], "port_overworld")

    def test_sea_family_aggregation(self):
        for st in ("sea", "sailing_idle"):
            self.assertEqual(SCREEN_TYPE_TO_FAMILY[st], "sea")

    def test_chromed_family_aggregation(self):
        for st in ("building_interior", "sub_menu", "village", "main_menu"):
            self.assertEqual(SCREEN_TYPE_TO_FAMILY[st], "chromed")

    def test_transient_family_includes_dialogs_and_overlays(self):
        for st in ("dialog_gameplay", "dialog_system", "dialog_reward",
                   "announcement", "result_screen", "loading",
                   "negotiation", "task_progress"):
            self.assertEqual(SCREEN_TYPE_TO_FAMILY[st], "transient")

    def test_labels_corpus_has_no_unmapped_screen_types(self):
        """Every screen_type that appears in the actual labelled corpus
        must be in the mapping (or explicitly excluded — only 'other')."""
        labels_path = Path("data/labels.jsonl")
        if not labels_path.exists():
            self.skipTest("labels.jsonl not present")
        seen = set()
        with labels_path.open() as f:
            for line in f:
                try:
                    seen.add(json.loads(line).get("screen_type"))
                except json.JSONDecodeError:
                    pass
        unknown = seen - set(SCREEN_TYPE_TO_FAMILY) - {"other", None}
        self.assertEqual(
            unknown, set(),
            f"Unmapped screen_types in corpus: {sorted(unknown)}",
        )


class NoModelFallbackTests(unittest.TestCase):

    def setUp(self):
        _reset_for_test()

    def tearDown(self):
        _reset_for_test()

    def test_returns_unknown_when_model_missing(self):
        """If the model file doesn't exist, callers get a graceful
        'unknown' verdict instead of a crash."""
        with patch("vision.family_classifier._MODEL_PATH",
                   Path("/tmp/does_not_exist_family_classifier.pt")):
            frame = Image.new("RGB", (2400, 1080), (50, 80, 120))
            verdict = classify_family(frame)
        self.assertEqual(verdict.family, "unknown")
        self.assertEqual(verdict.confidence, 0.0)

    def test_load_failure_is_remembered(self):
        """After the first 'no model' verdict, subsequent calls must
        not re-attempt the disk read."""
        from pathlib import Path as _P
        nonexistent = _P("/tmp/does_not_exist_family_classifier.pt")
        with patch("vision.family_classifier._MODEL_PATH", nonexistent), \
             patch.object(_P, "exists", return_value=False) as mock_exists:
            frame = Image.new("RGB", (2400, 1080), (50, 80, 120))
            classify_family(frame)
            classify_family(frame)
            classify_family(frame)
        # _MODEL_PATH.exists() called at most once (first invocation).
        self.assertLessEqual(mock_exists.call_count, 1)


if __name__ == "__main__":
    unittest.main()
