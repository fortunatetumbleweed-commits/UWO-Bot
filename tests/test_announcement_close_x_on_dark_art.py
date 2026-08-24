"""The announcement close-X must be found when the popup art is DARK.

Live 2026-08-23, run 29: the fleet sailed to Melanesian Village with an announcement popup
over the screen — a night-sky picture. The detector looked for the densest patch of "dark"
pixels (max channel < 70), and at that threshold 28% of the search band was dark sky, so the
densest window landed on empty sky:

    obstruction detected: popup bbox=(1259,101,2068,438)  close_x@(2018,165)
    [clear_blockers] announcement — tapped close-X @ (1874, 234)      <- night sky
    [clear_blockers] announcement dismiss -> cleared=False

Tapped four times, never cleared. The real button — a round black disc with a white cross,
outside the front panel's rectangle at its top-right — was at (1695, 222). Measured:

    sky            [13 23 51]     max 51: "dark" at <70, not dark at <35
    disc           [0 0 0]        dark at any threshold
    cross (centre) [248 248 248]  bright

Because perception was blocked, the state read as 'unknown', the recovery planner decided to
head "back to overworld" — which from a village means LEAVING it — and the fleet gave up the
position it had just sailed for.
"""

from __future__ import annotations

import os
import unittest

import numpy as np
from PIL import Image

from brain.unexpected_dialog import find_announcement_close_x

FRAME = "/tmp/announcement_night_sky.png"       # captured from the live run
TRUTH = (1695, 222)
TOLERANCE = 30                                   # the disc is ~45px across


def _synthetic(bg=(13, 23, 51), disc_at=TRUTH):
    """A night-sky background with one black disc + white cross, at 2400x1080."""
    arr = np.zeros((1080, 2400, 3), dtype=np.uint8)
    arr[:, :] = bg
    cx, cy = disc_at
    yy, xx = np.mgrid[0:1080, 0:2400]
    disc = (xx - cx) ** 2 + (yy - cy) ** 2 <= 22 ** 2
    arr[disc] = (0, 0, 0)
    cross = disc & ((np.abs(xx - cx) < 4) | (np.abs(yy - cy) < 4))
    arr[cross] = (248, 248, 248)
    return Image.fromarray(arr)


class CloseXOnDarkArt(unittest.TestCase):

    def test_synthetic_night_sky(self):
        got = find_announcement_close_x(_synthetic())
        self.assertIsNotNone(got, "a black disc with a white cross must be found")
        self.assertLess(abs(got[0] - TRUTH[0]), TOLERANCE)
        self.assertLess(abs(got[1] - TRUTH[1]), TOLERANCE)

    def test_dark_art_alone_is_not_a_close_button(self):
        """Night sky with no disc — the old detector returned a point here anyway."""
        arr = np.zeros((1080, 2400, 3), dtype=np.uint8)
        arr[:, :] = (13, 23, 51)
        self.assertIsNone(find_announcement_close_x(Image.fromarray(arr)))

    def test_the_cross_decides_between_two_dark_candidates(self):
        """The white cross DISAMBIGUATES; it is not a hard requirement.

        On a LIGHT background a dark disc is already unambiguous, so demanding a cross would
        reject a real button (tests/test_announcement_dismiss.py draws a plain one). On DARK
        artwork darkness alone is useless — a night sky made 28% of the band "dark". So the
        contract is: when something in the band carries a bright core, that wins.
        """
        arr = np.zeros((1080, 2400, 3), dtype=np.uint8)
        arr[:, :] = (13, 23, 51)                       # night sky
        arr[200:245, 1500:1545] = (0, 0, 0)            # a plain black blob (a shadow)
        cx, cy = TRUTH
        yy, xx = np.mgrid[0:1080, 0:2400]
        disc = (xx - cx) ** 2 + (yy - cy) ** 2 <= 22 ** 2
        arr[disc] = (0, 0, 0)
        arr[disc & ((np.abs(xx - cx) < 4) | (np.abs(yy - cy) < 4))] = (248, 248, 248)
        got = find_announcement_close_x(Image.fromarray(arr))
        self.assertIsNotNone(got)
        self.assertLess(abs(got[0] - TRUTH[0]), TOLERANCE,
                        f"the disc WITH the cross must win, got {got}")

    @unittest.skipUnless(os.path.exists(FRAME), "live frame not present")
    def test_the_live_frame(self):
        got = find_announcement_close_x(Image.open(FRAME).convert("RGB"))
        self.assertIsNotNone(got)
        self.assertLess(abs(got[0] - TRUTH[0]), TOLERANCE, f"got {got}")
        self.assertLess(abs(got[1] - TRUTH[1]), TOLERANCE, f"got {got}")


if __name__ == "__main__":
    unittest.main()
