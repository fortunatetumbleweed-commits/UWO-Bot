"""Structural interruptor detection — text_zone + vertical-pair tests.

Origin: 2026-05-14.  Pure-keyword substring matching false-fires whenever
a building's normal UI text happens to contain the keywords:

  union_request         — fires on Union main view ('Union' + 'Request')
  harbor_supply_manual  — fires on Harbor Supply panel ('Water'+'Food'+
                          'Material'+'Ammo' are all visible there too)

The structural block (text_zone, requires_vertical_pair) restricts the
match to where the actual interruptor UI sits — a centered modal or a
central NPC speech overlay — instead of any matching text anywhere on
screen.  These tests pin that the legacy keyword-only entries keep
working AND structural anchors correctly suppress out-of-zone matches.
"""

from __future__ import annotations

import unittest

from brain.perceive import _interruptor_matches


SCREEN_W = 2400
SCREEN_H = 1080


def _tok(text: str, cx: int, cy: int, conf: float = 0.9):
    return (text, conf, cx, cy)


# ── Plain keyword-only (back-compat) ─────────────────────────────────────


class KeywordOnlyTests(unittest.TestCase):

    def test_all_keywords_present_anywhere_fires(self):
        tokens = [
            _tok("Discovery",   100, 100),
            _tok("New Village", 200, 100),
        ]
        self.assertTrue(_interruptor_matches(
            keywords=["discovery", "new village"],
            structural={},
            ocr_tokens=tokens,
            fw=SCREEN_W, fh=SCREEN_H,
        ))

    def test_missing_keyword_does_not_fire(self):
        tokens = [_tok("Discovery", 100, 100)]
        self.assertFalse(_interruptor_matches(
            keywords=["discovery", "new village"],
            structural={},
            ocr_tokens=tokens,
            fw=SCREEN_W, fh=SCREEN_H,
        ))


# ── text_zone — keywords must appear inside the zone ────────────────────


class TextZoneTests(unittest.TestCase):

    def test_zone_restricted_keywords_only_count_in_zone(self):
        """Union main view: 'Union' at top-left, 'Request' in left-strip.
        Neither inside the central dialog zone — must NOT fire."""
        tokens = [
            _tok("Union",    275,  45),    # top-left title bar
            _tok("Requests", 220, 156),    # left-strip menu item
        ]
        self.assertFalse(_interruptor_matches(
            keywords=["union", "request"],
            structural={"text_zone": [0.30, 0.30, 0.70, 0.80]},
            ocr_tokens=tokens,
            fw=SCREEN_W, fh=SCREEN_H,
        ))

    def test_zone_restricted_keywords_fire_when_inside_zone(self):
        """Same keywords, but now in a centered dialog → must fire."""
        tokens = [
            _tok("Union Request", 1200, 500),
            _tok("Accept",        1100, 700),
            _tok("Decline",       1300, 700),
        ]
        self.assertTrue(_interruptor_matches(
            keywords=["union", "request"],
            structural={"text_zone": [0.30, 0.30, 0.70, 0.80]},
            ocr_tokens=tokens,
            fw=SCREEN_W, fh=SCREEN_H,
        ))

    def test_harbor_supply_panel_does_not_fire_when_in_right_strip(self):
        """Harbor's Supply tab has Water/Food/Material/Ammo down the right
        side (cx > 1800).  Without a zone the keyword match fires; with
        text_zone limiting to the centre, it doesn't."""
        right_side_tokens = [
            _tok("Water",    1850, 300),
            _tok("Food",     1850, 400),
            _tok("Material", 1850, 500),
            _tok("Ammo",     1850, 600),
        ]
        self.assertFalse(_interruptor_matches(
            keywords=["water", "food", "material", "ammo"],
            structural={"text_zone": [0.30, 0.20, 0.75, 0.80]},
            ocr_tokens=right_side_tokens,
            fw=SCREEN_W, fh=SCREEN_H,
        ))


# ── Vertical-pair (NPC speech overlay) ───────────────────────────────────


class VerticalPairTests(unittest.TestCase):

    def test_npc_speech_pair_fires_when_two_stacked_rows_present(self):
        """Harbor crew-hired overlay: 'Harbor Official' at cy=704 and
        'The Crew is readyl' at cy=769 form a vertical pair."""
        tokens = [
            _tok("Harbor Official",     909, 704),
            _tok("The Crew is readyl",  929, 769),
        ]
        self.assertTrue(_interruptor_matches(
            keywords=["harbor official"],
            structural={
                "text_zone": [0.30, 0.60, 0.55, 0.78],
                "requires_vertical_pair": True,
                "vertical_pair_min_gap": 30,
                "vertical_pair_max_gap": 120,
            },
            ocr_tokens=tokens,
            fw=SCREEN_W, fh=SCREEN_H,
        ))

    def test_npc_speech_pair_does_not_fire_with_only_one_row(self):
        """If only the NPC name text is present without a body line, the
        overlay isn't active — must NOT fire."""
        tokens = [_tok("Harbor Official", 909, 704)]
        self.assertFalse(_interruptor_matches(
            keywords=["harbor official"],
            structural={
                "text_zone": [0.30, 0.60, 0.55, 0.78],
                "requires_vertical_pair": True,
                "vertical_pair_min_gap": 30,
                "vertical_pair_max_gap": 120,
            },
            ocr_tokens=tokens,
            fw=SCREEN_W, fh=SCREEN_H,
        ))

    def test_npc_speech_pair_does_not_fire_when_keyword_outside_zone(self):
        tokens = [
            _tok("Harbor Official",     200, 50),    # outside zone
            _tok("The Crew is readyl",  929, 769),
        ]
        self.assertFalse(_interruptor_matches(
            keywords=["harbor official"],
            structural={
                "text_zone": [0.30, 0.60, 0.55, 0.78],
                "requires_vertical_pair": True,
            },
            ocr_tokens=tokens,
            fw=SCREEN_W, fh=SCREEN_H,
        ))

    def test_inn_innkeeper_overlay_fires(self):
        tokens = [
            _tok("Innkeeper",                 872, 704),
            _tok("Here's all the mates we",   971, 767),
            _tok("got:",                     1195, 770),
        ]
        self.assertTrue(_interruptor_matches(
            keywords=["innkeeper"],
            structural={
                "text_zone": [0.30, 0.60, 0.55, 0.78],
                "requires_vertical_pair": True,
                "vertical_pair_min_gap": 30,
                "vertical_pair_max_gap": 120,
            },
            ocr_tokens=tokens,
            fw=SCREEN_W, fh=SCREEN_H,
        ))


# ── End-to-end: KB-driven _detect_interruptors honours new schema ──────


class EndToEndKBLoadingTests(unittest.TestCase):
    """Verify the freshly-edited interruptors.json loads cleanly and
    the new structural blocks are visible from the registry."""

    def test_union_request_has_text_zone(self):
        from brain.fsm_registry import get_fsm_registry
        ir = get_fsm_registry().interruptors.get("union_request")
        self.assertIsNotNone(ir)
        structural = (ir._raw or {}).get("structural") or {}
        self.assertIn("text_zone", structural)

    def test_crew_hired_overlays_loaded(self):
        from brain.fsm_registry import get_fsm_registry
        registry = get_fsm_registry()
        self.assertIn("crew_hired_overlay_harbor", registry.interruptors)
        self.assertIn("crew_hired_overlay_inn", registry.interruptors)
        for iid in ("crew_hired_overlay_harbor", "crew_hired_overlay_inn"):
            ir = registry.interruptors[iid]
            structural = (ir._raw or {}).get("structural") or {}
            self.assertTrue(structural.get("requires_vertical_pair"),
                            f"{iid} should require_vertical_pair")
            self.assertEqual(ir.dismissal, "tap_anywhere")


class ObstructionGateTests(unittest.TestCase):
    """Phase A2: _detect_interruptors gates the keyword loop on the
    structural obstruction classifier.  When classify_obstruction()
    returns kind=none, NO keyword interruptor fires — even ones whose
    broad keywords would match on the underlying screen.

    This is the fix for the union_request / perk_event / daily_login_reward
    false-fire loops that happen on building main views.
    """

    def _mock_frame(self):
        from PIL import Image
        return Image.new("RGB", (2400, 1080), color=(0, 0, 0))

    def _stub_inventory(self):
        # Minimal stand-in: classify_obstruction reads frame_dims +
        # raw_elements; we feed an empty inventory so the classifier
        # falls through to KIND_NONE.
        class _Inv:
            raw_elements = []
            tagged = []
            by_role: dict = {}
            frame_dims = (2400, 1080)
            nav_state = None
        return _Inv()

    def test_no_obstruction_means_no_keyword_interruptors_fire(self):
        """Bureau main view with 'Event' visible — `perk_event` keywords
        `['perk', 'event']` would match on the substring path, but the
        obstruction gate skips the keyword loop entirely.  Regression
        for the 2026-05-15 Bureau Invest loop."""
        from unittest.mock import patch
        from brain.perceive import _detect_interruptors

        bureau_tokens = [
            ("Bureau",   0.9, 287, 48),
            ("Invest",   0.9, 196, 153),
            ("Event",    0.9, 195, 386),    # the false-firing word
            ("Mayor",    0.9, 2156, 560),
            ("Bureaucrat", 0.9, 881, 705),
        ]
        with patch("vision.screen_perception.parse_screen", return_value=self._stub_inventory()), \
             patch("brain.perceive._has_daily_news_close_x", return_value=False):
            # classify_obstruction will see an empty inventory → KIND_NONE
            found, _obs = _detect_interruptors(self._mock_frame(), bureau_tokens)
        self.assertEqual(found, [],
                         f"keyword loop must be skipped when no obstruction; got {found}")

    def test_obstruction_present_restricts_keywords_to_bbox(self):
        """When the classifier reports an obstruction with a bbox, the
        legacy keyword loop only sees tokens inside that bbox.  A token
        outside the bbox can't trigger a keyword match."""
        from unittest.mock import patch
        from vision.obstruction_classifier import ObstructionResult, KIND_OVERLAY
        from brain.perceive import _detect_interruptors

        obstruction = ObstructionResult(
            kind=KIND_OVERLAY,
            bbox=(700, 600, 1300, 800),    # central overlay area
            confidence="high",
        )
        tokens = [
            # OUTSIDE the bbox — would match union_request keywords if
            # we didn't restrict
            ("Union",    0.9, 275, 45),
            ("Requests", 0.9, 220, 156),
            # INSIDE the bbox — Harbor Official + bubble body
            ("Harbor Official",       0.9, 909, 704),
            ("The Crew is ready!",    0.9, 929, 769),
        ]
        with patch("vision.screen_perception.parse_screen", return_value=self._stub_inventory()), \
             patch("vision.obstruction_classifier.classify_obstruction", return_value=obstruction), \
             patch("brain.perceive._has_daily_news_close_x", return_value=False):
            found, returned_obs = _detect_interruptors(self._mock_frame(), tokens)
        # Inside-bbox keywords for crew_hired_overlay_harbor matched
        self.assertIn("crew_hired_overlay_harbor", found)
        # Outside-bbox keywords for union_request did NOT match
        self.assertNotIn("union_request", found)
        # Obstruction propagates so the dismiss layer can scope its search
        self.assertIs(returned_obs, obstruction)

    def test_daily_news_pixel_fallback_runs_regardless_of_obstruction(self):
        """daily_news has an image-signature fallback that must run
        even when classify_obstruction says kind=none."""
        from unittest.mock import patch
        from brain.perceive import _detect_interruptors

        with patch("vision.screen_perception.parse_screen", return_value=self._stub_inventory()), \
             patch("brain.perceive._has_daily_news_close_x", return_value=True):
            found, _obs = _detect_interruptors(self._mock_frame(), [])
        self.assertEqual(found, ["daily_news"])


class ReturnTypeContractTests(unittest.TestCase):
    """Pin the 2-tuple return of _detect_interruptors.

    Origin: 2026-05-15.  perceive() captures the result into
    `remaining_interruptors` and later does `remaining_interruptors.append(ov)`
    when merging Qwen overlay hints.  After this function was changed to
    return a tuple `(found, obstruction)`, downstream code stayed on the
    old shape and crashed with
        AttributeError: 'tuple' object has no attribute 'append'
    every Qwen tick for the entire 76-minute run.

    These tests pin:
      - _detect_interruptors always returns a 2-tuple
      - The first element is a list (so .append works)
      - The second is the obstruction (or None)
    """

    def _mock_frame(self):
        from PIL import Image
        return Image.new("RGB", (2400, 1080), color=(0, 0, 0))

    def _stub_inv(self):
        class _Inv:
            raw_elements = []
            tagged = []
            by_role: dict = {}
            frame_dims = (2400, 1080)
            nav_state = None
        return _Inv()

    def test_return_is_two_tuple(self):
        from unittest.mock import patch
        from brain.perceive import _detect_interruptors
        with patch("vision.screen_perception.parse_screen", return_value=self._stub_inv()), \
             patch("brain.perceive._has_daily_news_close_x", return_value=False):
            result = _detect_interruptors(self._mock_frame(), [])
        self.assertEqual(len(result), 2)
        found, obs = result
        # Must be a LIST so callers can .append() to it after
        self.assertIsInstance(found, list)


class ConsultDeclineWiringTests(unittest.TestCase):
    """When the Phase B2a consult comes back with dismissal='tap_decline'
    (e.g. the Exit Game? confirmation), _detect_interruptors must append
    a synthetic id `_consult:tap_decline` to `found` so the dismiss layer
    actually taps Cancel instead of leaving the dialog up.

    Origin: 2026-05-15 Amsterdam run.  The consult cached the analysis
    on visits #1…#6 but the bot kept seeing the same Exit Game dialog
    because nothing acted on the recommendation.
    """

    def _mock_frame(self):
        from PIL import Image
        return Image.new("RGB", (2400, 1080), color=(0, 0, 0))

    def _stub_inv(self):
        class _Inv:
            raw_elements = []
            tagged = []
            by_role: dict = {}
            frame_dims = (2400, 1080)
            nav_state = None
        return _Inv()

    def _obstruction(self):
        from vision.obstruction_classifier import ObstructionResult, KIND_DIALOG
        return ObstructionResult(
            kind=KIND_DIALOG,
            bbox=(957, 254, 1702, 830),
            confidence="high",
        )

    def _make_analysis(self, dismissal: str, outcome_for_goal: str = "still_blocking"):
        from vision.obstruction_consult import ObstructionAnalysis
        return ObstructionAnalysis(
            purpose="Exit Game confirmation",
            full_text="Quit the game?",
            dismissal=dismissal,
            relates_to_goal=False,
            # NOT 'irrelevant': since 2026-05-28 an irrelevant verdict short-circuits
            # dismissal entirely (the right-side nav panel was being tap_anywhere'd every
            # tick). A dialog that is genuinely in the way is 'still_blocking' — the
            # fixture said 'irrelevant' while the test asserted it WOULD be dismissed,
            # which is self-contradictory once the short-circuit exists.
            outcome_for_goal=outcome_for_goal,
            confidence="high",
            obstruction_kind="dialog",
            structural_hash="abcdef12",
        )

    def test_consult_tap_decline_injects_synthetic_id(self):
        from unittest.mock import patch
        from brain.perceive import _detect_interruptors

        with patch("vision.screen_perception.parse_screen",
                   return_value=self._stub_inv()), \
             patch("vision.obstruction_classifier.classify_obstruction",
                   return_value=self._obstruction()), \
             patch("vision.obstruction_consult.consult_obstruction",
                   return_value=self._make_analysis("tap_decline")), \
             patch("brain.perceive._has_daily_news_close_x", return_value=False):
            found, _obs = _detect_interruptors(self._mock_frame(), [])

        self.assertIn("_consult:tap_decline", found)

    def test_consult_tap_anywhere_injects_synthetic_id(self):
        """As of 2026-05-19 the consult dispatcher handles the full set
        of dismissal methods Claude can return (tap_decline,
        tap_close_x, tap_anywhere, tap_ok, tap_accept, press_back).
        The previous 'tap_decline only' policy left dialogs stuck on
        screen when Claude returned other dismissal types — the
        Company-Info-panel incident, 2026-05-19 14:00–14:03."""
        from unittest.mock import patch
        from brain.perceive import _detect_interruptors

        with patch("vision.screen_perception.parse_screen",
                   return_value=self._stub_inv()), \
             patch("vision.obstruction_classifier.classify_obstruction",
                   return_value=self._obstruction()), \
             patch("vision.obstruction_consult.consult_obstruction",
                   return_value=self._make_analysis("tap_anywhere")), \
             patch("brain.perceive._has_daily_news_close_x", return_value=False):
            found, _obs = _detect_interruptors(self._mock_frame(), [])

        self.assertIn("_consult:tap_anywhere", found)
        self.assertNotIn("_consult:tap_decline", found)

    def test_consult_tap_close_x_injects_synthetic_id(self):
        """Specific repro for the Company-Info-panel incident."""
        from unittest.mock import patch
        from brain.perceive import _detect_interruptors

        with patch("vision.screen_perception.parse_screen",
                   return_value=self._stub_inv()), \
             patch("vision.obstruction_classifier.classify_obstruction",
                   return_value=self._obstruction()), \
             patch("vision.obstruction_consult.consult_obstruction",
                   return_value=self._make_analysis("tap_close_x")), \
             patch("brain.perceive._has_daily_news_close_x", return_value=False):
            found, _obs = _detect_interruptors(self._mock_frame(), [])

        self.assertIn("_consult:tap_close_x", found)

    def test_consult_unknown_method_does_not_inject(self):
        """Methods not in the actionable set still stay passive
        (data collection only) — protects against silently injecting
        something the dispatcher can't handle."""
        from unittest.mock import patch
        from brain.perceive import _detect_interruptors

        with patch("vision.screen_perception.parse_screen",
                   return_value=self._stub_inv()), \
             patch("vision.obstruction_classifier.classify_obstruction",
                   return_value=self._obstruction()), \
             patch("vision.obstruction_consult.consult_obstruction",
                   return_value=self._make_analysis("something_weird")), \
             patch("brain.perceive._has_daily_news_close_x", return_value=False):
            found, _obs = _detect_interruptors(self._mock_frame(), [])

        self.assertEqual(
            [f for f in found if f.startswith("_consult:")], [],
            "Unknown consult method should not be injected"
        )

    def test_consult_returns_none_does_not_inject(self):
        from unittest.mock import patch
        from brain.perceive import _detect_interruptors

        with patch("vision.screen_perception.parse_screen",
                   return_value=self._stub_inv()), \
             patch("vision.obstruction_classifier.classify_obstruction",
                   return_value=self._obstruction()), \
             patch("vision.obstruction_consult.consult_obstruction",
                   return_value=None), \
             patch("brain.perceive._has_daily_news_close_x", return_value=False):
            found, _obs = _detect_interruptors(self._mock_frame(), [])

        self.assertEqual(found, [])


class DismissInterruptorConsultDispatchTests(unittest.TestCase):
    """_dismiss_interruptor must recognise the `_consult:<method>` prefix
    and dispatch to the right helper without looking up an interruptor
    record in the registry (these synthetic ids are not stored)."""

    def _mock_frame(self):
        from PIL import Image
        return Image.new("RGB", (2400, 1080), color=(0, 0, 0))

    def test_consult_tap_decline_dispatches_to_helper(self):
        from unittest.mock import patch
        from brain.perceive import _dismiss_interruptor

        with patch("brain.perceive._dismiss_tap_decline") as decliner:
            _dismiss_interruptor(
                "_consult:tap_decline",
                self._mock_frame(),
                obstruction_bbox=(957, 254, 1702, 830),
            )
        decliner.assert_called_once()
        # The bbox is forwarded so the helper can scope its button search
        _args, kwargs = decliner.call_args
        self.assertEqual(kwargs.get("obstruction_bbox"),
                         (957, 254, 1702, 830))

    def test_consult_unknown_method_does_not_dismiss(self):
        from unittest.mock import patch
        from brain.perceive import _dismiss_interruptor

        with patch("brain.perceive._dismiss_tap_decline") as decliner, \
             patch("brain.perceive._dismiss_tap_ok_or_x") as ok_x:
            _dismiss_interruptor(
                "_consult:tap_something_weird",
                self._mock_frame(),
                obstruction_bbox=(0, 0, 100, 100),
            )
        decliner.assert_not_called()
        ok_x.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class ConsultIrrelevantVerdictTests(unittest.TestCase):
    """An 'irrelevant' consult verdict must NOT produce a dismissal.

    `dismissal` is Claude's "if you HAD to dismiss this, how would you" — not a
    directive. Acting on it regardless is what made the bot tap the centre of the
    screen every tick over the right-side nav panel (2026-05-28).

    This case had no coverage, which is why the three ConsultDeclineWiringTests
    could sit on an 'irrelevant' fixture and still look like they were testing
    the dismissal path.
    """

    def _run(self, outcome: str):
        from unittest.mock import patch
        from brain.perceive import _detect_interruptors
        from PIL import Image
        from vision.obstruction_classifier import ObstructionResult, KIND_DIALOG
        from vision.obstruction_consult import ObstructionAnalysis

        class _Inv:
            raw_elements = []
            tagged = []
            by_role: dict = {}
            frame_dims = (2400, 1080)
            nav_state = None

        analysis = ObstructionAnalysis(
            purpose="Right-side navigation panel",
            full_text="mini-map | ports",
            dismissal="tap_anywhere",
            relates_to_goal=False,
            outcome_for_goal=outcome,
            confidence="high",
            obstruction_kind="dialog",
            structural_hash="abcdef12",
        )
        with patch("vision.screen_perception.parse_screen", return_value=_Inv()), \
             patch("vision.obstruction_classifier.classify_obstruction",
                   return_value=ObstructionResult(kind=KIND_DIALOG,
                                                  bbox=(957, 254, 1702, 830),
                                                  confidence="high")), \
             patch("vision.obstruction_consult.consult_obstruction", return_value=analysis), \
             patch("brain.perceive._has_daily_news_close_x", return_value=False):
            found, _obs = _detect_interruptors(
                Image.new("RGB", (2400, 1080), color=(0, 0, 0)), [])
        return found

    def test_irrelevant_verdict_injects_no_dismissal(self):
        self.assertEqual([f for f in self._run("irrelevant") if f.startswith("_consult:")], [])

    def test_still_blocking_verdict_does_inject_dismissal(self):
        self.assertIn("_consult:tap_anywhere", self._run("still_blocking"))
