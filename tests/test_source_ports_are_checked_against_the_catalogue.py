"""A material's Source panel lists more than ports, and every line reads as text.

The panel carries producing VILLAGES and section headings beside the market ports, and the
reader took the lot. What the KB ended up holding, against the truth from the game data:

    Matchlock Gun  read 13 "ports"  truth: Barcelona, Seville
    Iron           read 16          truth: Amsterdam, Barcelona, Dublin, Kuching
    Candle         read 14          truth: Santa Island, Tripoli
    Pig            read  4          truth: Faro, Gijón, Prey Nokor

'Village' appeared three times as a standalone port. So did 'Production', 'Smelting
Handbook: Uncut Ore' and 'Sundry Goods Company Directory_' — panel headings, filed as places
to sail to. 'Prey Nokor' was split into 'Nokor' and 'Prey', neither of which is anywhere.
The Birch Tree missions only worked because Barcelona and Seville sorted to the front.

The world-map catalogue is the cross-check and was already baked: 224 ports, 69 villages,
offline and complete (user, 2026-09-04: "we can cross verify with port list, which we
currently already have on the world map").
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from actions.village_remote_reader import read_material_sources, resolve_source_port


def _panel(names, x=400, y0=440):
    """Source-panel lines as OmniParser returns them — left column, top to bottom."""
    return [SimpleNamespace(label=n, cx=x, cy=y0 + 20 * i, x1=x - 40, y1=y0 + 20 * i - 8,
                            x2=x + 40, y2=y0 + 20 * i + 8, element_type="text")
            for i, n in enumerate(names)]

# The Source dialog's own box, as `find_title_bars` measures it on the real frames
# (2026-08-25, all three materials): (326, 167, 2074, 915). The reader is bounded BY THE
# DIALOG now — a fixed window is what let the world map's village list, at cx 188-278, be
# read as sources. See `read_material_sources_by_kind`.
_CARD = (326, 167, 2074, 915)


class TheLiveReadsAreCorrectedToTheTruth(unittest.TestCase):
    """Each input is the exact list the KB holds; each expectation is the game's own data."""

    def test_matchlock_gun(self):
        got = read_material_sources(_panel(
            ["Barcelona", "Chinook", "Village", "Seville", "Ciguayo Village", "Oriya Village",
             "Nubia", "Village", "Sioux Village", "Bari Village", "Lusitanian Villag",
             "Yawuru", "Village"]), card=_CARD)
        self.assertEqual(got, ["Barcelona", "Seville"])

    def test_iron_drops_the_headings(self):
        got = read_material_sources(_panel(
            ["Amsterdam", "Chinook Village", "Barcelona", "Ciguayo Village", "Dublin",
             "Oriya Village", "Kuching", "Nubia", "Village", "Production", "Sioux Village",
             "Smelting Handbook: Uncut Ore", "Bari Village", "Lusitanian Villag", "Yawuru"]), card=_CARD)
        self.assertEqual(sorted(got), ["Amsterdam", "Barcelona", "Dublin", "Kuching"])

    def test_candle(self):
        got = read_material_sources(_panel(
            ["Santa Island", "Chinook Village", "Tripoli", "Ciguayo Village", "Production",
             "Oriya Village", "Sundry Goods Company Directory_", "Nubia", "Village"]), card=_CARD)
        self.assertEqual(got, ["Santa Island", "Tripoli"])

    def test_a_wrapped_port_name_is_rejoined(self):
        """'Prey Nokor' wrapped onto two lines. Neither half is a port, and a gather leg
        aimed at whichever came first."""
        self.assertEqual(read_material_sources(_panel(["Faro", "Gijon", "Nokor", "Prey"]), card=_CARD),
                         ["Faro", "Gijón", "Prey Nokor"])

    def test_an_already_clean_list_is_unchanged(self):
        self.assertEqual(read_material_sources(_panel(["Bordeaux", "Madeira", "Trabzon"]), card=_CARD),
                         ["Bordeaux", "Madeira", "Trabzon"])

    def test_accents_are_restored_not_rejected(self):
        """The panel reads accent-free. Málaga is where Almond is sold."""
        self.assertEqual(read_material_sources(_panel(
            ["Lisboa", "Malaga", "Palma", "Valencia"]), card=_CARD),
            ["Lisboa", "Málaga", "Palma", "Valencia"])


class AVillageIsARejectionNotAMatch(unittest.TestCase):
    """Tested BEFORE any fuzziness, and this is why: `correct_port_name` scores
    'Sioux Village' against 'Seville' at 0.60. Filing a port that does not sell the material
    is worse than the heading it replaces — a heading sends nobody anywhere."""

    def test_a_known_village_is_never_a_port(self):
        for v in ("Sioux Village", "Ciguayo Village", "Chinook Village", "Oriya Village"):
            with self.subTest(v):
                self.assertIsNone(resolve_source_port(v))

    def test_a_village_does_not_become_a_similarly_spelled_port(self):
        self.assertNotEqual(resolve_source_port("Sioux Village"), "Seville")

    def test_panel_headings_are_not_places(self):
        for h in ("Production", "Smelting Handbook: Uncut Ore",
                  "Sundry Goods Company Directory_", "Village"):
            with self.subTest(h):
                self.assertIsNone(resolve_source_port(h))


class TheResolverItself(unittest.TestCase):

    def test_an_exact_port_passes_through(self):
        self.assertEqual(resolve_source_port("Barcelona"), "Barcelona")

    def test_accent_free_spellings_resolve(self):
        self.assertEqual(resolve_source_port("Gijon"), "Gijón")
        self.assertEqual(resolve_source_port("Malaga"), "Málaga")
        self.assertEqual(resolve_source_port("Male"), "Malé")

    def test_nothing_is_not_a_port(self):
        for junk in ("", "   ", None, "_", "123"):
            with self.subTest(junk=junk):
                self.assertIsNone(resolve_source_port(junk))

    def test_it_returns_the_CANONICAL_spelling(self):
        """Downstream sails to this name, so it must match the catalogue, not the read."""
        self.assertEqual(resolve_source_port("gijon"), "Gijón")


if __name__ == "__main__":
    unittest.main()
