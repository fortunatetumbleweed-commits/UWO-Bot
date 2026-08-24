"""The daily-news detector no longer calls Moondream, and no longer suppresses itself.

This file used to test a session-level gate: after Moondream returned NO once, further
checks short-circuited for `_DAILY_NEWS_NO_SUPPRESS_TTL_S` (60 minutes) to avoid paying
~12s of inference repeatedly.

Both the arbiter and the suppression were removed on 2026-08-22 after measurement:

  * Against the EXACT production prompt over the labelled set, Moondream scored recall 2/4
    with 3/5 false positives — noise in both directions, not corroboration. Every
    rephrasing was worse (shape/contents 0/12, two-overlapping-windows 2/12): at the
    800x360 thumbnail it receives, it cannot see the distinguishing detail.
  * It also carried an "unavailable -> return True" default, so a MISSING model made the
    detector MORE likely to fire — backwards for a precision-first check.
  * The suppression existed only to limit that cost, and had its own hazard: one negative
    answer blinded the detector for an hour. Live 2026-08-22 it returned False on a real
    daily-news frame for exactly that reason.

The replacement is size + dimming — see tests/test_daily_news_large_popup_gate.py, and
`_large_dimmed_popup` for the measurements (recall 4/4, false positives 0/147).
"""
import inspect

import brain.perceive as perceive_mod


class TestArbiterRemoved:
    def test_the_detector_makes_no_model_call(self):
        src = inspect.getsource(perceive_mod._has_daily_news_close_x)
        for call in ("ask_cached", "get_vision(", "vision.ask"):
            assert call not in src, (
                f"{call} is back in the daily_news detector. It was measured as noise "
                "(recall 2/4, false positives 3/5) — re-measure before reinstating."
            )

    def test_no_session_suppression_window(self):
        """A single negative must not blind the detector for an hour."""
        src = inspect.getsource(perceive_mod._has_daily_news_close_x)
        assert "_daily_news_no_suppress_until" not in src, (
            "session suppression is back; it existed only to limit Moondream cost, and it "
            "silenced the detector for 60 minutes after one negative answer."
        )

    def test_the_detector_is_deterministic_for_a_given_frame(self):
        """No model, no TTL — the same frame must give the same answer every time, which
        is what makes the live traces reproducible."""
        from unittest import mock
        import numpy as np
        from PIL import Image
        img = Image.fromarray(np.full((1080, 2400, 3), 22, dtype=np.uint8))
        crop = np.zeros((50, 50, 3), dtype=np.uint8)
        crop[:30, :] = 255
        big = mock.MagicMock(x1=100, y1=100, x2=1300, y2=800)
        chrome = mock.MagicMock(has_home=False, has_back_arrow=False)
        with mock.patch("vision.chrome_detector.get_chrome_detector") as gcd, \
             mock.patch("actions.sail_actions._ocr_frame", return_value=[("Jakarta", .9, 0, 0)]), \
             mock.patch("numpy.array", return_value=crop), \
             mock.patch("vision.omniparser.parse_fast_cached", return_value=[big]):
            gcd.return_value.detect.return_value = chrome
            answers = {perceive_mod._has_daily_news_close_x(img) for _ in range(3)}
        assert len(answers) == 1, f"non-deterministic verdicts: {answers}"
