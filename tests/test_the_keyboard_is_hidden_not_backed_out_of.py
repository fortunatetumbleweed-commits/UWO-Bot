"""ESCAPE puts the keyboard away; BACK takes the screen with it — and the list is read first.

TWO DEFECTS, ONE TICK. Live 2026-09-15, hunting Bordeaux from sea:

    12:41:06  typed 'bord'
    12:41:24  [world_map] the soft keyboard is up — putting it away before deciding
              what this screen is (1/4); it covers the list
    12:41:34  [classify] → sea

The frame at that Back shows the list already filtered to a SINGLE starred row reading
'Bordeaux' at (234,198). The gate never looked. And the press took the world map, the typed
query and that row together, so the next tick started the search again from sea.

WHY BACK BEHAVES THIS WAY NOW. The IME is supposed to consume BACK while its window is
shown, which is why the press used to hide only the keyboard. On the phone in use from
2026-09-14 it does not. Six dismissals on the OLD phone all left the map open (09-03, 09-07,
three on 09-08, 09-10); the first one on this phone closed it, with nothing in the bot
changed. Measured A/B on the live device, same screen, keyboard up both times:

    ESCAPE  ->  keyboard down, world map still open, port list intact
    BACK    ->  keyboard down, world map CLOSED, back at the port overworld
"""
from __future__ import annotations

import inspect
import unittest
from unittest import mock

import brain.activities.world_map as W


class TheKeyboardIsHiddenNotBackedOutOf(unittest.TestCase):

    def test_the_primitive_sends_ESCAPE_AND_NOT_BACK(self):
        """Read off the source, because conftest replaces this primitive with a recorder
        offline — as it must, since firing it for real would key the live game. 111 is
        ESCAPE and 4 is BACK, and sending 4 here is the defect itself."""
        # FROM THE FILE, NOT THE ATTRIBUTE. conftest has already swapped the attribute for
        # a recorder, so `inspect.getsource` on it returns the recorder — which is how the
        # first version of this test failed while the code was right.
        import ast
        import pathlib

        import actions.adb_actions as adb_actions

        text = pathlib.Path(adb_actions.__file__).read_text()
        fn = next(n for n in ast.parse(text).body
                  if isinstance(n, ast.FunctionDef) and n.name == "hide_keyboard")
        src = ast.get_source_segment(text, fn)
        self.assertIn('"111"', src)
        self.assertNotIn('"4"', src)

    def test_the_activity_calls_it_rather_than_pressing_back(self):
        """The behavioural half: whatever the primitive sends, the activity must reach for
        THIS one. conftest's recorder makes the call observable without touching the phone."""
        from tests.conftest import _INPUT_LOG

        a = W.WorldMapActivity()
        before = len(_INPUT_LOG)
        a._dismiss_keyboard()
        fired = [name for name, *_ in _INPUT_LOG[before:]]
        self.assertIn("hide_keyboard", fired)
        self.assertNotIn("press_back", fired)

    def test_the_activity_no_longer_presses_back_for_this(self):
        src = inspect.getsource(W.WorldMapActivity._dismiss_keyboard)
        self.assertIn("hide_keyboard", src)
        self.assertNotIn("press_back", src)


class TheListIsReadBeforeTheKeyboardIsBlamed(unittest.TestCase):
    """The filtered list renders ABOVE the IME, so a visible row is the SUCCESSFUL case."""

    def _activity(self, *, visible):
        a = W.WorldMapActivity()
        a._kb_up_fn = lambda: True
        a._kb_dismiss_fn = mock.Mock()
        a._find_on_screen = mock.Mock(return_value=(234, 198) if visible else None)
        # THE TAB GATE RUNS FIRST and needs real perception. Left unstubbed it returns
        # BLOCKED, `work` never reaches the keyboard gate, and the "did not dismiss" test
        # passes for the wrong reason — which is what it did on the first writing.
        a._ensure_tab = mock.Mock(return_value=True)
        return a

    def test_A_READABLE_DESTINATION_IS_TAPPED_NOT_HIDDEN(self):
        a = self._activity(visible=True)
        with mock.patch.object(W.WorldMapActivity, "_classify",
                               return_value="nothing this test routes"):
            a.work(_Goal(), _State())
        a._kb_dismiss_fn.assert_not_called()
        a._find_on_screen.assert_called()      # it LOOKED, which is the point

    def test_an_unreadable_destination_still_puts_the_keyboard_away(self):
        """The 2026-09-03 case the gate was written for is untouched: the IME really can
        cover the rows, and a look through it finds nothing and scrolls the map for a port
        in plain view."""
        a = self._activity(visible=False)
        a.work(_Goal(), _State())
        a._kb_dismiss_fn.assert_called_once()

    def test_it_asks_THE_SAME_READER_the_handler_will(self):
        """A second, different 'is it there?' would drift from the one that decides the tap."""
        src = inspect.getsource(W.WorldMapActivity.work)
        self.assertIn("_find_on_screen", src)


def _Goal():
    """THE REAL GOAL TYPE. `work` refuses a goal the map does not serve before it reaches
    any of this, so a stand-in class returns BLOCKED and every assertion below passes for
    the wrong reason."""
    from brain.activities.world_map import ChooseDestination
    return ChooseDestination(where="Bordeaux", kind="port")


class _State:
    state = "world_map"


if __name__ == "__main__":
    unittest.main()
