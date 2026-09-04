"""A port name is a NAME — not prose, and not a fragment stretched onto a longer name.

Live 2026-08-26, standing at BARCELONA:

    [text_correction] port 'tac' → 'Tacoma' (similarity 0.67)
      Overworld confirmed: port name 'Tacoma' visible
    [read_port_name] raw OCR 'Espana and Portugal' did not match any known port
    [read_port_name] raw OCR 'is the place where' did not match any known port
    [sail_to] tick=1 phase=INIT state='port_overworld' port='is the place where'

Two separate faults, both ending in a confident wrong answer about where the fleet is:

  * the reader picked up an NPC's line of flavour text and returned it as the port name,
    because the only things it rejected were UI titles and strings containing digits;
  * three characters of OCR noise, 'tac', were fuzzy-matched onto Tacoma — a port on the
    Pacific coast of North America — at a similarity of 0.67, over a 0.6 cutoff.

Handing back prose is worse than handing back None: None is recognisably an absence, and
callers treat any non-empty return as proof of location. CLAUDE.md states the invariant —
a port_overworld ALWAYS has a port name, and failing to read one is an anomaly to flag.
"""

from __future__ import annotations

import unittest

from vision import ocr
from vision.text_correction import correct_port_name, _known_ports


class TheRulesMustNotRejectRealPorts(unittest.TestCase):
    """The guard rails are only safe if every one of the 224 catalogued ports clears them."""

    def test_no_known_port_is_too_many_words(self):
        offenders = [p for p in _known_ports() if len(p.split()) > ocr._PORT_NAME_MAX_WORDS]
        self.assertEqual(offenders, [], "real ports go up to 'Rio de Janeiro' — three words")

    def test_no_known_port_contains_a_function_word(self):
        offenders = [p for p in _known_ports()
                     if any(w.lower() in ocr._NEVER_IN_A_PORT_NAME for w in p.split())]
        self.assertEqual(offenders, [], "'de' must stay OUT of the denylist — two names use it")

    def test_every_known_port_still_canonicalises_to_itself(self):
        for p in _known_ports():
            with self.subTest(port=p):
                self.assertEqual(correct_port_name(p)[0], p)


class AFragmentIsNotAPort(unittest.TestCase):

    def test_the_read_that_invented_tacoma(self):
        self.assertIsNone(correct_port_name("tac")[0])

    def test_a_short_read_does_not_become_a_longer_port(self):
        self.assertIsNone(correct_port_name("Bar")[0])

    def test_genuinely_short_ports_still_resolve(self):
        """Short reads cannot simply be banned — these are real places."""
        for p in ("Goa", "Diu", "Edo", "Ezo"):
            with self.subTest(port=p):
                self.assertEqual(correct_port_name(p)[0], p)

    def test_corruption_that_ADDS_characters_still_resolves(self):
        """The case this fuzzy match exists for. Real OCR corruption lengthens a name; it
        does not halve it."""
        self.assertEqual(correct_port_name("Amsterdamads!")[0], "Amsterdam")
        self.assertEqual(correct_port_name("Amsterdanted")[0], "Amsterdam")


class ProseIsNotAPort(unittest.TestCase):

    def _read(self, raw):
        """Drive read_port_name's tail: no OmniParser, a raw read that matches nothing."""
        from unittest.mock import patch
        with patch.object(ocr, "_try_get_omniparser_elements", return_value=None), \
             patch.object(ocr, "read_text", return_value=raw), \
             patch.object(ocr, "_looks_like_real_text", return_value=True):
            class _F:
                width, height = 2400, 1080
                def crop(self, box): return self
            return ocr.read_port_name(_F())

    def test_the_sentence_that_became_the_fleets_position(self):
        self.assertIsNone(self._read("is the place where"))

    def test_the_npc_line_before_it(self):
        self.assertIsNone(self._read("Espana and Portugal"))

    def test_a_plausibly_novel_port_still_passes_through(self):
        """The pass-through exists for ports not yet catalogued; these rules must not kill
        it. 'Vellmark Cove' resembles nothing in the catalogue (best 0.45), so it survives
        canonicalisation and must survive the prose rules too — two words, no function
        words."""
        self.assertEqual(self._read("Vellmark Cove"), "Vellmark Cove")
