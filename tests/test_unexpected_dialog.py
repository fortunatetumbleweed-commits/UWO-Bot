"""Unexpected-dialog handler — promo classify + safe dismiss (X or Back, never buy)."""
import unittest
from unittest import mock

import brain.unexpected_dialog as ud
from brain.unexpected_dialog import looks_like_promo, classify, find_close_x, handle


class _E:
    def __init__(self, label, cx, cy, et="icon", w=40, h=40):
        self.label, self.cx, self.cy, self.element_type = label, cx, cy, et
        self.x1, self.y1, self.x2, self.y2 = cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2


def _Frame():
    """A real PIL frame, not a width/height stub.

    `clear_blockers` now pixel-inspects the frame (`np.asarray(frame.convert("RGB"))`)
    to decide whether a dismissal changed anything, so a duck-typed stub with only
    width/height no longer satisfies it.
    """
    from PIL import Image
    return Image.new("RGB", (2400, 1080), (0, 0, 0))


class PromoDetectTests(unittest.TestCase):
    def test_promo_phrases(self):
        self.assertTrue(looks_like_promo(
            "Shortcut to Growth 2 | This item may be shown again in the same form"))
        self.assertTrue(looks_like_promo(
            "Even if not purchased, it may be displayed again after a certain time"))

    def test_normal_screen_not_promo(self):
        self.assertFalse(looks_like_promo("Purchase | Cargo 18/4,108 | Whisky | Steel"))
        self.assertFalse(looks_like_promo("Company Overview | Ducat 55,436 | Fleet"))


class ClassifyTests(unittest.TestCase):
    def test_llm_json_parsed(self):
        v = classify("TEXT: ...", lambda p: '{"dismiss": true, "kind": "promo", "why": "store"}')
        self.assertTrue(v["dismiss"]) ; self.assertEqual(v["kind"], "promo")

    def test_llm_garbage_returns_empty(self):
        self.assertEqual(classify("x", lambda p: "no json"), {})


class FindCloseXTests(unittest.TestCase):
    def test_dialog_corner_icon_found(self):
        els = [_E("icon", 2050, 100)]               # dialog top-right X
        self.assertEqual(find_close_x(els, 2400, 1080), (2050, 100))

    def test_far_hud_corner_excluded(self):
        els = [_E("icon", 2380, 40)]                # screen HUD corner (cx>0.93w)
        self.assertIsNone(find_close_x(els, 2400, 1080))

    def test_content_button_not_close(self):
        els = [_E("Purchase", 2050, 100, et="button")]
        self.assertIsNone(find_close_x(els, 2400, 1080))


class HandleTests(unittest.TestCase):
    def setUp(self):
        self._orig = ud._ocr_text

    def tearDown(self):
        ud._ocr_text = self._orig

    def test_promo_dismissed_via_back_when_no_x(self):
        ud._ocr_text = lambda f: "Chance Sea Ship Pack | This item may be shown again"
        backs = []
        r = handle(_Frame(), elements=[], back_fn=lambda: backs.append(1))
        self.assertTrue(r["handled"]) ; self.assertEqual(r["action"], "back")
        self.assertEqual(len(backs), 1)

    def test_promo_dismissed_via_x_when_present(self):
        ud._ocr_text = lambda f: "may be shown again in the same form"
        taps = []
        r = handle(_Frame(), elements=[_E("icon", 2050, 100)],
                   tap_fn=lambda x, y: taps.append((x, y)), back_fn=lambda: None)
        self.assertTrue(r["handled"]) ; self.assertIn("tap_close", r["action"])
        self.assertEqual(taps, [(2050, 100)])

    def test_non_promo_not_dismissed(self):
        ud._ocr_text = lambda f: "Confirm Purchase | Total 194,523 | Cancel | OK"
        r = handle(_Frame(), elements=[], back_fn=lambda: None)   # no llm, no promo phrase
        self.assertFalse(r["handled"])

    def test_never_taps_a_purchase_button(self):
        ud._ocr_text = lambda f: "may be shown again"
        taps = []
        # only a Purchase button present, no X -> must NOT tap it; falls to Back
        handle(_Frame(), elements=[_E("Purchase", 2050, 100, et="button")],
               tap_fn=lambda x, y: taps.append((x, y)), back_fn=lambda: taps.append("back"))
        self.assertNotIn((2050, 100), taps)      # never tapped Purchase
        self.assertIn("back", taps)


class ClearBlockersTests(unittest.TestCase):
    def setUp(self):
        self._ocr, self._handle = ud._ocr_text, ud.handle

    def tearDown(self):
        ud._ocr_text, ud.handle = self._ocr, self._handle

    def test_lock_screen_detected(self):
        self.assertTrue(ud.looks_like_lock_screen("London | Slide up to unlock | WiFi"))
        self.assertFalse(ud.looks_like_lock_screen("Purchase | Cargo 18/4,108 | Whisky"))

    def test_clear_lock_wakes_and_swipes_up(self):
        ud._ocr_text = lambda f: "London | Slide up to unlock"
        calls = []
        r = ud.clear_blockers(frame=object(),
                              wake_fn=lambda: calls.append("wake"),
                              swipe_fn=lambda *a: calls.append(("swipe", a[1], a[3])),
                              tap_fn=lambda x, y: calls.append("tap"),
                              back_fn=lambda: calls.append("back"))
        self.assertTrue(r["cleared"]) ; self.assertEqual(r["kind"], "lock")
        self.assertIn("wake", calls)
        self.assertTrue(any(c[0] == "swipe" and c[1] > c[2] for c in calls if isinstance(c, tuple)))

    def test_clear_promo_dispatches_to_handle(self):
        ud._ocr_text = lambda f: "Chance Sea Ship Pack | may be shown again"
        ud.handle = lambda frame, **kw: {"handled": True, "kind": "promo", "why": "x"}
        r = ud.clear_blockers(frame=_Frame(), wake_fn=lambda: None, swipe_fn=lambda *a: None,
                              tap_fn=lambda x, y: None, back_fn=lambda: None)
        self.assertTrue(r["cleared"]) ; self.assertEqual(r["kind"], "promo")

    def test_normal_screen_not_cleared(self):
        ud._ocr_text = lambda f: "Purchase | Cargo 18/4,108 | Whisky | Steel"
        r = ud.clear_blockers(frame=_Frame(), wake_fn=lambda: None, swipe_fn=lambda *a: None,
                              tap_fn=lambda x, y: None, back_fn=lambda: None)
        self.assertFalse(r["cleared"]) ; self.assertIsNone(r["kind"])

    def test_announcement_detected(self):
        # the daily-news/anniversary popup — tab row OR the residual updates panel
        self.assertTrue(ud.looks_like_announcement(
            "Perk | Season | Event | Product | Competition | Attendance Time"))
        self.assertTrue(ud.looks_like_announcement(
            "Updates | August 10 Patch Notes | Notices | Server Merge Notice"))
        self.assertFalse(ud.looks_like_announcement("Purchase | Cargo 18/4,108 | Whisky"))

    def test_clear_announcement_via_back(self):
        """Back dismisses the announcement, and the re-read confirms it went away.

        `clear_blockers` no longer trusts the dismissal — it re-captures and re-OCRs,
        reporting cleared only if the popup is actually gone. So the stub has to model
        the popup disappearing; an _ocr_text that always returns the announcement text
        describes a dismissal that did NOT work.
        """
        reads = ["Perk | Season | Event | Product | Competition",
                 "Purchase | Cargo 18/4,108 | Whisky | Steel"]
        calls_ocr = []

        def _ocr(_f):
            text = reads[min(len(calls_ocr), len(reads) - 1)]
            calls_ocr.append(1)
            return text

        ud._ocr_text = _ocr
        calls = []
        # Back is the FALLBACK — only taken when no close-X is found. Pin that here,
        # or the detector's answer on the stub frame decides which branch runs.
        with mock.patch.object(ud, "find_announcement_close_x", return_value=None):
            r = ud.clear_blockers(frame=_Frame(), wake_fn=lambda: None, swipe_fn=lambda *a: None,
                                  tap_fn=lambda x, y: calls.append("tap"),
                                  back_fn=lambda: calls.append("back"),
                                  capture_fn=_Frame)
        self.assertTrue(r["cleared"]) ; self.assertEqual(r["kind"], "announcement")
        self.assertIn("back", calls)
        self.assertNotIn("tap", calls)

    def test_clear_announcement_prefers_close_x_over_back(self):
        """When the popup's own close-X is found, tap it — never fall back to Back.

        Back is unreliable on this popup, and on an overworld the screen-corner is the
        ☰ hamburger, so the X is the safe dismissal.
        """
        reads = ["Perk | Season | Event | Product | Competition",
                 "Purchase | Cargo 18/4,108 | Whisky | Steel"]
        calls_ocr = []

        def _ocr(_f):
            text = reads[min(len(calls_ocr), len(reads) - 1)]
            calls_ocr.append(1)
            return text

        ud._ocr_text = _ocr
        calls = []
        with mock.patch.object(ud, "find_announcement_close_x", return_value=(1900, 150)):
            r = ud.clear_blockers(frame=_Frame(), wake_fn=lambda: None, swipe_fn=lambda *a: None,
                                  tap_fn=lambda x, y: calls.append(("tap", x, y)),
                                  back_fn=lambda: calls.append("back"),
                                  capture_fn=_Frame)
        self.assertTrue(r["cleared"])
        self.assertIn(("tap", 1900, 150), calls)
        self.assertNotIn("back", calls)

    def test_announcement_that_survives_dismissal_reports_not_cleared(self):
        """If the popup is still there after both attempts, say so rather than lying.

        This is the case the old test accidentally described — worth asserting on
        purpose, since a false "cleared" sends the caller on to tap into a popup.
        """
        ud._ocr_text = lambda f: "Perk | Season | Event | Product | Competition"
        with mock.patch.object(ud, "find_announcement_close_x", return_value=None):
            r = ud.clear_blockers(frame=_Frame(), wake_fn=lambda: None, swipe_fn=lambda *a: None,
                                  tap_fn=lambda x, y: None, back_fn=lambda: None,
                                  capture_fn=_Frame)
        self.assertFalse(r["cleared"]) ; self.assertEqual(r["kind"], "announcement")


if __name__ == "__main__":
    unittest.main()
