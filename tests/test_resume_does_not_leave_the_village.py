"""Resuming at the village must not sail anywhere.

Live 2026-08-23, run 31. The phase model routed correctly:

    [barter_command] already bartering for Melanesian Village (4501s ago) — skipping the
                     check and the plan, arriving and bartering with what is aboard

and then the resume path called the sail step anyway, from inside the village:

    [sail_to] tick=1  phase=INIT  state='village'  port=None
    [sail_to] Inside village (state.port=None) — pressing Back to exit to sea
    [sail_to] tick=2  state='sea'
    [open_world_map] world map opened

A village's name cannot be read from its interior, so SailToGoal's INIT sees "a village,
identity unknown", assumes it is in the wrong place, and leaves. Its ARRIVAL check knows
better — "sailed-to a village destination -> treating as arrival" — but INIT never consults
it. The fleet was taken out of the village it was standing in, to go looking for that village.

Being in a village while the mission is BARTERING is the evidence that settles it: that phase
is only reached by sailing here.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from PIL import Image

_FRAME = Image.new("RGB", (2400, 1080))

from brain import barter_command


def _cmd():
    return barter_command.BarterCommand(good="Box of Nutmeg", village="Melanesian Village")


VILLAGE_MENU = ["Explore", "Gifting", "Loot", "Recruit Crew", "Barter"]
PORT_MENU = ["Purchase", "Sell"]


def _resume(state, menu=None):
    """Run _resume_at_village from `state` (and left menu); return executors called."""
    import types
    called = []

    def _ex(name):
        def run(task):
            called.append(name)
            return {"ok": True, "reason": name}
        return run

    labels = list(menu if menu is not None else VILLAGE_MENU)
    region = types.SimpleNamespace(labels=lambda: labels, items=[], find=lambda l: None)
    executors = {"sail_to_village": _ex("sail_to_village"), "barter": _ex("barter")}
    with patch("brain.barter_mission_live.make_live_executors", return_value=executors), \
         patch("actions.sail_actions.where_am_i", return_value={"location": state}), \
         patch("capture.adb_capture.capture_screen", return_value=_FRAME), \
         patch("vision.omniparser.parse_fast_cached", return_value=[]), \
         patch("vision.region_detectors.left_menu.detect_left_menu", return_value=region), \
         patch("brain.mission_progress.finish"):
        barter_command._resume_at_village(_cmd(), {"rounds": 1})
    return called


class ResumeAtTheVillage(unittest.TestCase):

    def test_it_does_not_sail_when_already_at_a_village(self):
        self.assertEqual(_resume("village"), ["barter"])

    def test_it_sails_when_at_sea(self):
        """At sea there is no left menu at all."""
        self.assertEqual(_resume("sea", menu=[]), ["sail_to_village", "barter"])

    def test_it_sails_from_a_port(self):
        self.assertEqual(_resume("port_overworld", menu=[]), ["sail_to_village", "barter"])

    def test_a_village_SUB_SCREEN_is_still_the_village(self):
        """The Barter sub-menu classifies as 'building', but the left menu is a village's.

        Live 2026-08-23: a run began with the Barter panel still open, the narrow
        state=='village' test failed, and the fleet sailed away from the village it was in.
        """
        self.assertEqual(_resume("building", menu=VILLAGE_MENU), ["barter"])

    def test_a_port_building_is_NOT_a_village(self):
        """A market's menu must not be mistaken for a village's."""
        self.assertEqual(_resume("building", menu=PORT_MENU),
                         ["sail_to_village", "barter"])

    def test_an_unreadable_menu_at_a_port_still_sails(self):
        self.assertEqual(_resume("port_overworld", menu=[]), ["sail_to_village", "barter"])

    def test_an_unreadable_state_still_sails(self):
        """Unknown is not 'already there' — sailing is the safe default when blind."""
        called = []
        executors = {"sail_to_village": lambda t: called.append("sail") or {"ok": True},
                     "barter": lambda t: called.append("barter") or {"ok": True}}
        with patch("actions.sail_actions.where_am_i", side_effect=RuntimeError("no frame")), \
             patch("capture.adb_capture.capture_screen", return_value=_FRAME), \
             patch("brain.barter_mission_live.make_live_executors", return_value=executors), \
             patch("brain.mission_progress.finish"):
            barter_command._resume_at_village(_cmd(), {"rounds": 1})
        self.assertEqual(called, ["sail", "barter"])


if __name__ == "__main__":
    unittest.main()


class BlockedPerceptionIsNotAnAnswer(unittest.TestCase):
    """A blocked screen says nothing about where the fleet is.

    Live 2026-08-23, run 36: the game dropped to its standby lock while idle.

        [barter_command] perceived before deciding: at_village=False
            (state is 'learned_on_standby_at_sea_slide_up_to_unlock', menu=unreadable)

    The fleet was at the village, behind the lock. Deciding on that reading would have sailed
    it away again. Same rule as brain/goals/sail_to._handle_unknown: clear what is in the way,
    THEN decide.
    """

    def _decide(self, *, cleared, state_after, menu_after):
        import types
        region = types.SimpleNamespace(labels=lambda: list(menu_after), items=[],
                                       find=lambda l: None)
        with patch("brain.unexpected_dialog.clear_blockers",
                   return_value={"cleared": cleared}) as cb, \
             patch("capture.adb_capture.capture_screen", return_value=_FRAME), \
             patch("actions.sail_actions.where_am_i", return_value={"location": state_after}), \
             patch("vision.omniparser.parse_fast_cached", return_value=[]), \
             patch("vision.region_detectors.left_menu.detect_left_menu", return_value=region):
            ok, why = barter_command._at_a_village()
        return ok, why, cb.called

    def test_a_blocker_is_cleared_before_the_decision(self):
        ok, _why, checked = self._decide(cleared=True, state_after="village",
                                         menu_after=VILLAGE_MENU)
        self.assertTrue(checked, "the screen must be cleared before deciding")
        self.assertTrue(ok, "after clearing, the village is visible again")

    def test_it_still_decides_when_nothing_was_blocking(self):
        ok, _why, _checked = self._decide(cleared=False, state_after="sea", menu_after=[])
        self.assertFalse(ok)

    def test_a_failing_blocker_check_does_not_stop_the_decision(self):
        import types
        region = types.SimpleNamespace(labels=lambda: VILLAGE_MENU, items=[],
                                       find=lambda l: None)
        with patch("brain.unexpected_dialog.clear_blockers",
                   side_effect=RuntimeError("no frame")), \
             patch("capture.adb_capture.capture_screen", return_value=_FRAME), \
             patch("actions.sail_actions.where_am_i", return_value={"location": "village"}), \
             patch("vision.omniparser.parse_fast_cached", return_value=[]), \
             patch("vision.region_detectors.left_menu.detect_left_menu", return_value=region):
            ok, _why = barter_command._at_a_village()
        self.assertTrue(ok)


class TheTailIsPartOfTheTask(unittest.TestCase):
    """`barter X at V, then take the route R` is ONE job.

    Live 2026-08-23: the resume path bartered, called mission_progress.finish(), and dropped
    the route and the sale — even though the barter node had just advanced the phase to
    `sailing_route` and `_resume_tail` existed for exactly that.
    """

    def _run(self, tail_kind, tail_value=None):
        import types
        called = []
        cmd = barter_command.BarterCommand(good="Box of Nutmeg",
                                           village="Melanesian Village",
                                           tail_kind=tail_kind, tail_value=tail_value)
        region = types.SimpleNamespace(labels=lambda: VILLAGE_MENU, items=[],
                                       find=lambda l: None)

        def _ex(name):
            return lambda task: called.append(name) or {"ok": True, "reason": name}

        executors = {k: _ex(k) for k in ("sail_to_village", "barter", "sail_route",
                                         "sail_to_sell", "sell")}
        # The DEPARTURE is a separate concern (tests/test_depart_village_for_the_tail.py).
        # Stubbed here so these tests measure only the chaining — and so they do not spend
        # eight seconds pressing Back at a scripted village that never yields.
        with patch("brain.barter_mission_live.make_live_executors", return_value=executors), \
             patch("actions.sail_actions.where_am_i", return_value={"location": "village"}), \
             patch("capture.adb_capture.capture_screen", return_value=_FRAME), \
             patch("vision.omniparser.parse_fast_cached", return_value=[]), \
             patch("vision.region_detectors.left_menu.detect_left_menu", return_value=region), \
             patch("brain.unexpected_dialog.clear_blockers", return_value={"cleared": False}), \
             patch.object(barter_command, "_depart_village_to_sea", return_value=True), \
             patch("brain.mission_progress.finish"):
            barter_command._resume_at_village(cmd, {"rounds": 1})
        return called

    def test_a_route_tail_runs_after_the_barter(self):
        self.assertEqual(self._run("route", "jakarta to london"),
                         ["barter", "sail_route", "sell"])

    def test_a_sail_tail_runs_after_the_barter(self):
        self.assertEqual(self._run("sail", "London"), ["barter", "sail_to_sell", "sell"])

    def test_no_tail_ends_after_the_barter(self):
        self.assertEqual(self._run("none"), ["barter"])
