"""When a primitive cannot see what it needs, the bot is LOST — not blind.

The screen is the truth; the bot's belief is what goes stale (user, 2026-08-24).

`read_fleet_status` hunting the ☰ inside a MARKET is the shape of it: the ☰ exists only on
the port overworld, so the fleet was never going to be readable from there. Its answer was to
re-perceive three times in place — which only re-confirms the same true screen. Re-perceiving
tells you WHERE YOU ARE; it cannot change it. What was missing was the transition.

So a lost caller hands back, re-perceives, and MOVES to the state the work needs.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from brain import nav_step
from brain.nav_step import ARRIVED, BLOCKED, MOVED, NO_ROUTE, StepResult, reorient_to


class ReorientingToTheStateTheWorkNeeds(unittest.TestCase):

    def _reorient(self, steps, **kw):
        seq = list(steps)
        with patch.object(nav_step, "step_toward", side_effect=lambda *a, **k: seq.pop(0)):
            return reorient_to("port_overworld", capture=lambda: None, **kw)

    def test_already_there_makes_no_moves(self):
        res = self._reorient([StepResult(outcome=ARRIVED, state="port_overworld")])
        self.assertEqual(res.outcome, ARRIVED)

    def test_it_walks_out_of_a_sub_menu(self):
        """The live case: the gate cleared into the market's Purchase page, and the ☰ was
        two Backs away."""
        res = self._reorient([
            StepResult(outcome=MOVED, state="sub_menu", action="back"),
            StepResult(outcome=MOVED, state="building", action="back"),
            StepResult(outcome=ARRIVED, state="port_overworld"),
        ])
        self.assertEqual(res.outcome, ARRIVED)

    def test_blocked_is_reported_not_pushed_through(self):
        """Something in the way is the unexpected-handler's business, not a reason to keep
        stepping — and never a reason to pretend the state was reached.

        The second result is the confirming re-perceive: a failed step now asks where the
        fleet actually is before reporting failure, because a step can fail precisely because
        it already arrived.
        """
        res = self._reorient([StepResult(outcome=BLOCKED, state="sub_menu", reason="dialog up"),
                              StepResult(outcome=BLOCKED, state="sub_menu", reason="dialog up")])
        self.assertEqual(res.outcome, BLOCKED)

    def test_no_route_stops_immediately(self):
        res = self._reorient([StepResult(outcome=NO_ROUTE, state="sea"),
                              StepResult(outcome=NO_ROUTE, state="sea")])
        self.assertEqual(res.outcome, NO_ROUTE)

    def test_it_is_bounded(self):
        """A caller that never arrives must not step forever."""
        forever = [StepResult(outcome=MOVED, state="sub_menu", action="back")] * 50
        res = self._reorient(forever, max_steps=4)
        self.assertNotEqual(res.outcome, ARRIVED)


if __name__ == "__main__":
    unittest.main()


class TheExecutorSpeaksTheRegistrysLanguage(unittest.TestCase):
    """Edge names come from `memory/knowledge/fsm/states.json`, not from what sounds right.

    The executor originally handled 'exit_screen'/'home'/'open_world_map'. The registry
    actually says `tap_home` and `open_world_map_from_sea`, so every edge except `press_back`
    fell through to "no executor" and reported BLOCKED — live 2026-08-24, reorienting out of
    a market backed out to `building` and then stalled one move short of the overworld.
    """

    def test_every_action_in_states_json_is_handled_or_deliberately_reported(self):
        import json
        from brain.nav_step import _execute

        handled = {"press_back", "tap_home", "tap_x_button", "open_world_map_from_sea",
                   "wait", "swipe_up"}
        # ENTERING moves need a target the state machine does not carry; reorienting is
        # about getting OUT, so these are reported on purpose.
        deliberately_reported = {"tap_building", "tap_sub_menu_item", "tap_mini_map",
                                 "tap_hamburger", "tap_globe_icon", "tap_city_go_to_city",
                                 "tap_centre"}

        found = set()

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k in ("action", "actions"):
                        found.update(str(a) for a in (v if isinstance(v, list) else [v]))
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        walk(json.load(open("memory/knowledge/fsm/states.json")))
        self.assertTrue(found, "states.json carries no transition actions")
        unaccounted = found - handled - deliberately_reported
        self.assertEqual(unaccounted, set(),
                         f"FSM actions nobody has decided about: {sorted(unaccounted)}")

    def test_the_exit_edge_out_of_a_building_is_wired(self):
        """`tap_home` is the building → port_overworld edge — the one that stalled."""
        from brain import nav_step
        with patch("actions.screen_exit.exit_current_screen",
                   return_value=type("R", (), {"method": "home"})()):
            self.assertTrue(nav_step._execute("tap_home", "building"))


class AFailedStepMayMeanWeAlreadyArrived(unittest.TestCase):
    """A step can fail BECAUSE the fleet is already there.

    Live 2026-08-24: `nav_step` read 'building' and asked for `tap_home`, while
    `exit_current_screen` looked at the same screen, saw port_overworld, and refused —
    pressing Back there opens "Exit Game?". The refusal surfaced as BLOCKED and the mission
    aborted one step AFTER it had succeeded. Two readings of one screen disagreed, and the
    stale one won.
    """

    def test_a_blocked_step_re_perceives_before_giving_up(self):
        from brain import nav_step
        seq = [StepResult(outcome=BLOCKED, state="building", reason="'tap_home' did not run"),
               StepResult(outcome=ARRIVED, state="port_overworld")]
        with patch.object(nav_step, "step_toward", side_effect=lambda *a, **k: seq.pop(0)):
            res = reorient_to("port_overworld", capture=lambda: None)
        self.assertEqual(res.outcome, ARRIVED)

    def test_a_genuine_block_is_still_reported(self):
        from brain import nav_step
        seq = [StepResult(outcome=BLOCKED, state="sub_menu", reason="dialog up"),
               StepResult(outcome=BLOCKED, state="sub_menu", reason="dialog up")]
        with patch.object(nav_step, "step_toward", side_effect=lambda *a, **k: seq.pop(0)):
            res = reorient_to("port_overworld", capture=lambda: None)
        self.assertEqual(res.outcome, BLOCKED)


class TheVillageListIsOnTheLeft(unittest.TestCase):
    """`_visible_row` must look only where the LIST is.

    Live 2026-08-24: scanning the whole frame matched "Svear Village" in the right-hand
    Village Info PANEL (x=1961) and tapped the panel title instead of a list row. The list
    never opened, the trade list was already scrolled, and the check read the wrong good.
    A row of the village list is never in the panel.
    """

    def test_a_panel_title_is_not_a_list_row(self):
        import types
        from actions.sail_actions import _visible_row
        panel_title = types.SimpleNamespace(label="Svear Village", cx=1961, cy=210)
        assert _visible_row([panel_title], "Svear Village") is None

    def test_a_real_list_row_still_matches(self):
        import types
        from actions.sail_actions import _visible_row
        row = types.SimpleNamespace(label="Svear Village", cx=263, cy=416)
        hit = _visible_row([row], "Svear Village")
        assert hit is not None and hit.cx == 263
