"""
Perception-layer caching tests — Items 1-3 of the perceive perf pass.

  Item 1: _ocr_frame caches readtext output per id(frame); different
          min_conf values share the cached raw output.
  Item 2: Qwen L2.5 is skipped when fingerprint detail is already
          specific (e.g. "building: harbor"), and runs when detail is
          generic ("building: 1/1 signals matched") or nav is unknown.
  Item 3: ask_cached memoizes Moondream answers per (id(frame), question);
          different questions on the same frame each fire once and cache
          independently.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch, MagicMock

from PIL import Image


def _blank_frame(w: int = 240, h: int = 108) -> Image.Image:
    """Tiny frame for tests — actual contents don't matter; we mock readtext."""
    return Image.new("RGB", (w, h), color=(0, 0, 0))


# ── Item 1: OCR cache ────────────────────────────────────────────────────────

class OcrFrameCacheTests(unittest.TestCase):

    def setUp(self):
        from actions.sail_actions import clear_ocr_frame_cache
        clear_ocr_frame_cache()
        from brain.perceive import clear_perceive_cache
        clear_perceive_cache()

    def test_two_calls_same_frame_share_one_readtext(self):
        """A second _ocr_frame call on the same frame must NOT re-run readtext."""
        from actions import sail_actions
        # Mock the reader so we count readtext invocations.
        fake_reader = MagicMock()
        fake_reader.readtext.return_value = [
            ([(0, 0), (10, 0), (10, 10), (0, 10)], "hello", 0.9),
        ]
        with patch("vision.ocr._get_reader", return_value=fake_reader):
            frame = _blank_frame()
            tokens_a = sail_actions._ocr_frame(frame, min_conf=0.30)
            tokens_b = sail_actions._ocr_frame(frame, min_conf=0.30)
        self.assertEqual(fake_reader.readtext.call_count, 1)
        self.assertEqual(tokens_a, tokens_b)

    def test_different_min_conf_share_cache_but_filter_independently(self):
        """Different min_conf on the same frame share the readtext run; each
        caller filters the cached raw output independently."""
        from actions import sail_actions
        fake_reader = MagicMock()
        fake_reader.readtext.return_value = [
            ([(0, 0), (10, 0), (10, 10), (0, 10)], "hi-conf",  0.9),
            ([(0, 0), (10, 0), (10, 10), (0, 10)], "low-conf", 0.20),
        ]
        with patch("vision.ocr._get_reader", return_value=fake_reader):
            frame = _blank_frame()
            tokens_strict = sail_actions._ocr_frame(frame, min_conf=0.30)
            tokens_loose  = sail_actions._ocr_frame(frame, min_conf=0.10)
        # readtext fires once total; both callers reused the cache
        self.assertEqual(fake_reader.readtext.call_count, 1)
        # Strict filter drops the low-conf entry; loose filter keeps both
        self.assertEqual([t[0] for t in tokens_strict], ["hi-conf"])
        self.assertEqual(sorted(t[0] for t in tokens_loose), ["hi-conf", "low-conf"])

    def test_different_frames_each_run_readtext_once(self):
        """Two distinct frames each pay one readtext call."""
        from actions import sail_actions
        fake_reader = MagicMock()
        fake_reader.readtext.return_value = []
        with patch("vision.ocr._get_reader", return_value=fake_reader):
            sail_actions._ocr_frame(_blank_frame(), min_conf=0.30)
            sail_actions._ocr_frame(_blank_frame(), min_conf=0.30)
        self.assertEqual(fake_reader.readtext.call_count, 2)

    def test_clear_cache_forces_fresh_inference(self):
        from actions import sail_actions
        fake_reader = MagicMock()
        fake_reader.readtext.return_value = []
        with patch("vision.ocr._get_reader", return_value=fake_reader):
            frame = _blank_frame()
            sail_actions._ocr_frame(frame, min_conf=0.30)
            sail_actions.clear_ocr_frame_cache()
            sail_actions._ocr_frame(frame, min_conf=0.30)
        self.assertEqual(fake_reader.readtext.call_count, 2)


# ── Item 2: Qwen-skip gate ───────────────────────────────────────────────────

class QwenSkipGateTests(unittest.TestCase):
    """The L2.5 block in perceive() must be skipped when the fingerprint
    already gave specific detail, and run otherwise.  We exercise the
    gate by mocking qwen_perceive and asserting whether it was called."""

    def setUp(self):
        from actions.sail_actions import clear_ocr_frame_cache
        clear_ocr_frame_cache()
        from vision.moondream_cache import clear_cache
        clear_cache()
        from brain.perceive import clear_perceive_cache
        clear_perceive_cache()   # the unchanged-screen cache must not carry across tests

    def _run_perceive_with_state(self, nav_state: str, detail: str):
        """Helper — invoke perceive with nav_state/detail forced."""
        # We patch the perceive internals so it returns the desired
        # nav/detail, then check whether qwen was called.
        from brain import perceive as _p

        with patch.object(_p, "_detect_navigation_state",
                          return_value=(nav_state, None, detail)), \
             patch.object(_p, "dismiss_interruptors",
                          side_effect=lambda f: f), \
             patch.object(_p, "_detect_interruptors", return_value=([], None)), \
             patch.object(_p, "handle_unknown_blocking", return_value=False), \
             patch.object(_p, "_detect_active_flow", return_value=(None, None)), \
             patch("vision.qwen_perception.qwen_perceive") as qwen_mock, \
             patch("capture.adb_capture.capture_screen", return_value=_blank_frame()):
            qwen_mock.return_value = None
            _p.perceive()
        return qwen_mock

    def test_specific_building_detail_skips_qwen(self):
        """detail='building: harbor' is specific — Qwen must NOT run."""
        qwen_mock = self._run_perceive_with_state("building", "building: harbor")
        qwen_mock.assert_not_called()

    def test_specific_sub_menu_detail_skips_qwen(self):
        qwen_mock = self._run_perceive_with_state("sub_menu", "sub_menu: recruit crew")
        qwen_mock.assert_not_called()

    def test_generic_signals_matched_detail_runs_qwen(self):
        """detail ending in 'signals matched' is generic — Qwen must run for
        enrichment."""
        qwen_mock = self._run_perceive_with_state(
            "building", "building: 1/1 signals matched",
        )
        qwen_mock.assert_called_once()

    def test_unknown_nav_state_runs_qwen(self):
        qwen_mock = self._run_perceive_with_state("unknown", "")
        qwen_mock.assert_called_once()


# ── Item 3: Moondream cache ──────────────────────────────────────────────────

class MoondreamCacheTests(unittest.TestCase):

    def setUp(self):
        from vision.moondream_cache import clear_cache
        clear_cache()
        from brain.perceive import clear_perceive_cache
        clear_perceive_cache()

    def test_repeat_same_question_hits_cache(self):
        from vision.moondream_cache import ask_cached
        ask_fn = MagicMock(return_value=True)
        frame = _blank_frame()
        a = ask_cached(frame, "at_sea", ask_fn)
        b = ask_cached(frame, "at_sea", ask_fn)
        self.assertEqual(ask_fn.call_count, 1)
        self.assertEqual(a, b, True)

    def test_different_questions_same_frame_each_fire_once(self):
        """daily_news + at_sea on the same frame are independent — each
        caches separately, neither returns a stale answer."""
        from vision.moondream_cache import ask_cached
        sea_fn = MagicMock(return_value=True)
        daily_fn = MagicMock(return_value=False)
        frame = _blank_frame()
        ask_cached(frame, "at_sea", sea_fn)
        ask_cached(frame, "daily_news_close_x", daily_fn)
        # second round on the same frame — both cached
        ask_cached(frame, "at_sea", sea_fn)
        ask_cached(frame, "daily_news_close_x", daily_fn)
        self.assertEqual(sea_fn.call_count, 1)
        self.assertEqual(daily_fn.call_count, 1)

    def test_different_frames_each_fire_inference(self):
        from vision.moondream_cache import ask_cached
        ask_fn = MagicMock(return_value=True)
        ask_cached(_blank_frame(), "at_sea", ask_fn)
        ask_cached(_blank_frame(), "at_sea", ask_fn)
        self.assertEqual(ask_fn.call_count, 2)

    def test_paired_check_pattern_shares_work(self):
        """Reproduce the in_town+at_sea pair pattern from the live log:
        both fire on the same frame within one perceive() tick.  With
        caching, repeating the pair on the same frame is free."""
        from vision.moondream_cache import ask_cached
        in_town = MagicMock(return_value=False)
        at_sea  = MagicMock(return_value=True)
        frame = _blank_frame()
        # First perceive iteration on this frame
        ask_cached(frame, "in_town", in_town)
        ask_cached(frame, "at_sea",  at_sea)
        # Repeat (e.g. recovery loop re-perceives same frame)
        ask_cached(frame, "in_town", in_town)
        ask_cached(frame, "at_sea",  at_sea)
        self.assertEqual(in_town.call_count, 1)
        self.assertEqual(at_sea.call_count, 1)


if __name__ == "__main__":
    unittest.main()
