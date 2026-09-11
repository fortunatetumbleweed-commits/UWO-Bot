"""Some screens are not left by Back.

Live 2026-08-26 at Stockholm, resuming the tail: the fleet was behind the idle lock, and the
leave-the-village loop pressed Back at it —

    [barter_command] leaving the village for the tail (state='idle_lock', back 1/4)
    [classify] → idle_lock (omniparser, conf=high, signals=['unlock_prompt=all_present'])

— which changed nothing, and would have spent all four attempts that way. The lock ends by
FINISHING, with the gesture its own prompt names; Back has no effect on it.

Asking "does this screen end by finishing?" is general — it covers `loading` too, and any
future state of the same shape — and it names no state at the call site.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import brain.barter_command as bc


class LeavingForTheTail(unittest.TestCase):

    def _run(self, states):
        """`states` are successive verdicts from the DISPATCHER'S perceive.

        THE SEAM MOVED. The departure stopped perceiving and pressing for itself: it returns a
        `ClearOfTheVillage` work order, reads `state` on the next consult, and `to_intent`
        turns it into one Back. So what a test scripts is what the dispatcher sees, and
        `finishes` is gone entirely — asking "does this screen end by finishing?" is no longer
        a branch here. A screen that ends by finishing has a CLEARING ACTIVITY, and the
        dispatcher routes to it; `to_intent` emits no transition for those states at all,
        which is what this class checks.
        """
        import types

        seq = list(states)
        backs = []

        def _perceive():
            w = seq.pop(0) if len(seq) > 1 else seq[0]
            return types.SimpleNamespace(state=w, location=w, port=None, frame=None)

        with patch("actions.adb_actions.press_back", side_effect=lambda: backs.append(1)), \
             patch("brain.run_goal._refined_state", _perceive), \
             patch("time.sleep", lambda *_a, **_k: None):
            ok = bc._depart_village_to_sea(max_backs=4)
        return ok, backs

    def test_the_lock_is_finished_not_backed_out_of(self):
        """THE REGRESSION THIS FILE CAUGHT. The migration to work orders dropped the
        finish-don't-Back branch, saying the clearing activities owned it — true of the
        ACTIVITY step, but `to_intent` went on emitting EXIT_BUILDING for `idle_lock`, so the
        fleet would have pressed Back at the lock exactly as it did at Stockholm.

        The rule is derived now, not listed: a state served by an activity that declares no
        GOALS is a state that ends by finishing, and no transition is emitted for it."""
        import types

        from brain.activities.sea import ClearOfTheVillage
        from brain.intents import to_intent

        for where in ("idle_lock", "transient", "unrecognized_chromed_screen"):
            state = types.SimpleNamespace(state=where, location=where, port=None)
            self.assertIsNone(
                to_intent(ClearOfTheVillage(), state),
                f"a Back was dispatched at {where!r}, which Back does nothing to")

    def test_an_ordinary_village_screen_still_uses_back(self):
        # TWO, not one: EXIT_BUILDING gets one retry at a village now (2026-09-04). See
        # `test_it_gives_up_rather_than_pressing_back_forever` for why that is bounded.
        ok, backs = self._run(["village"] * 4 + ["sea"])
        self.assertTrue(ok)
        self.assertEqual(len(backs), 2)

    def test_already_at_sea_needs_neither(self):
        ok, backs = self._run(["sea"])
        self.assertTrue(ok)
        self.assertEqual(backs, [])

    def test_it_gives_up_rather_than_pressing_back_forever(self):
        """Two presses, then it gives up — and the second is what this test now defends.

        It asserted ONE, because a second Back at a village lands at SEA and loses the
        village. What changed is the reading of that cost, not the risk: EXIT_BUILDING is
        dispatched when leaving IS the goal, so "lost the village" and "left the village" are
        the same event. Live 2026-09-04 at San a single swallowed Back stranded a finished
        barter — four rounds committed, 4,455 Bambara Groundnut aboard — because nothing
        pressed again.

        The bound is the point, and it is unchanged in spirit: the old loop pressed Back up
        to four times at a screen that never moved. This presses once more, only on an
        OBSERVED-unchanged screen, and stops.
        """
        ok, backs = self._run(["village"])
        self.assertFalse(ok)
        self.assertEqual(len(backs), 2, "one retry, then it stops — never the old loop")

    def test_the_presses_do_not_scale_with_the_looks(self):
        """The property the original assertion was really defending."""
        _, few = self._run(["village"] * 3)
        _, many = self._run(["village"] * 30)
        self.assertEqual(len(few), len(many))


class BackIsNeverPressedOnAnOverworld(unittest.TestCase):
    """Back is context-dependent: on an overworld or the main menu it raises "Exit Game?",
    which is a shutdown one tap away (CLAUDE.md; live 2026-08-19).

    Live 2026-08-26 this loop pressed Back at the STOCKHOLM port overworld — twice — because
    the fleet was at a port rather than the village the tail assumed, the user having moved it
    for supply. It raised a Cancel/Ok dialog. Nothing was lost, since Back on a dialog is
    Cancel, but the bot was one positive tap from quitting the game.

    A port overworld needs no escaping in any case: the world map opens from it directly, by
    its globe.
    """

    def _run(self, states):
        """Scripted DISPATCHER verdicts — see the note on the harness above."""
        import types

        seq, backs = list(states), []

        def _perceive():
            w = seq.pop(0) if len(seq) > 1 else seq[0]
            return types.SimpleNamespace(state=w, location=w, port=None, frame=None)

        with patch("actions.adb_actions.press_back", side_effect=lambda: backs.append(1)), \
             patch("brain.run_goal._refined_state", _perceive), \
             patch("time.sleep", lambda *_a, **_k: None):
            ok = bc._depart_village_to_sea(max_backs=4)
        return ok, backs

    def test_a_port_overworld_is_already_somewhere_the_tail_can_sail_from(self):
        ok, backs = self._run(["port_overworld"])
        self.assertTrue(ok, "the world map opens from a port by its globe")
        self.assertEqual(backs, [], "Back here raises 'Exit Game?'")

    def test_the_main_menu_is_never_backed_out_of_either(self):
        ok, backs = self._run(["main_menu"])
        self.assertFalse(ok)
        self.assertEqual(backs, [])

    def test_a_village_is_still_backed_out_of(self):
        """The case the function exists for — a village must be left to reach the map."""
        ok, backs = self._run(["village"] * 4 + ["sea"])
        self.assertTrue(ok)
        self.assertEqual(len(backs), 2)   # one retry; see LeavingForTheTail above

    def test_the_port_map_also_counts_as_clear(self):
        ok, backs = self._run(["port_map"])
        self.assertTrue(ok)
        self.assertEqual(backs, [])
