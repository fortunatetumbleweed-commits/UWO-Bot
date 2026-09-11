"""The largest BOX is not the largest text, and a scrap of prose is not a place.

Live 2026-09-07, arriving at FARO. The nameplate read `Faro` at 38px. An NPC bubble overhead
said "...Pyrenees in the north?" and OmniParser returned that fragment as a 71px `button` —
the BUBBLE's height, not its glyphs' — so the largest-height title rule picked `north?`. It
matched no port at 0.55, came through raw, and the leg failed:

    [read_port_name] raw OCR 'north?' did not match any known port (best similarity 0.55)
    the voyage ended at 'north?', not 'Faro' — the leg is not done

The fleet was standing in Faro. The picker already guarded against an NPC bubble tail
(2026-05-15, "Home" beating "Amsterdam") by taking the tallest — which is exactly the rule
that fails when the box outgrows its text.

The filter runs on the CANDIDATE POOL, not on the winners: the ranking keeps only what is
within 20% of the tallest, so an impossible-but-tall candidate does not merely win, it evicts
the real name before any later check can see it.
"""

from __future__ import annotations

import types
import unittest

import vision.ocr as O


def _el(label, x1, y1, w, h, kind="text"):
    e = types.SimpleNamespace(label=label, element_type=kind,
                              x1=x1, y1=y1, x2=x1 + w, y2=y1 + h)
    e.width, e.height = w, h
    e.cx, e.cy = x1 + w // 2, y1 + h // 2
    return e


FW, FH = 2400, 1080
# both measured off frame 68 of trace_barter_cmd_2026-09-07T09-53-44
BUBBLE = _el("north?", 385, 0, 200, 71, kind="button")
FARO = _el("Faro", 223, 41, 90, 38)


class WhatCannotBeAPortName(unittest.TestCase):

    def test_prose_punctuation_is_rejected(self):
        self.assertFalse(O._could_be_a_port_name("north?"))

    def test_a_lower_case_scrap_is_rejected(self):
        """Measured: none of the 224 known ports is without a capital."""
        self.assertFalse(O._could_be_a_port_name("north"))

    def test_digits_are_rejected(self):
        self.assertFalse(O._could_be_a_port_name("LV 92"))

    def test_a_real_port_passes(self):
        for name in ("Faro", "Amsterdam", "Port Royal", "Chang'an", "K'gari"):
            self.assertTrue(O._could_be_a_port_name(name), name)

    def test_an_UNCATALOGUED_name_still_passes(self):
        """The raw pass-through exists for ports not yet in the KB — shape only, never the
        catalogue."""
        self.assertTrue(O._could_be_a_port_name("Nieuw Amsterdam"))


class TheRealNameSurvivesATallerBubble(unittest.TestCase):

    def test_the_bubble_wins_without_the_filter(self):
        """The behaviour that failed the leg — kept so the fix cannot quietly regress."""
        self.assertEqual(
            O._top_left_title_from_elements([BUBBLE, FARO], FW, FH), "north?")

    def test_the_port_name_wins_with_it(self):
        self.assertEqual(
            O._top_left_title_from_elements([BUBBLE, FARO], FW, FH,
                                            acceptable=O._could_be_a_port_name), "Faro")

    def test_the_filter_never_empties_the_pool(self):
        """If nothing is acceptable, fall back rather than answering nothing — the caller's
        own guards still get their say."""
        self.assertEqual(
            O._top_left_title_from_elements([BUBBLE], FW, FH,
                                            acceptable=O._could_be_a_port_name), "north?")


if __name__ == "__main__":
    unittest.main()
