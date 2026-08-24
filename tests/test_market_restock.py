"""Market restock/refresh button detector — the white pill right of the restock timer
showing the blue-gem cost. Synthetic frames + injected OCR (no device)."""
import numpy as np
from PIL import Image

from vision.region_detectors.market_restock import find_restock_button


def _frame(pill=(1454, 140, 1580, 172), gem="blue", timer_xy=(1308, 156)):
    """Dark 2400×1080 frame with a white pill + a gem blob, and an OCR fn that reports
    the restock timer at timer_xy."""
    arr = np.full((1080, 2400, 3), 25, dtype=np.uint8)
    x0, y0, x1, y1 = pill
    arr[y0:y1, x0:x1] = (235, 235, 235)                      # white pill
    if gem:                                                  # gem icon on the pill right
        gx = x1 - 55
        arr[y0 + 8:y1 - 8, gx:gx + 26] = (95, 120, 195) if gem == "blue" else (200, 60, 60)
    frame = Image.fromarray(arr, "RGB")
    ocr = lambda _f, min_conf=0.2: ([("00:00:36", 0.9, timer_xy[0], timer_xy[1])]
                                    if timer_xy else [])
    return frame, ocr


def test_finds_blue_gem_refresh_pill():
    frame, ocr = _frame(gem="blue")
    b = find_restock_button(frame, ocr_fn=ocr)
    assert b is not None
    assert b.currency == "blue_gem"
    # Tap target is the ↻ refresh ICON (timer_x + 96), NOT the pill centre / gem-cost.
    assert b.cx == 1308 + 96 and b.cy == 156
    assert b.timer == "00:00:36"


def test_red_gem_is_flagged_not_blue():
    frame, ocr = _frame(gem="red")
    b = find_restock_button(frame, ocr_fn=ocr)
    assert b is not None and b.currency == "red_gem"


def test_no_timer_returns_none():
    frame, ocr = _frame(timer_xy=None)          # OCR finds no timer → not refreshable
    assert find_restock_button(frame, ocr_fn=ocr) is None


def test_no_white_pill_returns_none():
    # timer present but no white pill to its right (e.g. market already fully stocked)
    arr = np.full((1080, 2400, 3), 25, dtype=np.uint8)
    frame = Image.fromarray(arr, "RGB")
    ocr = lambda _f, min_conf=0.2: [("00:00:36", 0.9, 1308, 156)]
    assert find_restock_button(frame, ocr_fn=ocr) is None
