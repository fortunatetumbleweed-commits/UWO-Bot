"""Stage-C wiring tests — Qwen prompt now includes scene-layout context.

Origin: 2026-05-14.  Until now `vision/qwen_perception._build_prompt`
ignored the layout `.md` files we'd been seeding into
`memory/knowledge/scene_layouts/`.  This change wires `load_layout()`
into the prompt so Qwen knows the structure of the screen it's
reasoning about.

Tests pin the wiring (the prompt includes the layout markdown when one
matches) and the parent-building tracker that supplies sub-menu context.
"""

from __future__ import annotations

import unittest


class BuildPromptIncludesLayoutTests(unittest.TestCase):

    def test_port_overworld_prompt_includes_layout(self):
        from vision.qwen_perception import _build_prompt
        out = _build_prompt(
            nav_state="port_overworld",
            nav_detail="port_overworld: 1/1 signals matched",
            ocr_tokens=[],
        )
        # The port_overworld.md content should be in the prompt
        self.assertIn("Scene layout reference", out)
        self.assertIn("port_overworld", out.lower())
        # Right-edge tabbed panel description should be present
        self.assertIn("tabbed info panel", out.lower())

    def test_building_prompt_with_parent_loads_building_md(self):
        from vision.qwen_perception import _build_prompt
        out = _build_prompt(
            nav_state="building",
            nav_detail="building: harbor",
            ocr_tokens=[("Crew Size 786/1,676", 0.9, 2120, 635)],
            parent_building="harbor",
        )
        self.assertIn("Scene layout reference", out)
        # Harbor's main view content
        self.assertIn("Harbor", out)
        # The shared building chrome (back arrow / home) from _default.md
        self.assertIn("Back arrow", out)

    def test_submenu_with_parent_loads_submenu_layer(self):
        from vision.qwen_perception import _build_prompt
        out = _build_prompt(
            nav_state="sub_menu",
            nav_detail="sub_menu: recruit crew",
            ocr_tokens=[],
            parent_building="harbor",
        )
        self.assertIn("Scene layout reference", out)
        # Harbor's recruit_crew.md content (specifically the
        # never-tap-info-label hint from our hand-curated file)
        self.assertIn("Fleet Crew Size", out)

    def test_unknown_state_loads_only_universal_chrome(self):
        from vision.qwen_perception import _build_prompt
        out = _build_prompt(
            nav_state="never_seen_state",
            nav_detail="some detail",
            ocr_tokens=[],
        )
        # `_universal.md` is loaded for EVERY nav_state (bottom-bar
        # chrome is on every screen), so the section header IS present.
        # But scene-specific content (port_overworld, building, etc.)
        # is NOT.
        self.assertIn("Universal chrome", out)
        # Use a marker unique to the port_overworld scene layout, not
        # the bare word "port_overworld" — the chrome glossary mentions
        # the latter in the hamburger-icon description.
        self.assertNotIn("tabbed info panel", out.lower())
        self.assertNotIn("Harbor", out)
        # The rest of the prompt still builds correctly.
        self.assertIn("Return ONLY this JSON", out)


class ChromeGlossaryTests(unittest.TestCase):
    """Phase 2 — chrome glossary inlined into every Qwen prompt.

    Suppresses the "Wi-Fi 8.46% battery" hallucination class by
    catalogging static UI furniture (gold counter, server name,
    in-game clock, player level) so Qwen knows not to weave them
    into the scene narrative.
    """

    def test_every_prompt_includes_chrome_glossary(self):
        from vision.qwen_perception import _build_prompt
        # Any nav_state — glossary is unconditional.
        out = _build_prompt(
            nav_state="port_overworld",
            nav_detail="port_overworld: 1/1 signals matched",
            ocr_tokens=[],
        )
        self.assertIn("UWO UI Chrome", out)
        self.assertIn("Anti-hallucination rules", out)
        self.assertIn("Wi-Fi", out)

    def test_chrome_glossary_loaded_for_unknown_state_too(self):
        from vision.qwen_perception import _build_prompt
        out = _build_prompt(
            nav_state="never_seen_state",
            nav_detail="some detail",
            ocr_tokens=[],
        )
        self.assertIn("UWO UI Chrome", out)


class OmniParserElementsInPromptTests(unittest.TestCase):
    """Phase 1 — OmniParser elements serialised into the Qwen prompt.

    When the caller passes the cached `parse_fast_cached(frame)`
    output, the prompt includes a structured element list so Qwen
    reasons about real positions + types instead of guessing from
    a flat OCR token list.
    """

    def _make_element(self, label, etype, cx, cy, w, h):
        from vision.omniparser import DetectedElement
        return DetectedElement(
            label=label, element_type=etype,
            x1=cx - w // 2, y1=cy - h // 2,
            x2=cx + w // 2, y2=cy + h // 2,
            confidence=0.9,
        )

    def test_elements_serialised_into_prompt(self):
        from vision.qwen_perception import _build_prompt
        elements = [
            self._make_element("Amsterdam", "text", 180, 65, 200, 70),
            self._make_element("Harbor",    "text", 2090, 460, 80, 28),
            self._make_element("gear",      "icon", 145, 875, 50, 50),
        ]
        out = _build_prompt(
            nav_state="port_overworld",
            nav_detail="port_overworld: 1/1 signals matched",
            ocr_tokens=[],
            elements=elements,
        )
        # Section header is present
        self.assertIn("Detected UI elements", out)
        # Each element's label + type + coordinates show up
        self.assertIn('[text] "Amsterdam"', out)
        self.assertIn('[icon] "gear"', out)
        self.assertIn("(2090,460)", out)

    def test_no_elements_section_when_elements_none(self):
        """Backward-compat: callers that don't pass elements still
        get a working prompt — the section is simply omitted."""
        from vision.qwen_perception import _build_prompt
        out = _build_prompt(
            nav_state="port_overworld",
            nav_detail="port_overworld: 1/1 signals matched",
            ocr_tokens=[("Amsterdam", 0.9, 180, 65)],
            elements=None,
        )
        self.assertNotIn("Detected UI elements", out)
        # OCR section still present so Qwen has something to look at
        self.assertIn("OCR tokens from current screen", out)
        self.assertIn("Amsterdam", out)

    def test_no_elements_section_when_elements_empty(self):
        from vision.qwen_perception import _build_prompt
        out = _build_prompt(
            nav_state="port_overworld",
            nav_detail="port_overworld: 1/1 signals matched",
            ocr_tokens=[],
            elements=[],
        )
        self.assertNotIn("Detected UI elements", out)

    def test_long_element_list_truncates_at_80(self):
        from vision.qwen_perception import _build_prompt
        elements = [
            self._make_element(f"label_{i}", "text", 100 + i, 50, 60, 30)
            for i in range(120)
        ]
        out = _build_prompt(
            nav_state="port_overworld",
            nav_detail="x",
            ocr_tokens=[],
            elements=elements,
        )
        # First and 80th elements present, 81st onward not
        self.assertIn('"label_0"', out)
        self.assertIn('"label_79"', out)
        self.assertNotIn('"label_80"', out)
        # Truncation marker shows the omitted count
        self.assertIn("40 more elements omitted", out)


class ParentBuildingTrackerTests(unittest.TestCase):
    """The module-level last-known-building cache that supplies
    parent_building for sub_menu ticks."""

    def setUp(self):
        # Reset the module-level cache before each test
        import brain.perceive as p
        p._last_known_building = None

    def test_building_classification_records_slug(self):
        from brain.perceive import (
            _update_last_known_building, _resolve_parent_building,
        )
        _update_last_known_building("building", "building: harbor")
        # Next sub_menu tick should see 'harbor'
        self.assertEqual(
            _resolve_parent_building("sub_menu", "sub_menu: recruit crew"),
            "harbor",
        )

    def test_overworld_clears_tracker(self):
        from brain.perceive import (
            _update_last_known_building, _resolve_parent_building,
        )
        _update_last_known_building("building", "building: inn")
        self.assertEqual(
            _resolve_parent_building("sub_menu", "sub_menu: recruit crew"),
            "inn",
        )
        _update_last_known_building("port_overworld",
                                       "port_overworld: 1/1 signals matched")
        # After overworld, tracker is cleared
        self.assertIsNone(
            _resolve_parent_building("sub_menu", "sub_menu: recruit crew"),
        )

    def test_sea_state_also_clears_tracker(self):
        from brain.perceive import (
            _update_last_known_building, _resolve_parent_building,
        )
        _update_last_known_building("building", "building: harbor")
        _update_last_known_building("sea", "sailing — Atlantic Ocean")
        self.assertIsNone(
            _resolve_parent_building("sub_menu", "sub_menu: x"),
        )

    def test_qwen_extra_prose_in_detail_does_not_break_slug(self):
        from brain.perceive import (
            _update_last_known_building, _resolve_parent_building,
        )
        # Qwen sometimes appends free-text to the detail like
        # 'building: harbor — The bot is near the depart panel.'
        _update_last_known_building(
            "building",
            "building: harbor — The bot is near the depart panel.",
        )
        self.assertEqual(
            _resolve_parent_building("sub_menu", "sub_menu: x"),
            "harbor",
        )

    def test_sub_menu_without_prior_building_returns_none(self):
        from brain.perceive import _resolve_parent_building
        # Fresh tracker (None), sub_menu somehow classified — parent
        # should be None (caller / loader will fall back appropriately).
        self.assertIsNone(
            _resolve_parent_building("sub_menu", "sub_menu: recruit crew"),
        )

    def test_resolve_returns_none_for_non_submenu(self):
        from brain.perceive import (
            _update_last_known_building, _resolve_parent_building,
        )
        _update_last_known_building("building", "building: harbor")
        # Building tick: parent_building is irrelevant for layout lookup
        self.assertIsNone(
            _resolve_parent_building("building", "building: harbor"),
        )


if __name__ == "__main__":
    unittest.main()
