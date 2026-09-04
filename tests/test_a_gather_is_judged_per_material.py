# tests/test_a_gather_is_judged_per_material.py
#
# TWO FLAGS, NOT ONE (user, 2026-09-02).
#
# A material is met or it is not. The GATHERING is done only when every material is met.
# Sending each port the whole outstanding list is an OPTIMISATION — Barcelona stocks Iron
# and Matchlock Gun both, so one visit should buy both — which means the list is what to
# BUY and the per-material states are what to JUDGE BY. Conflating them cost two ways:
#
#   * a leg stayed open for a material bought elsewhere (Iron 972/822 at Barcelona, yet
#     `gather:Amsterdam` still pending because Candle was short — recorded as a known gap
#     on `_settle_gathers` since 2026-08-30), and
#
#   * a leg CLOSED with its own material short. Live 2026-09-02: Tripoli returned
#     `met: False` at Candle 206/704 because a tap never registered; the leg was marked
#     done, the mission recorded the phase "bartering", the run was killed mid-voyage, and
#     the relaunch read that phase, skipped the check and the plan, and bartered 2 rounds
#     where 6 were planned — never entering a market at all.
#
# The breakdown was always computable. `_goal_met` built it and flattened it to prose.

import unittest

from brain.mission import SubTask
from brain.mission_runner import MissionRunner

COORDS = {"Amsterdam": (10, 10), "Barcelona": (200, 200), "Tripoli": (120, 90),
          "Svear Village": (50, 300), "Lisboa": (150, 20)}


def _gather(port, orders):
    return SubTask(f"gather:{port}", "gather", port,
                   params={"port": port, "orders": dict(orders)})


def _runner(graph):
    return MissionRunner(subtasks=graph, coords=COORDS, good="Birch Tree",
                         village="Svear Village")


def _states(**kw):
    """{'Iron': 'met', 'Candle': 'short'} → the shape the market activity reports."""
    out = {}
    for material, state in kw.items():
        name = material.replace("_", " ")
        out[name] = {"have": None if state == "unknown" else (999 if state == "met" else 1),
                     "want": 500, "state": state}
    return out


class SettlingALegByItsOwnMaterials(unittest.TestCase):
    def test_a_leg_closes_when_what_IT_wanted_came_aboard_elsewhere(self):
        """The known gap, closed. Iron is aboard; Amsterdam was only ever for Iron."""
        ams, tri = _gather("Amsterdam", {"Iron": 822}), _gather("Tripoli", {"Candle": 704})
        r = _runner([ams, tri])
        r._leg = ams
        r._settle_gathers(False, _states(Iron="met", Candle="short"))
        self.assertTrue(ams.done, "Iron is aboard — Amsterdam has nothing left to give")
        self.assertFalse(tri.done, "Candle is still short — Tripoli must still be visited")

    def test_a_leg_stays_open_when_its_own_material_is_short(self):
        """The 2026-09-02 failure. Tripoli sells the Candle; Candle is short."""
        tri = _gather("Tripoli", {"Candle": 704})
        r = _runner([tri])
        r._leg = tri
        r._settle_gathers(False, _states(Candle="short"))
        self.assertFalse(tri.done)

    def test_an_unknown_amount_never_closes_a_leg(self):
        """"I could not read it" must not become "I have enough" — the ledger says unknown
        precisely so someone goes and reads the sell grid."""
        tri = _gather("Tripoli", {"Candle": 704})
        r = _runner([tri])
        r._leg = tri
        r._settle_gathers(False, _states(Candle="unknown"))
        self.assertFalse(tri.done)

    def test_met_still_closes_every_leg_at_once(self):
        """The 2026-08-29 regression: Amsterdam bought all three, Barcelona bought two
        again, and the fleet was heading to Tripoli for a third helping of Candle."""
        legs = [_gather("Amsterdam", {"Iron": 822}), _gather("Barcelona", {"Iron": 822}),
                _gather("Tripoli", {"Candle": 704})]
        r = _runner(legs)
        r._leg = legs[0]
        r._settle_gathers(True, None)
        self.assertTrue(all(t.done for t in legs))

    def test_a_partial_breakdown_settles_only_what_it_covers(self):
        """A leg wanting two materials needs BOTH met, not one."""
        bar = _gather("Barcelona", {"Iron": 822, "Matchlock Gun": 305})
        r = _runner([bar])
        r._leg = bar
        r._settle_gathers(False, _states(Iron="met", Matchlock_Gun="short"))
        self.assertFalse(bar.done)
        r._settle_gathers(False, _states(Iron="met", Matchlock_Gun="met"))
        self.assertTrue(bar.done)


class GatheringIsCompleteWhenEveryMaterialIs(unittest.TestCase):
    def test_it_is_not_complete_while_a_leg_still_wants_something(self):
        r = _runner([_gather("Tripoli", {"Candle": 704})])
        self.assertFalse(r.gathering_is_complete())

    def test_it_is_complete_once_every_gather_leg_is_settled(self):
        tri = _gather("Tripoli", {"Candle": 704})
        r = _runner([tri])
        tri.done = True
        self.assertTrue(r.gathering_is_complete())

    def test_it_is_not_the_same_question_as_no_legs_remaining(self):
        """The distinction the whole change turns on: a leg can be done with its material
        short (a runner that stopped is not a goal achieved), and then 'no legs remain'
        says gathering finished when the hold says otherwise. Asking the MATERIALS is what
        makes the two agree."""
        tri = _gather("Tripoli", {"Candle": 704})
        r = _runner([tri])
        r._leg = tri
        r._settle_gathers(False, _states(Candle="short"))
        self.assertFalse(tri.done)
        self.assertFalse(r.gathering_is_complete())


class TheMarketReportsWhereEachMaterialStands(unittest.TestCase):
    def test_material_states_names_met_short_and_unknown(self):
        from actions.buy_materials import material_states

        class _Ledger:
            def __init__(self, have, unknown=()):
                self._have, self._unknown = have, set(unknown)

            def amount_unknown(self, m):
                return m in self._unknown

            def believed(self, m):
                return self._have.get(m, 0)

        led = _Ledger({"Iron": 972, "Candle": 206}, unknown=["Matchlock Gun"])
        got = material_states(led, {"Iron": 822, "Candle": 704, "Matchlock Gun": 305})
        self.assertEqual(got["Iron"]["state"], "met")
        self.assertEqual(got["Candle"]["state"], "short")
        self.assertEqual(got["Candle"]["have"], 206)
        self.assertEqual(got["Matchlock Gun"]["state"], "unknown")

    def test_the_verdict_agrees_with_the_breakdown(self):
        """`_goal_met` is now the verdict OVER the breakdown, so they cannot disagree —
        which is how a surplus of one material once covered a shortfall of another."""
        from actions.buy_materials import _goal_met, material_states

        class _Ledger:
            def __init__(self, have):
                self._have = have

            def amount_unknown(self, m):
                return False

            def believed(self, m):
                return self._have.get(m, 0)

        goal = {"Iron": 506, "Matchlock Gun": 305}
        led = _Ledger({"Iron": 830, "Matchlock Gun": 158})     # the 2026-08-27 hold
        met, why = _goal_met(led, goal)
        self.assertFalse(met, "a surplus of Iron must not cover short Matchlock Gun")
        self.assertIn("Matchlock Gun", why)
        states = material_states(led, goal)
        self.assertEqual(states["Iron"]["state"], "met")
        self.assertEqual(states["Matchlock Gun"]["state"], "short")
