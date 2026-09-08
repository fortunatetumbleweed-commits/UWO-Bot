"""Daily news appears once per KOREAN day, so it is checked against the Korean date.

"Daily news shows up exactly one time a day... if it is not a new day (Korean time), and it
has been closed before, it should not check it" (user, 2026-09-08).

This replaces a one-hour TTL that was standing in for the same fact. The hour was a guess;
the day is what the game states. A false positive is the expensive failure here — dismissing
a phantom means tapping a disc on somebody's transaction — so the value is correctness, not
speed. The 285s this check cost on 2026-09-08 was the OmniParser pass it redid because
`parse_screen` cleared the frame cache; with that gone it measures 0.00s on a parsed frame.
"""

from __future__ import annotations

import json
import unittest
import unittest.mock as mock


class TheDayIsKorean(unittest.TestCase):

    def test_the_date_comes_from_the_games_clock_not_this_machine(self):
        import brain.perceive as bp
        from vision.trade_event_reader import KST
        from datetime import datetime
        self.assertEqual(datetime.now(KST).date().isoformat(), bp._korean_today(),
                         "one definition of Korean time, shared with the event reader")


class OnceRecordedItIsNotRaisedAgainToday(unittest.TestCase):

    def setUp(self):
        import brain.perceive as bp
        self.bp = bp

    def test_nothing_recorded_means_check_as_usual(self):
        self.assertFalse(self.bp._daily_news_already_met_today())

    def test_todays_record_suppresses(self):
        self.bp._remember_daily_news_met()
        self.assertTrue(self.bp._daily_news_already_met_today())

    def test_yesterdays_record_does_not(self):
        """A session running across 00:00 KST must see the new day's news."""
        self.bp._DAILY_NEWS_SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.bp._DAILY_NEWS_SEEN_PATH.write_text(json.dumps({"date_kst": "1999-01-01"}))
        self.assertFalse(self.bp._daily_news_already_met_today())

    def test_a_corrupt_record_checks_rather_than_skips(self):
        """Unreadable is not 'already seen'. Failing the other way blinds the bot for a day."""
        self.bp._DAILY_NEWS_SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.bp._DAILY_NEWS_SEEN_PATH.write_text("{ not json")
        self.assertFalse(self.bp._daily_news_already_met_today())


class TheRecordIsMadeWhenItIsCLOSED(unittest.TestCase):
    """Not at detection — a popup still on screen must stay visible to the next look."""

    def test_detecting_alone_records_nothing(self):
        import brain.perceive as bp
        self.assertFalse(bp._daily_news_already_met_today())

    def test_the_gate_declines_to_name_it_when_the_day_is_spent(self):
        import brain.perceive as bp
        bp._remember_daily_news_met()
        with mock.patch.object(bp, "_large_dimmed_popup", lambda f: (True, 20.0, 30.0)), \
             mock.patch.object(bp, "_round_close_x",
                               lambda f: mock.Mock(cx=1794, cy=240)):
            self.assertFalse(bp._has_daily_news_close_x(mock.Mock(width=2400, height=1080)))


if __name__ == "__main__":
    unittest.main()
