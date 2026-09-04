"""A FRESH read can be partial too, and the plan must not inherit the gap.

`_recipe_is_partial` guards the CACHED path. This is the same condition arriving by the other
route, and it was left open: live 2026-08-27 the trade-list read reached the bottom of the
list, still never returned Matchlock Gun, and the plan was built from two of three materials
anyway — `buy {'Iron': 145, 'Candle': 102}`.

The material list is INVARIANT, so a missing one is a reading failure and not a recipe change.
Completing it from the KB is what lets the mission proceed; refusing would be safe and would
also block every run until the read is fixed, and arriving with two of three materials is the
one outcome that guarantees no barter at all.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from brain.barter_command import _complete_materials_from_kb


def _kb(*pairs):
    return types.SimpleNamespace(
        inputs=[types.SimpleNamespace(material=m, ratio=r) for m, r in pairs])


def _trade(**materials):
    return types.SimpleNamespace(good="Birch Tree", materials=dict(materials), obtain=689)


class AMissingMaterialIsFilledFromTheKB(unittest.TestCase):

    def test_the_live_case(self):
        t = _trade(Iron=126, Candle=88)
        with patch("memory.barter_kb.load_recipe",
                   return_value=_kb(("Iron", 102), ("Candle", 102), ("Matchlock Gun", 53))):
            _complete_materials_from_kb(t, "Birch Tree", "Svear Village")
        self.assertEqual(t.materials["Matchlock Gun"], 53)

    def test_the_READ_ratios_are_never_overwritten(self):
        """Only the MISSING material is filled. Iron read 126 today against the KB's 102 —
        ratios re-roll, and the screen is what was actually there."""
        t = _trade(Iron=126, Candle=88)
        with patch("memory.barter_kb.load_recipe",
                   return_value=_kb(("Iron", 102), ("Candle", 102), ("Matchlock Gun", 53))):
            _complete_materials_from_kb(t, "Birch Tree", "Svear Village")
        self.assertEqual(t.materials["Iron"], 126)
        self.assertEqual(t.materials["Candle"], 88)

    def test_a_complete_read_is_left_alone(self):
        t = _trade(Iron=126, Candle=88, **{"Matchlock Gun": 61})
        with patch("memory.barter_kb.load_recipe",
                   return_value=_kb(("Iron", 102), ("Candle", 102), ("Matchlock Gun", 53))):
            _complete_materials_from_kb(t, "Birch Tree", "Svear Village")
        self.assertEqual(t.materials["Matchlock Gun"], 61, "the read wins over the KB")

    def test_names_are_matched_case_insensitively(self):
        t = _trade(**{"iron": 126, "matchlock gun": 61})
        with patch("memory.barter_kb.load_recipe",
                   return_value=_kb(("Iron", 102), ("Matchlock Gun", 53))):
            _complete_materials_from_kb(t, "Birch Tree")
        self.assertEqual(len(t.materials), 2, "no duplicate under a different casing")

    def test_it_says_loudly_that_a_filled_ratio_is_a_GUESS(self):
        """Every other quantity came off the screen; this one did not, and the log is the
        only place that distinction survives."""
        from loguru import logger
        seen = []
        sink = logger.add(seen.append, level="WARNING")
        try:
            t = _trade(Iron=126)
            with patch("memory.barter_kb.load_recipe",
                       return_value=_kb(("Iron", 102), ("Matchlock Gun", 53))):
                _complete_materials_from_kb(t, "Birch Tree", "Svear Village")
        finally:
            logger.remove(sink)
        joined = " ".join(seen)
        self.assertIn("Matchlock Gun", joined)
        self.assertIn("GUESS", joined)


class ItDegradesQuietlyWhenTheKBCannotHelp(unittest.TestCase):

    def test_no_kb_entry_changes_nothing(self):
        t = _trade(Iron=126)
        with patch("memory.barter_kb.load_recipe", return_value=None):
            _complete_materials_from_kb(t, "Birch Tree")
        self.assertEqual(t.materials, {"Iron": 126})

    def test_a_kb_failure_changes_nothing(self):
        t = _trade(Iron=126)
        with patch("memory.barter_kb.load_recipe", side_effect=RuntimeError("kb down")):
            _complete_materials_from_kb(t, "Birch Tree")
        self.assertEqual(t.materials, {"Iron": 126})


if __name__ == "__main__":
    unittest.main()
