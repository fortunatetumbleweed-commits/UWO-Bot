"""Entering a building is a WALK, and the world change is the CNN's call.

Live 2026-09-01 at Tripoli the bot tapped 'market' twice, twelve seconds apart:

    22:56:37  tap (2064,539)              the first tap — it worked
    22:56:44  classify → port_overworld   correct: the character is still walking
    22:56:46  dispatching ENTER_BUILDING(market)
    22:56:49  Tapping 'market' @ (2065,540)   the second tap, mid-walk
    frame 30  ...lands on the MARKET screen

The second tap hit nothing only by luck. `navigate_to_building` knew about this and waited —
"a retap issued mid-walk can land INSIDE the destination once the scene loads (on an NPC in
the inn, say)", TAP_RETRY_COOLDOWN = 60.0 — but that path was retired and the patience went
with it. The intent path waits for nothing and says so: "the dispatcher re-perceives after
every activity anyway, so the answer arrives without anybody waiting for it".

TWO THINGS LET THE SECOND TAP OUT.

  1. NOTHING WAITED FOR THE WALK. A transition that moves the PLAYER rather than the screen
     needs time to land. That is not the stale-checkback case `_set_wake_timer` guards: an
     activity's checkback describes the world before the tick acted, while a transition's
     settle describes the transition itself.

  2. THE GUARD ASKED THE WRONG WITNESS. "Has it landed?" was answered by comparing element
     LABELS, and on a port those include other players walking past (`taylorfp`, `mary`,
     `tay`), the announcement ticker (`season 1 admiral of m`), an investment banner, and OCR
     jitter on one unchanged string (`lv93_` vs `lv 93`). Measured: two looks at the same port
     shared 0.50-0.62 of their labels; port vs market shared 0.08-0.10. So "changed" fired on
     a screen nobody had touched.

     The family CNN had it right on every frame — port_overworld 0.9998, then chromed 0.958
     and 0.996. A world change is what it is trained to see, so when it is confident it
     decides, and scattered strings do not overrule it (user, 2026-09-02).
"""
import types
import unittest
from unittest import mock

from brain.dispatcher import (_FAMILY_IS_SURE, _INTENT_SETTLE_S, ActivityResult, Dispatcher,
                              FINISHED, Intent, WORKING)


class AWalkIsWaitedFor(unittest.TestCase):
    def test_entering_a_building_settles(self):
        self.assertGreaterEqual(_INTENT_SETTLE_S.get("ENTER_BUILDING", 0), 15,
                                "the character has to cross the port")

    def test_the_dispatcher_records_the_wait(self):
        d = Dispatcher(perceive=lambda: None, activities={}, next_goal=lambda r, s: None,
                       to_intent=lambda g, s: None, dispatch=lambda i: None)
        d._set_wake_timer(ActivityResult(FINISHED, {}), Intent("ENTER_BUILDING", {}))
        self.assertGreater(d._wake_at, 0, "a tick that starts a walk must wait for it")

    def test_a_transition_that_changes_only_the_screen_does_not_wait(self):
        d = Dispatcher(perceive=lambda: None, activities={}, next_goal=lambda r, s: None,
                       to_intent=lambda g, s: None, dispatch=lambda i: None)
        d._set_wake_timer(ActivityResult(FINISHED, {}), Intent("OPEN_WORLD_MAP", {}))
        self.assertEqual(d._wake_at, 0.0)

    def test_an_activitys_own_checkback_is_still_ignored_after_acting(self):
        """The rule this does not overturn: that pause describes a world already left behind."""
        d = Dispatcher(perceive=lambda: None, activities={}, next_goal=lambda r, s: None,
                       to_intent=lambda g, s: None, dispatch=lambda i: None)
        d._set_wake_timer(ActivityResult(WORKING, {"checkback_s": 450}),
                          Intent("OPEN_WORLD_MAP", {}))
        self.assertEqual(d._wake_at, 0.0, "7.5 minutes on the market's doorstep, 2026-08-29")


def _fam(name, conf):
    return types.SimpleNamespace(family=name, confidence=conf)


class TheWorldIsTheClassifiersCall(unittest.TestCase):
    def _sig(self, fam, labels=()):
        els = [types.SimpleNamespace(label=l) for l in labels]
        state = types.SimpleNamespace(frame=object())
        with mock.patch("vision.family_classifier.classify_family", return_value=fam), \
             mock.patch("vision.omniparser.parse_fast_cached", return_value=els):
            return Dispatcher._screen_signature(state)

    def test_one_world_stays_one_world_though_players_walk_past(self):
        a = self._sig(_fam("port_overworld", 0.9998), ["taylorfp", "lv93_", "market"])
        b = self._sig(_fam("port_overworld", 0.9997), ["mary", "tay", "lv 93", "market"])
        self.assertEqual(a, b, "this is what let the second tap out")

    def test_a_real_world_change_is_seen(self):
        a = self._sig(_fam("port_overworld", 0.9998), ["market"])
        b = self._sig(_fam("chromed", 0.958), ["market"])
        self.assertNotEqual(a, b)

    def test_an_unsure_classifier_falls_back_to_the_labels(self):
        """It can still show a panel opening inside one world, which the family cannot."""
        low = _FAMILY_IS_SURE - 0.2
        a = self._sig(_fam("port_overworld", low), ["market"])
        b = self._sig(_fam("port_overworld", low), ["market", "requested trade goods"])
        self.assertNotEqual(a, b)

    def test_a_classifier_that_raises_falls_back_too(self):
        state = types.SimpleNamespace(frame=object())
        els = [types.SimpleNamespace(label="market")]
        with mock.patch("vision.family_classifier.classify_family",
                        side_effect=RuntimeError("no model")), \
             mock.patch("vision.omniparser.parse_fast_cached", return_value=els):
            self.assertEqual(Dispatcher._screen_signature(state), ("market",))


if __name__ == "__main__":
    unittest.main()


class ADroppedBackIsRetriedToo(unittest.TestCase):
    """EXIT_BUILDING has no settle, so the settle-gated retry never covered it.

    Live 2026-09-02 at Lisboa, the end-to-end attempt: the first Back left the Sell sub-menu
    correctly; the second, from the market top menu, was DROPPED. Five frames read
    'building: market' and no further input was issued for three ticks, because the in-flight
    guard says "letting it land" and only a declared settle unlocked the retry. The mission
    died in the market — 12 seconds after a Back that had worked, and a manual Back cleared
    the same screen instantly afterwards.

    Back is safe to repeat here: the second press lands on the port overworld, where
    `_NEVER_BACK_FROM` stops it going further. That is why it may join the retry set and
    OPEN_WORLD_MAP may not — re-tapping the rail TOGGLES the map shut."""

    def _dispatcher(self, state_name):
        import types
        from brain.dispatcher import Dispatcher, Intent
        sent = []
        d = Dispatcher(
            perceive=lambda: types.SimpleNamespace(state=state_name, port="Lisboa"),
            activities={}, next_goal=lambda r, s: "leave",
            to_intent=lambda g, s: Intent("EXIT_BUILDING", {"from": state_name}),
            dispatch=sent.append)
        return d, sent

    def test_a_back_that_changed_nothing_is_pressed_once_more(self):
        d, sent = self._dispatcher("building:market")
        for _ in range(4):
            d.step()
        self.assertEqual(len(sent), 2, f"pressed {len(sent)}x; want one press + one retry")

    def test_and_never_a_third_time(self):
        d, sent = self._dispatcher("building:market")
        for _ in range(8):
            d.step()
        self.assertEqual(len(sent), 2, "repeating per tick is the 2026-08-26 failure")

    def test_open_world_map_is_NOT_retried(self):
        """The rail toggles: a second tap closes the map it just opened."""
        from brain.dispatcher import _RETRY_ONCE_IF_UNCHANGED
        self.assertNotIn("OPEN_WORLD_MAP", _RETRY_ONCE_IF_UNCHANGED)
        self.assertIn("EXIT_BUILDING", _RETRY_ONCE_IF_UNCHANGED)


class TheRetryReachesAVillageButNotTheDangerousPlaces(unittest.TestCase):
    """Where a second Back LANDS decides whether there may be one.

    From a building it lands on the port overworld and `_NEVER_BACK_FROM` stops it there.
    From a VILLAGE it lands at SEA, which this class used to treat as disqualifying —
    `test_depart_village_for_the_tail` records "the old loop pressed Back up to four times at
    a screen that never moved... that is how the fleet lost the village twice".

    VILLAGE IS ALLOWED NOW (user, 2026-09-04). The cost was misread, not the risk: an
    EXIT_BUILDING at a village is dispatched when leaving IS the goal, so "lost the village"
    and "left the village" are the same event. Live at San a single swallowed Back stranded a
    finished barter — four rounds, 4,455 Bambara Groundnut aboard — because nothing pressed
    again. One extra press, only on an observed-unchanged screen, is not that old loop.

    What stays out is where a second Back means something ELSE: the main menu, where it is
    "Exit Game?" and two presses once left the bot one positive tap from quitting, and the
    overworlds, which are not screens to back out of at all."""

    def _backs(self, state_name):
        import types
        from brain.dispatcher import Dispatcher, Intent
        sent = []
        d = Dispatcher(
            perceive=lambda: types.SimpleNamespace(state=state_name, port=None),
            activities={}, next_goal=lambda r, s: "leave",
            to_intent=lambda g, s: Intent("EXIT_BUILDING", {"from": state_name}),
            dispatch=sent.append)
        for _ in range(6):
            d.step()
        return len(sent)

    def test_a_village_gets_its_one_retry(self):
        self.assertEqual(self._backs("village"), 2,
                         "a swallowed Back at a village strands a finished barter")

    def test_a_building_gets_its_one_retry(self):
        self.assertEqual(self._backs("building:market"), 2)

    def test_the_main_menu_is_still_pressed_only_once(self):
        """Back there is "Exit Game?"."""
        self.assertEqual(self._backs("main_menu"), 1)

    def test_an_overworld_is_still_pressed_only_once(self):
        for state in ("port_overworld", "sea"):
            with self.subTest(state):
                self.assertEqual(self._backs(state), 1)

    def test_the_retry_never_scales_with_the_looks(self):
        """Six looks, two presses. The bound is what separates this from the old loop."""
        self.assertLessEqual(self._backs("village"), 2)

    def test_a_sub_menu_does_too(self):
        self.assertEqual(self._backs("sub_menu:purchase"), 2)
