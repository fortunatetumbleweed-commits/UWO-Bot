"""Tests for the City Info Trade sub-tab readers (#13): Preference + Cargo price.

Token lists are the REAL OCR output (panel-local px) from the barter walkthrough:
frame_0024 = Edinburgh Preference tab, frame_0023 = Veracruz Cargo tab (in range).
Parsers are pure over (text, conf, cx, cy) so these run without OCR.
"""
import unittest

from actions.world_map import _parse_preferences, _parse_good_price


# Real Preference-tab tokens (Edinburgh, frame_0024), incl. chrome + junk.
_PREF_TOKENS = [
    ("Preference", 1.0, 767, 350), ("Cargo", 0.98, 590, 354), ("Market", 0.75, 413, 351),
    ("Free Port", 1.0, 393, 214), ("Edinburgh", 0.84, 605, 215),
    ("Textile", 1.0, 445, 432), ("+50%", 1.0, 804, 432),
    ("VisE", 0.61, 888, 461),                     # junk, between rows
    ("Food", 1.0, 431, 516), ("+30%", 1.0, 805, 517),
    ("Livestock", 1.0, 461, 601), ("+10%", 1.0, 804, 600),
    ("Spices", 0.93, 442, 688), ("+20%", 0.96, 804, 686),
    ("Artwork", 1.0, 454, 769), ("+20%", 1.0, 805, 770), ("Gc", 0.99, 897, 772),  # junk right of %
    ("Fabrics", 1.0, 447, 854), ("+20%", 0.99, 805, 854),
]

# Real Cargo-tab tokens (Veracruz, frame_0023) — price visible = in range.
_CARGO_TOKENS = [
    ("Cargo", 0.95, 591, 355), ("Preference", 1.0, 767, 351),
    ("Port", 1.0, 259, 412),                       # chrome word, excluded
    ("Camas", 0.63, 443, 432), ("101%", 0.97, 686, 431), ("68,191", 1.0, 808, 432),
    ("Cargo", 1.0, 373, 990), ("3,902/4.108", 0.77, 773, 989),  # cargo total, far row
]


class PreferenceParseTests(unittest.TestCase):
    def test_all_six_categories(self):
        prefs = _parse_preferences(_PREF_TOKENS)
        self.assertEqual(prefs, {
            "Textile": 50, "Food": 30, "Livestock": 10,
            "Spices": 20, "Artwork": 20, "Fabrics": 20,
        })

    def test_junk_and_chrome_excluded(self):
        prefs = _parse_preferences(_PREF_TOKENS)
        self.assertNotIn("VisE", prefs)
        self.assertNotIn("Gc", prefs)
        self.assertNotIn("Cargo", prefs)
        self.assertNotIn("Preference", prefs)

    def test_empty_when_no_pct_rows(self):
        self.assertEqual(_parse_preferences(_CARGO_TOKENS), {})


class CargoPriceParseTests(unittest.TestCase):
    def test_in_range_reads_price_and_index(self):
        r = _parse_good_price(_CARGO_TOKENS)
        self.assertIsNotNone(r)
        self.assertEqual(r.good, "Camas")
        self.assertEqual(r.price, 68191)
        self.assertEqual(r.index_pct, 101)
        self.assertTrue(r.in_range)

    def test_out_of_range_no_price(self):
        # Good listed but no bare price number on its row → out of range.
        tokens = [
            ("Cargo", 0.95, 591, 355),
            ("Camas", 0.7, 443, 432), ("101%", 0.9, 686, 431),
        ]
        r = _parse_good_price(tokens)
        self.assertEqual(r.good, "Camas")
        self.assertIsNone(r.price)
        self.assertFalse(r.in_range)

    def test_none_when_no_good_row(self):
        self.assertIsNone(_parse_good_price([("Cargo", 1.0, 591, 355)]))


if __name__ == "__main__":
    unittest.main()
