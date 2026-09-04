"""A village whose label is partly hidden must still be recognised.

Svear Village sits under a discovery icon on the world map (user, 2026-08-24/25), so OCR
returns a fragment — 'vear', 'Svea', 'ear Village'. The matcher tested equality and prefix
only, so a village in plain view scored nothing and the bot fell back to panning the map to
hunt for it.

The risk in loosening this is the opposite failure: a fragment so short it matches the wrong
place and drags a tap onto it. These pin both sides.
"""

from __future__ import annotations

import types
import unittest

from actions.sail_actions import _map_label_matches, _visible_row


class AnOccludedLabelStillNamesItsVillage(unittest.TestCase):

    def test_the_whole_label(self):
        self.assertEqual(_map_label_matches("Svear Village", "Svear Village"), 1.0)
        self.assertEqual(_map_label_matches("Svear", "Svear Village"), 1.0)

    def test_the_head_is_covered(self):
        self.assertGreater(_map_label_matches("vear", "Svear Village"), 0.8)

    def test_the_tail_is_covered(self):
        self.assertGreater(_map_label_matches("Svea", "Svear Village"), 0.8)

    def test_a_fragment_beside_the_word_village(self):
        """'Village' narrows the field to a handful, so a shorter fragment is safe there."""
        self.assertGreater(_map_label_matches("ear Village", "Svear Village"), 0.8)


class ItMustNotMatchOnACoincidence(unittest.TestCase):

    def test_a_single_letter_is_not_a_village(self):
        for fragment in ("S", "Sv", "ar Village"):
            self.assertEqual(_map_label_matches(fragment, "Svear Village"), 0.0,
                             f"{fragment!r} is not evidence enough to tap")

    def test_a_different_village_does_not_match(self):
        for other in ("Hutu Village", "Cheyenne Village", "San Village", "Lisboa"):
            self.assertEqual(_map_label_matches(other, "Svear Village"), 0.0)


class TheBestMatchWins(unittest.TestCase):

    def _el(self, label, cx):
        return types.SimpleNamespace(label=label, cx=cx, cy=400)

    def test_a_full_label_beats_a_fragment(self):
        """Scanning returned the FIRST hit before, so a fragment could win over the real row."""
        els = [self._el("vear", 100), self._el("Svear Village", 300)]
        self.assertEqual(_visible_row(els, "Svear Village").cx, 300)

    def test_nothing_matches_returns_none(self):
        self.assertIsNone(_visible_row([self._el("Lisboa", 100)], "Svear Village"))

    def test_the_right_hand_panel_is_still_excluded(self):
        """The village's own name in the Village Info panel is not a list row — matching it
        left the list unopened and read the wrong good entirely (live 2026-08-24, x=1961)."""
        self.assertIsNone(_visible_row([self._el("Svear Village", 1961)], "Svear Village"))


# The fleet list TRUNCATES long names — "Imai Sokun Merc...", which OCR renders as
# "Imai Sokun Merc__." — and those trailing marks defeated the prefix test that would
# otherwise match perfectly. A pursued enemy is identified by name in that list, so a
# truncation that fails to match means the bot cannot tell it has caught up.

class ATruncatedNameStillNamesItsFleet(unittest.TestCase):

    def test_an_ellipsis_does_not_break_the_match(self):
        for fragment in ("Imai Sokun Merc...", "Imai Sokun Merc__.", "Imai Sokun Merc"):
            self.assertGreater(_map_label_matches(fragment, "Imai Sokun Mercenaries"), 0.8,
                               f"{fragment!r} should still name the fleet")

    def test_an_underscore_tail_does_not_break_the_match(self):
        self.assertGreater(_map_label_matches("Cespedes Advent_", "Cespedes Adventurers"), 0.8)

    def test_a_different_fleet_still_does_not_match(self):
        self.assertEqual(_map_label_matches("Doria Pirates", "Cespedes Adventurers"), 0.0)
        self.assertEqual(_map_label_matches("Reis Garrison", "Imai Sokun Mercenaries"), 0.0)
