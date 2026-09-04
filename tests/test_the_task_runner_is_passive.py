"""The barter task runner is consulted; it does not drive, and it does not look.

Guiding Principle #7. These tests are about the CONTRACT, not the barter rules: given what
an activity handed back, what does the runner ask for next, and what does it record?
"""
import unittest
from unittest import mock

from brain.barter_runner import (FAILED, HAVE_RECIPE, NEED_RECIPE, BarterTaskRunner)
from brain.activities.world_map import RemoteCheck
from brain.dispatcher import ActivityResult, BLOCKED, FINISHED, WORKING


class _Trade:
    def __init__(self, good):
        self.good = good


def _runner():
    return BarterTaskRunner(village="Svear", good="Birch Tree")


class ItAsksInsteadOfLooking(unittest.TestCase):
    def test_the_first_consult_asks_for_a_remote_check(self):
        """It does not read the village. It returns the work order that reads the village."""
        r = _runner()
        goal = r.next_goal(None, None)
        assert isinstance(goal, RemoteCheck)
        assert goal.village == "Svear"
        assert goal.good == "Birch Tree"

    def test_the_work_order_is_not_re_issued_while_it_is_outstanding(self):
        """A consult that arrives mid-work must not start the read again.

        The dispatcher consults after EVERY activity result, including WORKING ones."""
        r = _runner()
        first = r.next_goal(None, None)
        again = r.next_goal(ActivityResult(WORKING, {"did": "scrolled"}), None)
        assert again is first

    def test_it_asks_for_nothing_once_the_recipe_is_in_hand(self):
        r = _runner()
        r.next_goal(None, None)
        r.next_goal(ActivityResult(FINISHED, {"trades": [_Trade("Birch Tree")],
                                              "base": {"barters_used": 0,
                                                       "barters_total": 5}}), None)
        assert r.next_goal(None, None) is None


class ItUpdatesTaskStatus(unittest.TestCase):
    def test_a_finished_read_becomes_the_recipe(self):
        r = _runner()
        r.next_goal(None, None)
        r.next_goal(ActivityResult(FINISHED, {"trades": [_Trade("Birch Tree"), _Trade("Tar")],
                                              "base": {"barters_used": 2,
                                                       "barters_total": 5}}), None)
        assert r.status == HAVE_RECIPE
        assert r.trade_for("birch  tree") is not None      # case and spacing insensitive
        assert r.trade_for("Nothing") is None

    def test_the_rounds_are_the_days_ceiling_not_the_arrival_total(self):
        """Every amity grade opens one more barter, and amity climbs while trading. Planning
        on the arrival total (5) guarantees coming up short — the ceiling is 7."""
        r = _runner()
        r.base = {"barters_used": 2, "barters_total": 5}
        assert r.rounds_remaining == 5                     # 7 - 2, not 5 - 2

    def test_unreadable_rounds_are_None_not_a_guess(self):
        r = _runner()
        r.base = {"barters_used": None, "barters_total": None}
        assert r.rounds_remaining is None

    def test_a_PARTIAL_read_is_refused_not_used(self):
        """Materials are invariant, so a short list is a reading failure, not a recipe.

        Planning from one is how a fleet reached Svear with two of three materials and could
        not barter at all. The activity refuses to certify it; the runner refuses to use it."""
        r = _runner()
        r.next_goal(None, None)
        r.next_goal(ActivityResult(BLOCKED, {"trades": [_Trade("Birch Tree")], "partial": True},
                                   detail="did not complete in 6 scrolls — PARTIAL"), None)
        assert r.status == FAILED
        assert "did not read completely" in r.reason
        assert r.next_goal(None, None) is None             # it does not retry blindly


class ItNeverReachesForTheGame(unittest.TestCase):
    def test_the_module_imports_no_ui(self):
        """The enforcement in test_the_layering_is_enforced.py holds this at 0/0. This states
        it as a property of THIS module, so the reason is visible where the code is."""
        import ast
        import pathlib

        from brain.layers import is_ui_module

        src = pathlib.Path("brain/barter_runner.py").read_text()
        for node in ast.walk(ast.parse(src)):
            mod = None
            if isinstance(node, ast.ImportFrom) and node.module:
                mod = node.module
            elif isinstance(node, ast.Import):
                mod = node.names[0].name
            assert not (mod and is_ui_module(mod)), f"barter_runner imports UI: {mod}"

    def test_a_full_consult_cycle_touches_no_device(self):
        """Belt and braces: run the whole cycle with subprocess poisoned. Nothing notices."""
        def _boom(*a, **k):
            raise AssertionError("the task runner reached for the device")

        with mock.patch("subprocess.run", _boom), \
             mock.patch("subprocess.check_output", _boom):
            r = _runner()
            r.next_goal(None, None)
            r.next_goal(ActivityResult(FINISHED, {"trades": [_Trade("Birch Tree")],
                                                  "base": {"barters_used": 0,
                                                           "barters_total": 7}}), None)
        assert r.status == HAVE_RECIPE


class LeavingTheVillageIsAWorkOrder(unittest.TestCase):
    """The departure decides from `state`, and never presses anything itself."""

    def _runner_at(self, where):
        import types

        from brain.barter_runner import NEED_CLEAR

        r = BarterTaskRunner(village="", good="", status=NEED_CLEAR)
        state = types.SimpleNamespace(state=where, location=where)
        return r, r.next_goal(None, state)

    def test_a_village_asks_to_be_left(self):
        from brain.activities.sea import ClearOfTheVillage

        _, goal = self._runner_at("village")
        assert isinstance(goal, ClearOfTheVillage)

    def test_the_sea_is_already_clear(self):
        from brain.barter_runner import HAVE_CLEAR

        r, goal = self._runner_at("sea")
        assert goal is None
        assert r.status == HAVE_CLEAR

    def test_a_port_overworld_is_clear_because_the_globe_is_right_there(self):
        from brain.barter_runner import HAVE_CLEAR

        r, goal = self._runner_at("port_overworld")
        assert goal is None
        assert r.status == HAVE_CLEAR

    def test_the_main_menu_is_reported_not_pressed(self):
        """Back means "Exit Game?" there — a shutdown one tap away. Reaching it means the
        premise was wrong: live 2026-08-26 the fleet was at Stockholm, moved for supply and
        never in a village, and the old loop pressed Back at it twice."""
        r, goal = self._runner_at("main_menu")
        assert goal is None
        assert r.status == FAILED
        assert "Exit Game" in r.reason

    def test_the_two_lists_agree_with_the_routing(self):
        """The runner decides it is clear and `to_intent` decides not to press. Two copies of
        one fact, so they are asserted equal rather than trusted to stay in step — a change to
        one that misses the other is how a fleet would press Back at a port overworld."""
        from brain import intents
        from brain import barter_runner as br

        assert set(br._CLEAR_OF_THE_VILLAGE) == set(intents._TAIL_CAN_SAIL_FROM)
        assert set(br._BACK_WOULD_QUIT) <= set(intents._NEVER_BACK_FROM)
