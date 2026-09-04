"""The sea activity observes and answers one question: is the ship still moving?

"SeaActivity should only do one thing for now, that is observe, and check if the ship is
moving, if not, it should try to launch the world map to set the destination... we do not
need to unlock on the sea, as long as among certain ticks the ETA goes down" (user,
2026-08-28).
"""
import unittest

from brain.activities.sea import ArriveAshore, SeaActivity
from brain.activities.world_map import ChooseDestination
from brain.dispatcher import ActivityResult, BLOCKED, FINISHED, WORKING
from brain.voyage_runner import ADRIFT, ARRIVED, VoyageRunner


def _sea(etas, supply=9, speeds=None):
    """A SeaActivity fed scripted HUD readings. None = unreadable, for either field.

    `speeds=None` means the speed tile cannot be read at all, which is what puts these tests
    on the ETA fallback — the path they were written for.
    """
    seq = list(etas)
    sp = list(speeds) if speeds is not None else [None] * len(etas)
    return SeaActivity(hud_fn=lambda: {"supply_days": supply, "eta_days": seq.pop(0),
                                       "destination": "Lisboa"},
                       speed_fn=lambda: sp.pop(0),
                       checkback_fn=lambda d: 60.0)


def _run(etas, supply=9, speeds=None):
    a, goal = _sea(etas, supply, speeds), ArriveAshore("trip")
    return [a.work(goal, None) for _ in etas]


class TheSpeedAnswersFirst(unittest.TestCase):
    """One reading, where the ETA needs three — and it answers a case the ETA cannot answer
    at all: a SHORT hop, where a 1-day ETA has no finer granularity to be seen falling."""

    def test_any_speed_above_zero_is_under_way(self):
        r = _run([1], speeds=[11.5])[0]
        self.assertEqual(r.status, WORKING)
        self.assertIs(r.observed["moving"], True)
        self.assertEqual(r.observed["speed"], 11.5)

    def test_a_single_zero_is_a_ship_still_gathering_way(self):
        """Not symmetrical with a positive: 0.0 straight out of port is acceleration."""
        first, second = _run([9, 9], speeds=[0.0, 8.2])
        self.assertIsNone(first.observed["moving"])
        self.assertIs(second.observed["moving"], True)

    def test_two_zeros_is_a_ship_that_never_left(self):
        last = _run([9, 9], speeds=[0.0, 0.0])[-1]
        self.assertEqual(last.status, BLOCKED)
        self.assertIs(last.observed["moving"], False)

    def test_an_unreadable_speed_is_not_a_zero(self):
        """0.0 is a real reading and None is the absence of one; treating them alike would
        report every unreadable HUD as a stopped ship."""
        r = _run([9], speeds=[None])[0]
        self.assertEqual(r.status, WORKING)
        self.assertIsNone(r.observed["moving"])

    def test_an_unconfirmed_ship_is_looked_at_again_soon(self):
        """The supply cadence is minutes long — right for a voyage under way, useless for
        noticing one that never began. Three ~6-minute waits to learn nothing happened."""
        unconfirmed = _run([9], speeds=[0.0])[0]
        confirmed = _run([9], speeds=[11.5])[0]
        self.assertLess(unconfirmed.observed["checkback_s"],
                        confirmed.observed["checkback_s"])


class MovingIsEstablishedByTheETA(unittest.TestCase):
    """The fallback, for a HUD whose speed tile cannot be read."""

    def test_a_falling_eta_is_moving(self):
        last = _run([9, 8, 7])[-1]
        self.assertEqual(last.status, WORKING)
        self.assertIs(last.observed["moving"], True)

    def test_an_eta_that_never_moves_is_reported(self):
        """A fleet that never departed sits at sea with a perfectly good HUD."""
        last = _run([9, 9, 9])[-1]
        self.assertEqual(last.status, BLOCKED)
        self.assertIs(last.observed["moving"], False)
        self.assertIn("not moving", last.detail)

    def test_it_does_not_judge_before_it_has_evidence(self):
        """Two readings in, there is nothing to say yet — and saying nothing is WORKING."""
        for r in _run([9, 9])[:2]:
            self.assertEqual(r.status, WORKING)
            self.assertIsNone(r.observed["moving"])


class AnUnreadableEtaIsNotAStoppedShip(unittest.TestCase):
    """The idle lock covers the screen while the voyage continues underneath it. Clearing it
    is the dispatcher's business; this activity never learns a lock exists."""

    def test_gaps_delay_the_verdict_rather_than_manufacture_one(self):
        results = _run([9, None, None, 8])
        self.assertTrue(all(r.status == WORKING for r in results))
        self.assertIs(results[-1].observed["moving"], None)   # only 2 real readings so far

    def test_readings_around_the_gaps_still_decide(self):
        last = _run([9, None, 8, None, 7])[-1]
        self.assertIs(last.observed["moving"], True)

    def test_a_gap_does_not_count_as_a_stopped_ship(self):
        last = _run([9, None, None, None])[-1]
        self.assertEqual(last.status, WORKING, "unreadable is not stopped")


class ItDoesNotDecide(unittest.TestCase):
    def test_low_supply_is_reported_not_blocked_on(self):
        """Whether to press on or turn back needs the destination and the mission."""
        last = _run([9, 8, 7], supply=1)[-1]
        self.assertEqual(last.status, WORKING)
        self.assertEqual(last.observed["supply_days"], 1)

    def test_the_history_belongs_to_the_voyage_not_the_activity(self):
        """One SeaActivity is built and reused, so ETAs kept across goals would compare this
        voyage against the last one."""
        a = _sea([9, 9, 9, 5, 4, 3])
        for _ in range(3):
            a.work(ArriveAshore("first"), None)
        second = [a.work(ArriveAshore("second"), None) for _ in range(3)]
        self.assertIs(second[-1].observed["moving"], True, "the new voyage starts fresh")


class NotMovingAsksForTheCourseAgain(unittest.TestCase):
    def _stalled(self):
        return ActivityResult(BLOCKED, {"moving": False}, detail="at sea but not moving")

    def test_it_asks_the_world_map_to_set_the_destination(self):
        r = VoyageRunner(what="leg", destination="jakarta to london", kind="route")
        r.next_goal(None, None)
        goal = r.next_goal(self._stalled(), None)
        self.assertIsInstance(goal, ChooseDestination)
        self.assertEqual(goal.where, "jakarta to london")
        self.assertEqual(goal.kind, "route")

    def test_it_goes_back_to_watching_after_setting_it(self):
        """Whether the course took is a question for the ETA, not for the tap just made."""
        r = VoyageRunner(what="leg", destination="Lisboa")
        r.next_goal(None, None)
        r.next_goal(self._stalled(), None)
        after = r.next_goal(ActivityResult(FINISHED, {"where": "Lisboa"}), None)
        self.assertIsInstance(after, ArriveAshore)

    def test_it_gives_up_rather_than_re_setting_forever(self):
        """A course that will not take is not fixed by asking a third time."""
        r = VoyageRunner(what="leg", destination="Lisboa")
        r.next_goal(None, None)
        for _ in range(6):
            g = r.next_goal(self._stalled(), None)
            if g is None:
                break
            r.next_goal(ActivityResult(FINISHED, {}), None)
        self.assertEqual(r.status, ADRIFT)
        self.assertIn("did not get under way", r.reason)

    def test_with_no_destination_it_reports_rather_than_guesses(self):
        r = VoyageRunner(what="leg")
        r.next_goal(None, None)
        self.assertIsNone(r.next_goal(self._stalled(), None))
        self.assertEqual(r.status, ADRIFT)
        self.assertIn("no destination is known", r.reason)

    def test_arriving_ends_the_leg(self):
        r = VoyageRunner(what="leg", destination="Lisboa")
        r.next_goal(None, None)
        self.assertIsNone(r.next_goal(ActivityResult(FINISHED, {"port": "Lisboa"}), None))
        self.assertEqual(r.status, ARRIVED)
        self.assertEqual(r.port, "Lisboa")
