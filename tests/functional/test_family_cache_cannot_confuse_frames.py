"""A recycled id must never hand one image another image's answer.

Vision work is expensive and several consumers see the same frame within one perceive tick,
so every reader memoises. PIL Images are not hashable, so each reached for `id(frame)` — and
an id is unique only while the object is ALIVE. CPython hands the same address to the next
allocation once a frame is freed, so the next image silently inherits an unrelated answer.

FOUND TWICE, FIXED ONCE, WHICH IS WHY THE GUARD NOW LIVES IN ONE PLACE:

  * 2026-08-21 `omniparser.parse_fast_cached` — "200 sequentially-created 2400x1080 PIL
    images occupied just THREE distinct ids, i.e. 197 collisions"; a world-map frame came
    back carrying the port overworld's buildings. Fixed there, and the fix never left.
  * 2026-08-26 `family_classifier` — 109 collisions in 120 opened trace frames. A survey
    reported `chromed` and `transient` screens as `sea`, and a claim about what the sea HUD
    shows was made about frames that had never been at sea.

Silent by construction: every wrong answer is a plausible answer of the right shape.
"""

from __future__ import annotations

import glob
import unittest
from unittest.mock import patch

from vision.frame_cache import FrameCache


class _Img:
    """Stands in for a PIL Image: weakref-able, not hashable."""
    __slots__ = ("name", "__weakref__")

    def __init__(self, name):
        self.name = name

    __hash__ = None


class TheSharedCacheIsSafeAgainstIdReuse(unittest.TestCase):

    def setUp(self):
        self.c = FrameCache("test", max_entries=4)

    def test_a_live_frame_hits(self):
        """The reason the cache exists: one tick, several callers, one model run."""
        img = _Img("a")
        runs = []
        for _ in range(3):
            self.c.memoize(img, lambda: runs.append(1) or "sea")
        self.assertEqual(len(runs), 1)

    def test_a_different_frame_at_a_recycled_id_misses(self):
        """The bug. Forge the collision rather than hoping for one."""
        ghost = _Img("collected")
        key = id(ghost)
        self.c.put(ghost, "sea")

        other = _Img("a chromed screen")
        real_id = id
        with patch("vision.frame_cache.id",
                   side_effect=lambda o: key if o is other else real_id(o)):
            self.assertIsNone(self.c.get(other), "a colliding id must never be trusted")
            got = self.c.memoize(other, lambda: "chromed")
        self.assertEqual(got, "chromed")

    def test_extra_separates_answers_about_one_frame(self):
        """A threshold, a question — several answers about the same image."""
        img = _Img("a")
        self.c.put(img, "loose", extra=0.3)
        self.c.put(img, "strict", extra=0.9)
        self.assertEqual((self.c.get(img, 0.3), self.c.get(img, 0.9)), ("loose", "strict"))

    def test_it_stays_bounded(self):
        held = [_Img(str(i)) for i in range(20)]     # held alive so nothing is evicted early
        for im in held:
            self.c.put(im, "x")
        self.assertLessEqual(len(self.c), 4)

    def test_something_not_weakref_able_is_simply_uncached(self):
        """Correct, just not memoised — never wrong."""
        self.assertEqual(self.c.put(object(), "v"), "v")


class TheReadersAgreeWithGroundTruth(unittest.TestCase):
    """Hold every image alive so no id can be recycled, and compare that against the
    open-in-a-loop shape that was wrong."""

    FRAMES = sorted(x for x in glob.glob("data/sessions/trace_smart_*/frame_*.png")
                    if "_omni" not in x and "_marked" not in x)[:24]

    def _compare(self, read):
        from PIL import Image
        if not self.FRAMES:
            self.skipTest("no trace frames available")
        alive = [Image.open(f) for f in self.FRAMES]
        truth = [read(im) for im in alive]
        loop = []
        for f in self.FRAMES:
            loop.append(read(Image.open(f)))
        self.assertEqual(truth, loop)

    def test_the_family_classifier(self):
        from vision.family_classifier import classify_family
        self._compare(lambda im: classify_family(im).family)


if __name__ == "__main__":
    unittest.main()
