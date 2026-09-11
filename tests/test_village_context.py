"""The village context: which of the village's OWN screens is this, or MISS.

The outer `classify_nav_state` returns `village` both before AND after the game closes the
barter submenu. That verdict is correct and useless — the two screens need different
handling and it cannot tell them apart. Live 2026-08-27 the day's rounds ran out, the game
closed the submenu, and the missing distinction crashed the run: the code had no state for
"the panel is gone", so it fell through to "the panel must be stale" and called a refresh
path that had never once executed.
"""
import types
import unittest

import pytest

from brain.village_context import (BARTER_PANEL_BLOCKED, BARTER_PANEL_NO_GOOD,
                                   BARTER_PANEL_READY, BARTER_RESULT, EXCHANGE_CONFIRM,
                                   MISS, OVERFLOW_PROMPT, VILLAGE_TOP_MENU, classify)


def _els(*labels):
    return [types.SimpleNamespace(label=l) for l in labels]


def _panel(good="Bambara Groundnut", out=757, **kw):
    """Uses the REAL field names — a stub that agrees with a mistake proves nothing."""
    return types.SimpleNamespace(selected_good=good, output_quantity=out, **kw)


VILLAGE_MENU = _els("Barter", "Explore", "Gifting", "Loot", "Recruit")


def test_a_live_exchange_is_ready():
    assert classify(None, elements=_els("Exchange"), panel=_panel(),
                    exchange_live=True) == BARTER_PANEL_READY


def test_a_grey_exchange_is_blocked_NOT_exhausted():
    """Rounds REMAIN — the panel is still open. This is not the day ending."""
    assert classify(None, elements=_els("Exchange"), panel=_panel(),
                    exchange_live=False) == BARTER_PANEL_BLOCKED


def test_a_closed_submenu_is_the_village_top_menu():
    """The live crash. The game closes the submenu when the rounds run out; the outer
    perceive still says `village`, so only a scoped read can see this."""
    assert classify(None, elements=VILLAGE_MENU, panel=None,
                    exchange_live=None) == VILLAGE_TOP_MENU


def test_the_two_endings_are_different_states():
    grey = classify(None, elements=_els("Exchange"), panel=_panel(), exchange_live=False)
    closed = classify(None, elements=VILLAGE_MENU, panel=None, exchange_live=None)
    assert grey != closed, "a grey Exchange and a closed submenu are different PIXELS"


def test_an_open_panel_with_nothing_selected():
    assert classify(None, elements=_els("Select Trade Good."), panel=_panel(good=None, out=None),
                    exchange_live=None) == BARTER_PANEL_NO_GOOD


def test_our_own_dialogs_outrank_the_panel_beneath_them():
    """A dialog sits ON TOP of the panel. Reading the panel underneath and calling it
    'ready' would tap Exchange through the dialog."""
    assert classify(None, elements=_els("Barter Calculations", "OK"), panel=_panel(),
                    exchange_live=True) == EXCHANGE_CONFIRM
    assert classify(None, elements=_els("Exchange Complete"), panel=_panel(),
                    exchange_live=True) == BARTER_RESULT


def test_overflow_is_its_own_state():
    """THE CARD'S OWN FURNITURE, not a phrase we imagined it would use.

    This asserted on "Cargo is full" — one of three phrases in the old `_OVERFLOW_WORDS`,
    none of which the game has ever drawn. It says "Insufficient Empty Space", and carries a
    `Received Trade Goods` strip over a `Cargo` strip. So the test passed, `_on_overflow` was
    unreachable, and every overflow in the project's life was silently discarded — 360 units
    on one card at San Village and 400 on another (FC-3, docs/market_as_contexts.md).

    A test written from the same imagination as the code cannot catch the code being wrong
    about the world. The strings below are read off frame 214 of
    trace_barter_cmd_2026-09-05T21-38-09.
    """
    assert classify(None, elements=_els("Insufficient Empty Space",
                                        "Received Trade Goods", "Cargo", "Receive"),
                    panel=_panel(), exchange_live=True) == OVERFLOW_PROMPT


def test_the_discard_notice_is_not_the_overflow_card():
    """It opens OVER the overflow card, so both are on screen and the innermost is live.

    NOT told apart by "will be discarded" — BOTH cards say it. Only the notice asks a
    question.
    """
    from brain.village_context import DISCARD_NOTICE

    both_on_screen = _els("Insufficient Empty Space", "Received Trade Goods", "Cargo",
                          "Complete the trade?",
                          "400 Bambara Groundnut has not been claimed yet.",
                          "Unclaimed trade goods will be discarded.", "Cancel", "OK")
    assert classify(None, elements=both_on_screen, panel=_panel(),
                    exchange_live=True) == DISCARD_NOTICE


def test_the_overflow_card_alone_is_not_read_as_the_notice():
    """The overflow card also says "Unreceived trade goods will be discarded", and keying
    the notice on that phrase classified frame 214 as the notice."""
    assert classify(None, elements=_els("Insufficient Empty Space", "Received Trade Goods",
                                        "Cargo", "Unreceived trade goods will be discarded.",
                                        "Receive"),
                    panel=_panel(), exchange_live=True) == OVERFLOW_PROMPT


def test_anything_else_is_a_MISS():
    """Not an error — the normal signal that the context hypothesis expired."""
    assert classify(None, elements=_els("Harbor", "Market", "Shipyard"), panel=None,
                    exchange_live=None) == MISS


def test_amity_alone_does_not_keep_the_panel_open():
    """Amity is drawn on the village screen BEHIND the submenu, so it survives the close."""
    stale = types.SimpleNamespace(selected_good=None, output_quantity=None,
                                  amity_points=(79452, 100000))
    assert classify(None, elements=VILLAGE_MENU, panel=stale,
                    exchange_live=None) == VILLAGE_TOP_MENU


def test_every_state_has_a_handler():
    from brain.activities.village import VillageActivity
    assert set(VillageActivity._HANDLERS) == set(VillageActivity.CONTEXT_STATES)


class TheFieldNamesMatchTheRealObjects(unittest.TestCase):
    """The stubs agreed with the code and both were wrong.

    `classify` looked for `good` and `out` — names taken from how `read_barter_panel` LOGS
    its result ("good=Bambara Groundnut out=7"), not from the dataclass, whose fields are
    `selected_good` and `output_quantity`. Worse, `_read_panel_state()` returns a DERIVED
    `PanelBarterState` that has neither. Live 2026-08-28 the panel was open and readable and
    the context said `village_top_menu` on every tick, so the activity re-opened it for ever.

    A stub can agree with a mistake. These assert against the real classes.
    """

    def test_the_reading_has_the_fields_the_context_reads(self):
        from actions.barter_reader import BarterPanelReading
        fields = set(BarterPanelReading.__dataclass_fields__)
        self.assertIn("selected_good", fields)
        self.assertIn("output_quantity", fields)
        self.assertNotIn("good", fields, "there is no `good` field — do not read one")
        self.assertNotIn("out", fields)

    def test_the_derived_state_cannot_answer_which_good(self):
        """Which is why the classifier must read the RAW panel, not this."""
        from brain.barter_quantity import PanelBarterState
        fields = set(PanelBarterState.__dataclass_fields__)
        self.assertNotIn("selected_good", fields)
        self.assertNotIn("good", fields)

    def test_a_real_reading_is_seen_as_an_open_panel(self):
        from actions.barter_reader import BarterPanelReading
        from brain.village_context import _panel_is_up, selected_good
        reading = BarterPanelReading(selected_good="Bambara Groundnut", output_quantity=757,
                                     materials=[("Raisin", 279, 2)])
        self.assertTrue(_panel_is_up(reading))
        self.assertEqual(selected_good(reading), "Bambara Groundnut")

    def test_the_derived_state_is_NOT_seen_as_an_open_panel(self):
        """The live bug, pinned: passing this where a reading was expected read as closed."""
        from brain.barter_quantity import PanelBarterState
        from brain.village_context import _panel_is_up
        self.assertFalse(_panel_is_up(PanelBarterState(rounds_remaining=3)))


class AnUnreadableNameIsNotAnEmptyPanel(unittest.TestCase):
    """Live 2026-09-05 at Berber, an endless loop.

    Argan Oil's name would not OCR on any frame. `_select_trade_good` selected the tile and
    answered "selected Argan Oil" — via the live-Exchange rule one layer down — and the very
    next tick this classifier called the panel BARTER_PANEL_NO_GOOD again, so the activity
    re-selected, and re-selected, six taps a minute with no rounds committed.

    The game says "Select Trade Good." when nothing is chosen, and that prompt is tested
    FIRST. Reaching the name check means the game did not say it: a good IS selected and only
    our reading of its name is missing. The Exchange button then answers whether it can be
    bartered, which is the same authority `village._why_it_stopped` already cites for the end
    of a barter.
    """

    def _panel(self, good):
        import types
        return types.SimpleNamespace(
            selected_good=good, output_quantity=751,
            materials=[types.SimpleNamespace(label="Medicine", have=588, need=73),
                       types.SimpleNamespace(label="Food", have=595, need=126)])

    def test_an_unnamed_panel_with_a_live_exchange_is_READY(self):
        self.assertEqual(classify("", elements=[], panel=self._panel(None),
                                  exchange_live=True), BARTER_PANEL_READY)

    def test_an_unnamed_panel_with_a_GREYED_exchange_is_still_NO_GOOD(self):
        """Nothing tradable is selected, so asking for a good again is right."""
        self.assertEqual(classify("", elements=[], panel=self._panel(None),
                                  exchange_live=False), BARTER_PANEL_NO_GOOD)

    def test_the_games_own_prompt_still_wins(self):
        """'Select Trade Good.' is positive evidence that nothing is chosen, and it is
        checked before any of this."""
        import types as _t
        prompt = [_t.SimpleNamespace(label="Select Trade Good.", cx=1600, cy=480,
                                     x1=1500, y1=460, x2=1700, y2=500,
                                     element_type="text")]
        self.assertEqual(classify("", elements=prompt,
                                  panel=self._panel(None), exchange_live=True),
                         BARTER_PANEL_NO_GOOD)

    def test_a_named_panel_is_unaffected(self):
        self.assertEqual(classify("", elements=[], panel=self._panel("Argan Oil"),
                                  exchange_live=True), BARTER_PANEL_READY)
