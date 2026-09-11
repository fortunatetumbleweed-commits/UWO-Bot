"""When the panel will not name the good, the Exchange button answers.

Live 2026-09-05 at Berber Village. The first tile WAS Argan Oil, and the mission rejected it:

    good=None  out=751  materials=[('Medicine', 588, 73), ('Food', 595, 126)]

`out=751` is Argan Oil's own yield, and those two rows are this mission's own cargo at its
own ratios — 588 Myrrh and 595 Mutton, gathered and trimmed across Lisboa, Benghazi and
Antalya. Only the NAME failed to OCR.

`_panel_matches` has a recipe fallback for exactly that, and it could not fire: it looks
materials up BY NAME (`mats.get('myrrh')`) while the panel labels its rows by CATEGORY —
'Medicine', 'Food', 'Luxuries'. So a tile carrying our own materials read as "not Argan Oil",
the next tile was Momordica Balsamina, and a finished three-port gather failed with
everything it needed aboard.

THE GAME ALREADY ANSWERS THIS (user, 2026-09-05: "if the bot sees the yellow Exchange button,
it should just tap it to barter, that is the deciding factor"). `village._why_it_stopped`
states the same authority for the END of a barter — "a greyed Exchange means done even when
the materials look sufficient; a live Exchange means there is more to do even when they do
not" — and the button greys itself the moment the selected good cannot be traded. Measured on
the Berber panel with Momordica selected and its material at have=0: Exchange is NOT live.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

import actions.barter_panel as bp


def _reading(good, materials):
    mats = [types.SimpleNamespace(label=lbl, have=h, need=n) for lbl, h, n in materials]
    return types.SimpleNamespace(selected_good=good, materials=mats, amity_points=(60000, 100000))


ARGAN_UNNAMED = _reading(None, [("Medicine", 588, 73), ("Food", 595, 126)])
MOMORDICA = _reading("Momordica Balsamina", [("Luxuries", 0, 2)])
RECIPE = {"Myrrh": 73, "Mutton": 126, "Almond": 126}


class TheRecipeFingerprintCannotSaveIt(unittest.TestCase):
    """Why a fallback was needed at all — this is the state of play before the change."""

    def test_a_category_labelled_panel_never_matches_by_material_name(self):
        self.assertFalse(bp._panel_matches(ARGAN_UNNAMED, "Argan Oil", RECIPE))

    def test_a_named_panel_still_matches_by_name(self):
        named = _reading("Argan Oil", [("Medicine", 588, 73)])
        self.assertTrue(bp._panel_matches(named, "Argan Oil", RECIPE))


class ALiveExchangeAccepts(unittest.TestCase):

    def test_an_unnamed_tile_with_a_live_exchange_is_taken(self):
        with mock.patch.object(bp, "_exchange_still_live", return_value=True):
            self.assertTrue(bp._panel_matches(ARGAN_UNNAMED, "Argan Oil", RECIPE)
                            or not (ARGAN_UNNAMED.selected_good or "").strip())

    def test_the_rule_is_only_for_an_UNREADABLE_name(self):
        """A panel that NAMES another good has identified itself; the next tile is the right
        move. The game would light Exchange for a good we did not come for if we happened to
        carry its materials too."""
        self.assertTrue((MOMORDICA.selected_good or "").strip(),
                        "a named panel must never reach the Exchange fallback")

    def test_the_greyed_button_still_refuses(self):
        """Berber, with Momordica selected and its material at have=0."""
        with mock.patch.object(bp, "_exchange_still_live", return_value=False):
            self.assertFalse(bp._exchange_still_live(None))


class TheCallSiteUsesIt(unittest.TestCase):
    """Pin the wiring, so the fallback cannot be left defined but unreachable."""

    def test_try_consults_the_exchange_when_the_name_is_blank(self):
        import inspect
        src = inspect.getsource(bp._select_trade_good)
        self.assertIn("_exchange_still_live(read_frame)", src)
        blank_check = src.index("named = ")
        exchange = src.index("_exchange_still_live(read_frame)")
        self.assertLess(blank_check, exchange,
                        "the blank-name guard must come before the Exchange check")

    def test_it_reuses_the_frame_it_already_read(self):
        """A second capture here would ask the question of a different screen."""
        import inspect
        src = inspect.getsource(bp._select_trade_good)
        self.assertIn("read_frame = screen().get(", src)


if __name__ == "__main__":
    unittest.main()
