"""recruit_crew_verified: one verified commit, judged by the crew METRIC —
not by "a button was tapped". This is what stops the blind re-tap loop.
"""
import unittest
from unittest.mock import MagicMock

from brain.verified_recruit import recruit_crew_verified, _crew_progressed


class CrewProgressTests(unittest.TestCase):
    def test_increase_is_progress(self):
        ok, _ = _crew_progressed((100, 500), (300, 500)); self.assertTrue(ok)

    def test_capacity_is_progress(self):
        ok, why = _crew_progressed((500, 500), (500, 500))
        self.assertTrue(ok); self.assertIn("capacity", why)

    def test_unchanged_is_not_progress(self):
        ok, _ = _crew_progressed((0, 500), (0, 500)); self.assertFalse(ok)


class RecruitVerifiedTests(unittest.TestCase):
    def _run(self, crew_seq, commit=None):
        """crew_seq: successive read_crew() returns. commit_fn records a call."""
        reads = list(crew_seq)
        commits = []
        commit_fn = commit or (lambda: (commits.append(1) or ["Recruit"]))
        res = recruit_crew_verified(
            settle_secs=0,
            capture_fn=lambda: MagicMock(),
            read_crew_fn=lambda _f: reads.pop(0),
            commit_fn=commit_fn,
        )
        res["_commits"] = len(commits)
        return res

    def test_success_when_crew_increases(self):
        # before=(0,500) → commit → after=(953,2275)
        res = self._run([(0, 500), (953, 2275)])
        self.assertTrue(res["ok"])
        self.assertIn("increased", res["reason"])
        self.assertEqual(res["_commits"], 1)          # committed exactly once

    def test_no_progress_when_crew_flat_reports_stuck(self):
        # The bug scenario: tap did nothing, crew stays flat → ok=False, no re-tap.
        res = self._run([(0, 2275), (0, 2275)])
        self.assertFalse(res["ok"])
        self.assertIn("did not increase", res["reason"])
        self.assertEqual(res["crew_after"], (0, 2275))
        self.assertEqual(res["_commits"], 1)          # ONE commit, then report — no blind loop

    def test_already_full_short_circuits_without_committing(self):
        # crew already at capacity → success, and NO commit tapped.
        res = self._run([(2275, 2275)])
        self.assertTrue(res["ok"])
        self.assertIn("capacity", res["reason"])
        self.assertEqual(res["_commits"], 0)

    def test_none_crew_reading_is_not_false_success(self):
        # Unreadable crew before/after → cannot confirm progress → ok=False.
        res = self._run([None, None])
        self.assertFalse(res["ok"])


if __name__ == "__main__":
    unittest.main()
