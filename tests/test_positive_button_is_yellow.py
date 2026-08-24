"""A positive button is identified by its YELLOW BACKGROUND, not by its wording.

`POSITIVE_LABELS` contains the word "trade", and the match is a substring test, so it
also matches Trade Info, Trade Goods, Trade Points and Requested Trade Goods — none of
which commit anything. On 2026-08-22 an escalation ran `commit_via_positive_taps` on the
Market landing page and tapped `Trade Info` (177,817) and then the `Requested Trade Goods`
panel title (2024,567), on a screen with no transaction in flight at all.

Measured inside each element's own bbox on the frames from that run:

    Ok                     yellow 0.64   <- the real commit
    Cancel                 yellow 0.00
    Trade Info             yellow 0.00
    Requested Trade Goods  yellow 0.00
    Trade Points           yellow 0.00

The populations are not close, so requiring the colour costs nothing and removes the
whole class of word-match false positives.

User, 2026-08-22: "in this game a positive button is a button that has yellow back ground,
the words in it is less relevant … for now make sure the button has yellow back ground in
the same hue like in the dialog Ok button."
"""
import numpy as np
import pytest
from PIL import Image

from brain.commit_actions import has_positive_background


class _El:
    def __init__(self, x1, y1, x2, y2, label=""):
        self.x1, self.y1, self.x2, self.y2, self.label = x1, y1, x2, y2, label


def _frame(rgb, box=(100, 100, 300, 160)):
    """A frame with one button-sized patch of the given colour."""
    a = np.zeros((1080, 2400, 3), dtype=np.uint8)
    a[:, :] = (40, 40, 40)
    x1, y1, x2, y2 = box
    a[y1:y2, x1:x2] = rgb
    return Image.fromarray(a)


GOLD = (255, 200, 60)        # the dialog OK button: hue ≈ 38, sat ≈ 0.56, val ≈ 1.0
PALE = (242, 244, 247)       # Cancel
GREY_PANEL = (200, 196, 188)  # Trade Info / panel titles
BOX = (100, 100, 300, 160)


class TestBackgroundGate:
    def test_the_gold_commit_button_passes(self):
        assert has_positive_background(_frame(GOLD), _El(*BOX, "Ok"))

    def test_the_pale_cancel_button_is_rejected(self):
        assert not has_positive_background(_frame(PALE), _El(*BOX, "Cancel"))

    def test_a_grey_trade_labelled_control_is_rejected(self):
        """The exact false positive: matches "trade" by text, commits nothing."""
        assert not has_positive_background(_frame(GREY_PANEL), _El(*BOX, "Trade Info"))

    def test_a_mostly_grey_button_with_a_little_gold_is_rejected(self):
        """A gold icon or badge on an otherwise grey control must not qualify."""
        a = np.array(_frame(GREY_PANEL))
        a[100:112, 100:120] = GOLD          # ~4% of the button
        assert not has_positive_background(Image.fromarray(a), _El(*BOX, "Trade Info"))


class TestGateFailsClosed:
    def test_an_element_without_a_bbox_is_rejected(self):
        class _NoBox:
            label = "Ok"
        assert not has_positive_background(_frame(GOLD), _NoBox())

    def test_a_zero_area_bbox_is_rejected(self):
        assert not has_positive_background(_frame(GOLD), _El(100, 100, 100, 100, "Ok"))

    def test_an_unreadable_frame_is_rejected(self):
        """This gates an action — "cannot tell" must never mean "go ahead"."""
        assert not has_positive_background(None, _El(*BOX, "Ok"))



# ── A dimming overlay is not a disabled button ────────────────────────────────
#
# The colour test needs a minimum brightness, so a popup darkening the screen drags a
# perfectly gold Exchange below threshold. Live 2026-08-23, after the first barter round an
# overlay remained:
#
#     [commit] iter 2: 'Exchange' matched by text but has no positive (yellow) background
#     [commit] iter 0: 'Exchange' matched by text but has no positive (yellow) background
#     [progress] Melanesian Village: sailing_route
#
# The button was still yellow and still tappable (user 2026-08-23) — tapping it would have
# dismissed the dialog — but the mission gave up with a second funded round unspent. Clear
# what is in the way, THEN judge.

def _commit_run(cleared, positive_after):
    from unittest.mock import patch
    from brain import commit_actions
    frame = Image.new("RGB", (2400, 1080))
    btn = type("E", (), {"label": "Exchange", "cx": 2100, "cy": 997,
                         "x1": 2000, "y1": 970, "x2": 2200, "y2": 1024})()
    seq = [False, positive_after]
    taps = []
    with patch.object(commit_actions, "_yellow_commit_button", return_value=None), \
         patch.object(commit_actions, "find_positive_button", return_value=btn), \
         patch.object(commit_actions, "has_positive_background",
                      side_effect=lambda *_a, **_k: seq.pop(0) if seq else False), \
         patch("brain.unexpected_dialog.clear_blockers", return_value={"cleared": cleared}), \
         patch("vision.omniparser.parse_fast_cached", return_value=[btn]):
        commit_actions.commit_via_positive_taps(
            capture_fn=lambda: frame, tap_fn=lambda x, y: taps.append((x, y)), max_taps=3)
    return taps


def test_a_dimming_overlay_is_cleared_then_the_button_commits():
    assert _commit_run(cleared=True, positive_after=True) == [(2100, 997)]


def test_with_nothing_to_clear_it_still_refuses():
    """The guard that stopped the Trade Info mis-taps must survive."""
    assert _commit_run(cleared=False, positive_after=True) == []


# ── A pale gold commit button is still a commit button ────────────────────────
#
# The 0.45 saturation floor was calibrated on the DIALOG OK button (64-91% gold). The village
# barter panel's Exchange is a PALE gold gradient — median saturation 0.39 — so it scored
# 0.067 and was rejected while plainly live on screen (2026-08-23). It passed one frame and
# failed the next, being right on the boundary, and the mission gave up mid-barter:
#
#     [commit] iter 2: 'Exchange' matched by text but has no positive (yellow) background
#     FAILED at step mission: barter commit stalled after 0
#
# Measured on that panel at the new floor, the populations are nowhere near each other:
#
#     Exchange                 0.654
#     Negotiate                0.000
#     Check Village Influence  0.000

def _swatch(rgb, size=(120, 40)):
    """A solid button-sized patch of one colour, with a bbox element over it."""
    img = Image.new("RGB", (2400, 1080), (30, 30, 30))
    img.paste(Image.new("RGB", size, rgb), (2000, 980))
    el = type("E", (), {"x1": 2000, "y1": 980, "x2": 2000 + size[0], "y2": 980 + size[1]})()
    return img, el


def test_a_pale_gold_button_is_positive():
    """hue ~38, saturation ~0.39 — the live Exchange pill."""
    img, el = _swatch((247, 214, 150))
    assert has_positive_background(img, el)


def test_a_deep_gold_button_is_still_positive():
    """The dialog OK the threshold was originally calibrated on."""
    img, el = _swatch((240, 190, 70))
    assert has_positive_background(img, el)


def test_a_grey_button_is_not_positive():
    """Negotiate / Check Village Influence / Trade Info all measure 0.000."""
    img, el = _swatch((225, 225, 228))
    assert not has_positive_background(img, el)


def test_a_near_white_panel_is_not_positive():
    img, el = _swatch((250, 250, 245))
    assert not has_positive_background(img, el)
