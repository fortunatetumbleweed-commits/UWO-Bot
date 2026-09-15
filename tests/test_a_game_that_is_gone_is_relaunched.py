"""An unclassifiable screen is asked WHO OWNS IT before anything is pressed at it.

Live 2026-09-15, and it cost fourteen minutes and the mission. The game exited between one
frame and the next:

    frame 321  14:54:08   the world map, 2400x1080
    frame 322  14:54:19   the Android launcher, 1080x2400

The phone had rotated out of the game's landscape lock, so the size alone says the game was
gone. The tap issued at 14:54:19 landed on the launcher. The screen then timed out to black,
and `handle_unknown_blocking` polled it, consulted Claude SEVEN times and pressed Back after
each one. Claude answered "Completely black screen - app may be loading or crashed" every
time, and the run saved that black screen as TWO NEW INTERRUPTORS before the stall guard
ended it.

Two things were wrong and both are the same mistake. A screen nobody can classify has two
explanations — the game is showing something new, or the game is GONE — and the pixels
cannot separate them. Android owns the window stack and answers in one call. And a crash
learned as a dismissable popup poisons the catalogue for every future run.
"""
from __future__ import annotations

import unittest
from unittest import mock

import brain.perceive as P


class TheGameIsAskedAboutBeforeTheScreenIs(unittest.TestCase):

    def setUp(self):
        P._RELAUNCHED = False

    def tearDown(self):
        P._RELAUNCHED = False

    def _run(self, *, in_front):
        launched = []
        with mock.patch("actions.adb_actions.the_game_is_in_front", return_value=in_front), \
             mock.patch("actions.adb_actions.launch_game",
                        side_effect=lambda: launched.append(1)):
            fired = P._the_game_has_gone_and_was_relaunched()
        return fired, launched

    def test_A_GAME_THAT_IS_GONE_IS_RELAUNCHED(self):
        fired, launched = self._run(in_front=False)
        self.assertTrue(fired)
        self.assertEqual(len(launched), 1)

    def test_a_game_in_front_is_left_alone(self):
        fired, launched = self._run(in_front=True)
        self.assertFalse(fired)
        self.assertEqual(launched, [])

    def test_NONE_IS_NOT_FALSE(self):
        """`the_game_is_in_front` returns None when it could not ask, and 'I could not ask'
        must never be read as 'the game is gone' — that is the one answer that starts an
        app. Same shape as the ADB helpers returning '' on failure, which is what made a
        wrong serial and an unplugged cable look identical."""
        fired, launched = self._run(in_front=None)
        self.assertFalse(fired)
        self.assertEqual(launched, [])

    def test_it_relaunches_ONCE_and_then_says_so(self):
        """A game that will not stay up is a fact for a person, not something to restart in
        a loop."""
        first, launched1 = self._run(in_front=False)
        second, launched2 = self._run(in_front=False)
        self.assertTrue(first)
        self.assertFalse(second, "the second failure must not start the game again")
        self.assertEqual(len(launched1) + len(launched2), 1)


class TheCheckComesBeforeTheLearning(unittest.TestCase):

    def test_the_gate_runs_before_learn_and_dismiss(self):
        """Read off the source. If it ran after, the crash would still be consulted on,
        pressed at and SAVED as an interruptor before anyone asked who owned the screen."""
        import inspect

        src = inspect.getsource(P.handle_unknown_blocking)
        gate = src.index("_the_game_has_gone_and_was_relaunched")
        learn = src.index("invoking learn-and-dismiss")
        self.assertLess(gate, learn)


class TheGameIsNamedInOnePlace(unittest.TestCase):

    def test_the_dispatcher_reads_the_package_from_settings(self):
        """It held its own literal. The same fact written twice is the drift this codebase
        keeps paying for."""
        from config.settings import GAME_PACKAGE

        import brain.dispatcher as D

        self.assertEqual(D._THE_GAME, GAME_PACKAGE)


if __name__ == "__main__":
    unittest.main()
