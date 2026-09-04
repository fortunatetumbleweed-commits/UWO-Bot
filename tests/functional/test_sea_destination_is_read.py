"""Where the fleet is BOUND is printed on the sea HUD. Read it.

CLAUDE.md forbids storing a conclusion — "the departure failed" cannot be checked by looking.
This is the observation that answers the same question and can: the sea HUD carries the
destination at bottom-centre, in the same place the world map puts Move / Go-to-City.

Live 2026-08-26 the cost of not reading it: the fleet left Seville for Barcelona, the
departure worked, a post-tap check misread the cinematic as failure, and the goal re-opened
the world map MID-VOYAGE to search for Barcelona — while this readout said `Barcelona ETA 1 d`
and the map's own frame showed the fleet's marker beside the port.

The OCR strings below are REAL, surveyed across the trace corpus, noise included.
"""

from __future__ import annotations

import types
import unittest

from vision.region_detectors.sea_destination import (bound_for, read_sea_destination)


def _frame(w=2400, h=1080):
    return types.SimpleNamespace(width=w, height=h, size=(w, h),
                                 crop=lambda box: types.SimpleNamespace(box=box))


def _reads(*tokens):
    """The OCR seam yields TOKENS, not one joined line — see the icon case below."""
    return lambda _crop: list(tokens)


class TheReadoutIsParsedFromTokens(unittest.TestCase):

    def test_the_live_barcelona_frame(self):
        d = read_sea_destination(_frame(), ocr=_reads("Barcelona", "ETA", "1d"))
        self.assertEqual((d.port, d.eta_days), ("Barcelona", 1))

    def test_a_two_word_port(self):
        d = read_sea_destination(_frame(), ocr=_reads("Port", "Royal", "ETA", "1d"))
        self.assertEqual((d.port, d.eta_days), ("Port Royal", 1))

    def test_a_village(self):
        d = read_sea_destination(_frame(), ocr=_reads("Melanesian", "Village", "ETA", "2d"))
        self.assertEqual((d.port, d.eta_days), ("Melanesian Village", 2))

    def test_the_ship_icon_does_not_become_a_digit(self):
        """THE BUG THE USER CAUGHT. A small ship icon sits between 'ETA' and the number. Read
        at native size it blurs into the digits: `ETA (ship) 4 d` came back as the single
        string 'ETA 94d', and a four-day voyage was reported as ninety-four.

        The fix is not a cleverer regex — it is reading TOKENS at 2x, where the same crop
        separates into 'Lisboa' / 'ETA' / '4d' and the icon stops being a digit at all. This
        test pins the parse; `test_against_the_real_frames` below pins the reading."""
        d = read_sea_destination(_frame(), ocr=_reads("Lisboa", "ETA", "4d"))
        self.assertEqual((d.port, d.eta_days), ("Lisboa", 4))

    def test_a_day_count_is_matched_as_a_WHOLE_token(self):
        """So a stray digit elsewhere on the plate cannot be mistaken for the ETA."""
        d = read_sea_destination(_frame(), ocr=_reads("Lisboa", "9", "ETA", "4d"))
        self.assertEqual(d.eta_days, 4)

    def test_a_missing_day_count_still_yields_the_port(self):
        """'Amsterdam ETAI' — the ETA is unreadable and the DESTINATION is not."""
        d = read_sea_destination(_frame(), ocr=_reads("Amsterdam", "ETAI"))
        self.assertEqual(d.port, "Amsterdam")
        self.assertIsNone(d.eta_days)


class ItRefusesToInventADestination(unittest.TestCase):

    def test_market_text_in_the_region_is_not_a_destination(self):
        """Frames where a market panel overlays the sea read 'io Sell Supplies' here.
        Reporting the fleet as bound for 'Sell Supplies' is worse than reporting nothing."""
        for toks in (("io", "Sell", "Supplies"), ("Sell", "Supplies", "Sello"),
                     ("LV", "93", "62.70")):
            with self.subTest(tokens=toks):
                self.assertIsNone(read_sea_destination(_frame(), ocr=_reads(*toks)))

    def test_an_empty_readout_is_no_destination(self):
        self.assertIsNone(read_sea_destination(_frame(), ocr=_reads()))


class BoundForAnswersTheQuestionThatWasBeingGuessed(unittest.TestCase):

    def test_it_matches_the_port_it_names(self):
        self.assertTrue(bound_for(_frame(), "Barcelona", ocr=_reads("Barcelona", "ETA", "1d")))

    def test_it_does_not_match_the_port_just_left(self):
        self.assertFalse(bound_for(_frame(), "Seville", ocr=_reads("Barcelona", "ETA", "1d")))

    def test_no_destination_is_not_a_match(self):
        self.assertFalse(bound_for(_frame(), "Barcelona", ocr=_reads()))


class AgainstTheRealFrames(unittest.TestCase):
    """The parse tests above use tokens I typed. These read the actual pixels, because the
    icon-as-a-digit bug lived in the READING and a hand-typed token list cannot catch it."""

    def _read(self, path):
        from PIL import Image
        return read_sea_destination(Image.open(path))

    def test_lisboa_is_four_days_not_ninety_four(self):
        d = self._read("data/sessions/trace_barter_cmd_2026-08-26T13-57-28/frame_0009.png")
        self.assertEqual((d.port, d.eta_days), ("Lisboa", 4))

    def test_the_frame_from_the_thrashing_run(self):
        """At 19:20 this said Barcelona/1d while the bot was concluding it had never left."""
        d = self._read("data/sessions/trace_barter_cmd_2026-08-26T18-14-16/frame_0020.png")
        self.assertEqual((d.port, d.eta_days), ("Barcelona", 1))

    def test_manual_sailing_names_no_destination(self):
        self.assertIsNone(
            self._read("data/sessions/trace_sail_melanesian_2026-08-20T11-18-32/frame_0120.png"))


if __name__ == "__main__":
    unittest.main()
