"""The dispatcher starts activities and converts goals into intents.

The failure this exists to prevent, measured live on 2026-08-25: one call to
`depart_from_port_via_world_map` held the goal loop for SIX MINUTES. Inside it the primitive
decided the departure had failed, walked to the harbour, hunted a Depart button, tapped Supply
Departure and re-selected the destination three times — while perception correctly reported
`port_overworld / Amsterdam` on every tick to nobody who could act on it. The fleet had already
ARRIVED. There was no boundary at which anyone could notice.

So: a transition happens in exactly ONE place — when the dispatcher dispatches an intent — and
the task runner is consulted at every activity boundary.
"""

from __future__ import annotations

import types
import unittest

from brain.dispatcher import (Activity, ActivityResult, Dispatcher, Intent,
                              BLOCKED, FINISHED, UNRECOGNISED)


def _state(where, **kw):
    return types.SimpleNamespace(state=where, **kw)


class _Recording:
    """An activity that reports a scripted result and records what it was asked to do."""

    def __init__(self, name, results):
        self.name = name
        self._results = list(results)
        self.goals = []

    def work(self, goal, state):
        self.goals.append(goal)
        return self._results.pop(0) if len(self._results) > 1 else self._results[0]


class TheLoop(unittest.TestCase):

    def _build(self, *, where="building:market", results=None, goals=None):
        # The activity is always registered for the market; `where` is what is PERCEIVED, so a
        # test can perceive a screen no activity claims (e.g. "loading") to exercise the
        # unrecognised path.
        market = _Recording("market", results or [ActivityResult(FINISHED, {"Iron": 411})])
        asked, dispatched = [], []
        goal_iter = iter(goals or ["hold(Iron, 411)", "sail_to(Svear)", None])

        def next_goal(result, state):
            asked.append(result)
            return next(goal_iter, None)

        def to_intent(goal, state):
            return Intent("SAIL_TO", {"destination": "Svear"}) if goal == "sail_to(Svear)" else None

        d = Dispatcher(perceive=lambda: _state(where),
                       activities={"building:market": market},
                       next_goal=next_goal,
                       to_intent=to_intent,
                       dispatch=dispatched.append)
        return d, market, asked, dispatched

    def test_the_activity_is_given_the_goal(self):
        d, market, _asked, _dis = self._build()
        d.step()
        self.assertEqual(market.goals, ["hold(Iron, 411)"])

    def test_the_result_goes_up_to_the_task_runner(self):
        """`observed` is what perception saw, so the task runner never asks the screen."""
        d, _m, asked, _dis = self._build(
            results=[ActivityResult(FINISHED, {"Iron": 411, "cargo": "1307/2873"})])
        d.step()
        finished = [a for a in asked if a is not None]
        self.assertTrue(finished)
        self.assertEqual(dict(finished[-1].observed), {"Iron": 411, "cargo": "1307/2873"})

    def test_the_next_goal_becomes_an_intent(self):
        d, _m, _asked, dispatched = self._build()
        d.step()
        self.assertEqual([str(i) for i in dispatched], ["SAIL_TO(destination='Svear')"])

    def test_the_activity_never_dispatches(self):
        """A transition happens in ONE place. An activity that moves the bot is the bug."""
        d, market, _asked, dispatched = self._build(
            goals=["hold(Iron, 411)", None])
        d.step()
        self.assertEqual(dispatched, [], "nothing should move when the task runner says stop")

    def test_an_unrecognised_screen_ends_the_activity_and_asks(self):
        """A confused activity does not recover — it hands back and the task runner decides."""
        d, _m, asked, _dis = self._build(where="loading")
        rec = d.step()
        self.assertEqual(rec["result"].status, UNRECOGNISED)
        self.assertTrue(asked, "the task runner must be consulted, not a recovery call")

    def test_a_blocked_activity_still_reports_where_it_is(self):
        d, _m, asked, _dis = self._build(
            results=[ActivityResult(BLOCKED, {"port": "Amsterdam"}, "market shelf empty")])
        d.step()
        blocked = [a for a in asked if a is not None][-1]
        self.assertEqual(blocked.status, BLOCKED)
        self.assertEqual(blocked.observed["port"], "Amsterdam")

    def test_nothing_to_do_is_a_valid_answer(self):
        d, _m, _asked, dispatched = self._build(goals=[None])
        rec = d.step()
        self.assertIsNone(rec["goal"])
        self.assertEqual(dispatched, [])


class AnIntentCarriesItsPurpose(unittest.TestCase):
    """The world map is entered to SET SAIL or to make a REMOTE CHECK, and behaves
    differently for each — so the purpose travels with the request."""

    def test_the_purpose_is_part_of_the_intent(self):
        i = Intent("OPEN_WORLD_MAP", {"purpose": "REMOTE_CHECK", "subject": "Svear Village"})
        self.assertEqual(i.extras["purpose"], "REMOTE_CHECK")
        self.assertIn("purpose='REMOTE_CHECK'", str(i))

    def test_a_filter_travels_too(self):
        """The map has nine lenses; which one is wanted is the caller's business."""
        i = Intent("OPEN_WORLD_MAP", {"purpose": "INVEST_SURVEY", "filter": "Invest"})
        self.assertEqual(i.extras["filter"], "Invest")


class ALostActivityHandsBack(unittest.TestCase):
    """An activity that cannot recognise the screen is LOST, and being lost is not something it
    can fix — re-perceiving in place tells you where you are, it cannot change where you are.
    So it ends, and the dispatcher does the recovering: perceive, transition, refresh the data
    the wrong belief cached, and only THEN ask the task runner for a goal.
    """

    def _build(self, *, lost=True):
        order = []
        states = iter([_state("sub_menu:purchase"), _state("port_overworld"),
                       _state("port_overworld")])

        def perceive():
            order.append("perceive")
            return next(states, _state("port_overworld"))

        def reorient(state):
            order.append("reorient")
            return _state("port_overworld")

        def refresh(state):
            order.append("refresh")

        goals = iter(["buy(Iron, 500)"])          # a goal to run with, then nothing more

        def next_goal(result, state):
            order.append("ask_task_runner")
            return next(goals, None)

        market = _Recording("market", [ActivityResult(
            UNRECOGNISED if lost else FINISHED, {"where": "?"})])
        d = Dispatcher(perceive=perceive,
                       activities={"sub_menu:purchase": market},
                       next_goal=next_goal,
                       to_intent=lambda g, s: None,
                       dispatch=lambda i: order.append("dispatch"),
                       reorient=reorient, refresh=refresh)
        return d, order

    def test_the_order_is_perceive_transition_refresh_then_ask(self):
        d, order = self._build()
        d.step()
        # the tick's own look, then the goal, then the activity gets lost — and only then
        # does the recovery run, in the order the rule names.
        self.assertEqual(order, ["perceive", "ask_task_runner",          # tick: where, what
                                 "perceive", "reorient", "refresh",      # regaining bearings
                                 "ask_task_runner"])                     # ...and only then

    def test_the_task_runner_is_asked_last_and_only_for_a_goal(self):
        """It is not told to recover and not consulted about how. Recovery is a transition,
        and transitions are the dispatcher's."""
        d, order = self._build()
        d.step()
        self.assertEqual(order[-1], "ask_task_runner")
        self.assertLess(order.index("reorient"), len(order) - 1)

    def test_the_refresh_is_not_skipped(self):
        """The step that is easy to leave out. Getting lost usually means a belief was ALREADY
        wrong, so what was cached on it is suspect — recovering position without dropping the
        stale belief re-enters the same mistake from a tidier starting point."""
        d, order = self._build()
        d.step()
        self.assertIn("refresh", order)
        self.assertLess(order.index("reorient"), order.index("refresh"))

    def test_a_finished_activity_does_not_trigger_any_of_it(self):
        d, order = self._build(lost=False)
        d.step()
        self.assertNotIn("reorient", order)
        self.assertNotIn("refresh", order)

    def test_it_works_without_a_reorient_or_refresh_wired_up(self):
        """Both are optional — a dispatcher with neither still ends the lost activity and asks."""
        asked = []
        d = Dispatcher(perceive=lambda: _state("loading"),
                       activities={},
                       next_goal=lambda r, s: asked.append(r) or None,
                       to_intent=lambda g, s: None,
                       dispatch=lambda i: None)
        rec = d.step()
        self.assertEqual(rec["result"].status, UNRECOGNISED)
        self.assertTrue(asked)


class AnObstructionIsNotAWorld(unittest.TestCase):
    """The daily news, a promo, an announcement — each is a film laid OVER a world the bot is
    STILL IN. None is somewhere work can happen, so none is an activity.

    The test for membership is whether the world underneath survives. The idle lock does not
    belong here (corrected 2026-08-26): it replaces the entire screen, so it is a genuine
    STATE with a known exit action and an unknown destination. See docs/architecture_DRAFT.md,
    "Two kinds of unknown".

    The cost of handling neither, live 2026-08-26 at Svear Village: the game dropped into
    "Slide up to unlock" while the barter panel was opening. Perception named it correctly on
    every single look — `learned_on_standby_at_sea_slide_up_to_unlock: 1/1 signals matched` —
    and nothing acted on the name. The mission re-perceived a screen it could not act through
    for 34 minutes across two attempts, then aborted with 445 Iron, 146 Matchlock Gun and 438
    Candle aboard, tied up at the village it had sailed to.
    """

    def _build(self, *, obstructed_first):
        seen, cleared = [], []
        first = "locked" if obstructed_first else "village"
        states = iter([_state(first), _state("village")])

        def perceive():
            s = next(states, _state("village"))
            seen.append(s.state)
            return s

        def unblock(state):
            if state.state == "locked":
                cleared.append(state.state)
                return True
            return False

        village = _Recording("village", [ActivityResult(FINISHED, {"rounds": 2})])
        d = Dispatcher(perceive=perceive,
                       activities={"village": village},
                       next_goal=lambda r, s: "barter(Birch Tree)" if r is None else None,
                       to_intent=lambda g, s: None,
                       dispatch=lambda i: None,
                       unblock=unblock if obstructed_first else None)
        return d, seen, cleared, village

    def test_the_lock_is_cleared_before_an_activity_is_resolved(self):
        d, seen, cleared, village = self._build(obstructed_first=True)
        d.step()
        self.assertEqual(cleared, ["locked"], "the obstruction must be cleared, not dispatched on")
        self.assertEqual(seen[:2], ["locked", "village"],
                         "cleared, then re-perceived, and only then is an activity resolved")

    def test_the_activity_underneath_then_runs_normally(self):
        """The world was never lost — the lock did not move the fleet out of the village."""
        d, _seen, _cleared, village = self._build(obstructed_first=True)
        d.step()
        self.assertEqual(village.goals, ["barter(Birch Tree)"])

    def test_a_clean_screen_pays_for_no_extra_look_before_the_activity(self):
        """Exactly two perceives per tick: the one that resolves the activity, and the
        mandatory one after it finishes. A clean screen must not add a third."""
        d, seen, cleared, _v = self._build(obstructed_first=False)
        d.step()
        self.assertEqual(cleared, [])
        self.assertEqual(len(seen), 2, f"expected pre+post only, got {seen}")

    def test_a_screen_is_never_routed_by_what_it_SAYS(self):
        """The lock reads 'On Standby at Sea' and names the server 'Atlantic Ocean' — while
        the fleet stands in a village. That wording describes the standby MODE, not the
        fleet's position, so routing on it hands a village problem to the sea. It is also why
        the lock is ONE state rather than sea/port variants: there is nothing trustworthy for
        a parameter to carry."""
        sea = _Recording("sea", [ActivityResult(FINISHED)])
        village = _Recording("village", [ActivityResult(FINISHED)])
        d = Dispatcher(perceive=lambda: _state("village"),
                       activities={"sea": sea, "village": village},
                       next_goal=lambda r, s: "barter(Birch Tree)" if r is None else None,
                       to_intent=lambda g, s: None,
                       dispatch=lambda i: None,
                       unblock=lambda s: False)
        d.step()
        self.assertEqual(sea.goals, [], "the sea activity must not see this")
        self.assertEqual(village.goals, ["barter(Birch Tree)"])


class EveryActivityEndsWithReperceiveThenAsk(unittest.TestCase):
    """Re-perceive, then consult the task runner — the two general steps after EVERY activity
    (user, 2026-08-26). Not a special case for any particular state.

    The idle lock is what makes this obvious. Its whole repertoire is one action, swipe up, and
    when that returns the activity has FINISHED with nobody knowing where it landed. But
    nothing about that is lock-specific, which is why the lock needs no wiring into any path
    that might meet it — the barter code never has to learn what a lock is. Proposing to "wire
    the wake into the barter path" was the per-path habit this architecture exists to delete.
    """

    def _run(self, *, ends_at):
        asked, states = [], iter([_state("idle_lock"), _state(ends_at)])
        goals = iter(["unlock"])          # a goal to run the lock with, then nothing more
        lock = _Recording("idle_lock", [ActivityResult(FINISHED, {"woke": True})])

        def next_goal(result, state):
            asked.append(getattr(state, "state", None))
            return next(goals, None)

        d = Dispatcher(perceive=lambda: next(states, _state(ends_at)),
                       activities={"idle_lock": lock, ends_at: _Recording(ends_at, [])},
                       next_goal=next_goal,
                       to_intent=lambda g, s: None,
                       dispatch=lambda i: None)
        d.step()
        return asked, d

    def test_the_task_runner_is_asked_about_where_the_bot_IS(self):
        """Asking with the pre-activity state would tell the task runner "you are at the idle
        lock" about a bot that had just woken into a village."""
        asked, _d = self._run(ends_at="village")
        self.assertEqual(asked[-1], "village")
        self.assertNotIn("idle_lock", asked[-1:])

    def test_the_destination_is_discovered_not_remembered(self):
        """The swipe returns you to wherever you were, and the bot must not assume that. The
        same lock activity ends somewhere different here, and nothing about it changed."""
        for landed in ("village", "port_overworld", "sea"):
            with self.subTest(landed=landed):
                asked, _d = self._run(ends_at=landed)
                self.assertEqual(asked[-1], landed)

    def test_the_fresh_look_carries_into_the_next_tick(self):
        """The post-activity look IS the next tick's starting point — perceiving again would
        pay twice for one truth."""
        _asked, d = self._run(ends_at="village")
        self.assertIsNotNone(d._fresh)
        self.assertEqual(d._fresh.state, "village")


class TheTaskRunnerNeedsNoUIKnowledge(unittest.TestCase):
    """It is TOLD the state; it must never need to understand it (user, 2026-08-26).

    The task runner decides from task progress — which materials are aboard, which legs are
    done — not from which screen is showing. A task runner that branches on `sub_menu:purchase`
    has taken on knowledge that must then be kept in sync with every UI change the game ships.

    The practical test: could this task runner still decide if the UI were replaced wholesale?
    """

    def _mission(self):
        """A task runner that reads ONLY observed data. It never looks at the state at all."""
        need = {"Iron": 242, "Matchlock Gun": 122}
        hold = {}
        seen_states = []

        def next_goal(result, state):
            seen_states.append(getattr(state, "state", None))   # recorded, never consulted
            if result is not None:
                hold.update(result.observed)
            missing = [m for m, q in need.items() if hold.get(m, 0) < q]
            return f"gather {missing[0]}" if missing else None

        return next_goal, hold, seen_states

    def _tick_through(self, states, results):
        next_goal, hold, seen = self._mission()
        it_states, it_results = iter(states), iter(results)
        activity = _Recording("any", [])
        activity.work = lambda goal, state: next(it_results)
        d = Dispatcher(perceive=lambda: _state(next(it_states, states[-1])),
                       activities={s: activity for s in set(states)},
                       next_goal=next_goal,
                       to_intent=lambda g, s: Intent("GO", {"goal": g}) if g else None,
                       dispatch=lambda i: None)
        for _ in range(len(results)):
            d.step()
        return hold, seen, d

    def test_it_drives_the_mission_reading_only_observed_data(self):
        hold, _seen, d = self._tick_through(
            states=["port_overworld", "port_overworld", "port_overworld"],
            results=[ActivityResult(FINISHED, {"Iron": 445}),
                     ActivityResult(FINISHED, {"Matchlock Gun": 146})])
        self.assertEqual(hold, {"Iron": 445, "Matchlock Gun": 146})
        self.assertIsNone(d.goal, "with the hold covered, 'nothing' is the right answer")

    def test_the_same_decisions_hold_under_a_completely_different_UI(self):
        """Replace every state name with something the task runner has never heard of. If the
        boundary is clean, the mission is unaffected."""
        hold, seen, d = self._tick_through(
            states=["screen_A", "screen_B", "screen_C"],
            results=[ActivityResult(FINISHED, {"Iron": 445}),
                     ActivityResult(FINISHED, {"Matchlock Gun": 146})])
        self.assertEqual(hold, {"Iron": 445, "Matchlock Gun": 146})
        self.assertIsNone(d.goal)
        self.assertNotIn("port_overworld", seen, "the fixture really did change the UI")

    def test_nothing_remains_a_valid_answer(self):
        """A task runner with no next step says so, and the dispatcher invents no work."""
        dispatched = []
        d = Dispatcher(perceive=lambda: _state("port_overworld"),
                       activities={"port_overworld": _Recording("p", [ActivityResult(FINISHED)])},
                       next_goal=lambda r, s: None,
                       to_intent=lambda g, s: None,
                       dispatch=dispatched.append)
        d.step()
        self.assertEqual(dispatched, [])


class ATransitionIsNotRepeatedWhileItLands(unittest.TestCase):
    """Entering a building takes a walk across the port, so the state stays `port_overworld`
    for several ticks after the tap — and asking the task runner again produces the same
    intent every time.

    Live 2026-08-26 at Lisboa that dispatched ENTER_BUILDING on EVERY tick. Re-tapping
    mid-walk is the hazard `navigate_to_building`'s TAP_RETRY_COOLDOWN exists for: the queued
    tap can land INSIDE the building once the scene loads, on whatever is under it. Only
    `tap_building_entry` refusing to tap a screen that is not the building list contained it.

    This is NOT a wait. The tick still perceives, still asks, still works — it just does not
    repeat an action whose effect has not been observed yet.

    WHAT CHANGED 2026-09-02: the mid-walk hazard is now held off by the intent's own SETTLE
    (`_INTENT_SETTLE_S`), which is waited out BEFORE the next look. So an unchanged screen no
    longer means "the walk is still going" — it means the walk has had its 20 seconds and
    started nothing, i.e. the tap never landed. Live at Faro that day the market tap did not
    register and this rule, applied past the settle, waited three times over ninety seconds
    without tapping again and failed the mission on `trim_before_gather`.

    So: never repeated WHILE it lands, exactly ONCE after its settle has passed, and never
    again after that — repeating per tick would be the 2026-08-26 failure at 20s spacing.
    """

    def _dispatcher(self, states):
        seq = list(states)
        sent = []
        d = Dispatcher(
            perceive=lambda: _state(seq.pop(0) if len(seq) > 1 else seq[0]),
            activities={},
            next_goal=lambda r, s: "enter the market",
            to_intent=lambda g, s: Intent("ENTER_BUILDING", {"name": "market"}) if g else None,
            dispatch=sent.append)
        return d, sent

    def test_it_is_not_repeated_while_the_walk_is_still_landing(self):
        """The original hazard: two taps in flight, the second landing INSIDE the building."""
        d, sent = self._dispatcher(["port_overworld"])
        d.step()
        self.assertEqual(len(sent), 1)
        # ...and the settle is what holds the second one off, not luck.
        self.assertGreater(d._wake_at, 0, "the walk must be waited out before looking again")

    def test_it_is_sent_once_more_after_the_settle_has_passed_and_nothing_moved(self):
        """A tap that never registered. Live at Faro 2026-09-02."""
        d, sent = self._dispatcher(["port_overworld"])
        for _ in range(4):
            d.step()
        self.assertEqual(len(sent), 2,
                         f"one tap, then one retry once the walk had its time — got {len(sent)}")

    def test_it_is_not_sent_a_third_time(self):
        """Retrying per tick would be the 2026-08-26 Lisboa failure at 20-second spacing. The
        stall guard ends it with a diagnosis instead."""
        d, sent = self._dispatcher(["port_overworld"])
        for _ in range(8):
            d.step()
        self.assertEqual(len(sent), 2, f"dispatched {len(sent)} times on one unchanged screen")

    def test_it_is_sent_again_once_the_screen_changes(self):
        """A changed screen means the last one landed; the same intent may be needed again.

        Four here, not three: one per genuine change of screen (ticks 1, 3, 4) PLUS the single
        post-settle retry on tick 2, where the screen had not moved. The retry is the point of
        the change above and is counted deliberately rather than tolerated.

        EACH SCREEN APPEARS TWICE because a tick that dispatches an intent now LOOKS TWICE:
        once to decide, and once more on the following tick because the intent spent the first
        look (see `_advance`, 2026-09-04 — the pre-Back frame that tapped a dismissed notice's
        OK and opened a modal nothing had asked for). This fixture feeds one screen per LOOK,
        so preserving one screen per TICK means doubling it. The behaviour asserted is
        unchanged."""
        d, sent = self._dispatcher(["port_overworld", "port_overworld",
                                    "port_overworld", "port_overworld",
                                    "sea", "sea",
                                    "port_overworld", "port_overworld"])
        for _ in range(4):
            d.step()
        self.assertEqual(len(sent), 4, "3 genuine changes + 1 retry of the tap that did not land")

    def test_a_different_intent_is_never_suppressed(self):
        sent = []
        goals = iter(["market", "market", "harbor"])
        d = Dispatcher(
            perceive=lambda: _state("port_overworld"), activities={},
            next_goal=lambda r, s: next(goals, "harbor"),
            to_intent=lambda g, s: Intent("ENTER_BUILDING", {"name": g}),
            dispatch=sent.append)
        for _ in range(3):
            d.step()
        names = [i.extras["name"] for i in sent]
        self.assertIn("harbor", names, "a new destination must go out immediately")
