"""A leg that needs nothing must be closed BEFORE the voyage, not after it.

`_settle_gathers` runs on ARRIVAL, from the market's per-material read, so a leg that needs
nothing is only discovered once the sailing has been spent.

Live 2026-09-06: the fleet finished at Tripoli, sailed to Barcelona, read the hold and found
both its materials already there —

    gather:Barcelona settled — Iron, Matchlock Gun already aboard, bought elsewhere

— then sailed BACK to Tripoli for the Candle. Everything needed to skip it was in the ledger
before the fleet left. This is the predicate half of `docs/the_plan_is_a_checklist.md`: an
item is done when the WORLD says so, and the world had already said so.
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

from brain.mission_runner import MissionRunner


def _leg(ident, orders):
    return types.SimpleNamespace(kind="gather", id=ident, done=False, deps=(),
                                 params={"orders": dict(orders)})


def _runner(legs, aboard):
    m = MissionRunner.__new__(MissionRunner)
    m.subtasks, m.completed = legs, []
    m._materials_aboard = dict(aboard)
    return m


def _settle(m):
    with mock.patch("brain.mission_progress.current", return_value={}):
        m._settle_gathers_already_aboard()


class ALegWhoseMaterialsAreAboardIsClosed(unittest.TestCase):

    def test_no_voyage_is_spent_on_it(self):
        leg = _leg("gather:Barcelona", {"Iron": 822, "Matchlock Gun": 411})
        m = _runner([leg], {"iron": 999, "matchlock gun": 486})
        _settle(m)
        self.assertTrue(leg.done)
        self.assertEqual(m.completed, ["gather:Barcelona"])

    def test_a_leg_still_SHORT_is_left_alone(self):
        leg = _leg("gather:Madeira", {"Raisin": 1755})
        m = _runner([leg], {"raisin": 1100})
        _settle(m)
        self.assertFalse(leg.done)

    def test_AN_UNREAD_MATERIAL_NEVER_CLOSES_A_LEG(self):
        """Unknown is not absent, and skipping a voyage on an unknown strands the material."""
        leg = _leg("gather:Faro", {"Pig": 900})
        m = _runner([leg], {"raisin": 1100})
        _settle(m)
        self.assertFalse(leg.done)

    def test_one_material_short_keeps_the_whole_leg(self):
        leg = _leg("gather:Barcelona", {"Iron": 822, "Matchlock Gun": 411})
        m = _runner([leg], {"iron": 999, "matchlock gun": 100})
        _settle(m)
        self.assertFalse(leg.done)

    def test_nothing_read_yet_settles_nothing(self):
        leg = _leg("gather:Faro", {"Pig": 900})
        m = _runner([leg], {})
        _settle(m)
        self.assertFalse(leg.done)

    def test_a_NON_gather_leg_is_never_settled_this_way(self):
        leg = types.SimpleNamespace(kind="barter", id="barter:Hutu", done=False, deps=(),
                                    params={})
        m = _runner([leg], {"iron": 999})
        _settle(m)
        self.assertFalse(leg.done)


if __name__ == "__main__":
    unittest.main()
