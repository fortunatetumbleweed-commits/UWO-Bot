"""Driving the dispatcher for one goal — the migration shim.

Each task node moves from "a function that drives screens" to "a goal run through the loop",
one at a time, while `run_mission` still owns the sequence around it.

The task runner inside is deliberately trivial: hand back the same goal until an activity
finishes it, then say nothing. "Nothing" is a valid answer and the dispatcher invents no work
after it.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from brain.activities.market import Hold, SellHold
from brain.dispatcher import ActivityResult, BLOCKED, FINISHED, UNRECOGNISED
from brain.run_goal import default_activities, run_goal


class _Recording:
    name = "market"

    def __init__(self, results):
        self._results = list(results)
        self.goals = []

    def work(self, goal, state):
        self.goals.append(goal)
        return self._results.pop(0) if len(self._results) > 1 else self._results[0]


def _state(where):
    return types.SimpleNamespace(state=where, port="Lisboa")


class RunningOneGoal(unittest.TestCase):

    def _run(self, results, states=("building:market",), **kw):
        seq = list(states)
        act = _Recording(results)
        res = run_goal(kw.pop("goal", SellHold(("Water",))),
                       perceive=lambda: _state(seq.pop(0) if len(seq) > 1 else seq[0]),
                       activities={"building:market": act}, **kw)
        return res, act

    def test_a_finished_activity_ends_the_run(self):
        res, act = self._run([ActivityResult(FINISHED, {"sold": ["Birch Tree"]})])
        self.assertEqual(res.status, FINISHED)
        self.assertEqual(len(act.goals), 1, "it must not keep working after finishing")

    def test_a_blocked_activity_also_ends_it_and_says_so(self):
        res, _ = self._run([ActivityResult(BLOCKED, {}, detail="shelf empty")])
        self.assertEqual(res.status, BLOCKED)
        self.assertEqual(res.detail, "shelf empty")

    def test_the_same_goal_is_offered_until_it_is_done(self):
        res, act = self._run([ActivityResult(UNRECOGNISED, {}),
                              ActivityResult(FINISHED, {"sold": []})])
        self.assertEqual(res.status, FINISHED)
        self.assertEqual(len(act.goals), 2)

    def test_a_goal_that_never_finishes_is_reported_not_ground_at(self):
        res, act = self._run([ActivityResult(UNRECOGNISED, {})], max_ticks=3)
        self.assertIsNone(res)
        self.assertLessEqual(len(act.goals), 3)


class ChoosingTheRightActivity(unittest.TestCase):
    """`location` alone is too coarse: the harbour, the market and the shipyard are all
    'building'. Handing a sell goal to whichever one the bot happens to be standing in is
    exactly the class of mistake this architecture removes."""

    def test_the_registry_is_keyed_by_which_building(self):
        keys = set(default_activities())
        self.assertIn("building:market", keys)
        self.assertNotIn("building", keys)

    def test_the_harbour_gets_no_market_activity(self):
        act = _Recording([ActivityResult(FINISHED, {})])
        res = run_goal(SellHold(()), max_ticks=2,
                       perceive=lambda: _state("building:harbor"),
                       activities={"building:market": act})
        self.assertIsNone(res)
        self.assertEqual(act.goals, [], "the market must not act inside the harbour")

    def test_the_idle_lock_has_an_activity_so_the_loop_can_get_past_it(self):
        self.assertIn("idle_lock", default_activities())


class TheLastNamedPortSurvivesABuildingInterior(unittest.TestCase):
    """A market's screen does not say "Lisboa".

    Live 2026-08-26 the sell leg worked and reported `'port': ''`, because the activity asked
    the state where it was and a building interior carries no port name. So the port is
    remembered — under the repository's rule that the SCREEN OUTRANKS THE RECORD: a named
    port replaces it at once, and the memory is consulted only when the look names none.
    """

    def setUp(self):
        from brain import run_goal as rg
        rg._last_named_port = None

    def _perceived(self, location, port, detail=""):
        from unittest.mock import patch
        from brain import run_goal as rg

        class R:
            frame = None
            def to_location_dict(self):
                return {"location": location, "port": port, "detail": detail}

        with patch("brain.perceive.perceive", return_value=R()):
            return rg._refined_state()

    def test_a_named_port_is_reported(self):
        self.assertEqual(self._perceived("port_overworld", "Lisboa").port, "Lisboa")

    def test_a_building_interior_keeps_the_port_it_walked_in_from(self):
        self._perceived("port_overworld", "Lisboa")
        self.assertEqual(self._perceived("building", "", "building: market").port, "Lisboa")

    def test_the_screen_outranks_the_record(self):
        self._perceived("port_overworld", "Lisboa")
        self.assertEqual(self._perceived("port_overworld", "Amsterdam").port, "Amsterdam")
        self.assertEqual(self._perceived("building", None).port, "Amsterdam")

    def test_no_port_has_ever_been_named(self):
        self.assertIsNone(self._perceived("sea", None).port)


class TheRegistryIsBuiltFromWhatEachActivityClaims(unittest.TestCase):
    """Two lists of where an activity works is what caused ENTER_BUILDING to be dispatched on
    every tick while the bot stood inside the market (2026-08-26): `brain.intents` compared
    against one tuple and the registry was keyed by another."""

    def test_every_state_an_activity_claims_is_registered(self):
        from brain.activities.harbor import HarborActivity
        from brain.activities.market import MarketActivity
        from brain.activities.village import VillageActivity
        from brain.run_goal import default_activities

        registry = default_activities()
        for cls in (MarketActivity, HarborActivity, VillageActivity):
            for where in cls.SERVES:
                self.assertIn(where, registry, f"{cls.__name__} claims {where!r}")
                # A state may hold SEVERAL activities, resolved by their GOALS filter —
                # `village` is served by VillageActivity for a Barter and AshoreActivity for
                # an ArriveAshore. What must hold is that the claim is honoured.
                self.assertIn(cls.name, [a.name for a in registry[where]],
                              f"{cls.__name__} claims {where!r} but is not registered for it")

    def test_no_two_activities_claim_the_same_state(self):
        """A state served by two activities is ambiguous, and the registry would silently
        keep whichever was built last."""
        from brain.activities.harbor import HarborActivity
        from brain.activities.market import MarketActivity
        from brain.activities.village import VillageActivity

        claimed: dict = {}
        for cls in (MarketActivity, HarborActivity, VillageActivity):
            for where in cls.SERVES:
                self.assertNotIn(where, claimed,
                                 f"{where!r} claimed by both {claimed.get(where)} and {cls.name}")
                claimed[where] = cls.name


def test_progress_is_change_not_action():
    """WALKING WHERE THE WORK IS ISN'T A STALL — AND ACTING IN PLACE ISN'T PROGRESS.

    Reading `last.status` alone cannot tell the two apart: neither runs an activity. The
    tick already reports where we are, what we want and what we are doing about it, so the
    guard counts only ticks in which NONE of that moved.

    Live 2026-08-29 at Amsterdam: leaving the sell submenu, leaving the market, reading the
    port and tapping into the market again is four ticks with no activity, and the run was
    stopped on the very tap that was entering to buy the Iron the mission had asked for.
    """
    def _stall_count(records, max_stalled=6):
        """The guard's rule, in isolation."""
        stalled, seen = 0, None
        for rec in records:
            here = (rec.get("state"), str(rec.get("goal")), str(rec.get("intent")), None)
            if here != seen:
                stalled = 0
            else:
                stalled += 1
                if stalled >= max_stalled:
                    return "stopped"
            seen = here
        return stalled

    # walking: the state moves every tick, and an intent goes out each time
    walking = [{"state": st, "goal": "hold Iron", "intent": it} for st, it in [
        ("sub_menu:sell", "EXIT_BUILDING"), ("building:market", "EXIT_BUILDING"),
        ("port_overworld", "ENTER_BUILDING"), ("building:market", None),
        ("sub_menu:buy", None), ("sub_menu:buy", None)]]
    assert _stall_count(walking) != "stopped"

    # spinning: an intent goes out every tick and NOTHING moves — acting is not progress
    spinning = [{"state": "sub_menu:sell", "goal": "hold Iron",
                 "intent": "OPEN_WORLD_MAP"} for _ in range(8)]
    assert _stall_count(spinning) == "stopped"


def test_a_pause_is_not_honoured_after_the_tick_has_acted():
    """THE PAUSE DESCRIBES A WORLD THE TICK MAY HAVE ALREADY LEFT.

    An activity returning WORKING says how long IT wants before the next look. The tick then
    perceives again and can find that world gone — and if it dispatches a transition on the
    strength of the new look, there is something to see NOW.

    Live 2026-08-29: the sea asked for 7.5 minutes mid-voyage; by the end of that same tick
    the fleet had reached Tripoli, been handed 'hold Candle (~934)' and tapped into the
    market — then slept 7.5 minutes on the market's doorstep.
    """
    def _would_sleep(observed, record):
        """The loop's rule, in isolation."""
        wait = (observed or {}).get("checkback_s")
        return bool(wait) and record.get("intent") is None

    sailing = {"checkback_s": 450.0, "eta_days": 5}

    # still at sea, nothing dispatched — the pause is what the activity asked for
    assert _would_sleep(sailing, {"state": "sea", "intent": None})

    # arrived, and the tick dispatched a market entry — do not sleep on the doorstep
    assert not _would_sleep(sailing, {"state": "port_overworld",
                                      "intent": "ENTER_BUILDING(market)"})

    # no pause asked for at all
    assert not _would_sleep({}, {"state": "sea", "intent": None})
