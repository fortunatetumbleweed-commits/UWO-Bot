"""The goal is "barter until you cannot" — and the panel, not a plan, says when.

`brain.barter_mission_live.barter` is 241 lines and is both the goal and the procedure.
Nearly every bug of 2026-08-26 lived in it. What the split fixes BY CONSTRUCTION:

  * the 74-round loop cannot happen — it happened because the TASK re-derived "rounds
    fundable" from materials and sent itself back in after the phase declared itself
    finished. An activity that is told by the panel cannot disagree with the panel.
  * `planned_rounds` stops being carried through the result. It was only ever there so the
    caller could compare plan against reality, and that comparison belongs to whoever made
    the plan.
  * the tile walk, the lock banner and the info tip leave the task layer entirely.

`stopped_because` is the load-bearing field: "the village refused", "the materials ran out"
and "the good is not on offer today" are three different situations for the task, and NONE
of them is a failure.
"""

from __future__ import annotations

import types
import unittest

from brain.activities.village import Barter, VillageActivity
from brain.dispatcher import BLOCKED, FINISHED, UNRECOGNISED, WORKING

RECIPE = {"Wares": 155, "Firearms": 60, "Sundries": 148}


def _state(where="village"):
    return types.SimpleNamespace(state=where, port="Svear Village")


def _panel(rounds=3, materials=None, amity=(60000, 100000), good="Birch Tree"):
    return types.SimpleNamespace(selected_good=good, rounds_remaining=rounds,
                                 materials=dict(materials or RECIPE),
                                 amity_points=amity, partial_fraction=0.0,
                                 binding=None, shortfall=0)


def _activity(*, panels=None, opened=True, selected=True, context=None,
              commit=None, refused=True, **kw):
    """`panels` is a sequence consumed by successive reads; the last one repeats.

    REWRITTEN 2026-08-27 for the context model. The activity no longer runs every round in
    one call — it classifies the village's own screen, takes ONE action, and reports
    WORKING. `context` injects that classification so a test can put the activity on a
    specific screen; `commit` injects the single round.
    """
    seq = list(panels if panels is not None else [_panel()])

    def read():
        return seq[0] if len(seq) == 1 else seq.pop(0)

    return VillageActivity(open_panel_fn=lambda: opened,
                           overflow_fn=lambda: 0,
                           saw_fn=lambda: {"screen": "Village interior", "submenu": None},
                           select_fn=lambda g, r: selected,
                           read_panel_fn=read,
                           context_fn=(lambda _st: context) if context else None,
                           commit_fn=commit or (lambda: {"ok": True}),
                           exchange_live_fn=lambda: not refused,
                           recipe_fn=lambda g: RECIPE, **kw)


def _run_to_completion(act, goal, state, *, max_ticks=30):
    """Drive the activity the way the dispatcher does: call again while it reports WORKING.

    This IS the change. The rounds used to happen inside one call; now each call does one
    thing and the loop lives here — in the caller, where a fresh perceive would happen.
    """
    results = []
    for _ in range(max_ticks):
        res = act.work(goal, state)
        results.append(res)
        if res.status != WORKING:
            return res, results
    raise AssertionError(f"never left WORKING after {max_ticks} calls")


GOAL = Barter("Birch Tree", "Svear Village")


import brain.village_context as C


class OneActionPerCall(unittest.TestCase):
    """The change: rounds happen ACROSS calls, not inside one.

    `_barter -> _loop -> run_barter_phase` was a `for _ in range(20)` that committed round
    after round without handing back. Nothing tells this bot that the world moved, so a loop
    that does not return cannot be told it is wrong — and on 2026-08-27 it kept tapping
    Exchange after the game had CLOSED the barter submenu, called that a stale panel, and
    crashed the run.
    """

    def test_a_ready_panel_commits_exactly_one_round_then_hands_back(self):
        commits = []
        a = _activity(context=C.BARTER_PANEL_READY,
                      commit=lambda: commits.append(1) or {"ok": True})
        r = a.work(GOAL, _state())
        self.assertEqual(len(commits), 1, "one call, one round")
        self.assertEqual(r.status, WORKING, "the goal is not done — ask again")

    def test_rounds_accumulate_across_calls(self):
        commits = []
        a = _activity(context=C.BARTER_PANEL_READY,
                      commit=lambda: commits.append(1) or {"ok": True})
        r, calls = _run_to_completion(a, GOAL, _state())
        self.assertGreaterEqual(r.observed["rounds_committed"], 4,
                                "each call commits one round and hands back")

    def test_a_gold_button_keeps_being_tapped_whatever_the_commit_infers(self):
        """WHILE EXCHANGE IS GOLD, TAP IT. Ending belongs to the classifier — a grey button or
        a closed submenu — not to `exhausted`, which `barter_commit_verified` INFERS from "no
        progress AND the panel looks closed".

        Live 2026-08-30 at Hutu Village both halves of that inference were false together:
        amity sat at its 100,000 cap and the hold at 4,952/4,952 with the overflow discarded,
        so a real round moved neither witness, and the panel only looked closed because the
        "unclaimed goods will be discarded" dialog was over it. The run ended with the strip
        reading 4 of 6 rounds spent and Exchange still gold.

        So a commit claiming `exhausted` while the panel still classifies READY must not stop
        the loop — that combination cannot occur in the game, and believing it cost a day.
        """
        commits = []
        a = _activity(context=C.BARTER_PANEL_READY,
                      commit=lambda: commits.append(1) or (
                          {"ok": True} if len(commits) <= 2 else {"exhausted": True,
                                                                 "reason": "rounds spent"}))
        r, calls = _run_to_completion(a, GOAL, _state())
        self.assertGreater(len(commits), 3,
                           "it kept tapping the gold button instead of believing 'exhausted'")

    def test_it_will_not_tap_forever(self):
        """The only thing worth guarding here is a loop. A day holds SEVEN rounds — the strip
        draws eight slots but the last must be paid for (user, 2026-08-31)."""
        from brain.activities.village import _MAX_DAILY_ROUNDS
        commits = []
        a = _activity(context=C.BARTER_PANEL_READY,
                      commit=lambda: commits.append(1) or {"ok": False, "reason": "no change"})
        r, calls = _run_to_completion(a, GOAL, _state())
        self.assertEqual(r.status, FINISHED)
        self.assertLessEqual(len(commits), _MAX_DAILY_ROUNDS + 1,
                             "bounded by what a day can hold")

    def test_the_rounds_come_from_the_panel_not_from_a_plan(self):
        self.assertNotIn("rounds", str(GOAL))
        self.assertFalse(hasattr(GOAL, "rounds"))


class WhyItStoppedIsTheLoadBearingField(unittest.TestCase):

    def test_a_grey_exchange_means_rounds_REMAIN(self):
        """The panel is still OPEN, so a round is available and something is short. This is
        NOT the day ending — that is a different screen (the submenu closes)."""
        r = _activity(context=C.BARTER_PANEL_BLOCKED,
                      panels=[_panel(materials={"Wares": (155, 2), "Firearms": (0, 2)})]
                      ).work(GOAL, _state())
        self.assertEqual(r.status, FINISHED)
        self.assertIn("grey", r.observed["stopped_because"])
        self.assertIn("Firearms", r.observed["stopped_because"])

    def test_a_closed_submenu_is_the_days_rounds_spent(self):
        """The live crash: the game closes the submenu itself when the rounds run out."""
        a = _activity(context=C.VILLAGE_TOP_MENU)
        a._open_panel = lambda: "unavailable"
        r = a.work(GOAL, _state())
        self.assertEqual(r.status, FINISHED)
        self.assertIn("used up", r.observed["stopped_because"])

    def test_a_good_not_on_offer_says_so(self):
        r = _activity(context=C.BARTER_PANEL_NO_GOOD, selected=False).work(GOAL, _state())
        self.assertIn("not on offer today", r.observed["stopped_because"])
        self.assertEqual(r.observed["rounds_committed"], 0)


class ItReportsInTaskVocabulary(unittest.TestCase):

    def test_it_reports_the_amity_and_the_materials_left(self):
        left = {"Wares": 155, "Firearms": 0, "Sundries": 148}
        r = _activity(context=C.BARTER_PANEL_BLOCKED,
                      panels=[_panel(materials=left, amity=(64769, 100000))]
                      ).work(GOAL, _state())
        self.assertEqual(r.observed["amity"], (64769, 100000))
        self.assertEqual(r.observed["materials_left"], left)

    def test_it_says_nothing_about_panels_or_tiles(self):
        """The task must not receive UI vocabulary it would have to understand."""
        r = _activity(context=C.BARTER_PANEL_BLOCKED).work(GOAL, _state())
        words = " ".join(str(v) for v in r.observed).lower()
        for ui_word in ("tile", "tab", "button", "banner"):
            self.assertNotIn(ui_word, words)


class AnOpenPanelIsNotASelectedGood(unittest.TestCase):
    """Live 2026-08-26 at Svear the bot had Naverslojd selected while it wanted Birch Tree.

    A panel full of Naverslojd's materials reads exactly like one full of Birch Tree's:
    populated. Only tapping and READING BACK establishes which good is selected.
    """

    def test_selecting_taps_and_reads_back(self):
        selected = []
        a = _activity(context=C.BARTER_PANEL_NO_GOOD)
        a._select = lambda g, r: selected.append(g) or True
        res = a.work(GOAL, _state())
        self.assertEqual(selected, ["Birch Tree"])
        self.assertEqual(res.status, WORKING, "selection is a step, not the goal")

    def test_nothing_is_committed_when_the_good_cannot_be_selected(self):
        commits = []
        a = _activity(context=C.BARTER_PANEL_NO_GOOD, selected=False,
                      commit=lambda: commits.append(1) or {"ok": True})
        r = a.work(GOAL, _state())
        self.assertEqual(commits, [], "a failed selection must not proceed to barter")
        self.assertEqual(r.observed["rounds_committed"], 0)


class ItStaysInsideItsWorld(unittest.TestCase):

    def test_somewhere_else_hands_back(self):
        r = _activity().work(GOAL, _state("building:market"))
        self.assertEqual(r.status, UNRECOGNISED)

    def test_a_goal_it_cannot_serve_is_refused(self):
        self.assertEqual(_activity().work("sell the hold", _state()).status, BLOCKED)

    def test_a_MISS_hands_back_and_names_the_screen(self):
        """A miss is not an error — it is the normal signal that the context hypothesis
        expired. What is wrong is the caller's POSITION, and it can only correct itself if
        told what is there."""
        r = _activity(context=C.MISS).work(GOAL, _state())
        self.assertEqual(r.status, UNRECOGNISED)
        self.assertEqual(r.observed["screen"], "Village interior")
        self.assertIn("Village interior", r.detail)

    def test_a_panel_that_will_not_open_is_being_lost(self):
        a = _activity(context=C.VILLAGE_TOP_MENU, opened=False)
        self.assertEqual(a.work(GOAL, _state()).status, UNRECOGNISED)

    def test_every_context_state_has_a_handler(self):
        """A state with no handler is a MISS by construction — the two cannot drift."""
        self.assertEqual(set(VillageActivity._HANDLERS),
                         set(VillageActivity.CONTEXT_STATES))


if __name__ == "__main__":
    unittest.main()


class TheCountBelongsToTheBarterNotTheActivity(unittest.TestCase):
    """`default_activities()` registers ONE VillageActivity and the dispatcher reuses it.

    A round count kept on the instance would report the previous mission's rounds as this
    one's — a datum outliving its owner, which is the lifetime rule (Guiding Principle #4)
    applied to a counter. The goal is the owner, so the count resets when the goal does.
    """

    def test_a_new_goal_resets_the_count(self):
        a = _activity(context=C.BARTER_PANEL_READY, commit=lambda: {"ok": True})
        a.work(GOAL, _state())
        a.work(GOAL, _state())
        self.assertEqual(a._committed, 2)

        # The panel still shows the PREVIOUS good, so the new barter's first action is to
        # select ours — an open panel is not our good. The count is what is being pinned.
        other = Barter("Bambara Groundnut", "Hutu Village")
        r = a.work(other, _state())
        self.assertEqual(a._committed, 0, "the new barter starts from zero")
        self.assertEqual(r.observed["rounds_committed"], 0)
        self.assertEqual(r.status, WORKING)

    def test_the_same_goal_keeps_counting(self):
        a = _activity(context=C.BARTER_PANEL_READY, commit=lambda: {"ok": True})
        for _ in range(3):
            a.work(GOAL, _state())
        self.assertEqual(a._committed, 3)


class SelectionIsBounded(unittest.TestCase):
    """An action that reports success but changes nothing must not be repeated for ever.

    `_select_trade_good` taps and READS BACK, so a True that leaves the panel showing
    another good means the tap and the read disagree. Retrying on every tick would be a
    WORKING loop with no end — the sub-loop failure re-created one level up, and the reason
    `run_goal`'s stall counter cannot catch it (each tick IS progress by its measure).
    """

    def test_a_selection_that_never_takes_gives_up_and_says_so(self):
        a = _activity(context=C.BARTER_PANEL_READY,
                      panels=[_panel(good="Naverslojd")], selected=True)
        for _ in range(6):
            res = a.work(GOAL, _state())
            if res.status != WORKING:
                break
        self.assertEqual(res.status, BLOCKED)
        self.assertIn("still shows", res.observed["stopped_because"])
        self.assertIn("Naverslojd", res.observed["stopped_because"])

    def test_the_right_good_is_never_re_selected(self):
        selected = []
        a = _activity(context=C.BARTER_PANEL_READY, panels=[_panel(good="Birch Tree")],
                      commit=lambda: {"ok": True})
        a._select = lambda g, r: selected.append(g) or True
        a.work(GOAL, _state())
        a.work(GOAL, _state())
        self.assertEqual(selected, [], "the panel already shows ours — do not tap")


class AnUnnamedPanelWeSelectedOurselvesIsNotReSelected(unittest.TestCase):
    """Live 2026-09-05 at Berber: Argan Oil's name does not OCR on the panel on ANY frame.

    `_select_trade_good` picked its tile and accepted it on the game's own live Exchange —
    and `_wrong_good` then called the result 'unreadable' and sent it back to select again,
    forever. Six taps a minute, no rounds, then BLOCKED with every material aboard.

    Confirming an unreadable name BY SELECTING means re-tapping the same tile and re-reading
    the same label that already failed. A repeat read measures the reader, not the panel.
    """

    ARGAN = Barter("Argan Oil", "Berber Village")

    def _unnamed(self):
        # What Berber actually read back: no name, the yield and the materials present.
        return _panel(good=None, materials={"Medicine": 73, "Food": 126})

    def test_the_first_unnamed_panel_is_still_confirmed_by_selecting(self):
        """Before WE have selected, a populated panel is somebody else's good until proven
        otherwise — the Svear case, and it is untouched."""
        selected = []
        a = _activity(context=C.BARTER_PANEL_READY, panels=[self._unnamed()], refused=False)
        a._select = lambda g, r: selected.append(g) or True
        res = a.work(self.ARGAN, _state())
        self.assertEqual(selected, ["Argan Oil"], "an unexplained panel is selected once")
        self.assertEqual(res.status, WORKING)

    def test_after_selecting_it_is_accepted_and_bartered(self):
        commits = []
        a = _activity(context=C.BARTER_PANEL_READY, panels=[self._unnamed()], refused=False,
                      commit=lambda: commits.append(1) or {"ok": True})
        a.work(self.ARGAN, _state())            # tick 1 — selects
        a.work(self.ARGAN, _state())            # tick 2 — must commit, not re-select
        self.assertEqual(commits, [1], "the tile we chose ourselves must not be re-litigated")

    def test_it_does_not_block_at_the_select_cap(self):
        a = _activity(context=C.BARTER_PANEL_READY, panels=[self._unnamed()], refused=False)
        res, _ = _run_to_completion(a, self.ARGAN, _state())
        self.assertNotIn("still shows", str((res.observed or {}).get("stopped_because") or ""),
                         "the unreadable name must not be what stops the barter")

    def test_a_panel_that_NAMES_another_good_is_still_rejected(self):
        """The protection that matters is untouched: a positive identification of somebody
        else's good sends us back to select, however many times we have already tried."""
        a = _activity(context=C.BARTER_PANEL_READY, refused=False,
                      panels=[_panel(good="Naverslojd")])
        a._selects = 1                          # we have already selected once
        self.assertEqual(a._wrong_good(self.ARGAN), "Naverslojd")


class ABarterThatCommittedRoundsAndStoppedIsSuccess(unittest.TestCase):
    """Live 2026-09-05 at Berber. The fifth round consumed the last Mutton AND the last of
    the day's five barters. Exchange greyed, no tile could light it, and the activity
    reported "'Argan Oil' is not on offer today" — as a FAILURE.

    The mission ended on that verdict with ~3,900 units of Argan Oil aboard. It never
    sailed to London and never sold. The good was manifestly on offer: it had just been
    bartered five times in four minutes.
    """

    ARGAN = Barter("Argan Oil", "Berber Village")

    def _spent(self, committed):
        """A village that will not select: the materials are gone and the day is spent."""
        a = _activity(context=C.BARTER_PANEL_NO_GOOD, selected=False, refused=True)
        # The count belongs to THIS barter, so the goal key must match or work() resets it.
        a._goal_key = (self.ARGAN.good, self.ARGAN.village)
        a._committed = committed
        return a

    def test_it_does_not_call_a_finished_barter_a_failure(self):
        res = self._spent(5).work(self.ARGAN, _state())
        self.assertEqual(res.status, FINISHED,
                         "five committed rounds then a grey Exchange is a barter that WORKED")
        self.assertEqual(res.observed["rounds_committed"], 5)

    def test_it_does_not_claim_the_good_was_never_on_offer(self):
        res = self._spent(5).work(self.ARGAN, _state())
        self.assertNotIn("not on offer", res.observed["stopped_because"],
                         "it was on offer — we bartered it five times")

    def test_with_no_rounds_committed_it_IS_still_not_on_offer(self):
        """The original reading is right when nothing was ever bartered — untouched."""
        res = self._spent(0).work(self.ARGAN, _state())
        self.assertEqual(res.status, BLOCKED)
        self.assertIn("not on offer", res.observed["stopped_because"])
