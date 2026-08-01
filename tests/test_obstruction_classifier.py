"""Layer A: structural obstruction classifier.

Origin: 2026-05-15.  Phase A1 of the goal-aware perception redesign
(see feedback_goal_aware_perception_design.md).

Real-frame tests run against the labelled crew_hired overlays at
data/sessions/2026-05-14_21-04-29/ — the bot's actual captures of the
Harbor Official and Innkeeper confirmation overlays.  Synthetic tests
cover dialog/popup/none cases where we don't have real frames yet.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from vision.obstruction_classifier import (
    KIND_DIALOG, KIND_NONE, KIND_OVERLAY, KIND_POPUP,
    classify_obstruction,
)
from vision.omniparser import DetectedElement


SCREEN_W = 2400
SCREEN_H = 1080


def _icon(cx, cy, w, h, label="icon"):
    return DetectedElement(
        label=label, element_type="icon",
        x1=cx - w // 2, y1=cy - h // 2,
        x2=cx + w // 2, y2=cy + h // 2,
        confidence=0.7,
    )


def _text(label, cx, cy, w=200, h=40):
    return DetectedElement(
        label=label, element_type="text",
        x1=cx - w // 2, y1=cy - h // 2,
        x2=cx + w // 2, y2=cy + h // 2,
        confidence=0.9,
    )


def _button(label, cx, cy, w=200, h=80):
    return DetectedElement(
        label=label, element_type="button",
        x1=cx - w // 2, y1=cy - h // 2,
        x2=cx + w // 2, y2=cy + h // 2,
        confidence=0.85,
    )


def _make_inventory(elements):
    """Minimum ScreenInventory stand-in — classifier reads only
    raw_elements + frame_dims."""
    class _Inv:
        pass
    inv = _Inv()
    inv.raw_elements = elements
    inv.frame_dims = (SCREEN_W, SCREEN_H)
    return inv


# ── Overlay — real labelled frames ───────────────────────────────────────


_FRAMES_DIR = Path(__file__).resolve().parent.parent / "data" / "sessions" / "2026-05-14_21-04-29" / "frames"


@unittest.skipUnless(
    _FRAMES_DIR.exists() and os.environ.get("RUN_LIVE_OMNIPARSER_TESTS") == "1",
    "Live OmniParser frames not available; "
    "set RUN_LIVE_OMNIPARSER_TESTS=1 to enable.",
)
class OverlayRealFrameTests(unittest.TestCase):
    """End-to-end on the actual captured crew_hired frames.

    These tests load the frames, run parse_fast through OmniParser,
    and verify the classifier flags them as overlay.  Skipped by
    default because OmniParser/EasyOCR aren't fast to load and may
    not be available in every CI environment.
    """

    def _classify_path(self, path):
        from PIL import Image
        from vision.omniparser import get_omniparser
        frame = Image.open(path).convert("RGB")
        get_omniparser()._load_yolo()
        elements = get_omniparser().parse_fast(frame)
        inv = _make_inventory(elements)
        return classify_obstruction(inv)

    def test_harbor_crew_hired_overlay(self):
        result = self._classify_path(_FRAMES_DIR / "0000_2104485640.png")
        self.assertEqual(result.kind, KIND_OVERLAY)

    def test_inn_crew_hired_overlay(self):
        result = self._classify_path(_FRAMES_DIR / "0001_2105279779.png")
        self.assertEqual(result.kind, KIND_OVERLAY)


# ── Overlay — synthetic ────────────────────────────────────────────────


class OverlaySyntheticTests(unittest.TestCase):

    def test_npc_speech_pair_with_central_sprite_classifies_overlay(self):
        elements = [
            _icon(1147, 448, 1324, 273),               # central NPC sprite
            _text("Harbor Official", 909, 704),         # NPC name
            _text("The Crew is ready!", 929, 769),     # body line
            # Underlying chrome still visible (typical for overlay)
            _text("Recruit Crew", 328, 55),
        ]
        result = classify_obstruction(_make_inventory(elements))
        self.assertEqual(result.kind, KIND_OVERLAY)
        self.assertEqual(result.confidence, "high")
        self.assertIsNotNone(result.bbox)

    def test_text_pair_without_sprite_does_not_classify_overlay(self):
        """Tightened: a building's central NPC dialogue (e.g. Bureau's
        Bureaucrat NPC saying 'to aid its port') has the same vertical
        text-pair pattern as a real overlay, so sprite detection is
        required.  Without a matching central sprite icon, fall through
        to dialog/popup/none classification.

        Regression test for the 2026-05-15 false-fire where
        bureau_main_view classified as overlay (medium confidence)."""
        elements = [
            _text("Bureaucrat", 881, 705),
            _text("to aid its", 1239, 765),
            _text("Bureau", 287, 48),    # building title behind
        ]
        result = classify_obstruction(_make_inventory(elements))
        self.assertNotEqual(result.kind, KIND_OVERLAY)

    def test_single_text_no_pair_does_not_classify_overlay(self):
        elements = [
            _icon(1147, 448, 1324, 273),
            _text("Harbor Official", 909, 704),
        ]
        result = classify_obstruction(_make_inventory(elements))
        self.assertNotEqual(result.kind, KIND_OVERLAY)

    def test_text_pair_outside_central_zone_does_not_classify(self):
        elements = [
            _text("Recruit Crew", 220, 156),    # left strip
            _text("Hire",         220, 261),    # left strip
        ]
        result = classify_obstruction(_make_inventory(elements))
        self.assertNotEqual(result.kind, KIND_OVERLAY)


# ── Dialog — synthetic ─────────────────────────────────────────────────


class DialogSyntheticTests(unittest.TestCase):

    def test_centered_modal_with_button_row_classifies_dialog(self):
        elements = [
            _text("Union Battle Request",   1200, 350),
            _text("Player X has challenged you", 1200, 450),
            _button("Accept",                 1080, 700),
            _button("Decline",                1320, 700),
        ]
        result = classify_obstruction(_make_inventory(elements))
        self.assertEqual(result.kind, KIND_DIALOG)
        self.assertTrue(
            any("Accept" in s and "Decline" in s for s in result.signals),
            f"action_row should list both buttons: {result.signals}",
        )

    def test_dialog_needs_minimum_cluster_size(self):
        elements = [_button("OK", 1200, 700)]
        result = classify_obstruction(_make_inventory(elements))
        self.assertNotEqual(result.kind, KIND_DIALOG)

    def test_buttons_only_in_top_half_does_not_classify_dialog(self):
        """If the buttons are AT the top of the cluster (not below
        the text body), it's probably a tab bar / chrome, not a modal
        dialog action row."""
        elements = [
            _button("Tab 1", 1100, 250),
            _button("Tab 2", 1300, 250),
            _text("Body line A", 1200, 500),
            _text("Body line B", 1200, 600),
        ]
        result = classify_obstruction(_make_inventory(elements))
        self.assertNotEqual(result.kind, KIND_DIALOG)

    def test_isolated_single_bottom_button_does_not_classify_dialog(self):
        """Regression: Bureau / Harbor / Inn / Bank main views all have
        a Language Effect or similar isolated button near cy ~1010.
        With several central-zone text elements, the old detector
        false-fired dialog when it saw `1 button below median`.  Now
        we require a horizontal action ROW (≥ 2 buttons at similar cy),
        so single isolated buttons no longer trigger."""
        elements = [
            _text("Bureaucrat",          881, 705),
            _text("to aid its",         1239, 765),
            _text("port",               1125, 770),
            _text("development: A",      904, 814),
            _button("You can invest",   1154, 838),   # lone bottom button
        ]
        result = classify_obstruction(_make_inventory(elements))
        self.assertNotEqual(result.kind, KIND_DIALOG,
                             f"got {result.kind} signals={result.signals}")


# ── Popup — synthetic ──────────────────────────────────────────────────


class PopupSyntheticTests(unittest.TestCase):

    def test_top_right_close_x_with_body_classifies_popup(self):
        elements = [
            _icon(2016, 91, 80, 80),                   # close-X top-right
            _text("Perk Event", 1500, 200),            # popup title
            _button("Collect", 1500, 600),             # action button
        ]
        result = classify_obstruction(_make_inventory(elements))
        self.assertEqual(result.kind, KIND_POPUP)

    def test_lonely_top_right_icon_without_body_does_not_classify(self):
        """A top-right icon alone (chrome) shouldn't fire as popup."""
        elements = [
            _icon(2336, 47, 76, 76),    # chrome icon
        ]
        result = classify_obstruction(_make_inventory(elements))
        self.assertNotEqual(result.kind, KIND_POPUP)

    def test_oversized_bbox_rejected_as_popup(self):
        """Regression 2026-05-15: when the bot was in front of Fortune
        Teller with the expanded nameplate panel visible, the popup
        detector found a false close-X candidate in the right panel
        area at (2345, 358) and gathered the whole right side + center
        as 'body', producing a 1316×869 'popup' bbox.  Real popups in
        UWO are ≤ 900×700; bigger means it's chrome + content, not a
        single popup."""
        elements = [
            _icon(2345, 358, 80, 80),                 # false close-X
            # 17 scattered body-like elements far from each other
            *[_button(f"item-{i}", 1100 + (i*60), 500 + (i*40))
              for i in range(8)],
            *[_text(f"label-{i}", 1500 + (i*70), 700 + (i*50))
              for i in range(5)],
            _button("Confirm", 2100, 1050),
            _text("description here", 2050, 920),
            _button("Cancel", 2200, 1040),
        ]
        result = classify_obstruction(_make_inventory(elements))
        self.assertNotEqual(
            result.kind, KIND_POPUP,
            f"got popup with bbox spanning whole right side: {result.signals}",
        )

    def test_chrome_icon_row_with_right_panel_does_not_classify_popup(self):
        """Union / Cathedral main views: chrome icons at top-right
        plus a right-side panel of building info.  The chrome row sits
        in the very top band (cy < 80) which is OUTSIDE the popup
        close-X zone, so this must NOT classify as popup.

        Regression: 2026-05-15 saw both Union and Cathedral
        false-firing on popup because top-right currency icons were
        being matched as close-X candidates."""
        elements = [
            _icon(2334, 51, 76, 76),     # chrome icon — top edge
            _icon(2244, 50, 80, 80),     # chrome icon — top edge
            _button("Union Master", 890, 704),
            _button("There might be", 1155, 840),
            _text("16,854,231,386", 1588, 46),    # currency label
            _text("82,066",          1820, 46),
        ]
        result = classify_obstruction(_make_inventory(elements))
        self.assertNotEqual(result.kind, KIND_POPUP)


# ── None — normal screen ──────────────────────────────────────────────


class NoneClassificationTests(unittest.TestCase):

    def test_port_overworld_classifies_none(self):
        """Right-edge building list + top-left port name — no
        obstruction signature should fire."""
        elements = [
            _text("London", 275, 45),
            _text("harbor",   2100, 200),
            _text("market",   2100, 300),
            _text("shipyard", 2100, 400),
        ]
        result = classify_obstruction(_make_inventory(elements))
        self.assertEqual(result.kind, KIND_NONE)
        self.assertFalse(result.is_obstructed)

    def test_building_main_view_classifies_none(self):
        """Bureau main view — title + left-strip menu items.  No
        central NPC speech pair, no action button row, no popup
        frame.  Must NOT fire."""
        elements = [
            _text("Bureau", 250, 50),
            _text("Invest", 196, 153),
            _text("Tax",    196, 260),
            _text("Manage Market", 220, 370),
            _text("Event",  196, 480),     # the false-firing word!
        ]
        result = classify_obstruction(_make_inventory(elements))
        self.assertEqual(result.kind, KIND_NONE,
                         f"got {result.kind}, signals={result.signals}")


if __name__ == "__main__":
    unittest.main()
