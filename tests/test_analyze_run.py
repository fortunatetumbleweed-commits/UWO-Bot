"""The post-run analyser: count the failures instead of reading past them.

On 2026-08-26 `title_says_world_map` returned False on 85 of 89 looks — 96% — from the day's
first departure. Every gathering leg still sailed, because each needed one lucky read and the
goal retried until it got one, so the defect cost nothing until the Lisboa leg and had been
sitting in the log for ten hours by then.

Run over that morning's window alone, this tool surfaces it, plus the port-name reader and the
bulk checkbox — the three bugs that were found one at a time over the following hours.
"""

from __future__ import annotations

import unittest

from tools.analyze_run import analyse, report


def _log(*lines):
    return [f"2026-08-26 {t} | {lvl} | {msg}" for t, lvl, msg in lines]


class TheLogFormatIsTheBotsOwn(unittest.TestCase):
    """The fixture above invented a format. Read one the bot actually wrote.

    `_LINE` matched `| INFO |` while loguru writes `| INFO     |`, so every session log
    analysed to zero lines and the tool reported "(none)" for a run full of warnings. The
    tests passed throughout, because `_log` built the separator the regex wanted.
    """

    def test_a_real_loguru_line_is_read(self):
        line = ("2026-09-08 11:27:14.071 | WARNING  | brain.dispatcher:step:581 - "
                "[dispatch] something unusual happened")
        a = analyse([line])
        self.assertEqual(1, a["total"], "a padded level is still a level")
        self.assertIn("something unusual happened", report(a))


class TheSuspectHeuristic(unittest.TestCase):

    def test_a_flag_that_rarely_goes_one_way_is_flagged(self):
        """The world-map case: it CAN be True, and almost never is."""
        lines = _log(*[("00:0%d:00.0" % (i % 10), "INFO",
                        f"World map check: title_says_world_map={'True' if i < 2 else 'False'}")
                       for i in range(30)])
        out = report(analyse(lines))
        self.assertIn("title_says_world_map", out)
        self.assertIn("<-- SUSPECT", out)

    def test_a_flag_that_never_goes_the_other_way_is_not_flagged(self):
        """`has_sea_hud` is False everywhere in port, and that is simply correct — a check
        that never applies is not a check that is broken."""
        lines = _log(*[("00:0%d:00.0" % (i % 10), "INFO", "check: has_sea_hud=False")
                       for i in range(30)])
        out = report(analyse(lines))
        self.assertIn("has_sea_hud", out)
        self.assertNotIn("<-- SUSPECT", out)

    def test_a_balanced_flag_is_not_flagged(self):
        lines = _log(*[("00:0%d:00.0" % (i % 10), "INFO",
                        f"check: ok={'True' if i % 2 else 'False'}") for i in range(20)])
        self.assertNotIn("<-- SUSPECT", report(analyse(lines)))

    def test_too_few_samples_to_judge(self):
        lines = _log(("00:00:01.0", "INFO", "check: rare=False"))
        self.assertIn("no boolean checks seen", report(analyse(lines)))


class RepeatedFailuresGroupByShape(unittest.TestCase):

    def test_the_same_failure_with_different_numbers_counts_as_one(self):
        lines = _log(*[("00:0%d:00.0" % i, "WARNING",
                        f"[open_world_map] loc='world_map' not port/sea — attempt {i}/10")
                       for i in range(1, 6)])
        out = report(analyse(lines))
        self.assertIn("5x", out.replace("   5x", "5x"))
        self.assertIn("attempt N/N", out)

    def test_warnings_and_errors_count_even_without_a_failure_word(self):
        lines = _log(("00:00:01.0", "ERROR", "something unusual happened"))
        self.assertIn("something unusual happened", report(analyse(lines)))

    def test_ordinary_info_lines_are_not_failures(self):
        lines = _log(("00:00:01.0", "INFO", "arrived at Lisboa"))
        self.assertIn("(none)", report(analyse(lines)))

    def test_the_window_bounds_are_honoured(self):
        lines = _log(("00:00:01.0", "ERROR", "early failure"),
                     ("12:00:01.0", "ERROR", "late failure"))
        out = report(analyse(lines, since="10:00:00"))
        self.assertIn("late failure", out)
        self.assertNotIn("early failure", out)
