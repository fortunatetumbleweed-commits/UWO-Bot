"""Unit tests for the lat/lon OCR parser.

Pure-Python parser tests — no ADB, no OCR engine.  Covers the realistic
shapes of EasyOCR output for the Move-panel readout.
"""
import unittest

from actions.water_tap import _parse_latlon_from_text


class ParseLatLonTests(unittest.TestCase):

    def test_clean_format(self):
        self.assertEqual(
            _parse_latlon_from_text("40.19, -9.91"),
            (40.19, -9.91),
        )

    def test_with_parens(self):
        # OCR sometimes pulls in surrounding decoration.
        self.assertEqual(
            _parse_latlon_from_text("(40.19, -9.91)"),
            (40.19, -9.91),
        )

    def test_decimal_comma_locale(self):
        # Some renderings: "40,19  -9,91" (decimal comma, space separator).
        self.assertEqual(
            _parse_latlon_from_text("40,19  -9,91"),
            (40.19, -9.91),
        )

    def test_endash_for_minus(self):
        # EasyOCR often emits en-dash where the font shows a minus.
        self.assertEqual(
            _parse_latlon_from_text("40.19, –9.91"),
            (40.19, -9.91),
        )

    def test_positive_both(self):
        self.assertEqual(
            _parse_latlon_from_text("32.9, 13.2"),  # Tripoli-ish
            (32.9, 13.2),
        )

    def test_noise_around_numbers(self):
        # Surrounding tokens from a wider crop.
        self.assertEqual(
            _parse_latlon_from_text("Move  40.19, -9.91  My Location"),
            (40.19, -9.91),
        )

    def test_no_match_returns_none(self):
        self.assertIsNone(_parse_latlon_from_text("Move  Filter"))

    def test_empty_returns_none(self):
        self.assertIsNone(_parse_latlon_from_text(""))
        self.assertIsNone(_parse_latlon_from_text(None))

    def test_out_of_range_rejected(self):
        # Garbage numbers must not be returned even if regex matches.
        self.assertIsNone(_parse_latlon_from_text("999.99, -888.88"))

    def test_single_number_rejected(self):
        # Only one decimal in view — not enough.
        self.assertIsNone(_parse_latlon_from_text("just one number 40.19"))

    def test_minus_misread_as_period(self):
        # Live OCR sample: real "34.88, -51.51" → OCR "34.88,.51.51".
        self.assertEqual(
            _parse_latlon_from_text("34.88,.51.51"),
            (34.88, -51.51),
        )

    def test_minus_misread_as_comma(self):
        # Live OCR sample: real "35.87, -53.27" → OCR "35.87,,53.27".
        self.assertEqual(
            _parse_latlon_from_text("35.87,,53.27"),
            (35.87, -53.27),
        )

    def test_minus_misread_with_surrounding_tokens(self):
        # Same mangle, with neighbouring OCR cruft.
        self.assertEqual(
            _parse_latlon_from_text("Move  34.88,.51.51  My Location"),
            (34.88, -51.51),
        )


if __name__ == "__main__":
    unittest.main()
