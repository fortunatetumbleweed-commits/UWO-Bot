"""
Phase 5c tests — OmniParser-backed chrome detection.

Phase 5c migrates the chrome flag detection away from
template-matching-only toward an OmniParser element list.  The
ChromeState interface is preserved; only the implementation changes.

Coverage:
  - detect_chrome_from_elements: pure function, derives flags from
    a synthetic DetectedElement list (no PIL frame needed).
  - has_back_arrow: top-left icon → True; icon elsewhere → False.
  - has_world_map_btn: text label "world map" in bottom-left → True;
    same text elsewhere → False.
  - has_right_panel: ≥2 elements clustered in right-panel region → True;
    < 2 elements → False (avoids false-positive on stray icons).
  - has_home / has_hamburger: NOT populated by the OmniParser path
    alone — left at False so callers know to layer template match.
  - fuse_chrome_states: combines OmniParser-derived flags with
    template-matched home/hamburger.
"""

import unittest

from vision.chrome_detector import ChromeState
from vision.chrome_via_omniparser import (
    MIN_RIGHT_PANEL_ELEMENTS,
    detect_chrome_from_elements,
    fuse_chrome_states,
)
from vision.omniparser import DetectedElement


def _icon(x1, y1, x2, y2, label="icon", element_type="icon"):
    return DetectedElement(
        label=label, element_type=element_type,
        x1=x1, y1=y1, x2=x2, y2=y2, confidence=0.9,
    )


class DetectChromeFromElementsTests(unittest.TestCase):
    """The OmniParser-only path returns a partial ChromeState."""

    def test_empty_element_list_yields_all_flags_false(self):
        state = detect_chrome_from_elements([])
        self.assertFalse(state.has_hamburger)
        self.assertFalse(state.has_home)
        self.assertFalse(state.has_back_arrow)
        self.assertFalse(state.has_world_map_btn)
        self.assertFalse(state.has_right_panel)

    def test_back_arrow_icon_in_top_left_region(self):
        """An icon centred in (0,0)-(220,80) → has_back_arrow True."""
        # Centre at (50, 30) — clearly inside the back-arrow region.
        elements = [_icon(20, 10, 80, 50)]
        state = detect_chrome_from_elements(elements)
        self.assertTrue(state.has_back_arrow)

    def test_back_arrow_icon_outside_top_left_does_not_trigger(self):
        """Icon centred well outside the back-arrow region → False."""
        # Centre at (1000, 500) — middle of screen.
        elements = [_icon(950, 450, 1050, 550)]
        state = detect_chrome_from_elements(elements)
        self.assertFalse(state.has_back_arrow)

    def test_world_map_button_text_in_bottom_left(self):
        """Text 'World map' centred in CHROME_WORLD_MAP_BTN_REGION
        ((0, 880, 320, 1080)) → has_world_map_btn True."""
        # Centre at (160, 980) — inside bottom-left region.
        elements = [_icon(60, 950, 260, 1010,
                          label="World map", element_type="text")]
        state = detect_chrome_from_elements(elements)
        self.assertTrue(state.has_world_map_btn)

    def test_world_map_text_elsewhere_does_not_trigger(self):
        """Same text but in the wrong region → False."""
        # Centre at (1200, 200) — middle-top, not bottom-left.
        elements = [_icon(1100, 180, 1300, 220,
                          label="World map", element_type="text")]
        state = detect_chrome_from_elements(elements)
        self.assertFalse(state.has_world_map_btn)

    def test_right_panel_present_when_two_or_more_elements_clustered(self):
        """Right panel region (2050, 100, 2400, 420) — ≥2 icons → True."""
        # Two tab icons in the panel region: centres at (2100, 150) and (2200, 150).
        elements = [
            _icon(2080, 130, 2120, 170),
            _icon(2180, 130, 2220, 170),
        ]
        self.assertGreaterEqual(len(elements), MIN_RIGHT_PANEL_ELEMENTS)
        state = detect_chrome_from_elements(elements)
        self.assertTrue(state.has_right_panel)

    def test_right_panel_single_element_does_not_trigger(self):
        """Only one element in the panel region → False (could be a stray
        notification icon)."""
        elements = [_icon(2100, 130, 2150, 170)]
        state = detect_chrome_from_elements(elements)
        self.assertFalse(state.has_right_panel)

    def test_home_and_hamburger_left_at_default_false(self):
        """parse_fast cannot disambiguate home from hamburger — both
        flags must come back False, even when an icon is detected in the
        top-right region.  Callers must layer template matching."""
        # Icon centred at (2340, 40) — squarely in the home/hamburger region.
        elements = [_icon(2300, 10, 2380, 70)]
        state = detect_chrome_from_elements(elements)
        self.assertFalse(state.has_home)
        self.assertFalse(state.has_hamburger)


class FuseChromeStatesTests(unittest.TestCase):
    """fuse_chrome_states combines the two sources correctly."""

    def test_omni_back_template_home_yields_both(self):
        omni = ChromeState(has_back_arrow=True)
        tmpl = ChromeState(has_home=True)
        fused = fuse_chrome_states(omni, tmpl)
        self.assertTrue(fused.has_back_arrow)
        self.assertTrue(fused.has_home)
        self.assertFalse(fused.has_hamburger)

    def test_template_home_overrides_omni_home_default(self):
        """The OmniParser path always sets home=False; fuse must take the
        template result, even when omni state has home=False."""
        omni = ChromeState(has_home=False, has_right_panel=True)
        tmpl = ChromeState(has_home=True)
        fused = fuse_chrome_states(omni, tmpl)
        self.assertTrue(fused.has_home)
        self.assertTrue(fused.has_right_panel)

    def test_omni_right_panel_preserved_through_fuse(self):
        omni = ChromeState(has_right_panel=True, has_back_arrow=False)
        tmpl = ChromeState(has_hamburger=True)
        fused = fuse_chrome_states(omni, tmpl)
        self.assertTrue(fused.has_right_panel)
        self.assertTrue(fused.has_hamburger)

    def test_classify_uses_fused_state_correctly(self):
        """End-to-end check: a fused state representing port_overworld
        (omni: right_panel; template: hamburger) classifies correctly."""
        omni = ChromeState(has_right_panel=True)
        tmpl = ChromeState(has_hamburger=True)
        fused = fuse_chrome_states(omni, tmpl)
        self.assertEqual(fused.classify(""), "port_overworld")

    def test_classify_building_interior_via_fused_state(self):
        """Fused: omni back_arrow + template home; OCR title is a known
        building → building_interior."""
        omni = ChromeState(has_back_arrow=True)
        tmpl = ChromeState(has_home=True)
        fused = fuse_chrome_states(omni, tmpl)
        self.assertEqual(fused.classify("market"), "building_interior")


class LabelMatchesTests(unittest.TestCase):
    """Whole-word label matching used by _find_button to prevent
    short canonical labels from firing on unrelated words."""

    def test_ok_matches_ok(self):
        from actions.sail_actions import _label_matches
        self.assertTrue(_label_matches("ok", "OK"))
        self.assertTrue(_label_matches("ok", "OK button"))
        self.assertTrue(_label_matches("ok", "Tap OK to continue"))

    def test_ok_does_not_match_substring_words(self):
        """The May-2 class of bug: 'ok' must NOT match 'Stock', 'Look', etc."""
        from actions.sail_actions import _label_matches
        self.assertFalse(_label_matches("ok", "Stock max"))
        self.assertFalse(_label_matches("ok", "Looking"))
        self.assertFalse(_label_matches("ok", "Booked"))

    def test_x_does_not_match_inside_other_words(self):
        from actions.sail_actions import _label_matches
        self.assertTrue(_label_matches("x", "X"))
        self.assertTrue(_label_matches("x", "X close"))
        self.assertFalse(_label_matches("x", "Exit"))
        self.assertFalse(_label_matches("x", "Maximum"))

    def test_no_does_not_match_inside_other_words(self):
        from actions.sail_actions import _label_matches
        self.assertTrue(_label_matches("no", "No"))
        self.assertTrue(_label_matches("no", "No, cancel"))
        self.assertFalse(_label_matches("no", "Notice"))
        self.assertFalse(_label_matches("no", "Cannot"))

    def test_multi_word_label_requires_all_words(self):
        """'quantity display' requires both words present as whole words."""
        from actions.sail_actions import _label_matches
        self.assertTrue(_label_matches("quantity display", "Quantity Display"))
        self.assertTrue(_label_matches("quantity display", "Display the quantity"))
        self.assertFalse(_label_matches("quantity display", "Quantity"))
        self.assertFalse(_label_matches("quantity display", "Display only"))

    def test_buy_matches_auto_buy_button_label(self):
        """OmniParser-merged labels concatenate text — 'Auto Buy' should
        still match target 'buy'."""
        from actions.sail_actions import _label_matches
        self.assertTrue(_label_matches("buy", "Auto Buy"))
        self.assertTrue(_label_matches("buy", "Buy 100"))
        # But not autobuy (no whitespace)
        self.assertFalse(_label_matches("buy", "Autobuy"))

    def test_close_matches_only_whole_word(self):
        from actions.sail_actions import _label_matches
        self.assertTrue(_label_matches("close", "Close"))
        self.assertTrue(_label_matches("close", "X Close"))
        self.assertFalse(_label_matches("close", "Closet"))


class ParseFastCacheTests(unittest.TestCase):
    """Phase 5c Layer 4: per-frame parse_fast cache.

    Exercises parse_fast_cached's behaviour:
      - returns same list for repeated calls with same frame
      - re-runs parse_fast for a different frame
      - clears wholesale on overflow
    """

    def setUp(self):
        from vision.omniparser import clear_parse_fast_cache
        clear_parse_fast_cache()

    def tearDown(self):
        from vision.omniparser import clear_parse_fast_cache
        clear_parse_fast_cache()

    def test_repeated_call_with_same_frame_uses_cache(self):
        """parse_fast must be invoked at most once for a given frame id."""
        from unittest.mock import MagicMock, patch
        from vision.omniparser import parse_fast_cached, DetectedElement

        fake_frame = MagicMock()
        sentinel = [DetectedElement(label="a", element_type="icon",
                                     x1=0, y1=0, x2=10, y2=10)]

        with patch("vision.omniparser.get_omniparser") as MockGet:
            mock_parser = MagicMock()
            mock_parser.parse_fast.return_value = sentinel
            MockGet.return_value = mock_parser

            r1 = parse_fast_cached(fake_frame)
            r2 = parse_fast_cached(fake_frame)
            r3 = parse_fast_cached(fake_frame)

            self.assertIs(r1, sentinel)
            self.assertIs(r2, sentinel)
            self.assertIs(r3, sentinel)
            # parse_fast called exactly once despite three lookups
            self.assertEqual(mock_parser.parse_fast.call_count, 1)

    def test_different_frames_get_separate_results(self):
        from unittest.mock import MagicMock, patch
        from vision.omniparser import parse_fast_cached, DetectedElement

        frame_a = MagicMock()
        frame_b = MagicMock()
        result_a = [DetectedElement(label="a", element_type="icon",
                                     x1=0, y1=0, x2=10, y2=10)]
        result_b = [DetectedElement(label="b", element_type="icon",
                                     x1=0, y1=0, x2=10, y2=10)]

        with patch("vision.omniparser.get_omniparser") as MockGet:
            mock_parser = MagicMock()
            # Return different list for each call
            mock_parser.parse_fast.side_effect = [result_a, result_b]
            MockGet.return_value = mock_parser

            r_a = parse_fast_cached(frame_a)
            r_b = parse_fast_cached(frame_b)

            self.assertIs(r_a, result_a)
            self.assertIs(r_b, result_b)
            self.assertEqual(mock_parser.parse_fast.call_count, 2)

    def test_cache_clears_wholesale_on_overflow(self):
        """When the cache hits MAX_CACHE_ENTRIES, the next insertion
        clears it wholesale so it stays bounded."""
        from unittest.mock import MagicMock, patch
        from vision.omniparser import (
            parse_fast_cached, MAX_CACHE_ENTRIES, _FRAME_CACHE,
        )

        with patch("vision.omniparser.get_omniparser") as MockGet:
            mock_parser = MagicMock()
            mock_parser.parse_fast.return_value = []
            MockGet.return_value = mock_parser

            # Fill cache up to and beyond capacity
            frames = [MagicMock() for _ in range(MAX_CACHE_ENTRIES + 2)]
            for f in frames:
                parse_fast_cached(f)

            # After overflow handling, the cache contains <= MAX_CACHE_ENTRIES
            self.assertLessEqual(len(_FRAME_CACHE), MAX_CACHE_ENTRIES)


class DailyNewsConfirmTests(unittest.TestCase):
    """Phase 5c follow-up: tighten daily_news Moondream confirmation.

    The earlier prompt asked about generic structure ("centred popup +
    X close button") which matched any modal — including the market
    Purchase confirmation (May-2 15:41 incident: bot tapped Purchase,
    Purchase modal opened, daily_news handler dismissed it).

    The new prompt asks specifically about NEWS content (article
    headlines / news article body); generic modals say no.

    Also: fail-closed on indeterminate or error — better to miss a real
    daily_news (it fires again next tick) than dismiss a live game
    screen.
    """

    def _patched_pixel_signature(self):
        """Helper: numpy-array stub that satisfies the pixel signature
        check — dark<50 and bright>200 both above thresholds."""
        from unittest.mock import MagicMock, patch
        import numpy as np

        # Build an array that has lots of dark pixels and lots of bright
        # ones so (arr < 50).sum() > 100 and (arr > 200).sum() > 50 both pass.
        arr = np.full((50, 50), 128, dtype=np.uint8)
        arr[:20, :20] = 10   # dark region (400 px < 50)
        arr[20:30, 20:30] = 230  # bright region (100 px > 200)
        return arr

    def _make_chrome(self, has_home=False, has_back_arrow=False,
                     has_hamburger=False, has_right_panel=False):
        from unittest.mock import MagicMock
        c = MagicMock()
        c.has_home        = has_home
        c.has_back_arrow  = has_back_arrow
        c.has_hamburger   = has_hamburger
        c.has_right_panel = has_right_panel
        return c

    def test_large_dimmed_popup_on_overworld_returns_true(self):
        """Pixel signature + overworld chrome + a LARGE DIMMED POPUP → daily_news.

        Moondream used to arbitrate here. Measured on the labelled set with the exact
        production prompt it scored recall 2/4 / false positives 3/5, so it was replaced
        by size + dimming (recall 4/4, false positives 0/147 over 151 frames).
        """
        from unittest.mock import MagicMock, patch
        import numpy as np
        from PIL import Image
        from brain.perceive import _has_daily_news_close_x

        fake_arr = self._patched_pixel_signature()
        dim_frame = Image.fromarray(np.full((1080, 2400, 3), 22, dtype=np.uint8))
        popup = MagicMock(x1=100, y1=100, x2=1300, y2=800)      # ~10% of the screen

        with patch("numpy.array", return_value=fake_arr), \
             patch("vision.chrome_detector.get_chrome_detector") as MockChrome, \
             patch("vision.omniparser.parse_fast_cached", return_value=[popup]), \
             patch("actions.sail_actions._ocr_frame", return_value=[("Jakarta", .9, 0, 0)]):
            MockChrome.return_value.detect.return_value = self._make_chrome(
                has_hamburger=True,   # port_overworld
            )
            self.assertTrue(_has_daily_news_close_x(dim_frame))

    def test_inside_building_short_circuits_to_false(self):
        """The May-2 incident: bot inside market → has_back_arrow True →
        daily_news rejected without any Moondream call."""
        from unittest.mock import MagicMock, patch
        from brain.perceive import _has_daily_news_close_x

        fake_arr = self._patched_pixel_signature()
        fake_frame = MagicMock()
        fake_frame.crop.return_value = MagicMock()
        fake_frame.copy.return_value = MagicMock()

        with patch("numpy.array", return_value=fake_arr), \
             patch("vision.chrome_detector.get_chrome_detector") as MockChrome, \
             patch("vision.local_vision.get_vision") as MockVision:
            MockChrome.return_value.detect.return_value = self._make_chrome(
                has_back_arrow=True,  # inside a sub-menu like Purchase
            )
            self.assertFalse(_has_daily_news_close_x(fake_frame))
            # Critical: Moondream NOT called — we short-circuited
            MockVision.return_value.ask.assert_not_called()

    def test_inside_building_via_has_home_short_circuits(self):
        """Same as above for has_home (building interior)."""
        from unittest.mock import MagicMock, patch
        from brain.perceive import _has_daily_news_close_x

        fake_arr = self._patched_pixel_signature()
        fake_frame = MagicMock()
        fake_frame.crop.return_value = MagicMock()
        fake_frame.copy.return_value = MagicMock()

        with patch("numpy.array", return_value=fake_arr), \
             patch("vision.chrome_detector.get_chrome_detector") as MockChrome, \
             patch("vision.local_vision.get_vision") as MockVision:
            MockChrome.return_value.detect.return_value = self._make_chrome(
                has_home=True,
            )
            self.assertFalse(_has_daily_news_close_x(fake_frame))
            MockVision.return_value.ask.assert_not_called()

    def test_x_inside_dialog_returns_false_on_overworld(self):
        """Edge case: chrome looks like overworld (no home/back) but the
        modal under (1794, 240) is a different dialog whose X is INSIDE
        the popup frame.  Moondream rejects."""
        from unittest.mock import MagicMock, patch
        from brain.perceive import _has_daily_news_close_x

        fake_arr = self._patched_pixel_signature()
        fake_frame = MagicMock()
        fake_frame.crop.return_value = MagicMock()
        fake_frame.copy.return_value = MagicMock()

        with patch("numpy.array", return_value=fake_arr), \
             patch("vision.chrome_detector.get_chrome_detector") as MockChrome, \
             patch("vision.local_vision.get_vision") as MockVision:
            MockChrome.return_value.detect.return_value = self._make_chrome(
                has_hamburger=True,
            )
            MockVision.return_value.check_available.return_value = True
            MockVision.return_value.ask.return_value = (
                "No, the X is inside the popup frame."
            )
            self.assertFalse(_has_daily_news_close_x(fake_frame))

    def test_indeterminate_answer_returns_false_now(self):
        """Empty/whitespace answer → reject (was: accept)."""
        from unittest.mock import MagicMock, patch
        from brain.perceive import _has_daily_news_close_x

        fake_arr = self._patched_pixel_signature()
        fake_frame = MagicMock()
        fake_frame.crop.return_value = MagicMock()
        fake_frame.copy.return_value = MagicMock()

        with patch("numpy.array", return_value=fake_arr), \
             patch("vision.chrome_detector.get_chrome_detector") as MockChrome, \
             patch("vision.local_vision.get_vision") as MockVision:
            MockChrome.return_value.detect.return_value = self._make_chrome(
                has_hamburger=True,
            )
            MockVision.return_value.check_available.return_value = True
            MockVision.return_value.ask.return_value = ""
            self.assertFalse(_has_daily_news_close_x(fake_frame))

    def test_moondream_exception_returns_false(self):
        """When Moondream raises, fail closed (was: True)."""
        from unittest.mock import MagicMock, patch
        from brain.perceive import _has_daily_news_close_x

        fake_arr = self._patched_pixel_signature()
        fake_frame = MagicMock()
        fake_frame.crop.return_value = MagicMock()
        fake_frame.copy.return_value = MagicMock()

        with patch("numpy.array", return_value=fake_arr), \
             patch("vision.chrome_detector.get_chrome_detector") as MockChrome, \
             patch("vision.local_vision.get_vision",
                   side_effect=RuntimeError("model crashed")):
            MockChrome.return_value.detect.return_value = self._make_chrome(
                has_hamburger=True,
            )
            self.assertFalse(_has_daily_news_close_x(fake_frame))

    def test_no_model_is_consulted_at_all(self):
        """The detector no longer depends on a model being present.

        It used to treat "Moondream unavailable" as ACCEPT, so a missing model made the
        detector MORE likely to fire — backwards for a check whose false positives cause
        the harm (they tap a remembered coordinate on a misread screen).
        """
        import inspect
        from brain import perceive as _p
        src = inspect.getsource(_p._has_daily_news_close_x)
        for call in ("ask_cached", "get_vision(", "vision.ask"):
            self.assertNotIn(call, src)
