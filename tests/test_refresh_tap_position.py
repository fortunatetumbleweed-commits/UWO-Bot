"""Tests for _refresh_tap_position — the pre-tap stale-frame fix.

Regression: in the 2026-05-19 live run two consecutive taps on Port Royal
on the world map failed to open the City Info panel; a third tap on the
same coords worked.  Root cause: the position passed to tap() was 6-15 s
stale (OCR + matching + the anti-bot delay), and during that gap the
Aconite-Boom announcement banner or another overlay shifted the label
off the originally-OCR'd pixel.

The fix re-captures and re-locates *destination* immediately before
tapping.  These tests pin that behaviour.
"""
import unittest

from actions.sail_actions import _refresh_tap_position


class RefreshTapPositionTests(unittest.TestCase):

    def test_fresh_position_replaces_stale_when_found(self):
        stale = (2327, 833)
        fresh = (2280, 845)
        out = _refresh_tap_position(stale, fresh, "port royal")
        self.assertEqual(out, fresh)

    def test_keeps_stale_when_fresh_is_none(self):
        # OCR re-scan failed (label scrolled off, occluded, etc.) — fall
        # back to the original position.  Better than not tapping at all.
        stale = (2327, 833)
        out = _refresh_tap_position(stale, None, "port royal")
        self.assertEqual(out, stale)

    def test_small_drift_below_threshold_still_returns_fresh(self):
        # A 3 px drift is below the log threshold but still uses the fresh
        # value — fresh is always preferred when available, the threshold
        # only controls whether we log the diff.
        stale = (1000, 500)
        fresh = (1003, 502)
        out = _refresh_tap_position(stale, fresh, "lisbon")
        self.assertEqual(out, fresh)

    def test_large_drift_returns_fresh(self):
        stale = (2327, 833)
        fresh = (1085, 824)   # near-full-screen shift after re-pan
        out = _refresh_tap_position(stale, fresh, "port royal")
        self.assertEqual(out, fresh)


if __name__ == "__main__":
    unittest.main()
