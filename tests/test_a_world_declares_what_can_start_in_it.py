"""The world decides what can be started in it — not `to_intent`'s else-branch.

`to_intent` answers two questions: "does this work happen here?" and, if not, "go where it
does". It has no case for **"you cannot get there from here"**, so the second answer is
unconditional:

    if isinstance(goal, MarketActivity.GOALS):
        if where in MarketActivity.SERVES:
            return None
        return Intent("ENTER_BUILDING", {"name": "market", ...})   # ...from ANYWHERE

Live 2026-09-01 the fleet departed Tripoli with no destination set, the mission still wanted
its gather, and that else-branch produced ENTER_BUILDING at sea on every tick.
`tap_building_entry` refused every time — correctly, there is no building list at sea — and a
refusal concludes nothing, so the next tick asked the identical question. In between it read
the MINIMAP's tab strip as the building list's and cycled its four icons, tapping
(1917,165) (2112,165) (2212,166) (2017,165) for minutes.

That is the third world this has happened in, each patched by name where it surfaced:
OPEN_WORLD_MAP at a village (2026-08-29, "no world-map control on 'village'", the run stopped
one leg from done), OPEN_WORLD_MAP from a sub-menu, and now ENTER_BUILDING at sea. One
`if where ==` per world, added as each bites, is what a declaration replaces.

The facts were never missing — they were spread across the places that learned them:
`tap_world_map_control`'s branches say where a globe or minimap exists, `_is_inside` says a
village has neither, `_NEVER_BACK_FROM` says Back would leave the game at an overworld.
"""
import unittest

from brain.dispatcher import ActivityResult, BLOCKED, Dispatcher, FINISHED
from brain.intents import Intent
from brain.run_goal import affordances, default_activities


class _State:
    def __init__(self, where):
        self.state = where
        self.location = where


class _Gather:
    """A market order — served at a market, reachable only from a port overworld."""
    def __str__(self): return "hold Iron, Candle"


class _Sail:
    """Work the sea itself can do."""
    def __str__(self): return "arrive ashore"


def _dispatcher(where, goals, *, to_intent, activities=None):
    """A dispatcher whose task runner hands back `goals` in order."""
    handed, dispatched = [], []
    seq = list(goals)

    def next_goal(result, state):
        handed.append(result)
        return seq.pop(0) if seq else None

    d = Dispatcher(perceive=lambda: _State(where),
                   activities=activities if activities is not None else default_activities(),
                   next_goal=next_goal, to_intent=to_intent,
                   dispatch=dispatched.append)
    return d, handed, dispatched


def _needs(name):
    return lambda goal, state: Intent(name, {"purpose": str(goal)})


class TheDeclarationsMatchWhatTheGameAffords(unittest.TestCase):
    """Each entry is a fact already written down somewhere in the codebase."""

    def setUp(self):
        self.reg = default_activities()

    def test_at_sea_only_the_minimap_opens_the_map(self):
        self.assertEqual(affordances("sea", self.reg), frozenset({"OPEN_WORLD_MAP"}))

    def test_a_port_overworld_has_the_globe_and_the_building_list(self):
        self.assertEqual(affordances("port_overworld", self.reg),
                         frozenset({"OPEN_WORLD_MAP", "ENTER_BUILDING"}))

    def test_a_village_has_neither_globe_nor_building_list(self):
        # `_is_inside`: "no ☰ and NO GLOBE. There is no world map control in a village at
        # all; you leave to the sea and open it from the minimap."
        self.assertEqual(affordances("village", self.reg), frozenset({"EXIT_BUILDING"}))

    def test_inside_a_building_the_only_transition_is_leaving(self):
        for where in ("building:market", "sub_menu:purchase", "building:harbor"):
            self.assertEqual(affordances(where, self.reg), frozenset({"EXIT_BUILDING"}),
                             where)

    def test_the_map_only_closes_itself(self):
        self.assertEqual(affordances("world_map", self.reg), frozenset({"CLOSE_WORLD_MAP"}))

    def test_a_screen_that_ends_by_finishing_starts_nothing(self):
        for where in ("idle_lock", "transient"):
            self.assertEqual(affordances(where, self.reg), frozenset(), where)

    def test_an_undeclared_world_is_unconstrained_not_forbidden(self):
        # `port_map` has no activity, yet `tap_world_map_control` opens the map from it.
        # Silence must never read as a refusal.
        self.assertIsNone(affordances("port_map", self.reg))

    def test_the_bootstrap_claims_nothing_though_it_serves_everything(self):
        from brain.activities.bootstrap import PositionKnownActivity
        self.assertEqual(PositionKnownActivity.CAN_START, ())
        self.assertGreater(len(PositionKnownActivity.SERVES), 10,
                           "one entry here would afford that transition in every world")


class WorkTheWorldCannotStartIsNotDispatched(unittest.TestCase):
    def test_the_live_failure_entering_a_building_at_sea(self):
        d, _handed, dispatched = _dispatcher(
            "sea", [_Gather(), _Gather()], to_intent=_needs("ENTER_BUILDING"))
        d._advance(ActivityResult(FINISHED, {}), _State("sea"))
        self.assertEqual(dispatched, [], "there is no building list at sea")

    def test_the_earlier_failure_opening_the_map_at_a_village(self):
        """A village has no globe — so LEAVE, which is what the code has always prescribed.

        This asserted "dispatches nothing" when the registry could only decline. With routing
        it does the documented thing instead: `_is_inside` says "there is no world map control
        in a village at all; you leave to the sea and open it from the minimap", and
        EXIT_BUILDING is that leaving. What must never happen is OPEN_WORLD_MAP here, which is
        what stopped a run one leg from done on 2026-08-29.
        """
        d, _handed, dispatched = _dispatcher(
            "village", [_Sail(), _Sail()], to_intent=_needs("OPEN_WORLD_MAP"))
        d._advance(ActivityResult(FINISHED, {}), _State("village"))
        names = [i.name for i in dispatched]
        self.assertNotIn("OPEN_WORLD_MAP", names, "a village has no globe")
        self.assertEqual(names, ["EXIT_BUILDING"],
                         "leave to the sea, and the map opens from the minimap there")

    def test_what_the_world_does_afford_is_still_dispatched(self):
        d, _handed, dispatched = _dispatcher(
            "port_overworld", [_Gather()], to_intent=_needs("ENTER_BUILDING"))
        d._advance(ActivityResult(FINISHED, {}), _State("port_overworld"))
        self.assertEqual(len(dispatched), 1, "this is the ordinary path and must not change")

    def test_an_undeclared_world_still_dispatches(self):
        d, _handed, dispatched = _dispatcher(
            "port_map", [_Gather()], to_intent=_needs("OPEN_WORLD_MAP"))
        d._advance(ActivityResult(FINISHED, {}), _State("port_map"))
        self.assertEqual(len(dispatched), 1, "we do not know, so we must not refuse")


class TheTaskIsAskedAgainRatherThanLeftSpinning(unittest.TestCase):
    """A refusal is not an answer. Only the task knows a different order might do."""

    def test_it_is_told_what_the_world_could_not_do(self):
        d, handed, _dis = _dispatcher("sea", [_Gather(), _Sail()],
                                      to_intent=lambda g, s: Intent(
                                          "ENTER_BUILDING" if isinstance(g, _Gather)
                                          else "OPEN_WORLD_MAP", {}))
        d._advance(ActivityResult(FINISHED, {}), _State("sea"))
        refusal = handed[-1]
        self.assertEqual(refusal.status, BLOCKED)
        self.assertEqual(refusal.observed["world"], "sea")
        self.assertEqual(refusal.observed["needed"], "ENTER_BUILDING")
        self.assertEqual(refusal.observed["afforded"], ["OPEN_WORLD_MAP"])

    def test_a_second_order_the_world_can_do_is_dispatched(self):
        d, _handed, dispatched = _dispatcher("sea", [_Gather(), _Sail()],
                                             to_intent=lambda g, s: Intent(
                                                 "ENTER_BUILDING" if isinstance(g, _Gather)
                                                 else "OPEN_WORLD_MAP", {}))
        d._advance(ActivityResult(FINISHED, {}), _State("sea"))
        self.assertEqual([i.name for i in dispatched], ["OPEN_WORLD_MAP"])

    def test_asking_twice_is_the_limit(self):
        """Two re-asks would be a loop in the method that exists to remove one."""
        d, handed, dispatched = _dispatcher(
            "sea", [_Gather(), _Gather(), _Gather()], to_intent=_needs("ENTER_BUILDING"))
        d._advance(ActivityResult(FINISHED, {}), _State("sea"))
        self.assertEqual(dispatched, [])
        self.assertEqual(len(handed), 2, "asked once, then once more, then stopped")

    def test_a_task_with_nothing_else_to_offer_dispatches_nothing(self):
        d, _handed, dispatched = _dispatcher("sea", [_Gather()],
                                             to_intent=_needs("ENTER_BUILDING"))
        d._advance(ActivityResult(FINISHED, {}), _State("sea"))
        self.assertEqual(dispatched, [])


if __name__ == "__main__":
    unittest.main()


class TheTwoCapabilityQuESTIONSMustNotContradict(unittest.TestCase):
    """The dispatcher asks one question about a world; the mission asks another.

    `affordances` — can this world START this transition? (the dispatcher)
    `port_is_underfoot` — is a market at most a door away? (the mission)

    They were two hand-written lists, `CAN_START` and `mission_runner._NO_MARKET`, free to
    drift. `port_is_underfoot` derives its answer from the same `SERVES` declarations, and
    these tests pin the relationship so a future edit cannot make them disagree quietly.

    They are still two questions, and deliberately so: `CAN_START` says what a world can
    start, never where that LANDS. From a building you step out and into another, so a market
    is two transitions away; from a village you also step out — and land at SEA. Same
    declared capability, opposite answer. Unifying them needs the destination of each
    transition, which is `docs/intent_graph.md` and still open.
    """

    def setUp(self):
        from brain.run_goal import port_is_underfoot
        self.reg = default_activities()
        self.underfoot = port_is_underfoot
        self.worlds = sorted(self.reg)

    def test_a_world_with_a_building_list_always_has_a_market_near(self):
        for w in self.worlds:
            can = affordances(w, self.reg) or frozenset()
            if "ENTER_BUILDING" in can:
                self.assertIsNot(self.underfoot(w), False,
                                 f"{w} offers a building list but is judged to have no market")

    def test_a_world_with_no_market_near_never_offers_a_building_list(self):
        for w in self.worlds:
            if self.underfoot(w) is False:
                can = affordances(w, self.reg) or frozenset()
                self.assertNotIn("ENTER_BUILDING", can,
                                 f"{w} is judged to have no market yet offers a building list")

    def test_a_screen_that_covers_a_world_is_never_judged(self):
        # We cannot see what is underneath, so neither answer may be invented.
        for w in ("world_map", "idle_lock", "transient"):
            self.assertIsNone(self.underfoot(w),
                              f"{w} is drawn over a world; judging it guesses at that world")

    def test_the_worlds_the_old_list_named_still_answer_the_same(self):
        # `_NO_MARKET` was ("sea", "sea_cinematic", "village"). Derivation must not quietly
        # change what those three mean.
        for w in ("sea", "sea_cinematic", "village"):
            self.assertIs(self.underfoot(w), False, w)
        for w in ("port_overworld", "building:market", "building:harbor", "sub_menu:purchase"):
            self.assertIs(self.underfoot(w), True, w)

    def test_the_village_barter_menu_is_inside_a_village_not_a_port(self):
        """The one answer the derivation CHANGES, and it changes it to the right one.

        `sub_menu:barter` is the village's own menu. The old hand-written list did not name
        it, so a market leg picked there was allowed and would have gone looking for a
        building list that a village does not have.
        """
        self.assertIs(self.underfoot("sub_menu:barter"), False)


class AScreenCoveringTheWorldIsNotJudged(unittest.TestCase):
    """A notice, the idle lock and an unnameable chromed screen are drawn OVER a place.

    Asking whether one "affords ENTER_BUILDING" is asking about the wrong thing, and the
    answer is always no. What they need is clearing, and the activity that does it is about to
    run — so refusing is right, but calling it a DEFECT is not, and neither is spending the
    task's retry on it.

    Live 2026-09-01 a full-screen notice appeared mid-departure:

        23:01:33  classify → transient (family-classifier conf=1.00)
        23:01:36  'transient' cannot start ENTER_BUILDING (it affords nothing)
        23:01:36  [sail] departing for 'Barcelona' (2/2)     <- one of TWO attempts, gone
        23:01:36  DEFECT: depart for Barcelona needs ENTER_BUILDING ...
        23:01:36  [transient] tapping the notice away @ (432,172)

    The notice cleared on the very next line. The same shape as the typing budget spent on a
    screen with no search box: a bound consumed by a tick where nothing could have worked.

    Derived from `CLEARS_SCREEN`, which these activities already carry.
    """

    def _run(self, where):
        from brain.activities.harbor import Depart
        from brain.intents import to_intent
        asked, out = [], []
        state = _State(where)
        d = Dispatcher(perceive=lambda: state, activities=default_activities(),
                       next_goal=lambda r, s: (asked.append(1),
                                               Depart(destination="Barcelona"))[1],
                       to_intent=to_intent, dispatch=out.append)
        d._advance(ActivityResult(FINISHED, {}), state)
        return [i.name for i in out], len(asked)

    def test_nothing_is_dispatched_through_a_notice(self):
        self.assertEqual(self._run("transient")[0], [])

    def test_nothing_is_dispatched_through_the_lock(self):
        self.assertEqual(self._run("idle_lock")[0], [])

    def test_the_task_is_not_asked_twice_for_a_screen_that_is_about_to_clear(self):
        """The re-ask is what cost SailRunner a departure attempt."""
        self.assertEqual(self._run("transient")[1], 1)
        self.assertEqual(self._run("idle_lock")[1], 1)

    def test_a_real_world_that_cannot_serve_the_work_still_re_asks(self):
        """The sea IS a world; the work genuinely cannot happen there, so ask for other work."""
        dispatched, asked = self._run("sea")
        self.assertEqual(dispatched, [])
        self.assertEqual(asked, 2)

    def test_and_a_world_that_can_serve_it_is_unaffected(self):
        self.assertEqual(self._run("port_overworld")[0], ["ENTER_BUILDING"])

    def test_the_covering_screens_are_derived_not_listed(self):
        import inspect
        from brain import dispatcher
        src = inspect.getsource(dispatcher.Dispatcher._is_a_covering_screen)
        self.assertIn("CLEARS_SCREEN", src, "the marker these activities already carry")
