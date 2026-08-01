"""Standard fingerprints must win over learned_* ones at the same
confidence level.

Origin: 2026-05-22 harbor stuck loop.  bot enters harbor; the
learned fingerprint `learned_repair_supply` (state_id NOT in the FSM
graph) matches on the harbor menu's [Repair, Supply] column, shadowing
the standard `building` fingerprint.  sail_to's _handle_unknown then
backs out, and the bot loops in-and-out of the harbor for 22 minutes
without departing.

Fix: matcher tiebreaker prefers any state_id NOT starting with
'learned_' over any state_id starting with 'learned_'.
"""
import unittest
from dataclasses import dataclass, field

from vision.state_fingerprints import (
    CONFIDENCE_HIGH, CONFIDENCE_MEDIUM,
    Fingerprint, FINGERPRINT_REGISTRY,
    classify_via_registry, register_fingerprint,
    LabelSetSignal, ElementCountSignal,
)
from vision.omniparser import DetectedElement


SCREEN_W, SCREEN_H = 2400, 1080


def _text(label, cx, cy, w=200, h=70):
    return DetectedElement(
        label=label, element_type="text",
        x1=cx - w // 2, y1=cy - h // 2,
        x2=cx + w // 2, y2=cy + h // 2,
        confidence=0.9,
    )


def _icon(cx, cy, w=40, h=40):
    return DetectedElement(
        label="icon", element_type="icon",
        x1=cx - w // 2, y1=cy - h // 2,
        x2=cx + w // 2, y2=cy + h // 2,
        confidence=0.85,
    )


class LearnedPriorityTests(unittest.TestCase):
    """Inject a learned-style fingerprint that would otherwise win the
    fingerprint match against the standard `building` fingerprint,
    and verify the standard one wins."""

    def setUp(self):
        # Snapshot the registry so we can restore after injecting test
        # fingerprints.  (state_fingerprints uses a module-level dict.)
        self._snapshot = dict(FINGERPRINT_REGISTRY)

    def tearDown(self):
        FINGERPRINT_REGISTRY.clear()
        FINGERPRINT_REGISTRY.update(self._snapshot)

    def _inject_learned(self, state_id: str, label_to_match: str):
        """Register a learned-style fingerprint that matches if any
        element in the top-left region has the given label."""
        fp = Fingerprint(
            state_id=state_id,
            description=f"Test learned fp matching label {label_to_match!r}",
            positive_signals=(
                LabelSetSignal(
                    name="claude_observed_column",
                    region=(0.0, 0.0, 0.5, 1.0),  # half the screen
                    labels=frozenset({label_to_match}),
                    min_matches=1,
                ),
            ),
            min_positive_to_match=1,
        )
        register_fingerprint(fp)

    def test_standard_building_beats_learned_at_same_confidence(self):
        """Harbor frame has both a real `building: harbor` match AND a
        learned_repair_supply-style match.  Standard wins."""
        # Use a synthetic id so we don't collide with a real auto-loaded
        # learned fingerprint of the same name.
        self._inject_learned("learned_test_repair_supply", "harbor")
        elements = [
            _text("Harbor", 120, 40),       # building_title
            _icon(60, 40),                  # back_arrow_icon
        ]
        result = classify_via_registry(elements, SCREEN_W, SCREEN_H)
        self.assertIsNotNone(result)
        self.assertEqual(result.state, "building")
        self.assertEqual(result.detail, "building: harbor")
        self.assertFalse(result.state.startswith("learned_"))

    def test_learned_still_wins_when_no_standard_matches(self):
        """When no standard fingerprint matches but a learned one does,
        the learned one is still returned.  This protects the legitimate
        fallback behaviour of learned fingerprints for genuinely-novel
        screens that the standard registry doesn't cover."""
        # Clear standard registry briefly to simulate "no standard match"
        FINGERPRINT_REGISTRY.clear()
        # Re-add only the learned one
        self._inject_learned("learned_unique_thing", "uniquelabel")
        elements = [_text("UniqueLabel", 600, 540)]
        result = classify_via_registry(elements, SCREEN_W, SCREEN_H)
        self.assertIsNotNone(result)
        self.assertEqual(result.state, "learned_unique_thing")

    def test_learned_loses_even_with_more_signals(self):
        """A learned fingerprint with MORE matched signals should still
        lose to a standard fingerprint with fewer — the learned-vs-
        standard ranking takes precedence over signal-count."""
        # Build a learned fp that requires TWO matches in a wider region.
        fp = Fingerprint(
            state_id="learned_two_signal",
            description="Two-signal learned fp",
            positive_signals=(
                LabelSetSignal(
                    name="col_a",
                    region=(0.0, 0.0, 1.0, 1.0),
                    labels=frozenset({"harbor"}),
                    min_matches=1,
                ),
                LabelSetSignal(
                    name="col_b",
                    region=(0.0, 0.0, 1.0, 1.0),
                    labels=frozenset({"repair"}),
                    min_matches=1,
                ),
            ),
            min_positive_to_match=2,
        )
        register_fingerprint(fp)
        elements = [
            _text("Harbor", 120, 40),
            _icon(60, 40),
            _text("Repair", 500, 400),
        ]
        result = classify_via_registry(elements, SCREEN_W, SCREEN_H)
        self.assertEqual(result.state, "building")


if __name__ == "__main__":
    unittest.main()
