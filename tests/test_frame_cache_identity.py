"""Per-frame perception caches must never serve one screen's data for another.

Both caches key on `id(frame)`, which is only unique while the object is ALIVE — CPython
hands a freed PIL image's address straight to the next allocation. Measured on this
machine: 200 sequentially-created 2400x1080 images occupied THREE distinct ids.

Live consequence (2026-08-21): a world-map frame came back carrying the port overworld's
OmniParser elements and OCR ("Inn", "recruit mates"), so consumers acted on a screen that
was not in front of them. The fix is that each entry holds a REFERENCE to its frame, which
pins the id for as long as the entry lives."""
import unittest
from unittest import mock

from PIL import Image


class IdReuseIsRealTests(unittest.TestCase):
    def test_pil_images_do_reuse_ids_once_freed(self):
        seen, collisions = {}, 0
        for i in range(60):
            img = Image.new("RGB", (64, 64), (i, 0, 0))
            key = id(img)
            if key in seen and seen[key] != i:
                collisions += 1
            seen[key] = i
            del img
        self.assertGreater(collisions, 0,
                           "id() reuse is the premise of this bug; if this ever stops "
                           "being true the caches could go back to bare ids")


class OmniParserCacheTests(unittest.TestCase):
    def test_a_recycled_id_does_not_serve_the_previous_frames_elements(self):
        import vision.omniparser as omni
        omni.clear_parse_fast_cache()
        calls = []

        class _FakeParser:
            def parse_fast(self, frame):
                calls.append(frame.getpixel((0, 0)))
                return [f"elements-for-{frame.getpixel((0, 0))[0]}"]

        with mock.patch.object(omni, "get_omniparser", return_value=_FakeParser()):
            results = []
            for i in (1, 2, 3, 4):
                img = Image.new("RGB", (8, 8), (i, 0, 0))
                results.append(omni.parse_fast_cached(img))
                del img                       # invites id reuse for the next iteration
        # Every DIFFERENT frame must have been parsed on its own merits.
        self.assertEqual(len(calls), 4, "a recycled id served a stale parse")
        self.assertEqual(results, [["elements-for-1"], ["elements-for-2"],
                                   ["elements-for-3"], ["elements-for-4"]])

    def test_the_same_frame_object_still_hits_the_cache(self):
        import vision.omniparser as omni
        omni.clear_parse_fast_cache()
        calls = []

        class _FakeParser:
            def parse_fast(self, frame):
                calls.append(1)
                return ["elements"]

        img = Image.new("RGB", (8, 8), (9, 0, 0))
        with mock.patch.object(omni, "get_omniparser", return_value=_FakeParser()):
            for _ in range(5):
                omni.parse_fast_cached(img)     # same object — one inference only
        self.assertEqual(len(calls), 1, "caching must still work for one live frame")


class OcrCacheTests(unittest.TestCase):
    def test_a_recycled_id_does_not_serve_the_previous_frames_tokens(self):
        from actions import sail_actions
        sail_actions.clear_ocr_frame_cache()
        calls = []

        class _FakeReader:
            def readtext(self, arr, detail=1):
                shade = int(arr[0][0][0])
                calls.append(shade)
                return [([[0, 0], [9, 0], [9, 9], [0, 9]], f"screen-{shade}", 0.99)]

        with mock.patch("vision.ocr._get_reader", return_value=_FakeReader()):
            texts = []
            for i in (11, 22, 33, 44):
                img = Image.new("RGB", (8, 8), (i, i, i))
                texts.append(sail_actions._ocr_frame(img)[0][0])
                del img
        self.assertEqual(len(calls), 4, "a recycled id served stale OCR")
        self.assertEqual(texts, ["screen-11", "screen-22", "screen-33", "screen-44"])


if __name__ == "__main__":
    unittest.main()
