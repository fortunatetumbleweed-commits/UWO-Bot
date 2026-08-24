"""The barter executor sizes rounds from the PANEL, not from the pre-sail plan (gap #6).

The 2026-08-20 fleet death took 75% of the materials and the loop never noticed, because
it read the panel and discarded the reading. These tests pin the fix: the panel is ground
truth, a shortfall is surfaced, and an unreadable panel is not silently treated as fine."""
import types
import unittest
from unittest import mock

from brain.barter_mission_live import make_live_executors


def _mat(label, have, need):
    return types.SimpleNamespace(label=label, have=have, need=need)


def _panel(materials, output=552):
    return types.SimpleNamespace(materials=materials, output_quantity=output,
                                 amity_points=(60000, 100000), amity_grade="Neutral",
                                 selected_good="Box of Nutmeg")


def _task(rounds):
    return types.SimpleNamespace(params={"rounds": rounds, "good": "Box of Nutmeg"})


class BarterExecutorPanelTests(unittest.TestCase):
    def _run(self, panel, rounds=6, commit_ok=True):
        commits = {"n": 0}

        def _commit(*_a, **_k):
            commits["n"] += 1
            return {"ok": commit_ok, "reason": "committed" if commit_ok else "no change"}

        with mock.patch("actions.barter_reader.read_barter_panel", return_value=panel), \
             mock.patch("actions.barter_executor.barter_commit_verified",
                        side_effect=_commit), \
             mock.patch("capture.adb_capture.capture_screen", return_value=object()):
            res = make_live_executors(object())["barter"](_task(rounds))
        return res, commits["n"]

    def test_the_melanesian_shortfall_is_caught_on_arrival(self):
        # Plan wanted 6 rounds; the hold (post fleet-death) funds exactly 1.
        panel = _panel([_mat("Ebony", 167, 152), _mat("Coral", 252, 204),
                        _mat("Textiles", 222, 180)])
        res, commits = self._run(panel, rounds=6)
        self.assertEqual(commits, 1)                     # NOT 6 — the panel bound it
        self.assertEqual(res["committed"], 1)
        short = res["material_shortfall"]
        self.assertEqual((short["planned"], short["fundable"]), (6, 1))
        self.assertEqual(short["binding"], "Ebony")
        self.assertEqual(res["panel_on_arrival"]["Ebony"], (167, 152))

    def test_the_arrival_panel_bounds_the_target_even_if_later_reads_go_stale(self):
        """A static/stale panel read must not let the loop keep attempting rounds — the
        arrival bound is what caps the target."""
        panel = _panel([_mat("Ebony", 167, 152)])       # funds exactly 1 round
        res, commits = self._run(panel, rounds=6)
        self.assertEqual(commits, 1)
        self.assertEqual(res["attempted_rounds"], 1)

    def test_a_healthy_hold_runs_the_planned_rounds_with_no_shortfall(self):
        panel = _panel([_mat("Ebony", 10_000, 152), _mat("Coral", 10_000, 204)])
        res, commits = self._run(panel, rounds=4)
        self.assertEqual(commits, 4)
        self.assertIsNone(res["material_shortfall"])
        self.assertTrue(res["ok"])

    def test_a_short_material_stops_the_loop_before_any_commit(self):
        # Textiles 0 → the game greys out Exchange; never tap into a refusal.
        panel = _panel([_mat("Ebony", 700, 152), _mat("Textiles", 0, 180)])
        res, commits = self._run(panel, rounds=6)
        self.assertEqual(commits, 0)
        self.assertFalse(res["ok"])
        self.assertEqual(res["material_shortfall"]["fundable"], 0)

    def test_a_partial_round_is_reported_but_not_spent(self):
        # ~9.9% of a round is fundable; spending a daily round on it is the caller's call.
        panel = _panel([_mat("Ebony", 15, 152), _mat("Coral", 48, 204)])
        res, commits = self._run(panel, rounds=6)
        self.assertEqual(commits, 0)
        self.assertGreater(res["partial_round_left"], 0.09)
        self.assertLess(res["partial_round_left"], 0.11)

    # ── Unreadable panel: OPEN it, and if that fails, do not commit ──────────────
    #
    # This used to fall back to "attempt one verified commit and let the gate stop it".
    # `barter_commit_verified` acts on an ALREADY-OPEN panel, so on the village INTERIOR
    # that meant hunting for a positive button on a screen that has none. Live 2026-08-22:
    #     [mission.barter] barter panel unreadable on arrival — running VERIFIED commits
    #     [commit] iter 0: no positive button found — settled after 0 tap(s)
    #     FAILED at step mission: barter failed: barter commit stalled after 0
    # — twice, without ever tapping the 'barter' item perceive had just read out by name.
    # Not knowing where we are is a reason to stop and re-establish position, never a reason
    # to start tapping.

    def test_an_unreadable_panel_is_opened_first(self):
        with mock.patch("brain.barter_mission_live._open_barter_panel",
                        return_value=False) as opener:
            self._run(None, rounds=3, commit_ok=False)
        opener.assert_called_once()

    def test_an_unopenable_panel_commits_nothing(self):
        with mock.patch("brain.barter_mission_live._open_barter_panel", return_value=False), \
             mock.patch("brain.barter_mission_live._no_panel_failure",
                        return_value={"ok": False, "reason": "panel not open",
                                      "screen": "Village interior", "submenu": None}):
            res, commits = self._run(None, rounds=3, commit_ok=False)
        self.assertEqual(commits, 0, "committing from an unidentified screen taps blind")
        self.assertFalse(res["ok"])

    def test_the_refusal_names_the_screen_it_saw(self):
        """The caller's state is what is wrong; it can only fix itself if told what is there."""
        with mock.patch("brain.barter_mission_live._open_barter_panel", return_value=False), \
             mock.patch("brain.barter_mission_live._no_panel_failure",
                        return_value={"ok": False, "reason": "panel not open",
                                      "screen": "Village interior", "submenu": None}):
            res, _ = self._run(None, rounds=3)
        self.assertIn("Village interior", res["screen"])

    def test_an_unreadable_panel_does_not_invent_a_shortfall(self):
        with mock.patch("brain.barter_mission_live._open_barter_panel", return_value=False), \
             mock.patch("brain.barter_mission_live._no_panel_failure",
                        return_value={"ok": False, "reason": "panel not open",
                                      "screen": "Village interior", "submenu": None}):
            res, _ = self._run(None, rounds=3)
        self.assertIsNone(res.get("material_shortfall"))


if __name__ == "__main__":
    unittest.main()
