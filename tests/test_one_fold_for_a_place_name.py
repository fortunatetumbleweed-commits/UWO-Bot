"""Two spellings of one port must not become two ports.

Nine accent-strippers had grown across brain/, actions/, vision/ and memory/, and they
disagreed — NFD in four, NFKD in three, `.lower()` in some and `.casefold()` in others, and
only the map reader's handled `ø`. A name that survives one fold and not another is how the
same place becomes two.

Live 2026-09-09: `catalogue_coords` keys ports accent-STRIPPED (`Gijon`) so an accent-free
map read matches, while a recipe holds the game's canonical `Gijón`. `plan_gathering` tested
one against the other with `in`, so Gijón fell out of the coordinate map. Pig's sources
shrank to Faro alone, Faro is recorded scarce, `all()` over a single port is trivially true,
and the mission refused to sail:

    [plan] every known source is scarce this season for ['Pig'] — not profitable now

Gijón had no season record at all. It was UNREAD, not scarce — and it was the very leg the
fleet had been sailing to when the previous run died.
"""

from __future__ import annotations

import unittest


class TheFoldIsOneFold(unittest.TestCase):

    def test_the_spellings_of_one_port_agree(self):
        from memory.places import fold_name
        for a, b in (("Gijón", "Gijon"), ("Málaga", "Malaga"), ("Malé", "male"),
                     ("Lübeck", "Lubeck"), ("Vardø", "Vardo"), ("São Tomé", "Sao Tome"),
                     ("  Faro ", "faro")):
            with self.subTest(pair=(a, b)):
                self.assertEqual(fold_name(a), fold_name(b))

    def test_it_flattens_letters_the_normal_forms_leave_alone(self):
        """`ø` and `ł` are their own letters, not accented o and l — NFD keeps them."""
        from memory.places import fold_name
        self.assertTrue(fold_name("Vardø").isascii())
        self.assertEqual("vardo", fold_name("Vardø"))
        self.assertEqual("lodz", fold_name("Łódź"))

    def test_every_accented_port_in_the_catalogue_folds_to_ascii(self):
        from memory.places import fold_name
        from vision.world_map_parser import load_port_catalogue
        for name in load_port_catalogue():
            with self.subTest(port=name):
                self.assertTrue(fold_name(name).isascii(),
                                f"{name!r} would not match an accent-free read")

    def test_different_places_stay_different(self):
        from memory.places import fold_name
        self.assertNotEqual(fold_name("Faro"), fold_name("Gijón"))


class AnUnreadPortIsNotAScarcePort(unittest.TestCase):
    """The refusal that started this: a sample of one, with the other source discarded."""

    def _plan(self, seasons):
        from brain.gathering_solver import plan_gathering
        return plan_gathering(
            ["Pig"], {"Pig": ["Faro", "Gijón"]},
            {"Faro": (4430, 1820), "Gijon": (4200, 1500)},   # keys accent-STRIPPED
            start=(4430, 1820), quantities={"Pig": 1081},
            season_fn=lambda p, m: seasons.get(p))

    def test_the_accented_source_is_not_lost_to_the_coordinate_map(self):
        gp = self._plan({"Faro": "low"})
        self.assertEqual(set(), gp.low_everywhere,
                         "Gijón has no season record — unread is not scarce")
        self.assertEqual(set(), gp.unsourced)

    def test_it_still_refuses_when_every_source_really_is_scarce(self):
        gp = self._plan({"Faro": "low", "Gijón": "low"})
        self.assertEqual({"Pig"}, gp.low_everywhere,
                         "the rule's real job is untouched")

    def test_the_route_keeps_the_games_spelling(self):
        """The route is typed into the search box, which holds the accent."""
        gp = self._plan({})
        for port in gp.route:
            with self.subTest(port=port):
                self.assertIn(port, ("Faro", "Gijón"),
                              "not the stripped key — the game's own name")


if __name__ == "__main__":
    unittest.main()
