"""The three ways an Exchange stops committing, told apart by the SCREEN.

    panel OPEN,  Exchange grey   -> rounds REMAIN; a material is short or amity too low
    panel CLOSED                 -> the game shut it: the day's rounds are spent (completion)
    panel OPEN,  taps did nothing-> a STALE panel; reopen it and try once more

Live 2026-08-27 at Hutu Village the rounds ran out, the game closed the barter submenu, the
Exchange tap landed on the village menu behind it, and the code read "no change" as a stale
panel — then crashed calling `refresh_stale_panel()` without the `good` it requires. That
path had never been reached before, which is how the signature mismatch survived.
"""
import pytest

from actions.barter_executor import _call_refresh, _panel_is_open, _short_materials


def test_a_panel_showing_a_good_is_open():
    assert _panel_is_open({"good": "Bambara Groundnut", "out": 793})


def test_a_closed_submenu_reads_as_no_panel():
    assert not _panel_is_open({"good": None, "out": None, "materials": []})
    assert not _panel_is_open(None)


def test_amity_alone_does_not_mean_the_panel_is_open():
    """Amity is drawn on the village screen behind the submenu, so it survives the close."""
    assert not _panel_is_open({"amity": (79452, 100000), "good": None, "out": None})


def test_refresh_is_given_the_good_it_asks_for():
    got = []
    assert _call_refresh(lambda good: got.append(good) or True,
                         {"good": "Bambara Groundnut"})
    assert got == ["Bambara Groundnut"]


def test_a_no_argument_refresh_still_works():
    assert _call_refresh(lambda: True, {"good": None})


def test_a_refresh_that_raises_is_reported_not_propagated():
    """A recovery must never be the thing that ends the run — which is what happened."""
    def boom(*_a):
        raise RuntimeError("nope")
    assert _call_refresh(boom, {"good": "X"}) is False


def test_the_short_material_is_named_for_a_grey_exchange():
    assert _short_materials({"materials": [("Luxury", 0, 2), ("Pig", 900, 2)]}) == ["Luxury"]
