"""Tests for the no-obstruction perceptual-hash cache.

The cache short-circuits _detect_interruptors when a visually-similar
frame was confirmed obstruction-free within the last 5 seconds.
"""
import time
import unittest
from unittest.mock import patch

from PIL import Image

from brain.perceive import (
    _OBSTRUCTION_NONE_CACHE,
    _OBSTRUCTION_NONE_TTL_S,
    _frame_perceptual_signature,
    _no_obstruction_cache_check,
    _no_obstruction_cache_store,
)


def _solid_frame(color, size=(2400, 1080)):
    return Image.new("RGB", size, color)


class PerceptualSignatureTests(unittest.TestCase):

    def setUp(self):
        _OBSTRUCTION_NONE_CACHE.clear()

    def test_identical_frames_match(self):
        a = _solid_frame((100, 100, 100))
        b = _solid_frame((100, 100, 100))
        self.assertEqual(
            _frame_perceptual_signature(a),
            _frame_perceptual_signature(b),
        )

    def test_visually_different_frames_differ(self):
        a = _solid_frame((100, 100, 100))
        b = _solid_frame((200, 200, 200))
        self.assertNotEqual(
            _frame_perceptual_signature(a),
            _frame_perceptual_signature(b),
        )

    def test_signature_is_stable_bytes(self):
        # Same frame, two calls, identical output — important so the
        # cache key is stable.
        a = _solid_frame((50, 80, 200))
        s1 = _frame_perceptual_signature(a)
        s2 = _frame_perceptual_signature(a)
        self.assertEqual(s1, s2)


class CacheBehaviourTests(unittest.TestCase):

    def setUp(self):
        _OBSTRUCTION_NONE_CACHE.clear()

    def test_store_then_check_hits(self):
        a = _solid_frame((100, 100, 100))
        self.assertFalse(_no_obstruction_cache_check(a))
        _no_obstruction_cache_store(a)
        self.assertTrue(_no_obstruction_cache_check(a))

    def test_cache_miss_on_different_frame(self):
        a = _solid_frame((100, 100, 100))
        b = _solid_frame((200, 50, 50))
        _no_obstruction_cache_store(a)
        self.assertTrue(_no_obstruction_cache_check(a))
        self.assertFalse(_no_obstruction_cache_check(b))

    def test_ttl_expiry(self):
        a = _solid_frame((100, 100, 100))
        _no_obstruction_cache_store(a)
        # Forcibly age the stored timestamp
        sig = _frame_perceptual_signature(a)
        _OBSTRUCTION_NONE_CACHE[sig] = time.time() - _OBSTRUCTION_NONE_TTL_S - 1.0
        self.assertFalse(_no_obstruction_cache_check(a))
        # Expired entry is purged on the miss.
        self.assertNotIn(sig, _OBSTRUCTION_NONE_CACHE)

    def test_pruning_when_max_exceeded(self):
        # Stuff cache past the limit; ensure size shrinks back.
        from brain.perceive import _OBSTRUCTION_NONE_CACHE_MAX
        for i in range(_OBSTRUCTION_NONE_CACHE_MAX + 10):
            # Use unique colors so each gets a unique signature.
            f = _solid_frame((i % 256, (i * 3) % 256, (i * 7) % 256))
            _no_obstruction_cache_store(f)
        self.assertLessEqual(
            len(_OBSTRUCTION_NONE_CACHE), _OBSTRUCTION_NONE_CACHE_MAX,
        )


class DetectInterruptorsShortCircuitTests(unittest.TestCase):
    """When the cache hits, _detect_interruptors should not call
    parse_screen or classify_obstruction at all."""

    def setUp(self):
        _OBSTRUCTION_NONE_CACHE.clear()

    def test_cache_hit_skips_parse_screen(self):
        from brain.perceive import _detect_interruptors
        frame = _solid_frame((100, 100, 100))
        # Prime the cache
        _no_obstruction_cache_store(frame)

        # parse_screen should NOT be called.
        with patch("vision.screen_perception.parse_screen") as mock_parse, \
             patch("vision.obstruction_classifier.classify_obstruction") as mock_cls:
            found, obs = _detect_interruptors(frame, ocr_tokens=[])
        mock_parse.assert_not_called()
        mock_cls.assert_not_called()
        self.assertEqual(found, [])
        self.assertEqual(obs.kind, "none")


if __name__ == "__main__":
    unittest.main()
