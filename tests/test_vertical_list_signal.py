"""VerticalListSignal + learned-fingerprint builder.

Origin: 2026-05-14.  Pure-substring LabelSetSignal in learned
fingerprints over-fires on unrelated screens whose text happens to
contain the target words (the `learned_deposit_withdrawal_savings_acco`
poison case).  VerticalListSignal anchors the match to the column
LAYOUT — labels must appear stacked at similar cx with distinct cy.

This pins:
  - the signal fires only when labels are arranged as a column;
  - it does NOT fire when the same labels are scattered (no cluster);
  - it does NOT fire when only one label appears, even if it's in
    the right region;
  - the learned-fingerprint builder picks VerticalListSignal when
    Claude's interactive elements form a column, and falls back to
    LabelSetSignal otherwise.
"""

from __future__ import annotations

import unittest

from vision.omniparser import DetectedElement
from vision.state_fingerprints import (
    Fingerprint, LabelSetSignal, VerticalListSignal,
    _evaluate_signal,
)


SCREEN_W = 2400
SCREEN_H = 1080


def _text(label: str, cx: int, cy: int, half_w: int = 80, half_h: int = 24):
    return DetectedElement(
        label=label, element_type="text",
        x1=cx - half_w, y1=cy - half_h,
        x2=cx + half_w, y2=cy + half_h,
        confidence=0.9,
    )


# ── Signal evaluator ────────────────────────────────────────────────────────


class VerticalListSignalTests(unittest.TestCase):

    def test_fires_when_labels_form_a_column(self):
        """Bank's left-strip menu: 3 labels at cx ≈ 300, distinct cy."""
        signal = VerticalListSignal(
            name="bank_left_strip",
            region=(0.0, 0.10, 0.30, 0.80),
            labels=frozenset({"deposit/ withdrawal", "savings acco", "insurance"}),
            min_matches=2,
        )
        elements = [
            _text("Deposit/ Withdrawal", 305, 160),
            _text("Savings Acco",        300, 262),
            _text("Insurance",           290, 369),
        ]
        fired, dbg = _evaluate_signal(signal, elements, SCREEN_W, SCREEN_H)
        self.assertTrue(fired, dbg)
        self.assertIn("column", dbg)

    def test_does_not_fire_when_labels_are_scattered(self):
        """The same label substrings present, but on disjoint columns
        and rows.  Without column structure, the signal must NOT fire —
        this is the poison-fingerprint regression test."""
        signal = VerticalListSignal(
            name="bank_left_strip",
            region=(0.0, 0.10, 0.95, 0.95),   # broader region intentionally
            labels=frozenset({"savings acco", "insurance"}),
            min_matches=2,
        )
        # 'Insurance' as a tooltip in the right panel; 'Savings Acco'
        # mentioned in a banner at the top — neither in the same column.
        elements = [
            _text("Insurance",   2100, 540),
            _text("Savings Acco", 800,  80),
        ]
        fired, dbg = _evaluate_signal(signal, elements, SCREEN_W, SCREEN_H)
        self.assertFalse(fired, dbg)

    def test_does_not_fire_with_only_one_label(self):
        signal = VerticalListSignal(
            name="bank_left_strip",
            region=(0.0, 0.10, 0.30, 0.80),
            labels=frozenset({"deposit/ withdrawal", "savings acco", "insurance"}),
            min_matches=2,
        )
        elements = [_text("Insurance", 290, 369)]
        fired, dbg = _evaluate_signal(signal, elements, SCREEN_W, SCREEN_H)
        self.assertFalse(fired, dbg)

    def test_does_not_fire_when_rows_overlap_vertically(self):
        """Two labels on the same y row — not a stacked list.  Must NOT
        match even though they share cx."""
        signal = VerticalListSignal(
            name="bank_left_strip",
            region=(0.0, 0.10, 0.30, 0.80),
            labels=frozenset({"savings acco", "insurance"}),
            min_matches=2,
            y_min_gap=60,
        )
        elements = [
            _text("Savings Acco", 300, 200),
            _text("Insurance",    310, 220),   # only 20 px below
        ]
        fired, dbg = _evaluate_signal(signal, elements, SCREEN_W, SCREEN_H)
        self.assertFalse(fired, dbg)

    def test_region_restriction(self):
        """When the labels form a column outside the configured region,
        the signal does NOT fire."""
        signal = VerticalListSignal(
            name="left_strip_only",
            region=(0.0, 0.10, 0.30, 0.80),
            labels=frozenset({"alpha", "beta"}),
            min_matches=2,
        )
        # Column at the right side (cx ~ 1900) — outside the left-strip region.
        elements = [
            _text("Alpha", 1900, 200),
            _text("Beta",  1900, 320),
        ]
        fired, _dbg = _evaluate_signal(signal, elements, SCREEN_W, SCREEN_H)
        self.assertFalse(fired)


# ── Learned fingerprint builder ────────────────────────────────────────────


class LearnedFingerprintBuilderTests(unittest.TestCase):

    def test_column_labels_produce_vertical_list_signal(self):
        from vision.claude_vision import _build_learned_fingerprint

        # Three labels arranged as a vertical column.
        positions = [
            ("Deposit/ Withdrawal", 305, 160),
            ("Savings Acco",        300, 262),
            ("Insurance",           290, 369),
        ]
        labels = [p[0] for p in positions]
        fp = _build_learned_fingerprint(
            state_id="bank_subview",
            button_labels=labels,
            frame_w=SCREEN_W, frame_h=SCREEN_H,
            labeled_positions=positions,
        )
        sigs = fp["positive_signals"]
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["kind"], "VerticalListSignal")
        # Region should be tight around the left strip
        l, t, r, b = sigs[0]["region"]
        self.assertLess(r, 0.35)            # right edge stays in the left half
        self.assertEqual(sigs[0]["min_matches"], 2)

    def test_scattered_labels_fall_back_to_label_set_signal(self):
        from vision.claude_vision import _build_learned_fingerprint

        # Labels on a single row (horizontal arrangement) → no column.
        positions = [
            ("Alpha",   200, 400),
            ("Beta",    700, 400),
            ("Gamma",  1200, 400),
        ]
        labels = [p[0] for p in positions]
        fp = _build_learned_fingerprint(
            state_id="horizontal_test",
            button_labels=labels,
            frame_w=SCREEN_W, frame_h=SCREEN_H,
            labeled_positions=positions,
        )
        sigs = fp["positive_signals"]
        self.assertEqual(sigs[0]["kind"], "LabelSetSignal")

    def test_no_positions_falls_back_to_label_set_signal(self):
        from vision.claude_vision import _build_learned_fingerprint

        fp = _build_learned_fingerprint(
            state_id="legacy_call",
            button_labels=["alpha", "beta", "gamma"],
            frame_w=SCREEN_W, frame_h=SCREEN_H,
            labeled_positions=None,
        )
        self.assertEqual(fp["positive_signals"][0]["kind"], "LabelSetSignal")


# ── End-to-end: signal survives load_learned_fingerprints ──────────────────


class SignalRoundTripTests(unittest.TestCase):

    def test_vertical_list_signal_round_trips_through_json(self):
        from vision.state_fingerprints import _signal_from_dict
        sig = _signal_from_dict({
            "kind":         "VerticalListSignal",
            "name":         "claude_observed_column",
            "region":       [0.0, 0.1, 0.3, 0.8],
            "labels":       ["deposit/ withdrawal", "savings acco", "insurance"],
            "min_matches":  2,
            "x_tolerance":  80,
            "y_min_gap":    60,
        })
        self.assertIsInstance(sig, VerticalListSignal)
        self.assertEqual(sig.min_matches, 2)
        self.assertEqual(sig.region, (0.0, 0.1, 0.3, 0.8))
        self.assertIn("savings acco", sig.labels)


if __name__ == "__main__":
    unittest.main()
