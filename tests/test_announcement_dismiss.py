"""The event/announcement popup close-X detector + clear_blockers dismissal.

Regression for the gather-run bug (2026-08-17): clear_blockers tapped the SCREEN
CORNER (2340,54) as the popup's close, but at sea/port_overworld that is the ☰
hamburger → it OPENED Company Overview instead of closing the popup. It must tap the
popup's OWN close-X (a black disc at its top-right). See
project_home_button_is_chromed_only_escape.
"""
import numpy as np
from PIL import Image
from unittest import mock

from brain import unexpected_dialog as ud


def _frame(disc=(1810, 220)):
    arr = np.full((1080, 2400, 3), 185, dtype=np.uint8)     # light background
    if disc:
        cx, cy = disc
        yy, xx = np.ogrid[:1080, :2400]
        arr[(xx - cx) ** 2 + (yy - cy) ** 2 <= 22 ** 2] = (8, 8, 8)   # black disc
    return Image.fromarray(arr, "RGB")


def test_finds_the_popup_close_disc():
    x = ud.find_announcement_close_x(_frame((1810, 220)))
    assert x is not None
    assert abs(x[0] - 1810) < 30 and abs(x[1] - 220) < 30


def test_none_when_no_disc():
    assert ud.find_announcement_close_x(_frame(disc=None)) is None


def test_clear_blockers_taps_popup_x_never_the_corner():
    frame = _frame((1810, 220))
    taps, backs = [], []
    # OCR: first frame is the announcement, after dismiss it's clean.
    with mock.patch.object(ud, "_ocr_text",
                           side_effect=["perk season competition updates notices",
                                        "", "", ""]):
        r = ud.clear_blockers(
            frame,
            tap_fn=lambda x, y: taps.append((x, y)),
            back_fn=lambda: backs.append(1),
            capture_fn=lambda: _frame(disc=None),   # cleared after the tap
        )
    assert r["cleared"] is True and r["kind"] == "announcement"
    assert taps, "should have tapped the popup close-X"
    tx, ty = taps[0]
    assert abs(tx - 1810) < 40 and abs(ty - 220) < 40   # the popup X
    assert (tx, ty) != (2340, 54)                        # NOT the screen-corner hamburger
    assert all(t != (2340, 54) for t in taps)


def test_clear_blockers_falls_back_to_back_when_no_disc():
    # announcement text but no close-X disc → press Back (not a corner tap).
    frame = _frame(disc=None)
    taps, backs = [], []
    with mock.patch.object(ud, "_ocr_text",
                           side_effect=["attendance time anniversary stars", "", ""]):
        ud.clear_blockers(frame,
                          tap_fn=lambda x, y: taps.append((x, y)),
                          back_fn=lambda: backs.append(1),
                          capture_fn=lambda: _frame(disc=None))
    assert backs, "no disc → should fall back to Back"
    assert all(t != (2340, 54) for t in taps)
