"""
Phase 6 tests — fingerprint registry.

Each fingerprint is exercised with synthetic OmniParser DetectedElement
lists derived from the labelled corpus.  The integration validation
(in vision/fingerprint_survey.py + the standalone registry_validation.py
script) runs against real OmniParser output on the labelled frames; these
unit tests pin individual fingerprints against representative element
patterns drawn from the survey results.
"""

import unittest
from unittest.mock import MagicMock

import vision.state_fingerprints_data  # noqa: F401  registers all fingerprints
from vision.omniparser import DetectedElement
from vision.screen_classifier import (
    CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW, CONFIDENCE_UNKNOWN,
)
from vision.state_fingerprints import (
    Fingerprint, LabelSetSignal, ElementCountSignal, TextContainsSignal,
    classify_via_registry, evaluate_fingerprint,
    FINGERPRINT_REGISTRY,
)


SCREEN_W = 2400
SCREEN_H = 1080


def _frame():
    f = MagicMock()
    f.width = SCREEN_W
    f.height = SCREEN_H
    return f


def _text(label, cx, cy, half_w=40, half_h=12):
    return DetectedElement(
        label=label, element_type="text",
        x1=cx - half_w, y1=cy - half_h,
        x2=cx + half_w, y2=cy + half_h,
        confidence=0.85,
    )


def _button(label, cx, cy, half_w=80, half_h=30):
    return DetectedElement(
        label=label, element_type="button",
        x1=cx - half_w, y1=cy - half_h,
        x2=cx + half_w, y2=cy + half_h,
        confidence=0.9,
    )


class _RegistryIsolationMixin:
    """Strip learned_* fingerprints + clear _LEARNING_TRIGGERED before
    each test, restore on tearDown.  Prevents test order from polluting
    other tests via the in-memory registry hot-registration cache."""

    def setUp(self):
        from vision.state_fingerprints import FINGERPRINT_REGISTRY
        import vision.screen_classifier as scm
        self._registry_snapshot = dict(FINGERPRINT_REGISTRY)
        for k in list(FINGERPRINT_REGISTRY.keys()):
            if k.startswith("learned_"):
                del FINGERPRINT_REGISTRY[k]
        scm._LEARNING_TRIGGERED.clear()

    def tearDown(self):
        from vision.state_fingerprints import FINGERPRINT_REGISTRY
        import vision.screen_classifier as scm
        FINGERPRINT_REGISTRY.clear()
        FINGERPRINT_REGISTRY.update(self._registry_snapshot)
        scm._LEARNING_TRIGGERED.clear()


def _icon(cx, cy, label="icon", half=20):
    return DetectedElement(
        label=label, element_type="icon",
        x1=cx - half, y1=cy - half,
        x2=cx + half, y2=cy + half,
        confidence=0.9,
    )


# ── Registry has all expected fingerprints ───────────────────────────────────


class RegistryShapeTests(unittest.TestCase):
    """Each navigation state in the bot's vocabulary has a fingerprint."""

    def test_main_menu_registered(self):
        self.assertIn("main_menu", FINGERPRINT_REGISTRY)

    def test_world_map_registered(self):
        self.assertIn("world_map", FINGERPRINT_REGISTRY)

    def test_port_map_registered(self):
        self.assertIn("port_map", FINGERPRINT_REGISTRY)

    def test_building_registered(self):
        self.assertIn("building", FINGERPRINT_REGISTRY)

    def test_sub_menu_registered(self):
        self.assertIn("sub_menu", FINGERPRINT_REGISTRY)

    def test_port_overworld_registered(self):
        self.assertIn("port_overworld", FINGERPRINT_REGISTRY)

    def test_sea_registered(self):
        self.assertIn("sea", FINGERPRINT_REGISTRY)


# ── Per-fingerprint classification ───────────────────────────────────────────


class MainMenuFingerprintTests(unittest.TestCase):
    """main_menu: 4-region fingerprint, bottom-right tile bar alone is
    enough to identify the state."""

    def test_full_main_menu_classifies_high(self):
        elements = [
            # R1 top-left: company panel
            _text("Ducat",                     222, 176),
            _text("Total Load Capacity",       276, 456),
            # R2 right grid: a few menu items
            _button("Ship",                   2095, 145),
            _button("Fleet",                  2280, 145),
            _button("Mission",                2095, 250),
            _button("LV 25 Combat",           2095, 460),
            # R3 bottom-left utility
            _button("Settings",                100, 990),
            _button("Exit",                    250, 990),
            # R4 bottom-right tile bar
            _button("LV 20 Auction",          1937, 982),
            _button("Rank",                   2066, 983),
            _button("Guild",                  2194, 983),
            _button("Friend",                 2320, 982),
        ]
        result = classify_via_registry(elements, SCREEN_W, SCREEN_H)
        self.assertIsNotNone(result)
        self.assertEqual(result.state, "main_menu")
        self.assertEqual(result.confidence, CONFIDENCE_HIGH)

    def test_only_bottom_right_tile_bar_still_matches_main_menu(self):
        """The user's emphasised invariant: tile bar alone identifies it."""
        elements = [
            _button("Auction",  1937, 982),
            _button("Rank",     2066, 983),
            _button("Guild",    2194, 983),
            _button("Friend",   2320, 982),
        ]
        result = classify_via_registry(elements, SCREEN_W, SCREEN_H)
        self.assertIsNotNone(result)
        self.assertEqual(result.state, "main_menu")
        # Only 1 of 4 signals fired — gives LOW or MEDIUM, not HIGH.
        self.assertNotEqual(result.confidence, CONFIDENCE_UNKNOWN)


class WorldMapFingerprintTests(unittest.TestCase):

    def test_world_map_with_mode_tabs_and_title_classifies_high(self):
        elements = [
            _text("World Map",  140,   40),
            _text("Port",      1180,   30),
            _text("Explore",   1310,   30),
            _text("Route",     1430,   30),
            _text("Trade",     1550,   30),
            _text("Go to City", 1200, 970, half_w=80),
        ]
        result = classify_via_registry(elements, SCREEN_W, SCREEN_H)
        self.assertIsNotNone(result)
        self.assertEqual(result.state, "world_map")
        # 3 of 4 positives fired (no bottom-left controls in this synthetic).
        self.assertEqual(result.confidence, CONFIDENCE_HIGH)


class BuildingFingerprintTests(unittest.TestCase):

    def test_building_title_identifies_state(self):
        """Building fingerprint matches on a known building title in
        the top-left region.  Defence against port_overworld false
        positives (Palma shipyard case) is upstream via the family
        classifier short-circuit; this fingerprint trusts the title."""
        elements = [_text("Harbor", 120, 40)]
        result = classify_via_registry(elements, SCREEN_W, SCREEN_H)
        self.assertIsNotNone(result)
        self.assertEqual(result.state, "building")
        self.assertEqual(result.confidence, CONFIDENCE_HIGH)

    def test_random_top_right_icon_alone_does_not_match_building(self):
        """Generic chrome icons should NOT match building — only a
        known building title in top-left does."""
        elements = [_icon(2340, 40)]
        result = classify_via_registry(elements, SCREEN_W, SCREEN_H)
        if result is not None:
            self.assertNotEqual(result.state, "building")

    def test_building_detail_surfaces_matched_building_name(self):
        """The result detail must include the building name (e.g.
        'building: harbor') so downstream code (sail_to._handle_building)
        can branch on it."""
        elements = [_text("Harbor", 120, 40)]
        result = classify_via_registry(elements, SCREEN_W, SCREEN_H)
        self.assertIsNotNone(result)
        self.assertEqual(result.detail, "building: harbor")

    def test_item_shop_two_word_title_resolves_full_name(self):
        """Multi-word titles must resolve to the longer KB label
        regardless of OmniParser detection order."""
        elements = [_text("Item", 80, 40), _text("Shop", 240, 40)]
        result = classify_via_registry(elements, SCREEN_W, SCREEN_H)
        self.assertIsNotNone(result)
        self.assertEqual(result.detail, "building: item shop")

        # Right-to-left detection order — same outcome
        elements = [_text("Shop", 240, 40), _text("Item", 80, 40)]
        result = classify_via_registry(elements, SCREEN_W, SCREEN_H)
        self.assertIsNotNone(result)
        self.assertEqual(result.detail, "building: item shop")

    def test_fortune_teller_two_word_title_resolves_full_name(self):
        elements = [_text("Fortune", 120, 40), _text("Teller", 280, 40)]
        result = classify_via_registry(elements, SCREEN_W, SCREEN_H)
        self.assertIsNotNone(result)
        self.assertEqual(result.detail, "building: fortune teller")


class SubMenuFingerprintTests(unittest.TestCase):

    def test_sub_menu_title_identifies_state(self):
        elements = [_text("Recruit Crew", 150, 40)]
        result = classify_via_registry(elements, SCREEN_W, SCREEN_H)
        self.assertIsNotNone(result)
        self.assertEqual(result.state, "sub_menu")
        self.assertEqual(result.confidence, CONFIDENCE_HIGH)

    def test_back_arrow_alone_does_not_match_sub_menu(self):
        elements = [_icon(60, 40)]
        result = classify_via_registry(elements, SCREEN_W, SCREEN_H)
        if result is not None:
            self.assertNotEqual(result.state, "sub_menu")


class PortOverworldFingerprintTests(unittest.TestCase):

    def test_right_edge_cluster_with_no_specific_title_classifies_port(self):
        """port_overworld is the default at-port screen — matches when
        a right-edge cluster is present and no more specific state's
        signal fires."""
        elements = [
            _text("Plymouth", 130, 40),  # variable port name (not in any KB allow-list)
            _icon(2200, 130),
            _icon(2280, 130),
            _icon(2200, 200),
        ]
        result = classify_via_registry(elements, SCREEN_W, SCREEN_H)
        self.assertIsNotNone(result)
        self.assertEqual(result.state, "port_overworld")

    def test_port_overworld_yields_to_building_when_title_present(self):
        """A known building title takes precedence over the right-edge
        cluster — port_overworld's negative signals reject the building
        case once building actually fires."""
        elements = [
            _text("Harbor", 120, 40),     # building title
            _icon(2200, 130),             # would otherwise match port
            _icon(2280, 130),
        ]
        result = classify_via_registry(elements, SCREEN_W, SCREEN_H)
        self.assertEqual(result.state, "building")


class SeaFingerprintTests(unittest.TestCase):

    def test_sea_hud_tokens_classify_sea(self):
        elements = [
            _text("Day 14",      80, 60),
            _text("Sailing",     80, 100),
            _text("ETA: 3 days", 1100, 1000),
        ]
        result = classify_via_registry(elements, SCREEN_W, SCREEN_H)
        self.assertIsNotNone(result)
        self.assertEqual(result.state, "sea")


class PortMapFingerprintTests(unittest.TestCase):

    def test_world_map_button_plus_back_arrow_classifies_port_map(self):
        elements = [
            _icon(60, 40),                   # back arrow top-left
            _text("World", 100, 980),
            _text("map",   180, 980),
        ]
        result = classify_via_registry(elements, SCREEN_W, SCREEN_H)
        self.assertIsNotNone(result)
        self.assertEqual(result.state, "port_map")


# ── Confidence rule edge cases ───────────────────────────────────────────────


class ConfidenceRuleTests(unittest.TestCase):

    def test_all_signals_fire_yields_high(self):
        # Build a minimal fingerprint with 2 signals; both fire → HIGH.
        fp = Fingerprint(
            state_id="_test_high",
            positive_signals=(
                LabelSetSignal(name="a", region=(0, 0, 1, 1),
                               labels=frozenset({"a"})),
                LabelSetSignal(name="b", region=(0, 0, 1, 1),
                               labels=frozenset({"b"})),
            ),
        )
        elements = [_text("a", 100, 100), _text("b", 200, 200)]
        result = evaluate_fingerprint(fp, elements, SCREEN_W, SCREEN_H)
        self.assertEqual(result.confidence, CONFIDENCE_HIGH)

    def test_three_of_four_signals_yields_high(self):
        """≥75% fired → HIGH — one optional miss tolerated."""
        fp = Fingerprint(
            state_id="_test_3of4",
            positive_signals=(
                LabelSetSignal(name="a", region=(0, 0, 1, 1),
                               labels=frozenset({"a"})),
                LabelSetSignal(name="b", region=(0, 0, 1, 1),
                               labels=frozenset({"b"})),
                LabelSetSignal(name="c", region=(0, 0, 1, 1),
                               labels=frozenset({"c"})),
                LabelSetSignal(name="d", region=(0, 0, 1, 1),
                               labels=frozenset({"d"})),
            ),
        )
        elements = [_text("a", 100, 100), _text("b", 200, 200), _text("c", 300, 300)]
        result = evaluate_fingerprint(fp, elements, SCREEN_W, SCREEN_H)
        self.assertEqual(result.confidence, CONFIDENCE_HIGH)

    def test_half_signals_yields_medium(self):
        fp = Fingerprint(
            state_id="_test_half",
            positive_signals=(
                LabelSetSignal(name="a", region=(0, 0, 1, 1),
                               labels=frozenset({"a"})),
                LabelSetSignal(name="b", region=(0, 0, 1, 1),
                               labels=frozenset({"b"})),
            ),
        )
        elements = [_text("a", 100, 100)]
        result = evaluate_fingerprint(fp, elements, SCREEN_W, SCREEN_H)
        self.assertEqual(result.confidence, CONFIDENCE_MEDIUM)

    def test_negative_signal_blocks_match(self):
        fp = Fingerprint(
            state_id="_test_neg",
            positive_signals=(
                LabelSetSignal(name="a", region=(0, 0, 1, 1),
                               labels=frozenset({"a"})),
            ),
            negative_signals=(
                LabelSetSignal(name="not_b", region=(0, 0, 1, 1),
                               labels=frozenset({"b"})),
            ),
        )
        elements = [_text("a", 100, 100), _text("b", 200, 200)]
        result = evaluate_fingerprint(fp, elements, SCREEN_W, SCREEN_H)
        self.assertIsNone(result)

    def test_no_positive_signals_returns_none(self):
        fp = Fingerprint(
            state_id="_test_no_match",
            positive_signals=(
                LabelSetSignal(name="a", region=(0, 0, 1, 1),
                               labels=frozenset({"absent"})),
            ),
        )
        elements = [_text("z", 100, 100)]
        result = evaluate_fingerprint(fp, elements, SCREEN_W, SCREEN_H)
        self.assertIsNone(result)


class LearningLoopTests(_RegistryIsolationMixin, unittest.TestCase):
    """Phase 6.5 — end-to-end learning loop.  When Claude analyses an
    unknown screen, a candidate fingerprint is persisted; the registry
    auto-loads it; subsequent encounters match the learned fingerprint
    directly without another Claude call.

    No screen-specific fingerprints are hand-authored beyond the
    foundational nav states — defeat_dialog, dialog_event, etc. are
    learned via this loop.
    """

    def test_first_encounter_unknown_then_learned_then_matches(self):
        from unittest.mock import patch
        from pathlib import Path
        import tempfile, json
        import vision.claude_vision as cv
        from vision.state_fingerprints import (
            FINGERPRINT_REGISTRY, classify_via_registry, load_learned_fingerprints,
        )

        # Synthesised "defeat dialog" frame elements
        elements = [
            _text("Quick Revive", 976, 318),
            _text("Port Return", 1426, 317),
            _text("Plymouth", 1425, 771),
        ]

        # 1. First encounter: registry knows nothing about this screen
        first_verdict = classify_via_registry(elements, SCREEN_W, SCREEN_H)
        if first_verdict is not None:
            # Sanity: it shouldn't classify as defeat_dialog (no such
            # foundational fingerprint exists)
            self.assertNotIn(first_verdict.state,
                             ("learned_quick_revive_port_return",))

        # 2. Claude analyses (simulated): scene_type was "port_overworld"
        #    (the wrong L0 verdict); Claude says it's a defeat dialog.
        inv = cv.SceneInventory(
            scene_type="port_overworld",
            screen_title="",
            layout_description="ship defeat / sinking recovery dialog",
            navigation_hints="Tap Port Return",
            elements=[
                cv.UIElement(label="Quick Revive", element_type="button",
                             tap_x=976, tap_y=318, purpose="revive in place"),
                cv.UIElement(label="Port Return", element_type="button",
                             tap_x=1426, tap_y=317, purpose="return to port"),
            ],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            with patch.object(cv, "_LEARNED_FP_DIR", tmpdir_path):
                # Learning hook fires; saves a usable Fingerprint shape
                # AND hot-registers it into the live registry.
                cv._maybe_save_learned_candidate(
                    inv=inv, detected_elements=elements,
                    frame_w=SCREEN_W, frame_h=SCREEN_H,
                )
                files = list(tmpdir_path.iterdir())
                self.assertEqual(len(files), 1)
                rec = json.loads(files[0].read_text())
                self.assertIn("fingerprint", rec)
                fp_shape = rec["fingerprint"]
                self.assertTrue(fp_shape["state_id"].startswith("learned_"))
                self.assertGreater(len(fp_shape["positive_signals"]), 0)

                # 3. Hot-registration: the learned fingerprint should
                #    already be in the registry.  Same elements should
                #    now classify to the new learned state.
                self.assertIn(fp_shape["state_id"], FINGERPRINT_REGISTRY)
                second_verdict = classify_via_registry(
                    elements, SCREEN_W, SCREEN_H,
                )
                self.assertIsNotNone(second_verdict)
                self.assertTrue(
                    second_verdict.state.startswith("learned_"),
                    f"expected a learned_* match, got {second_verdict.state}",
                )

    def test_learned_fingerprint_does_not_clobber_foundation(self):
        """If a learned candidate's state_id collides with a foundational
        fingerprint, the foundation wins — load_learned_fingerprints
        skips it."""
        from pathlib import Path
        import tempfile, json
        from vision.state_fingerprints import (
            FINGERPRINT_REGISTRY, load_learned_fingerprints,
        )

        # Pick an existing foundational state.
        target = "main_menu"
        self.assertIn(target, FINGERPRINT_REGISTRY)
        original = FINGERPRINT_REGISTRY[target]

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            (tmpdir_path / f"{target}.json").write_text(json.dumps({
                "scene_type_at_discovery": "x",
                "fingerprint": {
                    "state_id": target,
                    "positive_signals": [{
                        "kind": "LabelSetSignal",
                        "name": "fake",
                        "region": [0, 0, 1, 1],
                        "labels": ["pretend"],
                        "min_matches": 1,
                    }],
                    "min_positive_to_match": 1,
                },
            }))
            loaded = load_learned_fingerprints(tmpdir_path)
            self.assertEqual(loaded, [])
            self.assertIs(FINGERPRINT_REGISTRY[target], original)


class LearningHookTests(_RegistryIsolationMixin, unittest.TestCase):
    """Phase 6.5 L3 — when Claude analyses a scene whose registry
    verdict disagrees with the input scene_type, a candidate fingerprint
    is persisted under memory/knowledge/learned_fingerprints/."""

    def test_disagreement_writes_candidate(self):
        from unittest.mock import MagicMock, patch
        import json
        from pathlib import Path
        import tempfile
        import vision.claude_vision as cv

        # Build a SceneInventory that says scene_type='port_overworld'
        # (the wrong L0 verdict from the May-3 incident).  The element
        # list matches the defeat_dialog fingerprint, so the registry
        # will return 'defeat_dialog' — disagreement → candidate saved.
        inv = cv.SceneInventory(
            scene_type="port_overworld",
            screen_title="",
            layout_description="ship defeat / sinking recovery dialog",
            navigation_hints="Tap Port Return to safely return",
            elements=[],
        )
        elements = [
            _text("Quick Revive", 976, 318),
            _text("Port Return", 1426, 317),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(cv, "_LEARNED_FP_DIR", Path(tmpdir)):
                cv._maybe_save_learned_candidate(
                    inv=inv,
                    detected_elements=elements,
                    frame_w=SCREEN_W,
                    frame_h=SCREEN_H,
                )
                files = list(Path(tmpdir).iterdir())
                self.assertEqual(len(files), 1, "expected exactly one candidate file")
                rec = json.loads(files[0].read_text())
                # Discovery metadata
                self.assertEqual(rec["scene_type_at_discovery"], "port_overworld")
                # Registry returned None at discovery — no foundational fingerprint
                self.assertIsNone(rec["registry_verdict_at_discovery"])
                self.assertEqual(rec["visit_count"], 1)
                self.assertEqual(len(rec["elements"]), 2)
                # Auto-loadable fingerprint shape is present
                self.assertIn("fingerprint", rec)
                self.assertTrue(rec["fingerprint"]["state_id"].startswith("learned_"))

    def test_agreement_writes_no_candidate(self):
        """If registry already classifies this screen correctly (matches
        the input scene_type), no learning candidate is saved — the
        registry already covers it."""
        from unittest.mock import patch
        from pathlib import Path
        import tempfile
        import vision.claude_vision as cv

        # Use a screen where the foundational fingerprint matches: the
        # main_menu tile bar.  Registry returns 'main_menu' AND the
        # scene_type passed in is 'main_menu' → agreement → no save.
        inv = cv.SceneInventory(
            scene_type="main_menu",
            screen_title="",
            layout_description="main menu",
            navigation_hints="tap a tile",
            elements=[],
        )
        elements = [
            _button("Auction",  1937, 982),
            _button("Rank",     2066, 983),
            _button("Guild",    2194, 983),
            _button("Friend",   2320, 982),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(cv, "_LEARNED_FP_DIR", Path(tmpdir)):
                cv._maybe_save_learned_candidate(
                    inv=inv,
                    detected_elements=elements,
                    frame_w=SCREEN_W,
                    frame_h=SCREEN_H,
                )
                files = list(Path(tmpdir).iterdir())
                self.assertEqual(files, [],
                    "registry agreed with scene_type; no candidate expected")

    def test_repeat_visit_increments_count(self):
        """Same disagreement seen twice → visit_count goes from 1 to 2."""
        from unittest.mock import patch
        from pathlib import Path
        import json
        import tempfile
        import vision.claude_vision as cv

        inv = cv.SceneInventory(
            scene_type="port_overworld",
            screen_title="",
            layout_description="defeat dialog",
            navigation_hints="",
            elements=[],
        )
        elements = [
            _text("Quick Revive", 976, 318),
            _text("Port Return", 1426, 317),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(cv, "_LEARNED_FP_DIR", Path(tmpdir)):
                cv._maybe_save_learned_candidate(
                    inv=inv, detected_elements=elements,
                    frame_w=SCREEN_W, frame_h=SCREEN_H,
                )
                cv._maybe_save_learned_candidate(
                    inv=inv, detected_elements=elements,
                    frame_w=SCREEN_W, frame_h=SCREEN_H,
                )
                files = list(Path(tmpdir).iterdir())
                self.assertEqual(len(files), 1)
                rec = json.loads(files[0].read_text())
                self.assertEqual(rec["visit_count"], 2)


class AsyncLearningTriggerTests(_RegistryIsolationMixin, unittest.TestCase):
    """When the registry has no fingerprint for the current screen,
    classify_screen kicks off Claude scene-analysis in a background
    thread so the bot LEARNS the screen rather than just falling
    through to legacy classification."""

    def test_no_fingerprint_match_triggers_learning(self):
        from unittest.mock import patch
        import vision.screen_classifier as scm
        from PIL import Image

        # Build a frame and elements that no foundational fingerprint matches.
        frame = MagicMock()
        frame.width = SCREEN_W
        frame.height = SCREEN_H
        elements = [
            _text("Quick Revive", 976, 318),
            _text("Port Return", 1426, 317),
        ]

        # Reset the dedup set so a previous test doesn't suppress firing.
        scm._LEARNING_TRIGGERED.clear()

        with patch("threading.Thread") as MockThread:
            scm.classify_screen(frame, elements)
            # Should have spawned at least one thread for learning.
            self.assertTrue(MockThread.called)
            kwargs = MockThread.call_args[1] if MockThread.call_args[1] else {}
            self.assertEqual(kwargs.get("name", ""), "claude_learning")

    def test_dedup_prevents_repeat_triggers(self):
        from unittest.mock import patch
        import vision.screen_classifier as scm

        frame = MagicMock()
        frame.width = SCREEN_W
        frame.height = SCREEN_H
        elements = [_text("Quick Revive", 976, 318), _text("Port Return", 1426, 317)]

        scm._LEARNING_TRIGGERED.clear()

        with patch("threading.Thread") as MockThread:
            scm.classify_screen(frame, elements)
            scm.classify_screen(frame, elements)
            scm.classify_screen(frame, elements)
            # The same screen shouldn't trigger learning more than once.
            self.assertEqual(MockThread.call_count, 1)

    def test_foundational_match_does_not_trigger_learning(self):
        """Registry returning HIGH/MEDIUM means the screen is already
        known — no learning needed."""
        from unittest.mock import patch
        import vision.screen_classifier as scm

        frame = MagicMock()
        frame.width = SCREEN_W
        frame.height = SCREEN_H
        # main_menu fingerprint matches
        elements = [
            _button("Auction",  1937, 982),
            _button("Rank",     2066, 983),
            _button("Guild",    2194, 983),
            _button("Friend",   2320, 982),
        ]

        scm._LEARNING_TRIGGERED.clear()

        with patch("threading.Thread") as MockThread:
            scm.classify_screen(frame, elements)
            MockThread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
