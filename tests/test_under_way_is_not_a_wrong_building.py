"""A screen that says the fleet has departed must not be pressed Back.

Live 2026-08-22, run 26 — the departure had already succeeded:

    23:07:42  Finding village 'Melanesian Village' on world map...
    23:09:48  [qwen] detail='sailing to Melanesian Village'          <- it worked
    23:09:51  [harbor] state='building' - sailing to Melanesian Village
    23:09:51  Title 'sailing to melanesian village' doesn't match 'harbor' - pressing Back
    23:12:42  Finding village...   23:19:36  Finding village...   23:24:19  Finding village...
    23:26:14  World map closed (now 'sea') - sailing started

Four cycles, 18.5 minutes, for a departure the first attempt had made in 65 seconds.

The principle (user, 2026-08-22): when the perceived screen does not match the expected
state, the PERCEPTION is ground truth and the stale state is what needs updating. The bot
must not blindly act to force the screen back into line, throwing away what it just saw.
"""

from __future__ import annotations

import unittest

from actions.sail_actions import _screen_says_under_way


class UnderWayIsGroundTruth(unittest.TestCase):

    def test_the_exact_live_title(self):
        self.assertTrue(_screen_says_under_way("building", "sailing to melanesian village"))

    def test_sea_states_count_however_the_title_reads(self):
        self.assertTrue(_screen_says_under_way("sea", ""))
        self.assertTrue(_screen_says_under_way("sea_cinematic", "anything"))

    def test_moving_to_a_village_counts(self):
        self.assertTrue(_screen_says_under_way("building", "moving to village"))

    def test_a_genuinely_wrong_building_is_still_wrong(self):
        """The Back press must survive for the case it exists for."""
        self.assertFalse(_screen_says_under_way("building", "inn"))
        self.assertFalse(_screen_says_under_way("building", "item shop"))

    def test_the_harbour_itself_is_not_under_way(self):
        self.assertFalse(_screen_says_under_way("building", "harbour"))

    def test_an_unreadable_title_is_not_under_way(self):
        """Unknown must not become a licence to abandon navigation either."""
        self.assertFalse(_screen_says_under_way("building", ""))
        self.assertFalse(_screen_says_under_way("building", "title unreadable"))


if __name__ == "__main__":
    unittest.main()
