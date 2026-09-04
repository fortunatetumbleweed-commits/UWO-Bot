"""Getting somewhere means being THERE. Ashore is not arrived; departed is not arrived.

Live 2026-09-01 the whole cascade ended here. A departure went out with no course set, and:

    12:31:34  [dispatch] harbor <- Depart(destination='Barcelona')
    12:31:56  [sail] Barcelona: the harbour confirmed the departure
    12:31:56  [dispatch] ashore <- ArriveAshore(what='the leg to Barcelona')
    12:31:56  [ashore] sail until ashore (the leg to Barcelona) — 'port_overworld' at TRIPOLI
    12:32:57  [dispatch] transient <- Hold(orders={'Iron': 822, ...})     <- the MARKET order

`ArriveAshore` means "keep going until the fleet is no longer at sea". Dispatched while
standing in the port we started from, it is satisfied on the tick it is asked, so `_absorb`
set ARRIVED with port='Tripoli' and the leg to Barcelona checked itself off before the ship
moved. The mission advanced to the buy, and spent twenty minutes at sea asking to enter a
building.

`SailRunner` already warns about this shape at the top of the class —

    TWO DIFFERENT FACTS, and collapsing them made a leg arrive before it left.
      `_seen_sea`     — the fleet has been OBSERVED at sea. Only this can mean arrival.
      `_departure_ok` — the harbour CONFIRMED a departure.

— and the guard was applied to one arrival path and not to the other.

TWO HOLES, either of which alone stops it:

  * `_absorb` took ArriveAshore/FINISHED as arrival without asking `_seen_sea`;
  * `_VoyageStep` said DONE on the runner's status alone, never comparing where it landed
    with where it was going — although `SailRunner` hands that over explicitly ("which port
    it is belongs to the caller") and records `self.port` for the caller to read.

This is what "done is a question about the world" means for a voyage: the port name is on the
screen, so a fresh look can prove it false.
"""
import unittest
from unittest import mock

from brain.mission_runner import DONE, FAILED, RUNNING, _VoyageStep
from brain.sail_runner import ARRIVED, UNDER_WAY


class _Sail:
    """Stands in for SailRunner: a status, an arrival port, and a goal stream."""

    def __init__(self, status, port, goals=(None,)):
        self.status, self.port, self.reason = status, port, ""
        self._goals = list(goals)

    def next_goal(self, result, state):
        return self._goals.pop(0) if self._goals else None


def _step(status, port, destination="Barcelona", kind="port"):
    step = _VoyageStep(destination=destination, kind=kind)
    step._sail = _Sail(status, port)
    step.next_goal(None, None)
    return step


class AVoyageEndsWhereItWasGoing(unittest.TestCase):
    def test_arriving_at_the_destination_is_done(self):
        self.assertEqual(_step(ARRIVED, "Barcelona").status, DONE)

    def test_the_live_failure_arriving_at_the_port_we_left(self):
        step = _step(ARRIVED, "Tripoli")
        self.assertEqual(step.status, FAILED,
                         "Tripoli is not Barcelona, however the runner reported it")
        self.assertIn("Tripoli", step.reason)
        self.assertIn("Barcelona", step.reason)

    def test_a_name_read_loosely_still_matches(self):
        # OCR drops accents and adds marks; the comparison is a prefix either way.
        self.assertEqual(_step(ARRIVED, "Malé", destination="Male").status, DONE)

    def test_an_unread_port_is_not_a_wrong_port(self):
        """The port reader has come back None before; refusing here strands good voyages."""
        self.assertEqual(_step(ARRIVED, None).status, DONE)

    def test_a_village_leg_is_not_judged_on_a_port_name(self):
        # A village interior reports no port name at all.
        self.assertEqual(_step(ARRIVED, "Tripoli", destination="Svear Village",
                               kind="village").status, DONE)

    def test_a_runner_that_did_not_arrive_still_fails(self):
        self.assertEqual(_step(UNDER_WAY, None).status, FAILED)


class AshoreIsNotArrivedUntilWeHaveBeenAtSea(unittest.TestCase):
    """`_absorb`'s half. The fleet must be OBSERVED at sea before landing counts."""

    def _absorb(self, *, seen_sea):
        from brain.activities.sea import ArriveAshore
        from brain.sail_runner import SailRunner
        r = SailRunner(destination="Barcelona")
        r._seen_sea = seen_sea
        r._pending = ArriveAshore(what="the leg to Barcelona")
        result = mock.Mock(status="finished", observed={"port": "Tripoli"}, detail="")
        return r, r._absorb(result)

    def test_landing_without_ever_sailing_is_not_arrival(self):
        r, _ = self._absorb(seen_sea=False)
        self.assertNotEqual(r.status, ARRIVED,
                            "this is what checked the leg off before the ship moved")

    def test_it_keeps_the_phase_running_rather_than_failing(self):
        """The departure has not landed — ask for it again. The bounds stop a real loop."""
        r, handled = self._absorb(seen_sea=False)
        self.assertFalse(handled)
        self.assertEqual(r.status, UNDER_WAY)

    def test_landing_after_the_sea_is_arrival(self):
        r, handled = self._absorb(seen_sea=True)
        self.assertEqual(r.status, ARRIVED)
        self.assertTrue(handled)
        self.assertEqual(r.port, "Tripoli", "the caller is the one that judges the name")


if __name__ == "__main__":
    unittest.main()
