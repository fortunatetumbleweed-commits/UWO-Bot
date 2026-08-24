"""Deterministic action-outcome classifier (vision/frame_diff)."""
import numpy as np
from PIL import Image

from vision.frame_diff import classify_action_outcome


def _frame(fill=60):
    return Image.fromarray(np.full((1080, 2400, 3), fill, dtype=np.uint8), "RGB")


def _paint(img, box, colour):
    """box = (x0,y0,x1,y1) fractions."""
    a = np.array(img)
    h, w = a.shape[:2]
    x0, y0, x1, y1 = box
    a[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)] = colour
    return Image.fromarray(a, "RGB")


def test_unchanged_frames():
    prev = _frame(60)
    cur = _frame(60)
    assert classify_action_outcome(prev, cur).kind == "unchanged"


def test_clock_tick_only_is_unchanged():
    # a tiny change in the top-HUD strip (the clock) → ignored → unchanged.
    prev = _frame(60)
    cur = _paint(_frame(60), (0.85, 0.01, 0.95, 0.06), (255, 255, 255))
    assert classify_action_outcome(prev, cur).kind == "unchanged"


def test_central_dialog():
    # a modal appears over the centre; periphery unchanged.
    prev = _frame(60)
    cur = _paint(_frame(60), (0.30, 0.25, 0.68, 0.80), (240, 240, 240))
    assert classify_action_outcome(prev, cur).kind == "central_dialog"


def test_panel_or_button():
    # right panel toggles on; centre unchanged.
    prev = _frame(60)
    cur = _paint(_frame(60), (0.80, 0.15, 0.99, 0.90), (200, 180, 120))
    assert classify_action_outcome(prev, cur).kind == "panel_or_button"


def test_bottom_button_change():
    prev = _frame(60)
    cur = _paint(_frame(60), (0.20, 0.90, 0.90, 0.99), (230, 210, 90))
    assert classify_action_outcome(prev, cur).kind == "panel_or_button"


def test_state_change_whole_screen():
    # enter/exit building — the whole content changes.
    prev = _frame(60)
    cur = _frame(200)
    assert classify_action_outcome(prev, cur).kind == "state_change"


def test_outcome_is_truthy_when_changed():
    assert not classify_action_outcome(_frame(60), _frame(60))       # unchanged → falsy
    assert classify_action_outcome(_frame(60), _frame(200))          # changed → truthy
