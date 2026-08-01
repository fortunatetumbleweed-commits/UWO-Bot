"""
Phase 5e L1 tests — OmniParser-primary screen classifier.

Each test builds a synthetic OmniParser DetectedElement list matching
a state's structural fingerprint and verifies classify_screen returns
the right state.  The key test is the harbor-with-NPC case from
May-2 18:00 (which today's chained chrome+OCR+Moondream classifier
mis-identifies as 'unknown').
"""

import unittest
from unittest.mock import MagicMock

from vision.omniparser import DetectedElement
from vision.screen_classifier import (
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_UNKNOWN,
    classify_screen,
)


SCREEN_W = 2400
SCREEN_H = 1080


def _frame():
    f = MagicMock()
    f.width = SCREEN_W
    f.height = SCREEN_H
    return f


def _text(label, cx, cy, half_w=40, half_h=18):
    """Default half_h=18 → height=36 px, which clears the 32 px title-
    glyph threshold used by _read_top_left_title.  Pass a larger
    half_h to simulate the real ~60-80 px title font, or a smaller
    one to simulate body text / NPC bubbles."""
    return DetectedElement(
        label=label, element_type="text",
        x1=cx - half_w, y1=cy - half_h,
        x2=cx + half_w, y2=cy + half_h,
        confidence=0.85,
    )


def _icon(cx, cy, label="icon", element_type="icon", half=20):
    return DetectedElement(
        label=label, element_type=element_type,
        x1=cx - half, y1=cy - half,
        x2=cx + half, y2=cy + half,
        confidence=0.9,
    )


def _button(label, cx, cy, half_w=80, half_h=30):
    """A YOLO-detected button promoted by _merge_icons_and_text — what
    OmniParser produces for a gold action button with text on it."""
    return DetectedElement(
        label=label, element_type="button",
        x1=cx - half_w, y1=cy - half_h,
        x2=cx + half_w, y2=cy + half_h,
        confidence=0.9,
    )


_KNOWN_PORTS = ["london", "plymouth", "lisbon", "amsterdam", "port royal"]


class BuildingClassificationTests(unittest.TestCase):
    """Building interiors are identified by top-left building-name title."""

    def test_harbor_with_npc_dialog_classifies_as_building(self):
        """May-2 18:00 stuck case: bot was inside the harbor with the
        Harbor Official speaking ('Supply your fleet and set sail.').
        The chrome chain returned 'unknown'.  OmniParser-primary path
        must classify this as building/harbor."""
        elements = [
            # Top-left title 'Harbor'
            _text("Harbor", 120, 40),
            # Top-right home icon
            _icon(2340, 40),
            # Top-left back arrow icon
            _icon(60, 40),
            # Bottom-center building NPC dialog text
            _text("Supply your fleet and set sail.", 1200, 950, half_w=200),
        ]
        sc = classify_screen(_frame(), elements, known_ports=_KNOWN_PORTS)
        self.assertEqual(sc.state, "building")
        self.assertEqual(sc.title, "Harbor")
        self.assertEqual(sc.confidence, CONFIDENCE_HIGH)
        # The building fingerprint matched on the building_title signal.
        self.assertTrue(any("harbor" in s.lower() for s in sc.signals))

    def test_market_classifies_as_building(self):
        elements = [
            _text("Market", 120, 40),
            _icon(2340, 40),
            _icon(60, 40),
        ]
        sc = classify_screen(_frame(), elements, known_ports=_KNOWN_PORTS)
        self.assertEqual(sc.state, "building")
        self.assertEqual(sc.title, "Market")
        self.assertEqual(sc.confidence, CONFIDENCE_HIGH)

    def test_inn_classifies_as_building(self):
        elements = [
            _text("Inn", 120, 40),
            _icon(2340, 40),
        ]
        sc = classify_screen(_frame(), elements, known_ports=_KNOWN_PORTS)
        self.assertEqual(sc.state, "building")
        self.assertEqual(sc.title, "Inn")


class SubMenuClassificationTests(unittest.TestCase):
    def test_purchase_sub_menu(self):
        elements = [
            _text("Purchase", 130, 40),
            _icon(60, 40),  # back arrow
        ]
        sc = classify_screen(_frame(), elements, known_ports=_KNOWN_PORTS)
        self.assertEqual(sc.state, "sub_menu")
        self.assertEqual(sc.title, "Purchase")

    def test_recruit_crew_sub_menu(self):
        elements = [
            _text("Recruit Crew", 150, 40),
            _icon(60, 40),
        ]
        sc = classify_screen(_frame(), elements, known_ports=_KNOWN_PORTS)
        self.assertEqual(sc.state, "sub_menu")


class PortOverworldClassificationTests(unittest.TestCase):
    def test_port_name_plus_right_edge_panel(self):
        elements = [
            _text("Plymouth", 130, 40),
            # Right-edge panel (4 tab icons + minimap)
            _icon(2200, 130), _icon(2280, 130),
            _icon(2200, 200), _icon(2280, 200),
        ]
        sc = classify_screen(_frame(), elements, known_ports=_KNOWN_PORTS)
        self.assertEqual(sc.state, "port_overworld")
        self.assertEqual(sc.port, "plymouth")
        self.assertEqual(sc.confidence, CONFIDENCE_HIGH)

    def test_port_name_only_medium_confidence(self):
        elements = [_text("London", 130, 40)]
        sc = classify_screen(_frame(), elements, known_ports=_KNOWN_PORTS)
        self.assertEqual(sc.state, "port_overworld")
        self.assertEqual(sc.port, "london")
        self.assertEqual(sc.confidence, CONFIDENCE_MEDIUM)


class WorldMapClassificationTests(unittest.TestCase):
    def test_world_map_title_plus_mode_tabs(self):
        elements = [
            _text("World Map", 140, 40),
            # Top-center mode tabs
            _text("Port",    1180, 30),
            _text("Explore", 1310, 30),
            _text("Route",   1430, 30),
            _text("Trade",   1550, 30),
            # Bottom-center 'Go to City'
            _text("Go to City", 1200, 970, half_w=80),
        ]
        sc = classify_screen(_frame(), elements, known_ports=_KNOWN_PORTS)
        self.assertEqual(sc.state, "world_map")
        self.assertEqual(sc.confidence, CONFIDENCE_HIGH)


class PortMapClassificationTests(unittest.TestCase):
    def test_port_map_via_world_map_button_plus_back(self):
        elements = [
            _icon(60, 40),  # back arrow top-left
            _text("World map", 150, 980),  # bottom-left button
        ]
        sc = classify_screen(_frame(), elements, known_ports=_KNOWN_PORTS)
        self.assertEqual(sc.state, "port_map")
        self.assertEqual(sc.confidence, CONFIDENCE_HIGH)


class SeaClassificationTests(unittest.TestCase):
    def test_sea_via_hud_tokens(self):
        elements = [
            _text("Day 14",        80, 60),
            _text("Sailing",       80, 100),
            _text("ETA: 3 days",   1100, 1000),
        ]
        sc = classify_screen(_frame(), elements, known_ports=_KNOWN_PORTS)
        self.assertEqual(sc.state, "sea")
        self.assertEqual(sc.confidence, CONFIDENCE_HIGH)


class UnknownClassificationTests(unittest.TestCase):
    def test_no_fingerprint_returns_unknown(self):
        elements = [_text("Random gibberish", 1200, 500)]
        sc = classify_screen(_frame(), elements, known_ports=_KNOWN_PORTS)
        self.assertEqual(sc.state, "unknown")
        self.assertEqual(sc.confidence, CONFIDENCE_UNKNOWN)

    def test_empty_elements_returns_unknown(self):
        sc = classify_screen(_frame(), [], known_ports=_KNOWN_PORTS)
        self.assertEqual(sc.state, "unknown")

    def test_npc_bubbles_alone_dont_classify_as_building(self):
        """Street NPC bubbles on port_overworld should NOT misclassify
        as building.  Without a building title, the matchers fail
        through to port_overworld (or unknown if no port name visible)."""
        elements = [
            # Random NPC bubble fragment (small, mid-screen)
            _text("Hello traveler!", 800, 500),
        ]
        sc = classify_screen(_frame(), elements, known_ports=_KNOWN_PORTS)
        self.assertNotEqual(sc.state, "building")


class MatcherOrderingTests(unittest.TestCase):
    """Building takes precedence over port_overworld when both signals
    are present (e.g. building title in top-left + accidental right-edge
    icons)."""

    def test_building_wins_over_port_overworld(self):
        elements = [
            _text("Harbor", 120, 40),  # title (height clears 32 px threshold)
            _icon(2340, 40),
            # Accidental right-edge cluster — doesn't matter
            _icon(2200, 130), _icon(2200, 200),
        ]
        sc = classify_screen(_frame(), elements, known_ports=_KNOWN_PORTS)
        self.assertEqual(sc.state, "building")


class NormalisedCoordinateTests(unittest.TestCase):
    """Verify the classifier is resolution-agnostic by simulating a
    different phone size."""

    def test_different_resolution_still_classifies_correctly(self):
        # Smaller phone — 1920x1080.  Same fingerprint, scaled positions.
        f = MagicMock()
        f.width = 1920
        f.height = 1080
        elements = [
            # 'Harbor' at ~5% from left, ~5% from top — in normalised
            # _TOP_LEFT_REGION on any resolution.  Title height scales
            # with frame (~3.5% of height ≈ 38 px on 1080).
            _text("Harbor", int(0.05 * 1920), int(0.05 * 1080),
                  half_h=int(0.018 * 1080)),
            _icon(int(0.97 * 1920), int(0.04 * 1080)),
            _icon(int(0.025 * 1920), int(0.04 * 1080)),
        ]
        sc = classify_screen(f, elements, known_ports=_KNOWN_PORTS)
        self.assertEqual(sc.state, "building")
        self.assertEqual(sc.title, "Harbor")


class L2OcrHelpersTests(unittest.TestCase):
    """Phase 5e L2: read_port_name / read_screen_title / read_building_menu
    use OmniParser elements first, with fixed-crop OCR as fallback."""

    def test_read_port_name_via_omniparser_elements(self):
        from vision.ocr import read_port_name

        elements = [_text("Plymouth", 130, 40)]   # default half_h clears the 32 px title threshold
        self.assertEqual(
            read_port_name(_frame(), elements=elements),
            "Plymouth",
        )

    def test_read_port_name_no_title_element_falls_through(self):
        """No element in the title region → fall back to legacy crop."""
        from unittest.mock import patch
        from vision.ocr import read_port_name

        # Element exists but is in the middle of screen, not top-left.
        elements = [_text("Some center text", 1200, 500)]
        with patch("vision.ocr.read_text", return_value="Lisbon"):
            self.assertEqual(
                read_port_name(_frame(), elements=elements),
                "Lisbon",
            )

    def test_read_screen_title_via_omniparser(self):
        from vision.ocr import read_screen_title

        elements = [_text("Harbor", 120, 40)]
        self.assertEqual(
            read_screen_title(_frame(), elements=elements),
            "harbor",
        )

    def test_read_screen_title_world_map(self):
        from vision.ocr import read_screen_title

        elements = [_text("World Map", 140, 40)]
        self.assertEqual(
            read_screen_title(_frame(), elements=elements),
            "world map",
        )

    def test_read_building_menu_via_omniparser(self):
        """Returns (label, cx, cy) entries sorted top-to-bottom for text
        elements inside the right-edge panel region."""
        from vision.ocr import read_building_menu

        elements = [
            _text("Harbor",   2050, 450),
            _text("Market",   2050, 530),
            _text("Inn",      2050, 610),
            # Outside panel region — should be filtered
            _text("Plymouth", 130,  40),
        ]
        result = read_building_menu(_frame(), elements=elements)
        labels = [r[0] for r in result]
        # All three building entries present, in top-to-bottom order
        self.assertEqual(labels, ["harbor", "market", "inn"])
        # Plymouth (port name top-left) excluded
        self.assertNotIn("plymouth", labels)

    def test_read_building_menu_filters_low_confidence(self):
        from vision.ocr import read_building_menu

        elements = [
            DetectedElement(
                label="Harbor", element_type="text",
                x1=2010, y1=440, x2=2090, y2=460,
                confidence=0.95,
            ),
            DetectedElement(
                label="noise", element_type="text",
                x1=2010, y1=520, x2=2090, y2=540,
                confidence=0.20,  # below threshold
            ),
        ]
        result = read_building_menu(_frame(), elements=elements)
        labels = [r[0] for r in result]
        self.assertIn("harbor", labels)
        self.assertNotIn("noise", labels)

    def test_read_screen_title_falls_back_when_no_elements_in_region(self):
        """OmniParser elements list provided but nothing in the title
        region — falls back to fixed-crop OCR."""
        from unittest.mock import patch
        from vision.ocr import read_screen_title

        elements = [_text("middle text", 1200, 500)]  # not in top-left
        with patch("vision.ocr.read_text", return_value="Harbor"):
            self.assertEqual(
                read_screen_title(_frame(), elements=elements),
                "harbor",
            )

    def test_read_port_name_rejects_short_glyph_in_title_region(self):
        """Regression for 2026-05-15 Amsterdam run.  An NPC speech-bubble
        tail with tiny "Home..." text drifted into the title region.
        Previously the leftmost-topmost picker returned "Home";  the
        height filter now rejects it because the glyph is too small."""
        from vision.ocr import read_port_name

        elements = [
            # Tiny "Home" — NPC bubble tail (height 14 px, well below
            # the 32 px title threshold).  Left of the real title.
            _text("Home", 80, 35, half_w=20, half_h=7),
            # Real port name — much taller.
            _text("Amsterdam", 200, 50, half_w=80, half_h=30),
        ]
        # Force the OmniParser path by passing elements; if the height
        # filter is wrong this would return "Home" or fall through to
        # the legacy crop.
        from unittest.mock import patch
        with patch("vision.ocr.read_text", return_value=None):
            self.assertEqual(
                read_port_name(_frame(), elements=elements),
                "Amsterdam",
            )

    def test_read_port_name_picks_largest_when_multiple_titles_present(self):
        """When two title-sized elements coexist in the region (rare —
        e.g. an event banner overlapping the port name), pick the taller
        one.  Same height → leftmost-topmost tiebreak."""
        from vision.ocr import read_port_name

        elements = [
            # Smaller-but-still-title-sized: not the real port name
            _text("Event", 300, 50, half_w=40, half_h=18),
            # Tallest: the actual port name
            _text("Lisbon", 130, 50, half_w=80, half_h=35),
        ]
        self.assertEqual(
            read_port_name(_frame(), elements=elements),
            "Lisbon",
        )

    def test_read_port_name_passes_elements_through_unchanged(self):
        """When elements is provided, the helper does NOT call parse_fast
        again — verified by patching get_omniparser to raise."""
        from unittest.mock import patch
        from vision.ocr import read_port_name

        elements = [_text("London", 130, 40)]
        with patch("vision.omniparser.get_omniparser",
                   side_effect=RuntimeError("would have called parse_fast")):
            self.assertEqual(
                read_port_name(_frame(), elements=elements),
                "London",
            )


class FsmSubMenuStateTests(unittest.TestCase):
    """Phase 5e fix-pack: sub_menu is a first-class FSM state with
    recovery paths to port_overworld.  Without this, recover_to_port_overworld
    from sub_menu falls back to a generic Back+Home which can dump
    recruitment progress."""

    def test_sub_menu_state_loaded_from_kb(self):
        from brain.fsm_registry import get_fsm_registry
        reg = get_fsm_registry()
        self.assertIn("sub_menu", reg.states)

    def test_sub_menu_has_recovery_path_to_port_overworld(self):
        from brain.fsm_registry import get_fsm_registry
        reg = get_fsm_registry()
        path = reg.recovery_path("sub_menu")
        self.assertGreater(len(path), 0,
                           "sub_menu must have a recovery path; "
                           "without one, recover_to_port_overworld "
                           "falls back to blind Back+Home which loses "
                           "sub-menu progress")
        # Last hop should land at port_overworld.
        self.assertEqual(path[-1].to, "port_overworld")

    def test_sub_menu_press_back_goes_to_building(self):
        """sub_menu has an explicit press_back → building edge so
        navigate_to_building can climb out of a target's sub-menu."""
        from brain.fsm_registry import get_fsm_registry
        reg = get_fsm_registry()
        sm_state = reg.states["sub_menu"]
        back_edges = [
            t for t in sm_state.exits
            if t.action == "press_back" and t.to == "building"
        ]
        self.assertEqual(len(back_edges), 1)


class PlanKbStructuralTests(unittest.TestCase):
    """Structural invariants on the inn_recruit_route plan KB.

    Originally these tests pinned the on-disk file's confidence and
    failure_count to safe values after the May-2 perception bugs.  That
    pinning was fragile because every live run mutates the file.  Post
    Fix C (2026-05-04), what matters is structural: the plan contains
    a commit_via_positive_taps transaction step, and Fix C's runtime
    rule exempts any plan whose transaction step has succeeded from
    confidence demotion.  These tests now verify the structural invariant
    so they are stable across live-run mutations."""

    def _load(self):
        import json
        from pathlib import Path
        path = Path("memory/knowledge/plans/has_enough_crew/inn_recruit_route.json")
        return json.loads(path.read_text())

    def test_plan_contains_commit_via_positive_taps_step(self):
        d = self._load()
        kinds = [s.get("action", {}).get("kind") for s in d.get("steps", [])]
        self.assertIn(
            "commit_via_positive_taps", kinds,
            "inn_recruit_route must contain a commit_via_positive_taps "
            "step — that step is the actual transaction that hires the "
            "crew.  Without it the plan can navigate but cannot commit.",
        )

    def test_commit_step_has_at_least_one_recorded_success(self):
        """Fix C exemption: a plan whose transaction step has succeeded
        is not demoted.  The recruit-crew flow has worked in practice
        (live log 2026-05-04), so this success_count is the proof Fix C
        relies on."""
        d = self._load()
        commit_steps = [
            s for s in d.get("steps", [])
            if s.get("action", {}).get("kind") == "commit_via_positive_taps"
        ]
        self.assertGreater(len(commit_steps), 0)
        total_success = sum(s.get("success_count", 0) for s in commit_steps)
        self.assertGreater(
            total_success, 0,
            "The commit step has never succeeded — without that proof "
            "Fix C's exemption rule cannot apply, and any failure_count "
            "accumulation will demote the plan.",
        )


class FindPositiveButtonTests(unittest.TestCase):
    """Phase 5e L3: find_positive_button picks the screen's primary
    action button by label allow-list with a right-side preference."""

    def test_recruit_button_picked(self):
        from brain.commit_actions import find_positive_button

        elements = [
            _text("Cancel", 200, 900),
            _button("Recruit", 2150, 800),
            _text("ship list", 1200, 500),
        ]
        btn = find_positive_button(
            elements, frame_w=SCREEN_W, frame_h=SCREEN_H,
        )
        self.assertEqual(btn.label, "Recruit")

    def test_negative_label_not_chosen(self):
        from brain.commit_actions import find_positive_button

        elements = [
            _button("Cancel", 200, 900),
            _button("Close", 2300, 50),
        ]
        btn = find_positive_button(
            elements, frame_w=SCREEN_W, frame_h=SCREEN_H,
        )
        self.assertIsNone(btn)

    def test_right_side_preferred_among_positive(self):
        """Two positive buttons — right one wins (UWO action convention)."""
        from brain.commit_actions import find_positive_button

        elements = [
            _button("OK", 100, 800),     # left side
            _button("Confirm", 2200, 800),  # right side
        ]
        btn = find_positive_button(
            elements, frame_w=SCREEN_W, frame_h=SCREEN_H,
        )
        self.assertEqual(btn.label, "Confirm")

    def test_word_boundary_prevents_false_match(self):
        """'OK' must not match 'Stock' or 'Booked'."""
        from brain.commit_actions import find_positive_button

        elements = [_button("Stock max", 1200, 500)]
        btn = find_positive_button(
            elements, frame_w=SCREEN_W, frame_h=SCREEN_H,
        )
        self.assertIsNone(btn)

    def test_negative_label_overrides_positive_substring(self):
        """A label containing both positive and negative verbs (e.g.
        'Cancel Recruit') should be rejected."""
        from brain.commit_actions import find_positive_button

        elements = [_button("Cancel Recruit", 1200, 500)]
        btn = find_positive_button(
            elements, frame_w=SCREEN_W, frame_h=SCREEN_H,
        )
        self.assertIsNone(btn)

    def test_text_type_alone_with_positive_label_rejected(self):
        """Title-bar text 'Recruit Crew' at top-left contains 'Recruit'
        but is element_type='text' (just OCR text on chrome bar, no
        rectangular button shape detected by YOLO).  Without a
        corresponding button-type element, no candidate is returned —
        even at the right-side action region cutoff."""
        from brain.commit_actions import find_positive_button

        elements = [_text("Recruit Crew", 305, 51)]  # top-left title
        btn = find_positive_button(
            elements, frame_w=SCREEN_W, frame_h=SCREEN_H,
        )
        self.assertIsNone(btn)

    def test_highlighted_menu_item_text_rejected(self):
        """Selected sub-menu list entry has a left-edge yellow-glow
        highlight, but the highlight is NOT a button shape — YOLO
        does not detect it as an icon, so OmniParser emits the menu
        entry as element_type='text'.  The selector rejects it."""
        from brain.commit_actions import find_positive_button

        elements = [_text("Recruit Crew", 200, 400)]  # left menu entry
        btn = find_positive_button(
            elements, frame_w=SCREEN_W, frame_h=SCREEN_H,
        )
        self.assertIsNone(btn)

    def test_recruit_screen_picks_gold_button_via_element_type(self):
        """End-to-end: a synthesised Recruit Crew screen with the title
        text, two menu-list-entry texts, a right-panel header text,
        and the actual gold YOLO-detected button.  Filtering by
        element_type='button' picks the gold button without any
        position heuristics — the four 'text' elements are all
        rejected because they aren't structurally buttons."""
        from brain.commit_actions import find_positive_button

        elements = [
            _text("Recruit Crew", 305, 51),     # title bar
            _text("Recruit Crew", 200, 400),    # selected menu entry
            _text("Hire", 200, 350),            # other menu entry
            _text("Recruit Crew", 2095, 140),   # right-panel header
            _button("Recruit", 2095, 703),      # the gold action button
        ]
        btn = find_positive_button(
            elements, frame_w=SCREEN_W, frame_h=SCREEN_H,
        )
        self.assertEqual(btn.label, "Recruit")
        self.assertEqual(btn.element_type, "button")

    def test_text_fallback_when_no_button_type_in_action_region(self):
        """Fallback path: if YOLO misses the button shape and the only
        positive-verb element is text, a candidate in the right-panel
        action region is still accepted.  Title bars (top strip, top-
        left) are excluded because they're outside the action region."""
        from brain.commit_actions import find_positive_button

        elements = [
            _text("Recruit Crew", 305, 51),     # title bar — out of region
            _text("Recruit", 2095, 703),        # right-panel mid — in region
        ]
        btn = find_positive_button(
            elements, frame_w=SCREEN_W, frame_h=SCREEN_H,
        )
        self.assertIsNotNone(btn)
        self.assertEqual(btn.label, "Recruit")
        self.assertEqual(btn.element_type, "text")


class CommitViaPositiveTapsTests(unittest.TestCase):
    """The transaction-commit loop: tap positive button, follow screen
    transitions, stop when the screen cycles back to a seen state."""

    def test_recruit_cycle_stops_when_recruit_button_repeats(self):
        """Classic recruit flow: Recruit screen -> tap Recruit (right
        side) -> confirm dialog -> tap OK -> back at Recruit screen
        (Recruit button still there).  The third iteration sees that
        the next positive button (Recruit, same label and position as
        the first tap) is a repeat and stops with exactly 2 taps."""
        from unittest.mock import MagicMock
        from brain.commit_actions import commit_via_positive_taps

        # Recruit @ (2150, 800) appears on round 0 and round 2.
        frame_recruit_a = _frame()
        elements_recruit_a = [
            _button("Recruit", 2150, 800),
            _text("ship list", 1000, 500),
        ]

        frame_confirm = _frame()
        elements_confirm = [
            _button("OK", 1300, 700),
            _text("Confirm hire?", 1200, 400),
        ]

        # Same Recruit button position; ship count slightly changed
        # (jitter that screen-signature equality would have missed)
        frame_recruit_b = _frame()
        elements_recruit_b = [
            _button("Recruit", 2150, 800),
            _text("ship list updated", 1000, 500),
        ]

        capture_calls = [frame_recruit_a, frame_confirm, frame_recruit_b]
        capture_fn = MagicMock(side_effect=capture_calls)
        tap_fn = MagicMock()

        def fake_parse_fast_cached(frame):
            if frame is frame_recruit_a: return elements_recruit_a
            if frame is frame_confirm:   return elements_confirm
            if frame is frame_recruit_b: return elements_recruit_b
            return []

        with unittest.mock.patch(
            "vision.omniparser.parse_fast_cached",
            side_effect=fake_parse_fast_cached,
        ), unittest.mock.patch("time.sleep"):
            tapped = commit_via_positive_taps(
                max_taps=10, settle_secs=0.0,
                capture_fn=capture_fn, tap_fn=tap_fn,
            )

        # 2 taps recorded: Recruit, then OK
        self.assertEqual(len(tapped), 2)
        self.assertEqual(tap_fn.call_count, 2)
        # Sequence labels in order
        labels = [t[0] for t in tapped]
        self.assertEqual(labels, ["recruit", "ok"])

    def test_no_positive_button_terminates_immediately(self):
        from unittest.mock import MagicMock
        from brain.commit_actions import commit_via_positive_taps

        frame = _frame()
        elements = [_text("Cancel", 200, 900)]
        capture_fn = MagicMock(return_value=frame)
        tap_fn = MagicMock()

        with unittest.mock.patch(
            "vision.omniparser.parse_fast_cached",
            return_value=elements,
        ), unittest.mock.patch("time.sleep"):
            tapped = commit_via_positive_taps(
                max_taps=10, settle_secs=0.0,
                capture_fn=capture_fn, tap_fn=tap_fn,
            )

        self.assertEqual(tapped, [])
        tap_fn.assert_not_called()

    def test_max_taps_cap(self):
        from unittest.mock import MagicMock
        from brain.commit_actions import commit_via_positive_taps

        capture_fn = MagicMock(side_effect=[_frame() for _ in range(20)])
        tap_fn = MagicMock()

        # Each iteration's positive button has a different label / position
        # so the cycle check doesn't fire — only max_taps stops the loop.
        unique_elements = [
            [_button(f"Continue {i}", 2100 - i * 50, 800 - i * 50)]
            for i in range(10)
        ]

        with unittest.mock.patch(
            "vision.omniparser.parse_fast_cached",
            side_effect=unique_elements,
        ), unittest.mock.patch("time.sleep"):
            tapped = commit_via_positive_taps(
                max_taps=3, settle_secs=0.0,
                capture_fn=capture_fn, tap_fn=tap_fn,
            )

        self.assertEqual(len(tapped), 3)
        self.assertEqual(tap_fn.call_count, 3)

    def test_negative_buttons_never_tapped(self):
        """Even if the only buttons on screen are negative
        (Cancel / Close / Back / X), the loop terminates without
        tapping any of them.  Dialogs in this flow are dismissed only
        via their positive button."""
        from unittest.mock import MagicMock
        from brain.commit_actions import commit_via_positive_taps

        frame = _frame()
        elements = [
            _text("Cancel", 200, 900),
            _text("Close", 2300, 50),
            _text("Back", 100, 50),
        ]
        capture_fn = MagicMock(return_value=frame)
        tap_fn = MagicMock()

        with unittest.mock.patch(
            "vision.omniparser.parse_fast_cached",
            return_value=elements,
        ), unittest.mock.patch("time.sleep"):
            tapped = commit_via_positive_taps(
                max_taps=10, settle_secs=0.0,
                capture_fn=capture_fn, tap_fn=tap_fn,
            )

        self.assertEqual(tapped, [])
        tap_fn.assert_not_called()


class FsmSubMenuNoShortcutTests(unittest.TestCase):
    """Phase 5e L3-followup: sub_menu must not have a tap_home shortcut
    edge to port_overworld.  The shortcut would let
    recover_to_port_overworld silently abandon in-progress transactions
    (e.g. tap Home while sitting at the Recruit Crew screen drops the
    recruit transaction).  Recovery must go via press_back -> building
    so commit_via_positive_taps inside building flows still has a
    chance to finish what was started."""

    def test_sub_menu_has_no_direct_tap_home_to_port_overworld(self):
        from brain.fsm_registry import get_fsm_registry
        reg = get_fsm_registry()
        sm_state = reg.states["sub_menu"]
        for t in sm_state.exits:
            self.assertFalse(
                t.action == "tap_home" and t.to == "port_overworld",
                "sub_menu must not have a tap_home -> port_overworld "
                "shortcut: it abandons in-progress transactions.  Use "
                "press_back -> building, then building -> tap_home -> "
                "port_overworld (2 hops) for recovery.",
            )

    def test_sub_menu_recovery_path_is_two_hops(self):
        from brain.fsm_registry import get_fsm_registry
        reg = get_fsm_registry()
        path = reg.recovery_path("sub_menu")
        self.assertEqual(len(path), 2,
                         f"expected 2-hop recovery (press_back -> "
                         f"building -> tap_home), got {len(path)} hops")
        self.assertEqual(path[0].action, "press_back")
        self.assertEqual(path[0].to, "building")
        self.assertEqual(path[1].action, "tap_home")
        self.assertEqual(path[1].to, "port_overworld")


class CommitViaPositiveTapsActionTests(unittest.TestCase):
    """The new action kind is registered and dispatched via the
    plan_actions registry."""

    def test_action_kind_registered(self):
        from brain.plan_actions import _REGISTRY
        self.assertIn("commit_via_positive_taps", _REGISTRY)

    def test_action_invokes_commit(self):
        from unittest.mock import patch
        from brain.plan_actions import execute_step

        with patch("brain.commit_actions.commit_via_positive_taps") as mock_commit:
            execute_step({
                "kind": "commit_via_positive_taps",
                "max_taps": 4,
                "settle_secs": 1.5,
            })
        mock_commit.assert_called_once_with(max_taps=4, settle_secs=1.5)


if __name__ == "__main__":
    unittest.main()
