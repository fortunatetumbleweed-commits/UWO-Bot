"""The port overworld classifies its own screen, instead of the entry tap deriving it.

Two defects live behind one fact: the port had no context module, so what its right panel was
showing got decided inside `tap_building_entry` — a function the dispatcher calls to perform
ONE transition.

  * Jakarta, 2026-08-21. A calibrated tab position landed on PLAYERS. The panel listed
    `kingdavid LV 70`, the list read found the word `harbor` in the standalone ⚓ shortcut
    button (which is not in the list at all), called that a building list, and scrolled player
    names for 60s. `gather:Jakarta` failed twice and the mission aborted.
  * Bordeaux. The Tasks tab was showing and a fuzzy match hit the word "market" inside a quest
    objective.

Both are the same question asked badly: WHICH TAB IS THIS. The answer needs the content and
the highlight, and neither may be trusted alone.
"""
from __future__ import annotations

import unittest

from brain import port_context as ctx


class _Plate:
    def __init__(self, label):
        from vision.element_postprocess import ROLE_BUILDING_NAMEPLATE
        self.role, self.label = ROLE_BUILDING_NAMEPLATE, label


def _classify(**kw):
    """No frame reaches a reader: every input is injected, so nothing captures."""
    kw.setdefault("rows", [])
    kw.setdefault("tabs", [])
    kw.setdefault("nameplates", [])
    return ctx.classify(object(), **kw)


def _rows(*names):
    """What `read_building_menu` returns: (label, x, y) per row."""
    return [(n, 2100, 200 + 60 * i) for i, n in enumerate(names)]


class TheListIsToldFromTheOtherTabs(unittest.TestCase):

    def test_a_real_building_list(self):
        rows = _rows("Harbor", "Market", "Shipyard", "Bank", "Inn")
        self.assertEqual(_classify(rows=rows), ctx.BUILDING_LIST)

    def test_JAKARTA_one_building_word_is_not_a_building_list(self):
        """The PLAYERS tab, read live: one hit, and it came from the ⚓ shortcut button."""
        rows = _rows("tropical", "kingdavid", "0.#", "lv 70", "harbor")
        self.assertEqual(_classify(rows=rows, tabs=[(2016, 156), (2114, 156)]),
                         ctx.OTHER_TAB)

    def test_BORDEAUX_a_quest_objective_that_contains_a_building_name(self):
        """Matched EXACTLY, never as a substring — 'palace:' and 'from istanbul union' are
        Tasks entries, and a sentence mentioning a market is not the market."""
        rows = _rows("Deliver goods to the market", "from istanbul union", "palace:")
        self.assertEqual(_classify(rows=rows, tabs=[(2016, 156)]), ctx.OTHER_TAB)

    def test_two_names_are_enough_and_one_is_not(self):
        self.assertEqual(_classify(rows=_rows("Harbor", "Bank"), tabs=[(1, 1)]),
                         ctx.BUILDING_LIST)
        self.assertEqual(_classify(rows=_rows("Bank"), tabs=[(1, 1)]), ctx.OTHER_TAB)

    def test_a_trailing_colon_or_stop_does_not_hide_a_name(self):
        self.assertEqual(_classify(rows=_rows("Harbor:", "Market.")), ctx.BUILDING_LIST)


class TheNameplateIsTheShortestWayIn(unittest.TestCase):

    def test_a_nameplate_wins_over_whatever_the_panel_shows(self):
        """It appears once the character has walked to the door, and tapping it enters. The
        panel behind it is irrelevant, which is why this is asked first."""
        self.assertEqual(_classify(nameplates=[_Plate("Market")],
                                   rows=_rows("kingdavid", "lv 70")),
                         ctx.NAMEPLATE)

    def test_WHICH_building_it_names_is_not_asked_here(self):
        """The context reads SCREENS; whether this is the building the ORDER wants is the
        activity's question. Stating it as a test so the goal never leaks in here."""
        self.assertEqual(_classify(nameplates=[_Plate("Shipyard")]), ctx.NAMEPLATE)


class NothingReadableIsNotAMiss(unittest.TestCase):

    def test_no_tabs_and_no_list(self):
        """The port overworld is still where we are — the panel is just not readable yet.
        MISS would mean 'not my screen at all', which would hand the tick to nobody."""
        self.assertEqual(_classify(), ctx.NO_PANEL)


class TheContextTakesNoAction(unittest.TestCase):

    def test_it_neither_taps_nor_captures(self):
        """A classifier that acts is the sub-loop this split exists to remove. Read off the
        source rather than trusted: the module must not reach for input or the screen."""
        import inspect

        src = inspect.getsource(ctx)
        for forbidden in ("from actions.adb_actions import tap", "capture_screen(",
                          "time.sleep", "swipe"):
            self.assertNotIn(forbidden, src, f"port_context should not {forbidden!r}")


if __name__ == "__main__":
    unittest.main()
