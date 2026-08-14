"""Fuzzy-match canonicalisation of noisy OCR reads of port and waters names.

Test fixtures use the ACTUAL corrupted strings observed in production
logs over the past week.  Every entry below was captured live; the
expected canonical name is verified against the screenshot the
corruption came from.
"""

from __future__ import annotations

import unittest

from vision.text_correction import (
    correct_port_name,
    correct_waters_name,
)


# ── Port-name corrections ──────────────────────────────────────────────────


class CorrectPortNameTests(unittest.TestCase):
    """Each fixture: (raw_ocr, expected_canonical) — verified against
    captured frames.  Empty expected = should NOT match anything."""

    # Empirical fixtures from production logs
    BUBBLE_FUSION = [
        # 2026-05-16 Amsterdam bubble frame 0000 — '...buildings! Perfect ads!'
        ("Amsterdamads!",         "Amsterdam"),
        # 2026-05-16 Amsterdam bubble frame 0002 — '...expert of the ez familyl'
        ("Amsterdamez familyl",   "Amsterdam"),
        # 2026-05-15 run — corruption observed throughout 73-min Amsterdam run
        ("Amsterdanted",          "Amsterdam"),
        # Frame variant with shorter bubble suffix
        ("Amsterdamrt",           "Amsterdam"),
        # 2026-05-15 home_-.json — chrome word fragment, no real port
        ("home -",                None),
        # 2026-05-15 horizons! — overlay banner text, not a port
        ("horizons!",             None),
        # 2026-05-15 'comtagious aiseases' — too far gone, correctly rejected
        ("comtagious aiseases",   None),
    ]

    def test_bubble_fusion_corruptions_corrected_to_amsterdam(self):
        for raw, expected in self.BUBBLE_FUSION:
            with self.subTest(raw=raw):
                canonical, ratio = correct_port_name(raw)
                if expected is None:
                    self.assertIsNone(
                        canonical,
                        f"{raw!r} should NOT match any port, got "
                        f"{canonical!r} (ratio={ratio:.2f})",
                    )
                else:
                    self.assertEqual(
                        canonical, expected,
                        f"{raw!r}: expected {expected!r}, got "
                        f"{canonical!r} (ratio={ratio:.2f})",
                    )

    def test_clean_read_passes_through(self):
        canonical, ratio = correct_port_name("Amsterdam")
        self.assertEqual(canonical, "Amsterdam")
        self.assertEqual(ratio, 1.0)

    def test_clean_london_passes_through(self):
        canonical, ratio = correct_port_name("London")
        self.assertEqual(canonical, "London")
        self.assertEqual(ratio, 1.0)

    def test_case_insensitive_match(self):
        # Lowercase OCR reads should still match canonical title-case.
        canonical, _ = correct_port_name("amsterdam")
        self.assertEqual(canonical, "Amsterdam")
        canonical, _ = correct_port_name("AMSTERDAM")
        self.assertEqual(canonical, "Amsterdam")

    def test_empty_string_returns_none(self):
        canonical, ratio = correct_port_name("")
        self.assertIsNone(canonical)
        self.assertEqual(ratio, 0.0)

    def test_does_not_falsely_match_similar_short_names(self):
        # 'London' should not be returned as 'London Bridge', 'Lisbon',
        # or 'Lima' just because they share letters.
        canonical, _ = correct_port_name("London")
        self.assertEqual(canonical, "London")

    def test_does_not_match_random_text(self):
        # Garbage text should not match anything in the seed list.
        for raw in ("asdfqwerty", "xyz123", "!!!"):
            with self.subTest(raw=raw):
                canonical, _ = correct_port_name(raw)
                self.assertIsNone(canonical, f"{raw!r} matched {canonical!r}")

    def test_market_ui_words_not_coerced_into_ports(self):
        # A market sub-menu title ('Sell') must not fuzzy-match a port
        # ('Sell'->'Seville' @0.73 falsely confirmed the sell screen as
        # overworld in the London<->Amsterdam run).
        for raw in ("Sell", "Buy", "Purchase", "Supply", "Supplies",
                    "Market", "Shipyard", "Bank", "Inn", "Cathedral", "Trade"):
            with self.subTest(raw=raw):
                canonical, _ = correct_port_name(raw)
                self.assertIsNone(canonical, f"{raw!r} matched {canonical!r}")

    def test_real_ports_still_match_after_ui_reject(self):
        # The UI-word reject must not harm real port canonicalisation.
        self.assertEqual(correct_port_name("Seville")[0], "Seville")
        self.assertEqual(correct_port_name("Amsterdam")[0], "Amsterdam")

    def test_read_port_name_returns_none_for_ui_title(self):
        # read_port_name's raw-fallback must NOT hand back a bare UI title —
        # otherwise _is_on_overworld reads 'Purchase'/'Sell' as a visible port
        # and falsely confirms overworld on a market screen.
        from unittest import mock
        import vision.ocr as ocr
        frame = mock.Mock(width=2400, height=1080)
        with mock.patch.object(ocr, "_top_left_title_from_elements", return_value="Purchase"):
            self.assertIsNone(ocr.read_port_name(frame, elements=[object()]))
        with mock.patch.object(ocr, "_top_left_title_from_elements", return_value="London"):
            self.assertEqual(ocr.read_port_name(frame, elements=[object()]), "London")


# ── Waters / sea-region corrections ────────────────────────────────────────


class CorrectWatersNameTests(unittest.TestCase):
    """OCR character confusion on stylised sea-region text."""

    def test_lawless_waters_typical_corruption_corrected(self):
        # 2026-05-15 sea frame — red/orange text against animated water
        canonical, ratio = correct_waters_name("Lauless Watters")
        self.assertEqual(canonical, "Lawless Waters")
        self.assertGreater(ratio, 0.7)

    def test_clean_atlantic_ocean_passes_through(self):
        canonical, ratio = correct_waters_name("Atlantic Ocean")
        self.assertEqual(canonical, "Atlantic Ocean")
        self.assertEqual(ratio, 1.0)

    def test_does_not_match_random_text(self):
        canonical, _ = correct_waters_name("Amsterdam")
        self.assertIsNone(canonical)

    def test_empty_string_returns_none(self):
        canonical, ratio = correct_waters_name("")
        self.assertIsNone(canonical)
        self.assertEqual(ratio, 0.0)


# ── Slug filtering ─────────────────────────────────────────────────────────


class SlugFilterTests(unittest.TestCase):
    """The KB loader must reject obviously-corrupted slugs so corrupted
    entries (like the 'amsterdanted.json' file from a previous failed
    run) don't pollute the candidate set."""

    def test_corrupted_kb_entries_not_loaded_as_candidates(self):
        # If we ever load the actual KB and find 'amsterdanted' or
        # 'home_-' as port entries (we do — they exist on disk),
        # they should NOT be added to the candidate set.  The seed
        # list's 'Amsterdam' must still match correctly.
        canonical, _ = correct_port_name("Amsterdam")
        self.assertEqual(canonical, "Amsterdam")
        # And 'amsterdanted' itself should still resolve to 'Amsterdam',
        # NOT match a corrupted KB entry with similarity 1.0.
        canonical, ratio = correct_port_name("Amsterdanted")
        self.assertEqual(canonical, "Amsterdam")
        # If the corrupted slug had been accepted as a candidate, the
        # ratio would be 1.0.  We want it to be < 1.0 (matched seed,
        # not the corrupted KB entry).
        self.assertLess(ratio, 1.0)


if __name__ == "__main__":
    unittest.main()
