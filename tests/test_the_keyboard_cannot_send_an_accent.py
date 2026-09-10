"""`adb shell input text` cannot carry a non-ASCII character. It throws.

Live 2026-09-09, one leg short of Gijón with the Pig already aboard:

    [world_map] typing 'Gijó' (prefix of 'Gijón') — attempt 1/4
    ADB error: java.lang.NullPointerException: Attempt to get length of null array
        at android.os.Binder.shellCommand(Binder.java:1088)          — stopping

Not a failed search — a dead run. Eleven ports and eleven villages in the catalogue carry
one: Málaga, Lübeck, Malé, Reykjavík, Vardø, São Tomé, Copiapó, Valparaíso, Mérida, and some
village names hold a full-width comma.

CUTTING IS RIGHT AND STRIPPING IS NOT. The game filters on ITS spelling, which keeps the
accent, so `Gijo` matches nothing — character four is `ó`, not `o` (user, 2026-09-09: "it
renders exactly Gijón, with the accent, so if you search gijo, there is nothing in the
list"). A shorter prefix is a SUPERSET: it can filter less, never exclude the destination.

Verified on the live world map — typing `Gij` left exactly one row, `'Gijon'` @(213,199).
"""

from __future__ import annotations

import unittest


# What the game holds, and what we can type to filter it down to that.
# The right-hand column was chosen by `_keyable_query`; the middle two were verified live
# on the world map on 2026-09-09 — each left exactly one row.
ACCENTED = {
    "Gijón":      "Gij",     # live: one row, 'Gijon' @(213,199)
    "Málaga":     "laga",    # live: one row, 'Malaga' @(226,199) — the user's own example
    "Malé":       "Mal",
    "Reykjavík":  "Reyk",
    "Ávila":      "vila",    # nothing typeable at the FRONT; the substring saves it
    "Lübeck":     "beck",
    "Vardø":      "Vard",
    "Copiapó":    "Copi",
    "Valparaíso": "Valp",
    "Mérida":     "rida",
}


class TheQueryIsATypeableSubstring(unittest.TestCase):
    """The search matches a SUBSTRING, so the query need not start the name."""

    def test_each_accented_port_gets_the_expected_query(self):
        from brain.activities.world_map import _keyable_query
        for name, expected in ACCENTED.items():
            with self.subTest(port=name):
                self.assertEqual(expected, _keyable_query(name))

    def test_every_query_is_typeable_and_really_in_the_name(self):
        from brain.activities.world_map import _keyable_query
        for name in ACCENTED:
            with self.subTest(port=name):
                q = _keyable_query(name)
                self.assertTrue(q.isascii(), f"{q!r} would throw in the input service")
                self.assertIn(q, name, "and it must occur in the GAME's spelling")

    def test_a_plain_name_is_just_its_prefix(self):
        from brain.activities.world_map import _keyable_query
        self.assertEqual("Faro", _keyable_query("Faro"))
        self.assertEqual("Bord", _keyable_query("Bordeaux"))

    def test_a_name_with_no_ascii_yields_nothing_to_type(self):
        from brain.activities.world_map import _keyable_query
        self.assertEqual("", _keyable_query("東京"))


class NothingUntypeableReachesTheInputService(unittest.TestCase):

    def _typed(self, query):
        from brain.activities.world_map import WorldMapActivity
        out = []
        a = WorldMapActivity.__new__(WorldMapActivity)
        a._type = out.append
        a._type_prefix(query)
        return out

    def test_an_accent_is_refused_rather_than_sent(self):
        self.assertEqual([], self._typed("Gijó"))

    def test_an_empty_query_is_refused_because_it_would_clear_the_filter(self):
        self.assertEqual([], self._typed(""))

    def test_a_plain_query_goes_through(self):
        self.assertEqual(["laga"], self._typed("laga"))


class StrippingWouldHaveFailed(unittest.TestCase):
    """The accent is in the game's own text, so an accent-free form matches nothing."""

    def test_the_stripped_form_is_not_in_the_name(self):
        self.assertNotIn("Gijo", "Gijón",
                         "character four is 'ó' — this is why stripping fails")
        self.assertNotIn("Mala", "Málaga")


if __name__ == "__main__":
    unittest.main()
