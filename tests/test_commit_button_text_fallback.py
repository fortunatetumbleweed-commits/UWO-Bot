"""Text-pair fallback for the yellow commit-button detector.

When OmniParser drops the button bbox and emits the commit pill only as <cost> +
<verb> TEXT (the ~0.63 flakiness — the live Goa Sell button that failed 2026-08-16),
detect_commit_buttons must still reconstruct it from the text over the yellow band.
"""
import numpy as np
from PIL import Image

from vision.region_detectors.commit_button import detect_commit_buttons


class E:
    """Minimal OmniParser-element stand-in."""
    def __init__(self, content, etype, box):
        self.content = content
        self.element_type = etype
        self.x1, self.y1, self.x2, self.y2 = box
        self.cx = (self.x1 + self.x2) // 2
        self.cy = (self.y1 + self.y2) // 2


def _frame_with_yellow_band(box=None):
    """2400×1080 dark frame with a yellow pill filled in `box` (default = the
    bottom-right Sell-button region)."""
    W, H = 2400, 1080
    arr = np.full((H, W, 3), 20, dtype=np.uint8)           # dark
    x1, y1, x2, y2 = box or (1800, 960, 2300, 1040)
    arr[y1:y2, x1:x2] = (235, 200, 60)                      # yellow/gold pill
    return Image.fromarray(arr, "RGB")


def test_reconstructs_sell_from_text_pair_over_yellow():
    frame = _frame_with_yellow_band()
    els = [
        E("557,550", "text", (1820, 980, 1930, 1015)),     # cost, LEFT
        E("Sell",    "text", (2080, 980, 2160, 1015)),     # verb, RIGHT
    ]
    commits = detect_commit_buttons(els, frame)
    assert len(commits) == 1
    c = commits[0]
    assert c.verb == "Sell"
    assert c.cost == "557,550"
    assert c.currency == "ducat"          # gold pill, no gem icon
    assert 2000 < c.cx < 2250             # taps on the button


def test_no_false_positive_off_yellow():
    """Same text pair over a DARK region (no yellow pill) → not a commit."""
    frame = _frame_with_yellow_band(box=(0, 0, 1, 1))      # effectively no band
    els = [
        E("557,550", "text", (1820, 980, 1930, 1015)),
        E("Sell",    "text", (2080, 980, 2160, 1015)),
    ]
    assert detect_commit_buttons(els, frame) == []


def test_ignores_verb_pair_high_on_screen():
    """A number+word pair in the goods grid (top half) is a tile, not the commit."""
    frame = _frame_with_yellow_band(box=(1800, 200, 2300, 280))
    els = [
        E("2,489", "text", (1820, 220, 1930, 255)),
        E("Buy",   "text", (2080, 220, 2160, 255)),
    ]
    assert detect_commit_buttons(els, frame) == []


def test_button_path_still_wins_when_present():
    """When OmniParser DOES emit a yellow wide button, the text fallback stays off."""
    frame = _frame_with_yellow_band(box=(1800, 960, 2320, 1035))
    els = [E("Sell", "button", (1800, 960, 2320, 1035))]   # wide yellow pill
    commits = detect_commit_buttons(els, frame)
    assert len(commits) == 1
    assert "sell" in commits[0].verb.lower() or commits[0].cost == ""
