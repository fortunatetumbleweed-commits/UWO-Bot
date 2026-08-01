"""Tests for _match_port_list_tokens — the port-search list matcher.

Regression: in the 2026-05-19 live run the port-list scroll scanned the
list 36 times without finding 'Port Royal'.  Root cause: the matcher only
compared single OCR tokens against the destination name.  When EasyOCR
split "Port Royal" into two tokens (or appended trailing metadata like
"12d"), no single token reached the 0.75 similarity threshold.

These tests cover:
  - single-token match still works (backwards compatible)
  - two-token row match for "Port Royal" / "London Bridge" style names
  - row tolerance: tokens far apart in y are NOT combined
  - trailing metadata in the row (Port Royal 12d) still resolves
  - confidence filter excludes low-conf garbage
  - returned coordinates apply the offset
"""
import unittest

from actions.sail_actions import _match_port_list_tokens


class SingleTokenMatchTests(unittest.TestCase):
    """The original behaviour — one OCR token matches the dest name."""

    def test_exact_single_token_match(self):
        tokens = [("lisbon", 0.95, 100, 200)]
        pos, labels = _match_port_list_tokens(tokens, "lisbon")
        self.assertEqual(pos, (100, 200))
        self.assertIn("lisbon", labels)

    def test_single_token_match_with_offsets(self):
        tokens = [("lisbon", 0.95, 100, 200)]
        pos, _ = _match_port_list_tokens(
            tokens, "lisbon", x_offset=10, y_offset=150,
        )
        self.assertEqual(pos, (110, 350))

    def test_ocr_typo_within_threshold(self):
        # token_sim("lcndon", "london") ≈ 0.83 — passes 0.75
        tokens = [("lcndon", 0.85, 200, 300)]
        pos, _ = _match_port_list_tokens(tokens, "london")
        self.assertEqual(pos, (200, 300))

    def test_unrelated_token_no_match(self):
        tokens = [("nassau", 0.95, 100, 200)]
        pos, _ = _match_port_list_tokens(tokens, "lisbon")
        self.assertIsNone(pos)


class MultiTokenRowMatchTests(unittest.TestCase):
    """Two-word names — the case that motivated the fix."""

    def test_port_royal_split_into_two_tokens_same_row(self):
        # Same y coord → grouped into one row → combined "port royal".
        tokens = [
            ("port",  0.95, 100, 400),
            ("royal", 0.95, 220, 400),
        ]
        pos, labels = _match_port_list_tokens(tokens, "port royal")
        self.assertIsNotNone(pos, "Expected to find 'Port Royal' from two-token row")
        # Tap centre = average of the two token centres.
        self.assertEqual(pos, (160, 400))
        self.assertIn("port",  labels)
        self.assertIn("royal", labels)

    def test_port_royal_with_trailing_distance_token(self):
        # Row: "Port Royal 12d" — should still match by sliding window.
        tokens = [
            ("port",  0.95, 100, 400),
            ("royal", 0.95, 220, 400),
            ("12d",   0.90, 360, 400),
        ]
        pos, _ = _match_port_list_tokens(tokens, "port royal")
        self.assertIsNotNone(pos)
        # Match window is the first two tokens.
        self.assertEqual(pos, (160, 400))

    def test_far_apart_tokens_not_combined(self):
        # "port" and "royal" on different rows — must NOT combine.
        tokens = [
            ("port",  0.95, 100, 400),
            ("royal", 0.95, 220, 600),   # 200 px below — different row
        ]
        pos, _ = _match_port_list_tokens(tokens, "port royal")
        self.assertIsNone(pos)

    def test_row_tolerance_boundary(self):
        # y diff of 25 (== _ROW_TOL default) — should still combine.
        tokens = [
            ("port",  0.95, 100, 400),
            ("royal", 0.95, 220, 425),
        ]
        pos, _ = _match_port_list_tokens(tokens, "port royal")
        self.assertIsNotNone(pos)

    def test_row_tolerance_just_over(self):
        # y diff of 30 (> _ROW_TOL default) — should NOT combine.
        tokens = [
            ("port",  0.95, 100, 400),
            ("royal", 0.95, 220, 432),
        ]
        pos, _ = _match_port_list_tokens(tokens, "port royal")
        self.assertIsNone(pos)


class FilterTests(unittest.TestCase):

    def test_low_confidence_token_ignored(self):
        tokens = [("lisbon", 0.20, 100, 200)]   # below 0.30
        pos, labels = _match_port_list_tokens(tokens, "lisbon")
        self.assertIsNone(pos)
        self.assertNotIn("lisbon", labels)

    def test_short_token_ignored(self):
        # 2-char token — under min_len 3.
        tokens = [("ab", 0.95, 100, 200)]
        pos, labels = _match_port_list_tokens(tokens, "ab")
        self.assertIsNone(pos)
        self.assertNotIn("ab", labels)

    def test_left_edge_token_ignored(self):
        # cx < 50 — noise on the left edge.
        tokens = [("lisbon", 0.95, 30, 200)]
        pos, _ = _match_port_list_tokens(tokens, "lisbon")
        self.assertIsNone(pos)


class PreferenceTests(unittest.TestCase):

    def test_higher_score_wins(self):
        # Two single-token matches; the higher-confidence one wins.
        tokens = [
            ("lisbon",  0.50, 100, 200),
            ("lisbon",  0.95, 300, 400),
        ]
        pos, _ = _match_port_list_tokens(tokens, "lisbon")
        self.assertEqual(pos, (300, 400))

    def test_single_token_beats_weaker_row_match(self):
        # A clean single-token "port royal" should beat a noisy row.
        tokens = [
            ("port royal", 0.99, 100, 200),   # clean single token
            ("port",       0.40, 100, 400),
            ("royaI",      0.40, 220, 400),   # capital-I OCR error
        ]
        pos, _ = _match_port_list_tokens(tokens, "port royal")
        self.assertEqual(pos, (100, 200))


if __name__ == "__main__":
    unittest.main()
