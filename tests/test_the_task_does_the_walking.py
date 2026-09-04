"""When a primitive stops walking the bot around, the task has to.

`navigate_to_building` no longer presses Back on a screen it does not recognise — that fold
is what stopped it cancelling a successful departure four times over. The cost is that
"could not reach the Market" now reaches the mission layer in cases the primitive used to
absorb: standing on the Market's own Purchase sub-menu, for instance.

`_run_with_retry` alone does not close that, because the executor re-runs from
`drive_sail_to(port)`, which reports "already there" without walking out of the panel. So the
task reorients, once, and tries again.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch, MagicMock

import brain.barter_mission_live as bml
from brain.nav_step import ARRIVED, StepResult, NO_ROUTE


class EnteringTheMarket(unittest.TestCase):

    def test_a_clean_entry_does_not_reorient(self):
        with patch("actions.sail_actions.navigate_to_building", return_value=True) as nav, \
             patch("brain.nav_step.reorient_to") as reorient:
            res = bml._enter_market_at("Lisboa")
        self.assertTrue(res["ok"])
        nav.assert_called_once()
        reorient.assert_not_called()

    def test_a_failure_reorients_then_retries_once(self):
        """The sub-menu case: the old primitive pressed Back itself; now the task does."""
        with patch("actions.sail_actions.navigate_to_building",
                   side_effect=[False, True]) as nav, \
             patch("brain.nav_step.reorient_to",
                   return_value=StepResult(outcome=ARRIVED, reason="")) as reorient:
            res = bml._enter_market_at("Lisboa")
        self.assertTrue(res["ok"])
        self.assertEqual(nav.call_count, 2)
        reorient.assert_called_once()

    def test_it_never_gives_up_the_settlement(self):
        """Leaving a PANEL is mechanics; leaving the place the mission sailed to is not."""
        with patch("actions.sail_actions.navigate_to_building", return_value=False), \
             patch("brain.nav_step.reorient_to",
                   return_value=StepResult(outcome=ARRIVED, reason="")) as reorient:
            bml._enter_market_at("Svear Village")
        self.assertIs(reorient.call_args.kwargs["may_leave_a_place"], False)

    def test_a_refused_reorientation_is_reported_not_worked_around(self):
        with patch("actions.sail_actions.navigate_to_building", return_value=False) as nav, \
             patch("brain.nav_step.reorient_to",
                   return_value=StepResult(outcome=NO_ROUTE, reason="no route from sea")):
            res = bml._enter_market_at("Lisboa")
        self.assertFalse(res["ok"])
        self.assertIn("no route from sea", res["reason"])
        self.assertEqual(nav.call_count, 1, "it must not tap on after a refused reorientation")

    def test_two_tries_is_the_limit(self):
        """A third attempt would be this helper growing its own retry loop — the thing the
        fold removed. `run_mission`'s ladder owns anything beyond this."""
        with patch("actions.sail_actions.navigate_to_building", return_value=False) as nav, \
             patch("brain.nav_step.reorient_to",
                   return_value=StepResult(outcome=ARRIVED, reason="")):
            res = bml._enter_market_at("Lisboa")
        self.assertFalse(res["ok"])
        self.assertEqual(nav.call_count, 2)
