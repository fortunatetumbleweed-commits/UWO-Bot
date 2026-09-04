"""Bartering is finished when the materials are, not when one call returns ok.

Live 2026-08-23: the node committed one round, advanced the phase to `sailing_route`, and the
mission left for London with 2-3 rounds' worth of materials still aboard (user).

    [progress] Melanesian Village: sailing_route
    [barter_command] bartered — now the tail: route 'jakata to london'

"A round succeeded" is not "the phase is done". What is fundable AFTER a batch cannot be
derived from before it either: amity tiers and the stock refresh move the ratio, so the panel
has to be re-read.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

import actions.barter_panel as bp

from brain import barter_command


def _cmd(tail_kind="route", tail_value="jakata to london"):
    return barter_command.BarterCommand(good="Box of Nutmeg", village="Melanesian Village",
                                        tail_kind=tail_kind, tail_value=tail_value)


class KeepBarteringWhileFundable(unittest.TestCase):

    def _run(self, barter_results):
        """Drive the resume path with scripted barter outcomes; return the call order."""
        called, seq = [], list(barter_results)

        def _barter(_task):
            called.append("barter")
            return seq.pop(0) if seq else {"ok": True, "committed": 0}

        executors = {"barter": _barter,
                     "sail_route": lambda t: called.append("sail_route") or {"ok": True},
                     "sail_to_sell": lambda t: called.append("sail_to_sell") or {"ok": True},
                     "sell": lambda t: called.append("sell") or {"ok": True},
                     "sail_to_village": lambda t: called.append("sail") or {"ok": True}}
        # WHERE WE ARE COMES FROM THE BOOTSTRAP NOW. `_at_a_village` stopped capturing a
        # screen and reading the left menu itself — it reads the position the dispatcher
        # established — so stubbing `where_am_i` and `detect_left_menu` no longer reaches it.
        # conftest stands `establish_position` down to 'port_overworld' for the whole suite so
        # that nothing perceives, which means a test that needs to BE somewhere has to say so;
        # without this the resume path added a `sail` leg to a village it was standing in.
        with patch("brain.barter_mission_live.make_live_executors", return_value=executors), \
             patch("brain.activities.bootstrap.establish_position",
                   return_value={"ok": True, "state": "village", "port": None,
                                 "sub_menu": None, "scene_type": "village"}), \
             patch("brain.unexpected_dialog.clear_blockers", return_value={"cleared": False}), \
             patch.object(barter_command, "_depart_village_to_sea", return_value=True), \
             patch("brain.mission_progress.finish"):
            barter_command._resume_at_village(_cmd(), {"rounds": 1})
        return called

    def test_it_barters_again_while_rounds_remain(self):
        order = self._run([{"ok": True, "committed": 1, "more_rounds_fundable": 2},
                           {"ok": True, "committed": 2, "more_rounds_fundable": 0}])
        self.assertEqual(order, ["barter", "barter", "sail_route", "sell"])

    def test_it_goes_to_the_tail_when_nothing_more_is_fundable(self):
        order = self._run([{"ok": True, "committed": 1, "more_rounds_fundable": 0}])
        self.assertEqual(order, ["barter", "sail_route", "sell"])

    def test_a_pass_that_commits_nothing_stops_the_loop(self):
        """The panel may keep claiming rounds that never commit — do not spin on it."""
        order = self._run([{"ok": True, "committed": 1, "more_rounds_fundable": 3},
                           {"ok": True, "committed": 0, "more_rounds_fundable": 3}])
        self.assertEqual(order.count("barter"), 2)
        self.assertIn("sail_route", order)

    def test_the_loop_is_bounded(self):
        endless = [{"ok": True, "committed": 1, "more_rounds_fundable": 9}] * 20
        order = self._run(endless)
        self.assertLessEqual(order.count("barter"), barter_command._MAX_BARTER_PASSES + 1)


if __name__ == "__main__":
    unittest.main()


class TheGameDecidesWhetherMoreIsPossible(unittest.TestCase):
    """A live (yellow) Exchange button is the game's own answer to "can I barter again?".

    It has already applied every rule the bot would otherwise re-derive — materials, the daily
    count, the stock state. Live 2026-08-23 the bot completed 2 rounds and stopped while
    Exchange was still enabled (user): the material arithmetic said "done", the game said
    "no", and the game was right.
    """

    def _more(self, *, live, computed):
        from brain import barter_mission_live as bml
        state = types.SimpleNamespace(rounds_remaining=computed)
        with patch.object(bp, "_read_panel_state", return_value=state), \
             patch.object(bp, "_exchange_still_live", return_value=live):
            after = bp._read_panel_state()
            more = int(getattr(after, "rounds_remaining", 0) or 0) if after else 0
            if bp._exchange_still_live():
                more = max(more, 1)
        return more

    def test_a_live_button_overrides_a_zero_count(self):
        self.assertEqual(self._more(live=True, computed=0), 1)

    def test_a_greyed_button_leaves_the_count_alone(self):
        self.assertEqual(self._more(live=False, computed=0), 0)

    def test_it_never_lowers_a_higher_count(self):
        self.assertEqual(self._more(live=True, computed=3), 3)


class ExchangeLivenessIsSafeWhenUnreadable(unittest.TestCase):

    def test_a_perceive_failure_is_not_a_live_button(self):
        """Unknown must not mean "keep bartering" — that would spin at the village."""
        from brain import barter_mission_live as bml
        with patch("capture.adb_capture.capture_screen", side_effect=RuntimeError("no frame")):
            self.assertFalse(bp._exchange_still_live())


class ExhaustionIsCompletionNotFailure(unittest.TestCase):
    """A stalled commit with nothing left to barter means FINISHED.

    Live 2026-08-23 the fleet bartered until Coral hit 2 against a need of 3. The last tap
    changed nothing, `run_barter_phase` reported ok=False, and the whole mission failed:

        [barter_commit] NO PROGRESS: tapped [('exchange',...), ('ok',...)] — escalate
        FAILED at step mission: barter commit stalled after 0

    So the tail never ran and the fleet sat in the village with its cargo. But the game was
    right to refuse — the materials were spent. Only a stall with a barter STILL AVAILABLE is
    a real failure, and only when something was actually committed is it completion: arriving
    unable to barter at all means the materials never made it, which is worth reporting.
    """

    def _finish(self, *, committed, exchange_live):
        from brain import barter_mission_live as bml
        res = {"ok": False, "committed": committed, "reason": "stalled"}
        with patch.object(bp, "_exchange_still_live", return_value=exchange_live), \
             patch.object(bp, "_read_panel_state",
                          return_value=types.SimpleNamespace(rounds_remaining=0,
                                                             shortfall={"Coral": 1})):
            if (not res.get("ok") and int(res.get("committed") or 0) >= 1
                    and not bp._exchange_still_live()):
                res = {**res, "ok": True, "exhausted": True}
        return res

    def test_a_stall_after_committing_with_a_grey_button_is_finished(self):
        res = self._finish(committed=2, exchange_live=False)
        self.assertTrue(res["ok"])
        self.assertTrue(res.get("exhausted"))

    def test_a_stall_with_the_button_still_live_stays_a_failure(self):
        """Something went wrong — the game would still accept a barter."""
        self.assertFalse(self._finish(committed=2, exchange_live=True)["ok"])

    def test_never_bartering_at_all_stays_a_failure(self):
        """Arriving unable to barter is not completion; the materials never made it."""
        self.assertFalse(self._finish(committed=0, exchange_live=False)["ok"])
