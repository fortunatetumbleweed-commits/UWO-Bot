"""Accented port names must resolve from either spelling.

Live 2026-08-21: the gather leg could not reach Malé — its NEAREST supplier, 156 units
away. `catalogue_coords()` hands the mission the accent-stripped 'Male', the catalogue
stores the game's 'Malé', and the raw string compare failed:
    [pan_to_port] 'Male' not in port catalogue
The typed search failed for the same reason from the other side — it typed 'Male', whose
fourth character is exactly the one that differs from 'Malé'."""
import unittest

from actions.sail_actions import _ascii_search_prefix
from actions.world_map_nav import WorldMapNavigator


class FoldTests(unittest.TestCase):
    def test_accents_and_case_fold_together(self):
        for name in ("Malé", "Male", "MALE", "  malé  "):
            self.assertEqual(WorldMapNavigator._fold(name), "male")

    def test_unaccented_names_are_unchanged(self):
        self.assertEqual(WorldMapNavigator._fold("Masulipatnam"), "masulipatnam")


class SearchPrefixTests(unittest.TestCase):
    def test_an_accented_port_is_typed_only_up_to_the_accent(self):
        # 'Mal' is a prefix of BOTH spellings; 'Male' is a prefix of neither.
        self.assertEqual(_ascii_search_prefix("Male"), "Mal")
        self.assertEqual(_ascii_search_prefix("Malé"), "Mal")

    def test_plain_names_still_type_four_characters(self):
        self.assertEqual(_ascii_search_prefix("Masulipatnam"), "Masu")
        self.assertEqual(_ascii_search_prefix("Jakarta"), "Jaka")

    def test_the_prefix_never_contains_a_non_ascii_character(self):
        for name in ("Malé", "Málaga", "São Paulo", "Ceylon"):
            self.assertTrue(all(ord(c) < 128 for c in _ascii_search_prefix(name)), name)

    def test_a_name_that_is_almost_all_accents_still_types_something(self):
        self.assertGreaterEqual(len(_ascii_search_prefix("Éire")), 2)


class CatalogueLookupTests(unittest.TestCase):
    def test_the_stripped_form_finds_the_accented_entry(self):
        from actions.world_map_nav import make_village_navigator
        nav = make_village_navigator()
        for spelling in ("Male", "Malé", "male"):
            info = nav._lookup_port(spelling)
            self.assertIsNotNone(info, spelling)
            self.assertEqual(WorldMapNavigator._fold(info["name"]), "male")


if __name__ == "__main__":
    unittest.main()
