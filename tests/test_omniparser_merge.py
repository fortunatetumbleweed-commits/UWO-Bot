"""OmniParser merge-logic regression tests.

Origin: 2026-05-14.  The previous merge rule promoted an icon to a
button when ≥30% of a text bbox overlapped — and on conflict, appended
each additional text to the icon's label.  This produced two failure
modes:

1.  Large YOLO panel bboxes (ship list, market grid, NPC overlay area)
    swallowed every text that overlapped, turning the icon's label
    into a soup string like
        'Improved Clipper 99/223 X99,453 ... Harbor Official ... The Crew is readyl ...'

2.  Real overlay / speech-bubble text was hidden inside that soup
    string — downstream consumers (interruptor detection, scene
    classification) couldn't see it as a distinct element.

The fix:
    - Containment threshold raised 0.3 → 0.7 (text must be mostly
      inside the icon, not just clipping).
    - Panel-sized icons (>25% of frame area) are skipped as merge
      targets — their interior text stays standalone.
    - On conflict (icon already has a real label), the new text stays
      standalone instead of being appended.

These tests pin both behaviours.
"""

from __future__ import annotations

import unittest

from vision.omniparser import DetectedElement, _merge_icons_and_text


SCREEN_W = 2400
SCREEN_H = 1080


def _icon(cx, cy, w, h, conf=0.7) -> DetectedElement:
    return DetectedElement(
        label="icon", element_type="icon",
        x1=cx - w // 2, y1=cy - h // 2,
        x2=cx + w // 2, y2=cy + h // 2,
        confidence=conf,
    )


def _text(label, cx, cy, w=120, h=32, conf=0.9) -> DetectedElement:
    return DetectedElement(
        label=label, element_type="text",
        x1=cx - w // 2, y1=cy - h // 2,
        x2=cx + w // 2, y2=cy + h // 2,
        confidence=conf,
    )


def _by_label(elements, label):
    return next((e for e in elements if e.label == label), None)


# ── Panel-sized icon must NOT swallow interior texts ───────────────────────


class PanelIconNotMergeTargetTests(unittest.TestCase):

    def test_speech_bubble_text_stays_standalone(self):
        """The recruit-crew confirmation overlay: NPC speech bubble at
        the center sits over the ship-list panel.  Panel bbox covers
        the bubble area too.  Before the fix, 'Harbor Official' got
        appended to the ship-list panel's label.  After: it stays as
        its own text element."""
        ship_list_panel = _icon(1147, 682, 1339, 456)   # huge panel
        elements_in = [ship_list_panel]
        texts = [
            _text("Improved Clipper",   1147, 502),     # actually inside
            _text("Harbor Official",     909, 704),     # speech bubble
            _text("The Crew is ready!",  929, 769),     # speech bubble
        ]
        out = _merge_icons_and_text(
            elements_in, texts, frame_dims=(SCREEN_W, SCREEN_H),
        )
        # Panel icon stays as icon (not promoted to button)
        panel = next(e for e in out if e.x1 == ship_list_panel.x1)
        self.assertEqual(panel.element_type, "icon")
        # Speech bubble texts survive as standalone text elements
        for label in ["Harbor Official", "The Crew is ready!",
                      "Improved Clipper"]:
            el = _by_label(out, label)
            self.assertIsNotNone(el, f"missing {label!r}")
            self.assertEqual(el.element_type, "text",
                              f"{label!r} should stay as text")

    def test_building_list_panel_does_not_concatenate_rows(self):
        """The right-panel building list is one huge YOLO bbox covering
        multiple rows.  Each row's text must stay as its own element,
        not be concatenated onto the panel.  Regression for the
        'fortune teller 4.0501.041.291 2605141023 atlantic ocean'
        garbage rows seen in the explore_port logs."""
        right_panel = _icon(2100, 600, 600, 800)   # tall right panel
        texts = [
            _text("harbor",   2100, 200),
            _text("market",   2100, 300),
            _text("shipyard", 2100, 400),
            _text("4.0501.041.291 atlantic ocean", 2150, 950),
        ]
        out = _merge_icons_and_text(
            [right_panel], texts, frame_dims=(SCREEN_W, SCREEN_H),
        )
        # Panel stays icon — no soup label
        panel = next(e for e in out if e.x1 == right_panel.x1)
        self.assertEqual(panel.element_type, "icon")
        # All row texts survive standalone
        self.assertIsNotNone(_by_label(out, "harbor"))
        self.assertIsNotNone(_by_label(out, "market"))
        self.assertIsNotNone(_by_label(out, "shipyard"))


# ── Normal button-sized icon still gets its text label ────────────────────


class ButtonPromotionStillWorksTests(unittest.TestCase):

    def test_small_icon_with_contained_text_becomes_button(self):
        """The bread-and-butter case: a YOLO button bbox around a text
        label.  Must still be promoted to element_type='button'."""
        btn = _icon(500, 300, 200, 80)
        txt = _text("Confirm", 500, 300, w=150, h=40)
        out = _merge_icons_and_text(
            [btn], [txt], frame_dims=(SCREEN_W, SCREEN_H),
        )
        # Exactly one element returned (icon promoted, no leftover text)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].element_type, "button")
        self.assertEqual(out[0].label, "Confirm")

    def test_low_containment_does_not_promote(self):
        """A text whose bbox barely clips an icon should NOT promote
        that icon to a button — too low containment."""
        btn = _icon(500, 300, 200, 80)
        txt = _text("Drifting Text", 800, 300, w=200, h=40)   # mostly outside
        out = _merge_icons_and_text(
            [btn], [txt], frame_dims=(SCREEN_W, SCREEN_H),
        )
        self.assertEqual(len(out), 2)
        # Icon stays icon; text stays text
        icon = next(e for e in out if e.element_type == "icon")
        text = next(e for e in out if e.element_type == "text")
        self.assertEqual(icon.label, "icon")
        self.assertEqual(text.label, "Drifting Text")


# ── Conflict path: no more append-on-conflict ─────────────────────────────


class NoAppendOnConflictTests(unittest.TestCase):

    def test_second_text_inside_same_small_icon_stays_standalone(self):
        """When two texts both land inside the same small icon, the
        first promotes it; the second must NOT be appended to the
        first's label.  No more soup strings."""
        btn = _icon(500, 300, 400, 200)
        out = _merge_icons_and_text(
            [btn],
            [_text("Primary", 500, 280, w=120, h=30),
             _text("Secondary", 500, 330, w=120, h=30)],
            frame_dims=(SCREEN_W, SCREEN_H),
        )
        # Icon promoted with first label only
        promoted = next(e for e in out if e.element_type == "button")
        self.assertEqual(promoted.label, "Primary")
        # Second stays as standalone text
        secondary = _by_label(out, "Secondary")
        self.assertIsNotNone(secondary)
        self.assertEqual(secondary.element_type, "text")


# ── Back-compat: no frame_dims means panel-size cap is skipped ────────────


class BackwardCompatTests(unittest.TestCase):

    def test_no_frame_dims_skips_panel_cap_but_still_uses_threshold(self):
        """Legacy callers without frame_dims: panel cap is off, but
        the tighter 0.7 containment threshold still applies."""
        small_icon = _icon(500, 300, 200, 80)
        txt = _text("Confirm", 500, 300, w=150, h=40)
        out = _merge_icons_and_text([small_icon], [txt])
        # Promotion still works
        self.assertEqual(out[0].element_type, "button")
        self.assertEqual(out[0].label, "Confirm")


if __name__ == "__main__":
    unittest.main()
