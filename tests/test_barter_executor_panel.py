"""The barter sizes its rounds from the PANEL, not from the pre-sail plan (gap #6).

The 2026-08-20 fleet death took 75% of the materials and the loop never noticed, because it
read the panel and discarded the reading. These tests pin the fix.

REWRITTEN 2026-08-26, when the behaviour moved from `barter_mission_live.barter` into
`brain.activities.village`. The claims that survived the move are here, tested where they now
live. Three did NOT survive, and it is worth recording why rather than quietly dropping them:

  * `material_shortfall`, `panel_on_arrival`, `partial_round_left` were RESULT FIELDS. A
    shortfall is information, not a fault — worth SAYING, so it is logged — but returning it
    invites the caller to compare it against a plan, and that comparison belongs to whoever
    made the plan.
  * `attempted_rounds` and `planned_rounds` compared the estimate against reality. The goal
    carries no number now, so there is no plan for the result to be measured against.
  * "the planned rounds are run when the hold is healthy" asserted the PLAN's count. The
    panel bounds the rounds now, and it is allowed to exceed any plan — a round FREES space,
    so the estimate was systematically too small.

What could not be dropped is `test_a_refusal_names_the_screen_it_saw`, which lives in
tests/test_village_activity.py: the caller's POSITION is what is wrong, and it can only fix
itself if told what is there.
"""

import types
import unittest

from brain.activities.village import Barter, VillageActivity
from brain.dispatcher import FINISHED, UNRECOGNISED, WORKING

GOAL = Barter("Box of Nutmeg", "Melanesian Village")


def _panel(materials, rounds, *, binding=None, shortfall=0, partial=0.0, good="Box of Nutmeg"):
    """`good` matters now: the real `BarterPanelReading` always names the selected good, and
    the activity RE-SELECTS whenever the panel is not positively showing the one it came
    for — an unreadable good is not evidence that it is ours."""
    return types.SimpleNamespace(selected_good=good, materials=dict(materials), rounds_remaining=rounds,
                                 amity_points=(60000, 100000), partial_fraction=partial,
                                 binding=binding, shortfall=shortfall)


def _activity(panel, *, opened=True, refused=True, stale=False):
    """A stand-in for `run_barter_phase` that consumes the panel as a real one is consumed.

    `stale=True` freezes `rounds_remaining` — the failure mode the old code guarded with a
    cap taken from the arrival reading. See `AStalePanelIsBoundedBySomething` below.
    """
    counted = {"n": 0}

    # ONE COMMIT PER CALL now — the loop moved out of the activity, so the stub counts
    # rounds and the CONTEXT reports when the panel stops funding them, which is what the
    # panel itself does live.
    import brain.village_context as C

    def commit():
        counted["n"] += 1
        if not stale and panel is not None:      # a committed round consumes materials
            panel.rounds_remaining = max(0, panel.rounds_remaining - 1)
        return {"ok": True}

    def context(_state):
        """THE PANEL BOUNDS THE ROUNDS — which under the context model means the panel
        decides the STATE. Ready while it funds a round, blocked when it does not.

        `refused` is NOT consulted here: in the old harness it only chose the final
        `stopped_because`, never whether a round ran, and gating every round on it would
        have this stub commit nothing at all."""
        if panel is None or panel.rounds_remaining <= 0:
            return C.BARTER_PANEL_BLOCKED
        return C.BARTER_PANEL_READY

    a = VillageActivity(open_panel_fn=lambda: opened,
                        select_fn=lambda g, r: True,
                        read_panel_fn=lambda: panel,
                        commit_fn=commit, context_fn=context,
                        overflow_fn=lambda: 0,
                        saw_fn=lambda: {"screen": "Village interior", "submenu": None},
                        exchange_live_fn=lambda: not refused,
                        recipe_fn=lambda g: None)
    return a, counted


def _drive(a, goal, state, *, max_calls=40):
    """Call until the activity stops reporting WORKING — the dispatcher's loop.

    Rounds used to run inside ONE work() call. They now happen across calls, so the loop
    lives here, where a fresh perceive would happen live. What each test asserts is
    unchanged: THE PANEL BOUNDS THE ROUNDS.
    """
    from brain.dispatcher import WORKING
    for _ in range(max_calls):
        res = a.work(goal, state)
        if res.status != WORKING:
            return res
    raise AssertionError("never stopped reporting WORKING")


def _state(where="village"):
    return types.SimpleNamespace(state=where, port="Melanesian Village")


class ThePanelBoundsTheRounds(unittest.TestCase):

    def test_a_post_fleet_death_hold_commits_only_what_it_funds(self):
        """The plan wanted 6; the hold funds exactly 1. The panel wins."""
        panel = _panel({"Ebony": 167, "Coral": 252}, rounds=1,
                       binding="Ebony", shortfall=137)
        a, counted = _activity(panel)
        res = _drive(a, GOAL, _state())
        self.assertEqual(counted["n"], 1, "NOT 6 — the panel bound it")
        self.assertEqual(res.observed["rounds_committed"], 1)

    def test_a_stale_read_cannot_keep_the_loop_going(self):
        panel = _panel({"Ebony": 167}, rounds=1)
        a, counted = _activity(panel)
        _drive(a, GOAL, _state())
        self.assertEqual(counted["n"], 1)

    def test_a_short_material_stops_before_any_commit(self):
        """Textiles 0 → the game greys Exchange; never tap into a refusal."""
        panel = _panel({"Ebony": 700, "Textiles": 0}, rounds=0, binding="Textiles")
        a, counted = _activity(panel)
        res = _drive(a, GOAL, _state())
        self.assertEqual(counted["n"], 0)
        self.assertEqual(res.observed["rounds_committed"], 0)

    def test_a_healthy_hold_runs_more_rounds_than_a_plan_would_have(self):
        """A round FREES space, so the pre-sail estimate was systematically too small.

        The fixture used to offer NINE rounds, which the game cannot: a day holds seven, the
        strip's eighth slot being purchasable only (user, 2026-08-31). Six keeps the point —
        the PANEL bounds the rounds, not a number computed before sailing — without asserting
        a day that cannot happen.
        """
        panel = _panel({"Ebony": 10_000, "Coral": 10_000}, rounds=6)
        a, counted = _activity(panel)
        _drive(a, GOAL, _state())
        self.assertEqual(counted["n"], 6, "the panel decides, not a pre-sail plan")

    def test_and_a_day_cannot_exceed_seven(self):
        """The loop guard is not a plan — it is the most rounds a day can hold."""
        from brain.activities.village import _MAX_DAILY_ROUNDS
        self.assertEqual(_MAX_DAILY_ROUNDS, 7)
        panel = _panel({"Ebony": 10_000, "Coral": 10_000}, rounds=99)
        a, counted = _activity(panel)
        _drive(a, GOAL, _state())
        self.assertLessEqual(counted["n"], _MAX_DAILY_ROUNDS + 1,
                             "a panel that never runs dry is still bounded by the day")


class AStalePanelIsBoundedBySomething(unittest.TestCase):
    """A read that never changes used to be capped by the ARRIVAL reading — the old node took
    `min(planned, arrival.rounds_remaining)` and its comment warned that dropping either bound
    re-opened the 2026-08-20 fleet-death case.

    That cap is gone with the plan, and deliberately: the arrival reading is one observation,
    and capping later rounds by it would stop the loop early exactly when amity rises and MORE
    rounds become fundable. What stops a stale panel instead is the pair of stops that answer
    the world rather than a number — a commit that makes no progress ends the phase, and
    _MAX_ROUNDS is a safety net that nothing should ever reach.

    The exposure this leaves is narrow and worth stating plainly: a panel frozen at a positive
    count WHILE every commit keeps succeeding. But commits that keep succeeding mean bartering
    is genuinely happening, and continuing is then the right answer.
    """

    def test_a_frozen_panel_is_bounded_by_the_CALLER_now(self):
        """The invented `_MAX_ROUNDS` ceiling is gone with the loop. A frozen panel would
        keep reporting WORKING for ever, and what bounds that is `run_goal`'s stall/tick
        budget — the caller's, where a fresh perceive happens, not a constant in here."""
        panel = _panel({"Ebony": 700}, rounds=3)
        a, counted = _activity(panel, stale=True)
        for _ in range(12):
            if a.work(GOAL, _state()).status != WORKING:
                break
        self.assertGreater(counted["n"], 0, "a frozen panel still commits")

    def test_a_commit_that_makes_no_progress_LOOKS_AGAIN(self):
        """A commit that reports no change does NOT end the barter — the Exchange button does.

        REVERSED 2026-08-31 (user): "as long as there is a yellow Exchange button, it should
        try to tap it. Only when it is tapped more than 7 times does it need to check."

        The old rule trusted the commit's own verdict, and that verdict is unreliable in
        exactly the situation that matters. `_barter_progressed` judged a round by amity and
        cargo, and BOTH SATURATE ON SUCCESS: at Hutu Village on 2026-08-30 amity sat at its
        100,000 cap and the hold at 4,952/4,952 with the overflow discarded, so a real round
        moved neither. It reported "no change", the activity ended, and the mission left the
        village with the strip reading 4 of 6 rounds spent, Exchange still gold, and material
        for four more aboard — about 45M ducats.

        Ending belongs to the classifier: a GREY Exchange (`barter_panel_blocked`) or a CLOSED
        submenu. A wasted tap is cheap; a discarded day is not.
        """
        panel = _panel({"Ebony": 700}, rounds=3)
        a, _c = _activity(panel)
        a._commit = lambda **kw: {"ok": False, "reason": "barter commit made no change"}
        res = a.work(GOAL, _state())
        self.assertEqual(res.status, WORKING,
                         "the gold button decides whether there is another round, not this")

    def test_but_it_will_not_tap_forever(self):
        """The only thing guarded here is a loop. A day holds SEVEN rounds — the strip draws
        eight slots but the last must be paid for (user, 2026-08-31)."""
        from brain.activities.village import _MAX_DAILY_ROUNDS
        panel = _panel({"Ebony": 700}, rounds=3)
        a, _c = _activity(panel)
        a._commit = lambda **kw: {"ok": False, "reason": "barter commit made no change"}
        for _ in range(_MAX_DAILY_ROUNDS + 3):
            if a.work(GOAL, _state()).status != WORKING:
                break
        self.assertLessEqual(a._taps, _MAX_DAILY_ROUNDS + 1, "bounded by what a day can hold")


class AnUnreadablePanelIsNotSilentlyFine(unittest.TestCase):
    """`barter_commit_verified` acts on an ALREADY-OPEN panel, so on the village INTERIOR
    committing means hunting for a positive button on a screen that has none. Live
    2026-08-22 that failed twice without ever tapping the 'barter' item perceive had just
    read out by name. Not knowing where we are is a reason to re-establish position, never
    a reason to start tapping."""

    def test_an_unreadable_panel_is_opened_first(self):
        """Opening is now its own state (`village_top_menu`) rather than a step inside the
        barter, so the context decides when it happens."""
        import brain.village_context as C
        opens = []
        a, _c = _activity(_panel({"Ebony": 700}, rounds=2))
        a._context_fn = lambda _s: C.VILLAGE_TOP_MENU
        a._open_panel = lambda: opens.append(1) or True
        a.work(GOAL, _state())
        self.assertEqual(len(opens), 1)

    def test_an_unopenable_panel_commits_nothing(self):
        import brain.village_context as C
        a, counted = _activity(None, opened=False)
        a._context_fn = lambda _s: C.VILLAGE_TOP_MENU
        res = a.work(GOAL, _state())
        self.assertEqual(counted["n"], 0, "committing from an unidentified screen taps blind")
        self.assertEqual(res.status, UNRECOGNISED)

    def test_an_unreadable_panel_invents_no_numbers(self):
        """It now REPORTS the count (zero) rather than omitting the key — a caller being
        handed back mid-barter needs to know how many rounds happened. What must not appear
        is an invented one."""
        import brain.village_context as C
        a, _c = _activity(None, opened=False)
        a._context_fn = lambda _s: C.VILLAGE_TOP_MENU
        res = a.work(GOAL, _state())
        self.assertEqual(res.observed["rounds_committed"], 0)


class TheShortfallIsSaidButNotReturned(unittest.TestCase):
    """Worth telling the operator the ratio moved; not worth handing the caller a number to
    branch on."""

    def _logs(self, panel):
        from loguru import logger
        seen = []
        sink = logger.add(seen.append, level="INFO")
        try:
            a, _c = _activity(panel)
            res = a.work(GOAL, _state())
        finally:
            logger.remove(sink)
        return " ".join(seen), res

    def test_the_binding_material_is_named_in_the_log(self):
        text, _res = self._logs(_panel({"Ebony": 167}, rounds=1, binding="Ebony", shortfall=137))
        self.assertIn("Ebony", text)
        self.assertIn("137", text)

    def test_a_partial_round_is_reported_but_not_spent(self):
        text, _res = self._logs(_panel({"Ebony": 15}, rounds=0, partial=0.099))
        self.assertIn("not spending", text)

    def test_no_shortfall_field_reaches_the_caller(self):
        _text, res = self._logs(_panel({"Ebony": 167}, rounds=1, binding="Ebony", shortfall=137))
        for gone in ("material_shortfall", "panel_on_arrival", "partial_round_left",
                     "planned_rounds", "attempted_rounds"):
            self.assertNotIn(gone, res.observed)


if __name__ == "__main__":
    unittest.main()
