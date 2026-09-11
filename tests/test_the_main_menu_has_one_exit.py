"""The main menu has one exit, and it is NOT Back.

Nothing served `main_menu`, so the dispatcher said "no activity for state 'main_menu' —
asking for a goal", found nothing to do, and stopped after three identical ticks. Live
2026-09-07 that blocked two runs in a row: the menu sat over an arrival gate at Madeira and
the mission could not start, because the bot could not put the menu away.

IT COULD NOT BORROW AN ACTIVITY EITHER. `UnrecognizedChromedActivity` prefers Home and falls
back to BACK — and Back here raises "Exit Game?" (`_NEVER_BACK_FROM` lists main_menu for
exactly that). Measured on the live screen the chrome detector found no home, no back and no
hamburger, so borrowing it would have pressed Back every time.

THE RULE IS SAFE ONLY BECAUSE THE SCREEN IS IDENTIFIED. `classify_nav_state` names this menu
by its tile bar (auction, friend, guild, rank), and on THAT screen the close X is the
rightmost top-corner icon. A general "an unlabelled corner icon is a close" would tap the chat
bubble beside it: measured 2026-09-07, chat at (2078,2)-(2156,93), the X at (2173,4)-(2259,96)
— adjacent, both unlabelled.
"""

from __future__ import annotations

import types
import unittest

from brain.activities.main_menu import STATE, MainMenuActivity
from brain.dispatcher import BLOCKED, FINISHED


def _icon(x1, y1, x2, y2, kind="icon"):
    e = types.SimpleNamespace(label="icon", element_type=kind, x1=x1, y1=y1, x2=x2, y2=y2)
    e.cx, e.cy = (x1 + x2) // 2, (y1 + y2) // 2
    return e


FRAME = types.SimpleNamespace(width=2400, height=1080)
CHAT = _icon(2078, 2, 2156, 93)          # measured
CLOSE = _icon(2173, 4, 2259, 96)         # measured


def _act(els, taps):
    return MainMenuActivity(capture_fn=lambda: FRAME,
                            tap_fn=lambda x, y: taps.append((x, y)),
                            omni_fn=lambda _f: els)


def _state():
    return types.SimpleNamespace(state=STATE, frame=FRAME)


class TheRightmostCornerIconIsTheClose(unittest.TestCase):

    def test_it_taps_the_X_and_not_the_chat_beside_it(self):
        taps = []
        res = _act([CHAT, CLOSE], taps).work(None, _state())
        self.assertEqual(res.status, FINISHED)
        self.assertEqual(taps, [(2216, 50)])

    def test_order_does_not_matter(self):
        taps = []
        _act([CLOSE, CHAT], taps).work(None, _state())
        self.assertEqual(taps, [(2216, 50)])

    def test_a_tile_bar_button_is_too_big_to_be_a_close(self):
        """The menu's own buttons are large; only a small corner icon qualifies."""
        big = _icon(2100, 10, 2380, 300)
        taps = []
        _act([big, CLOSE], taps).work(None, _state())
        self.assertEqual(taps, [(2216, 50)])

    def test_an_icon_OUTSIDE_the_corner_is_ignored(self):
        taps = []
        res = _act([_icon(1200, 500, 1260, 560)], taps).work(None, _state())
        self.assertEqual(res.status, BLOCKED)
        self.assertEqual(taps, [])


class ItNeverPressesBack(unittest.TestCase):
    """Back here offers to quit the game, so no X means REPORT, never fall back."""

    def test_no_X_reports_rather_than_backing_out(self):
        taps = []
        res = _act([], taps).work(None, _state())
        self.assertEqual(res.status, BLOCKED)
        self.assertEqual(taps, [])
        self.assertIn("close X", (res.detail or ""))

    def test_main_menu_is_still_never_backed_from(self):
        from brain.intents import _NEVER_BACK_FROM
        self.assertIn("main_menu", _NEVER_BACK_FROM)


class ItIsRegisteredForTheState(unittest.TestCase):

    def test_the_dispatcher_can_find_it(self):
        from brain.run_goal import default_activities
        self.assertTrue(any(getattr(a, "name", "") == "main_menu"
                            for a in default_activities().get(STATE, [])))


if __name__ == "__main__":
    unittest.main()
