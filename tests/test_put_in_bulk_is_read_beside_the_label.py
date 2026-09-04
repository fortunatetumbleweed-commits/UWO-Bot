"""The 'Put In Bulk' tick is not always inside the label OmniParser detects.

Live 2026-08-26 at Amsterdam, with the box plainly ticked:

    Tapping 'Put in Bulk' checkbox @ (445, 1011) (OFF -> ON)
    'Put in Bulk' still not ON after tapping — the next buy may load nothing
    [Amsterdam] load Iron — tap tile @ (1448, 794)
    [Amsterdam] tap Purchase @ (1313,943) cost=0

The bot read a ticked box as unticked and "corrected" it — turning it OFF — after which
every tile tap loaded nothing. The user watched it happen: "it was on, the bot unchecked it."

Why: OmniParser detects the WORDS "Put In Bulk" and returns their bbox, (403,993)-(567,1029)
on this frame. The checkbox square sits immediately to the LEFT, x∈[374,402] — adjacent to
the label and entirely outside it. `_bulk_green_centre` searched inside the bbox, found no
green, and the code concluded the box was off, on the strength of a comment asserting that
reading "from INSIDE the detected button's bbox" made the read trustworthy.

The same signature — "tap Purchase … cost=0", repeatedly — was recorded at Kolkata on
2026-08-22 and patched at a different layer. This is the actual cause.

Both fixtures are real frames from that run: frame 16 is the moment before the bad tap
(ticked), frame 17 the moment after (unticked).
"""

from __future__ import annotations

import os
import unittest

from PIL import Image

FRAMES = os.path.join(os.path.dirname(__file__), "..", "data", "test_frames", "bulk_checkbox")
ON = os.path.join(FRAMES, "put_in_bulk_ON.png")
OFF = os.path.join(FRAMES, "put_in_bulk_OFF.png")


@unittest.skipUnless(os.path.exists(ON), "bulk-checkbox fixtures not present")
class TheTickIsBesideTheLabel(unittest.TestCase):

    def setUp(self):
        import actions.market_actions as ma
        self.ma = ma
        ma._BULK_CHECKBOX_POS[0] = None          # module-level cache; tests must not inherit it

    def test_a_ticked_box_reads_as_on(self):
        """The whole bug in one assertion."""
        self.assertTrue(self.ma._is_bulk_mode_on(Image.open(ON)))

    def test_an_unticked_box_reads_as_off(self):
        """And the fix must not simply always say ON — that would bulk-load a whole stack
        into the cart when the caller wanted the quantity dialog (live 2026-08-22, 1,681
        units of Ebony)."""
        self.assertFalse(self.ma._is_bulk_mode_on(Image.open(OFF)))

    def test_the_tick_here_lies_left_of_the_detected_label(self):
        """Pins THIS geometry, so a future 'tidy-up' back to bbox-only fails here rather than
        silently in a market at two in the morning.

        Note the companion case in test_bulk_checkbox_detection.py: on the sell page
        OmniParser returns a BUTTON whose bbox CONTAINS the tick. Both are real, which is why
        the search covers the union rather than picking a side."""
        frame = Image.open(ON)
        el = self.ma._find_bulk_button(frame)
        self.assertIsNotNone(el, "OmniParser should still detect the 'Put In Bulk' label")
        centre = self.ma._bulk_green_centre(frame, el)
        self.assertIsNotNone(centre, "the green tick must be found")
        self.assertLess(centre[0], el.x1, "the tick is LEFT of the label, never inside it")
        self.assertGreaterEqual(centre[1], el.y1)
        self.assertLessEqual(centre[1], el.y2)

    def test_reading_it_records_a_tap_target(self):
        """The tap coordinate should come from the tick that was actually seen, not from
        `anchor[0] - 40` — a guess that happened to land on the box this time."""
        frame = Image.open(ON)
        self.ma._is_bulk_mode_on(frame)
        pos = self.ma._BULK_CHECKBOX_POS[0]
        self.assertIsNotNone(pos)
        el = self.ma._find_bulk_button(frame)
        self.assertLess(pos[0], el.x1)
