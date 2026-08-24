"""A mission remembers how far it got, so finished steps are never re-run.

Live 2026-08-22, run 27: the fleet was standing IN Melanesian Village — its own destination,
all three materials aboard — and the mission started over from step one. It opened the world
map to run a REMOTE check on the village it was inside, could not open the map from a
village, pressed Back three times, and ended up at sea:

    [classify] -> village (left-menu vocab match ['barter', 'explore', ...])
    [open_world_map] loc='village' not port/sea — waiting to re-perceive
    [open_world_map] loc='village' has persisted 3 re-perceives — trying to exit
    Exiting to port overworld...  Not on overworld — pressing back
    [classify] -> sea

The rule (user, 2026-08-22): "if it has started sailing to the village, then no check for
material anymore, just arrive and barter". Once the voyage is committed, the materials
aboard are the materials we barter with.
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from brain import mission_progress as mp
from memory import observed_facts


class MissionProgress(unittest.TestCase):

    def setUp(self):
        observed_facts._reset_for_tests()
        self._save = patch.object(observed_facts, "_save", lambda: None)
        self._save.start()
        self.addCleanup(self._save.stop)

    def test_nothing_in_flight_reports_no_progress(self):
        self.assertFalse(mp.at_least("bartering"))
        self.assertIsNone(mp.current())

    def test_a_started_mission_is_only_in_planning(self):
        mp.start("Melanesian Village", "Box of Nutmeg", 3)
        self.assertTrue(mp.at_least("planning"))
        self.assertFalse(mp.at_least("bartering"))

    def test_the_barter_phase_implies_gathering_is_over(self):
        mp.start("Melanesian Village", "Box of Nutmeg", 3)
        mp.advance("bartering")
        self.assertTrue(mp.at_least("bartering"))
        self.assertTrue(mp.at_least("gathering"), "later phases imply the earlier ones")

    def test_progress_never_moves_backwards(self):
        mp.start("Melanesian Village", "Box of Nutmeg", 3)
        mp.advance("bartering")
        mp.advance("planning")
        self.assertTrue(mp.at_least("bartering"))

    def test_progress_toward_one_village_says_nothing_about_another(self):
        mp.start("Melanesian Village", "Box of Nutmeg", 3)
        mp.advance("bartering")
        self.assertTrue(mp.at_least("bartering", village="Melanesian Village"))
        self.assertFalse(mp.at_least("bartering", village="Apache Village"))

    def test_the_village_match_is_case_insensitive(self):
        mp.start("Melanesian Village", "Box of Nutmeg", 3)
        mp.advance("bartering")
        self.assertTrue(mp.at_least("bartering", village="melanesian village"))

    def test_a_stale_mission_is_not_trusted(self):
        """Half a day later the ship may have been sailed by hand and the window re-rolled."""
        mp.start("Melanesian Village", "Box of Nutmeg", 3)
        mp.advance("bartering")
        rec = observed_facts._load()[mp._KEY]
        rec["at"] = time.time() - (mp._MAX_AGE_S + 60)
        self.assertFalse(mp.at_least("bartering"))

    def test_finishing_clears_it(self):
        mp.start("Melanesian Village", "Box of Nutmeg", 3)
        mp.advance("bartering")
        mp.finish()
        self.assertIsNone(mp.current())

    def test_the_planned_rounds_survive_a_restart(self):
        """The resume path barters the rounds the plan sized, not a guess."""
        mp.start("Melanesian Village", "Box of Nutmeg", 3)
        mp.advance("bartering")
        self.assertEqual(mp.current()["rounds"], 3)

    def test_advancing_without_a_mission_is_harmless(self):
        mp.advance("bartering")
        self.assertIsNone(mp.current())

    def test_an_unknown_phase_is_rejected(self):
        with self.assertRaises(ValueError):
            mp.advance("teleported")


if __name__ == "__main__":
    unittest.main()


class RecipeIsPinnedAtTaskStart(unittest.TestCase):
    """The village is remote-checked ONCE, when the task starts.

    The rule (user, 2026-08-22): "the bot should only remote check the village when starting
    the task. After it has checked, we just use the originally recipe, even if the time has
    changed and refreshed, it is ok, it just means we may get less."

    Re-checking costs a trip to the world map on every run, and from a village that trip
    means leaving — which is how run 27 put the fleet back out to sea.
    """

    def setUp(self):
        observed_facts._reset_for_tests()
        self._save = patch.object(observed_facts, "_save", lambda: None)
        self._save.start()
        self.addCleanup(self._save.stop)

    RECIPE = {"good": "Box of Nutmeg", "obtain": 679,
              "materials": {"Ebony": 152, "Coral": 228, "Textiles": 228}}

    def test_the_recipe_is_kept_with_the_mission(self):
        mp.start("Melanesian Village", "Box of Nutmeg", 3, recipe=self.RECIPE,
                 rounds_remaining=7)
        self.assertEqual(mp.current()["recipe"], self.RECIPE)
        self.assertEqual(mp.current()["rounds_remaining"], 7)

    def test_the_pinned_recipe_is_reused_instead_of_re_checking(self):
        from brain.barter_command import _cached_trade
        mp.start("Melanesian Village", "Box of Nutmeg", 3, recipe=self.RECIPE)
        trade = _cached_trade(mp.current())
        self.assertEqual(trade.obtain, 679)
        self.assertEqual(trade.materials["Coral"], 228)

    def test_a_stale_ratio_is_accepted_rather_than_re_checked(self):
        """Even hours later the pinned recipe is used — less output beats another trip."""
        from brain.barter_command import _cached_trade
        mp.start("Melanesian Village", "Box of Nutmeg", 3, recipe=self.RECIPE)
        rec = observed_facts._load()[mp._KEY]
        rec["at"] = time.time() - 3600            # an hour old, still inside the window
        self.assertIsNotNone(_cached_trade(mp.current()))

    def test_no_recipe_means_a_real_check_is_needed(self):
        from brain.barter_command import _cached_trade
        self.assertIsNone(_cached_trade(None))
        mp.start("Melanesian Village", "Box of Nutmeg", 3)
        self.assertIsNone(_cached_trade(mp.current()))

    def test_a_recipe_without_materials_is_not_usable(self):
        from brain.barter_command import _cached_trade
        mp.start("Melanesian Village", "Box of Nutmeg", 3,
                 recipe={"good": "Box of Nutmeg", "obtain": 679, "materials": {}})
        self.assertIsNone(_cached_trade(mp.current()))


class ThePhasesCloseTheirQuestions(unittest.TestCase):
    """Each phase boundary settles something for the rest of the task (user, 2026-08-22):

        planning   — remote-check the village ONCE, size the materials. Then no more
                     remote checks.
        gathering  — buy what is missing, sell the surplus. Then no more cargo checks.
        bartering  — sail to the village and barter. No village check, no cargo check.
    """

    def setUp(self):
        observed_facts._reset_for_tests()
        self._save = patch.object(observed_facts, "_save", lambda: None)
        self._save.start()
        self.addCleanup(self._save.stop)
        mp.start("Melanesian Village", "Box of Nutmeg", 3,
                 recipe={"good": "Box of Nutmeg", "obtain": 679,
                         "materials": {"Ebony": 152, "Coral": 228, "Textiles": 228}})

    def test_planning_still_allows_the_one_remote_check(self):
        self.assertFalse(mp.at_least("gathering"))

    def test_gathering_ends_remote_checking(self):
        mp.advance("gathering")
        self.assertTrue(mp.at_least("gathering"))

    def test_bartering_ends_cargo_checking(self):
        mp.advance("bartering")
        self.assertTrue(mp.at_least("gathering"), "gathering is behind us")
        self.assertTrue(mp.at_least("bartering"))

    def test_the_phases_are_ordered(self):
        self.assertEqual(mp.PHASES,
                         ("planning", "gathering", "bartering", "sailing_route", "done"))

    def test_a_finished_barter_moves_to_the_tail_not_to_done(self):
        """The output is aboard; the route and the sale still have to happen."""
        mp.advance("sailing_route")
        self.assertTrue(mp.at_least("sailing_route"))
        self.assertFalse(mp.at_least("done"))

    def test_the_tail_phase_never_re_runs_the_barter(self):
        """A daily round spent twice is unrecoverable — the phase record is what stops it."""
        mp.advance("sailing_route")
        self.assertTrue(mp.at_least("bartering"), "bartering is behind us")

    def test_a_finished_task_leaves_nothing_to_resume(self):
        mp.advance("done")
        mp.finish()
        self.assertIsNone(mp.current())
        self.assertFalse(mp.at_least("bartering"))


class TheRecordDoesNotLeak(unittest.TestCase):
    """A mission record is persisted, so it outlives the process — and the test suite.

    Live 2026-08-23: a test started a mission, the record landed in
    memory/knowledge/state/observed_facts.json, and five TestOverloadedHold tests then took
    `run_barter_command`'s resume path instead of planning. Each passed alone and failed in
    the suite. conftest now redirects the store to a tmp file per test.

    Also guards a second slip: `current()` decorates its return with a COMPUTED age, and
    `advance()` wrote the whole dict back — persisting `age_s` as if it were data, where it
    was wrong the instant it was written.
    """

    def setUp(self):
        observed_facts._reset_for_tests()
        self._save = patch.object(observed_facts, "_save", lambda: None)
        self._save.start()
        self.addCleanup(self._save.stop)

    def test_the_computed_age_is_not_persisted(self):
        mp.start("Melanesian Village", "Box of Nutmeg", 3)
        mp.advance("gathering")
        self.assertNotIn("age_s", observed_facts._load()[mp._KEY]["value"])

    def test_the_store_is_isolated_from_the_real_state_file(self):
        """conftest must have redirected _PATH away from memory/knowledge/state/."""
        self.assertNotIn("memory/knowledge/state", str(observed_facts._PATH))


class TheResumePathActuallyRuns(unittest.TestCase):
    """The resume path must be able to build its executors.

    Live 2026-08-23, run 30: the phase model correctly routed a mission at `bartering`
    straight to arrive-and-barter, and then crashed before a single tap:

        File "brain/barter_command.py", line 174, in _resume_at_village
            ex = make_live_executors()
        TypeError: make_live_executors() missing 1 required positional argument: 'opp'

    Every unit test had injected fake executors, so nothing ever called the real factory.
    `opp` is accepted for the graph path's signature but read by no executor.
    """

    def test_executors_build_without_an_opportunity(self):
        from brain.barter_mission_live import make_live_executors
        ex = make_live_executors()
        for kind in ("sail_to_village", "barter", "gather", "sell"):
            self.assertIn(kind, ex)

    def test_the_graph_path_signature_still_works(self):
        from brain.barter_mission_live import make_live_executors
        self.assertIn("barter", make_live_executors(object()))
