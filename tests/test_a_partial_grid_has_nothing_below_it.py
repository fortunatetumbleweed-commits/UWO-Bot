"""A grid that does not fill the page is the end of the list.

Live 2026-09-08 at Jakarta the Sell page held two tiles — Ebony and Coral, seven empty cells
— and the clear scrolled anyway: a swipe, a capture and a whole dispatcher tick to be told
what the picture already showed. Every Sell page in that run measured 1 row by 2 columns.

The test is on the grid's SHAPE, not its cell count, and that is the load-bearing part. A
full 3x3 purchase shelf detects 8 of its 9 tiles about as often as 9, so `len(cells) == 9`
would call a full page partial — and ending a clear early is the 2026-08-26 failure that
sailed a hold away four pages deep. Rows and columns stay 3x3 across those same frames.
"""

from __future__ import annotations

import os
import unittest
import unittest.mock

from PIL import Image

FRAMES = os.path.join(os.path.dirname(__file__), "..", "data", "test_frames")


class ThePartialGridEndsTheList(unittest.TestCase):

    def test_two_tiles_have_nothing_under_them(self):
        from vision.market_reader import sell_page_can_have_more_below
        frame = Image.open(os.path.join(FRAMES, "sell_grid",
                                        "two_tiles_nothing_below.png"))
        self.assertFalse(sell_page_can_have_more_below(frame),
                         "a 1x2 grid cannot continue past the bottom of the screen")

    def test_an_unreadable_grid_still_scrolls(self):
        """Unreadable is not absent: refusing to scroll on a bad read ends a clear early."""
        from vision.market_reader import sell_page_can_have_more_below
        frame = Image.new("RGB", (2400, 1080), (0, 0, 0))
        self.assertTrue(sell_page_can_have_more_below(frame, elements=[]),
                        "no grid detected must fall back to scrolling, as before")


class TheClearStopsWithoutAScroll(unittest.TestCase):

    def test_a_partial_page_finishes_instead_of_scrolling(self):
        import brain.activities.market_sell as ms

        calls = []
        with unittest.mock.patch("vision.market_reader.sell_page_can_have_more_below",
                                        lambda *a, **k: False), \
             unittest.mock.patch.object(ms, "_scroll",
                                        lambda: calls.append("scrolled")):
            out = self._run(ms)
        self.assertEqual("finished", out.get("do"))
        self.assertEqual([], calls, "a partial page must not be scrolled")

    def test_a_full_page_still_scrolls(self):
        import brain.activities.market_sell as ms

        calls = []
        with unittest.mock.patch("vision.market_reader.sell_page_can_have_more_below",
                                        lambda *a, **k: True), \
             unittest.mock.patch.object(ms, "_scroll",
                                        lambda: calls.append("scrolled")):
            out = self._run(ms)
        self.assertEqual("scrolled", out.get("do"))
        self.assertEqual(["scrolled"], calls, "a full page may still hide more below")

    # ── the smallest sell page that reaches the scroll branch ────────────────
    def _run(self, ms):
        import unittest.mock as mock
        state = mock.MagicMock()
        state.last_intent = None
        state.scrolled_pages = 0
        state.sold = []
        state.award_claimed = True          # the award is not what this test is about
        goal = mock.MagicMock(keep=(), exclude=(), only=None)
        # every collaborator is imported INSIDE on_sell_page, so patch it at its source
        with mock.patch("actions.sell_goods._find_sell_commit", lambda *a, **k: None), \
             mock.patch("actions.sell_goods._sell_page", lambda *a, **k: []), \
             mock.patch.object(ms, "selection_for", lambda *a, **k: []), \
             mock.patch.object(ms, "_cargo_this_pass_declined", lambda *a, **k: []):
            return ms.on_sell_page(state, goal, frame=object(), capture_fn=lambda: None,
                                   tap_fn=lambda *a, **k: None, omni_fn=lambda f: [])


if __name__ == "__main__":
    import unittest.mock  # noqa: F401
    unittest.main()
