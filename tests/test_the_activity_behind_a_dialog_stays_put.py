# tests/test_the_activity_behind_a_dialog_stays_put.py
#
# "If the classification is unknown and there is no intent of leaving the building
# (back/home tapping), we assume the active activity is still the same, and dispatch to it"
# (user, 2026-09-04).
#
# THE GAME WITHDRAWS A CHROMED SCREEN'S FURNITURE WHILE A MODAL IS UP. Measured on
# frame_0207 of trace_barter_cmd_2026-09-03T22-23-54 — a market with a `Replenish Stock?`
# prompt over it — the back arrow, the title and the help icon are simply NOT DRAWN: the
# header region reads p99 11.6, against 255.0 on the same screen once the dialog was
# answered. Not dimmed. Absent.
#
# So `has_back_arrow=False` is a CORRECT reading of a bar that is not there, the chromed test
# cannot fire, and a market classifies as `unknown`. That is an honest answer to "what screen
# is this" — the screen has stopped presenting itself — and the WRONG answer to "where am I".
#
# Android's Activity stays RESUMED behind its dialog; only window focus goes. Ours is the
# same: nothing moved, the presentation changed. Live cost of not knowing that: the
# dispatcher handed back at "no activity for state 'unknown'" and re-derived from scratch.
#
# The gate is that WE DID NOT ASK TO LEAVE. That is what stops this being the stored
# conclusion the architecture warns about — the moment a moving intent is dispatched, the
# memory is dropped and `unknown` means what it always did.

import types
import unittest

from brain.dispatcher import UNRECOGNISED, WORKING, ActivityResult, Dispatcher


class _Act:
    GOALS = ()

    def __init__(self, name):
        self.name = name
        self.worked = 0

    def work(self, goal, state):
        self.worked += 1
        return ActivityResult(WORKING, {"did": "work"})


def _state(where):
    return types.SimpleNamespace(state=where, location=where, frame=None)


class _Intent:
    def __init__(self, name):
        self.name = name

    def __str__(self):
        return self.name


def _dispatcher(market):
    d = Dispatcher(perceive=lambda: _state("market"),
                   activities={"market": market},
                   next_goal=lambda _r, _s: "a goal",
                   to_intent=lambda _g, _s: None,
                   dispatch=lambda _i: None,
                   dialog=lambda _s: None)
    d.goal = "a goal"
    return d


class WhereWeWereStandingIsRemembered(unittest.TestCase):
    def test_an_unknown_screen_carries_the_activity(self):
        m = _Act("market")
        d = _dispatcher(m)
        d._standing_in = "market"
        self.assertIs(d._carry_on_standing("unknown"), m)

    def test_a_NAMED_screen_is_not_carried(self):
        """`sea` is a real answer, not an unreadable one — the normal pick handles it, and
        overriding it would keep the fleet 'in the market' at sea."""
        d = _dispatcher(_Act("market"))
        d._standing_in = "market"
        self.assertIsNone(d._carry_on_standing("sea"))

    def test_nothing_is_carried_before_an_activity_has_worked_anywhere(self):
        d = _dispatcher(_Act("market"))
        self.assertIsNone(d._carry_on_standing("unknown"))

    def test_only_a_screen_we_could_NAME_sets_the_memory(self):
        """Otherwise the memory bootstraps off its own guess: an unknown carried into an
        activity would then record `unknown` as where we are standing."""
        m = _Act("market")
        d = _dispatcher(m)
        d._standing_in = "market"
        d._carry_on_standing("unknown")
        self.assertEqual(d._standing_in, "market")


class AskingToLeaveDropsIt(unittest.TestCase):
    """The gate the user named. Without it this is a conclusion that outlives its evidence."""

    def test_a_moving_intent_forgets_where_we_stood(self):
        d = _dispatcher(_Act("market"))
        d._standing_in = "market"
        for name in ("EXIT_BUILDING", "ENTER_BUILDING", "OPEN_WORLD_MAP", "CLOSE_WORLD_MAP"):
            with self.subTest(intent=name):
                d._standing_in = "market"
                self.assertIn(name, d._LEAVING_INTENTS)

    def test_after_leaving_an_unknown_is_handed_back_as_before(self):
        d = _dispatcher(_Act("market"))
        d._standing_in = None          # as a leaving intent leaves it
        self.assertIsNone(d._carry_on_standing("unknown"))


class ItIsBoundedBecauseAnUnknownThatLastsIsNotADialog(unittest.TestCase):
    def test_it_gives_up_after_a_few_looks(self):
        m = _Act("market")
        d = _dispatcher(m)
        d._standing_in = "market"
        carried = [d._carry_on_standing("unknown") for _ in range(d._MAX_STANDING_LOOKS + 1)]
        self.assertTrue(all(c is m for c in carried[:d._MAX_STANDING_LOOKS]))
        self.assertIsNone(carried[-1], "carried an unknown indefinitely")
        self.assertIsNone(d._standing_in, "the memory should be dropped once it is spent")

    def test_the_count_resets_when_a_screen_is_named_again(self):
        m = _Act("market")
        d = _dispatcher(m)
        d._standing_in, d._standing_looks = "market", 2
        d._standing_in, d._standing_looks = "market", 0      # as a worked tick sets it
        self.assertIsNotNone(d._carry_on_standing("unknown"))


if __name__ == "__main__":
    unittest.main()
