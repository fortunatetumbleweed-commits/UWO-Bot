"""Fleet readiness is a READ. Resolving and re-navigating are the goal's decisions.

`_ensure_fleet_ready` used to be a mini task-runner: it detected a blocker, climbed a
four-rung resolution ladder (KB → learned recovery → Claude Vision → the human operator),
and re-entered the harbour after EVERY rung — three times over. That inner loop was a second
copy of the one SailToGoal already runs, so three allowed failures became nine attempts, and
the inner one navigated on the goal's behalf without being asked.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch, MagicMock

import actions.sail_actions as sa


def _reading(**kw):
    base = {"ready": False, "blocker": None, "on_departure_panel": True,
            "detail": "no yellow departure button", "text": "harbor", "frame": object()}
    base.update(kw)
    return base


class TwoSignals(unittest.TestCase):
    """Either alone is wrong: the KB catches a NAMED blocker, the yellow button catches an
    unnamed one. OCR missing the text does not mean the fleet can leave."""

    def _read(self, *, ocr, yellow, signals=(("not enough crew", "recruit"),)):
        flow = MagicMock()
        flow._raw = {"blocking_signals": [{"text": t, "description": d} for t, d in signals]}
        with patch.object(sa, "_ensure_harbor_top_level", return_value=object()), \
             patch.object(sa, "_ocr_frame",
                          return_value=[(w, 0.9, 0, 0) for w in ocr.split()]), \
             patch.object(sa, "_find_yellow_button", return_value=yellow), \
             patch("brain.fsm_registry.get_fsm_registry") as reg:
            reg.return_value.flows = {"harbor_departure": flow}
            return sa.read_fleet_readiness()

    def test_a_yellow_button_and_no_blocker_is_ready(self):
        r = self._read(ocr="depart now fleet status", yellow=(2100, 900))
        self.assertTrue(r["ready"])

    def test_a_named_blocker_beats_a_yellow_button(self):
        r = self._read(ocr="depart now Not Enough Crew", yellow=(2100, 900))
        self.assertFalse(r["ready"])
        self.assertEqual(r["blocker"]["text"], "not enough crew")

    def test_no_yellow_button_blocks_even_with_clean_ocr(self):
        """The unnamed blocker: OCR saw nothing wrong, but nothing is tappable either."""
        r = self._read(ocr="depart now fleet status", yellow=None)
        self.assertFalse(r["ready"])
        self.assertIsNone(r["blocker"])

    def test_it_says_whether_this_is_even_the_departure_panel(self):
        on = self._read(ocr="supply departure", yellow=None)
        off = self._read(ocr="recruit crew wages", yellow=None)
        self.assertTrue(on["on_departure_panel"])
        self.assertFalse(off["on_departure_panel"])

    def test_the_read_taps_nothing(self):
        """`_ensure_harbor_top_level` is patched out here, so this pins the read itself: given
        the panel, deciding whether the fleet can leave involves no input at all."""
        with patch.object(sa, "tap") as tap, patch.object(sa, "press_back") as back:
            self._read(ocr="depart now", yellow=(1, 2))
        tap.assert_not_called()
        back.assert_not_called()


class OneRungPerCall(unittest.TestCase):

    def test_a_kb_resolution_stops_the_ladder(self):
        with patch("brain.recovery.execute_resolution", return_value=True) as kb, \
             patch("brain.human_escalation._match_learned_recovery") as learned, \
             patch("brain.perceive.perceive"):
            res = sa.resolve_fleet_blocker(
                _reading(blocker={"text": "not enough crew", "action": "resolve"}))
        self.assertEqual(res["via"], "kb")
        kb.assert_called_once()
        learned.assert_not_called()

    def test_it_does_not_re_enter_the_harbour(self):
        """Re-navigating was how a readiness CHECK moved the bot."""
        with patch("brain.recovery.execute_resolution", return_value=True), \
             patch.object(sa, "navigate_to_building") as nav, \
             patch.object(sa, "_navigate_to_harbour", create=True) as harbour, \
             patch("brain.perceive.perceive"):
            sa.resolve_fleet_blocker(
                _reading(blocker={"text": "not enough crew", "action": "resolve"}))
        nav.assert_not_called()
        harbour.assert_not_called()

    def test_it_falls_through_to_the_human_when_nothing_else_works(self):
        escalated = MagicMock()
        escalated.state = "building"
        with patch("brain.human_escalation._match_learned_recovery", return_value=None), \
             patch.object(sa, "_resolve_blocker_with_reasoning", return_value=False), \
             patch("brain.human_escalation.escalate", return_value=escalated) as esc, \
             patch("brain.perceive.perceive"):
            res = sa.resolve_fleet_blocker(_reading())
        self.assertEqual(res["via"], "human")
        esc.assert_called_once()


class TheGoalOwnsTheWalkBack(unittest.TestCase):

    def _goal(self):
        from brain.goals.sail_to import SailToGoal, SailPhase
        g = SailToGoal("Lisboa")
        g.phase = SailPhase.FLEET_CHECK
        return g, SailPhase

    def test_a_wrong_panel_sends_the_goal_back_to_the_harbour(self):
        g, SailPhase = self._goal()
        with patch("actions.sail_actions.read_fleet_readiness",
                   return_value=_reading(on_departure_panel=False)), \
             patch("actions.sail_actions.resolve_fleet_blocker") as resolve:
            r = g._action_fleet_check()
        self.assertEqual(g.phase, SailPhase.GO_TO_HARBOR)
        resolve.assert_not_called()

    def test_a_real_blocker_is_resolved_in_place(self):
        g, SailPhase = self._goal()
        with patch("actions.sail_actions.read_fleet_readiness", return_value=_reading()), \
             patch("actions.sail_actions.resolve_fleet_blocker",
                   return_value={"attempted": True, "via": "kb", "resolved": True}) as resolve:
            g._action_fleet_check()
        resolve.assert_called_once()
        self.assertEqual(g.phase, SailPhase.FLEET_CHECK)

    def test_ready_advances_to_departure(self):
        g, SailPhase = self._goal()
        with patch("actions.sail_actions.read_fleet_readiness",
                   return_value=_reading(ready=True)):
            r = g._action_fleet_check()
        self.assertEqual(g.phase, SailPhase.DEPART)
        self.assertTrue(g._fleet_checked)

    def test_the_check_is_read_once_per_tick(self):
        """Two nested budgets meant nine attempts where the goal allowed three."""
        g, _ = self._goal()
        with patch("actions.sail_actions.read_fleet_readiness",
                   return_value=_reading()) as read, \
             patch("actions.sail_actions.resolve_fleet_blocker",
                   return_value={"attempted": True, "via": "kb", "resolved": False}):
            g._action_fleet_check()
        read.assert_called_once()
