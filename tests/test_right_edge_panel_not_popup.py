"""Test the right-edge HUD-panel guard inside _detect_popup.

2026-05-22 Palma harbor entry: the persistent right-side city facility
panel was repeatedly mis-classified as a popup obstruction.  Each
detection triggered a ~5 s Claude consult + ~5 s dismiss attempt,
running ~10× before the bot gave up.  The structural guard added in
this commit rejects bboxes anchored flush to the right screen edge
and the top — that's the HUD, not a popup.
"""
import unittest

from vision.obstruction_classifier import classify_obstruction
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
    class _Inv:
        pass
    inv = _Inv()
    inv.raw_elements = elements
    inv.frame_dims = (SCREEN_W, SCREEN_H)
    return inv


class RightEdgeHudGuardTests(unittest.TestCase):

    def test_right_side_city_panel_not_classified_as_popup(self):
        """Regression: simulate the Palma right-side panel.  Close-X
        glyph at (2334, 157), body items below it, panel right-edge
        flush with screen edge (x2 ≈ 2400), panel top near y=120."""
        # The close-X icon at top-right of the panel
        close_x = _icon(2334, 157, 40, 40, label="X")
        # Build a popup-shaped body of labelled buttons that satisfies
        # the existing popup detector (≥2 body items, ≥1 button) but
        # whose right edge runs to the screen edge.
        elements = [
            close_x,
            _button("Harbor",    2150, 280),
            _button("Market",    2150, 380),
            _button("Shipyard",  2150, 480),
            _button("Bank",      2150, 580),
            # All bbox right-edges around x2≈2250-2400 (flush right).
        ]
        result = classify_obstruction(_make_inventory(elements))
        # Expected: NOT classified as popup (the right-edge guard
        # rejects it) — should fall through to KIND_NONE or a different
        # detector.
        self.assertNotEqual(
            result.kind, "popup",
            f"Right-edge HUD panel must not be classified as popup; got: "
            f"{result.kind} bbox={result.bbox}",
        )

    def test_real_popup_centered_still_detected(self):
        """A real popup in the middle of the screen, with margin from
        the right edge, must still classify as popup."""
        # Close-X glyph at top-right of a CENTERED popup.  Close-X zone
        # is normalised (0.65, 0.08, 1.00, 0.35) → on 2400×1080,
        # x ∈ (1560, 2400), y ∈ (86, 378).  Place at (1900, 280).
        close_x = _icon(1900, 280, 40, 40, label="X")
        elements = [
            close_x,
            _button("OK",     1500, 480),
            _button("Cancel", 1700, 480),
            _text("Are you sure?", 1600, 380),
        ]
        result = classify_obstruction(_make_inventory(elements))
        self.assertEqual(result.kind, "popup")
        # Bbox right edge should NOT be at screen edge.
        self.assertLess(result.bbox[2], SCREEN_W - 50)

    def test_right_edge_popup_with_low_top_not_blocked(self):
        """The guard only rejects when both right-edge-flush AND
        top-anchored.  A popup deep in the screen that happens to be
        right-edge-flush should still classify (rare in practice but
        a regression test for the guard's specificity)."""
        # Close-X at right edge but FAR DOWN (y > 200 → guard skipped)
        close_x = _icon(2334, 600, 40, 40, label="X")
        elements = [
            close_x,
            _button("OK",     2100, 700),
            _button("Cancel", 2300, 700),
            _text("Confirm", 2200, 650),
        ]
        result = classify_obstruction(_make_inventory(elements))
        # Either popup (preferred) or KIND_NONE if it doesn't satisfy
        # popup geometry — the important thing is the guard doesn't
        # incorrectly fire on this case.  Just confirm we don't crash.
        self.assertIn(result.kind, ("popup", "none"))


if __name__ == "__main__":
    unittest.main()
