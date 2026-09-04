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


class ConfidenceMustNotOutrankProvenance(unittest.TestCase):
    """A learned fingerprint never outranks a standard one, whatever its confidence.

    Confidence used to be compared FIRST, and confidence is a fire RATIO — so a learned
    fingerprint with a single signal fired 1/1 = 100% and was graded HIGH, the top band, on
    the thinnest evidence there is, and beat a standard fingerprint that had matched less
    completely.

    Live 2026-08-26: the screen came back as 'learned_updates_august_10_mon_update_advance' —
    an announcement popup, which the FSM has no node for — and `open_world_map` spent three
    attempts on a state it could do nothing with before giving up.

    NOTE this module does not import `vision.state_fingerprints_data`, so the standard
    fingerprints are only present when another test module has loaded it first. That is a
    pre-existing order dependence in this file; the test below registers both sides itself so
    it holds either way.
    """

    def setUp(self):
        self._snapshot = dict(FINGERPRINT_REGISTRY)

    def tearDown(self):
        FINGERPRINT_REGISTRY.clear()
        FINGERPRINT_REGISTRY.update(self._snapshot)

    def _fp(self, state_id, *labels, min_match=1):
        register_fingerprint(Fingerprint(
            state_id=state_id,
            description=state_id,
            positive_signals=tuple(
                LabelSetSignal(name=f"sig_{i}", region=(0.0, 0.0, 1.0, 1.0),
                               labels=frozenset({lab}), min_matches=1)
                for i, lab in enumerate(labels)),
            min_positive_to_match=min_match,
        ))

    def test_a_one_signal_learned_match_loses_to_a_partial_standard_one(self):
        """The exact shape of the failure: learned fires 1/1 (HIGH), standard fires 1/2
        (MEDIUM). Provenance decides, not the ratio."""
        self._fp("learned_announcement", "updates")
        # A synthetic standard id — anything not starting with 'learned_'. Two signals, only
        # one of which will fire, so it grades MEDIUM against the learned one's HIGH.
        self._fp("standard_screen", "updates", "explore")
        result = classify_via_registry(
            [_text("Updates", 120, 40)], SCREEN_W, SCREEN_H)
        self.assertEqual(result.state, "standard_screen")

    def test_a_learned_match_still_wins_when_nothing_standard_matches(self):
        """Learned fingerprints are not useless — they are only outranked."""
        self._fp("learned_announcement", "zzz_unique_label")
        result = classify_via_registry(
            [_text("zzz_unique_label", 120, 40)], SCREEN_W, SCREEN_H)
        self.assertEqual(result.state, "learned_announcement")
