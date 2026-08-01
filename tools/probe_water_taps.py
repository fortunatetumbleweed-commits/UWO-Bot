#!/usr/bin/env python
"""Diagnose water-tap localization on the currently-displayed world map.

Run with the world map open in scrcpy.  The script:

  1. Captures one frame.
  2. For each pixel in WATER_TAP_CANDIDATES: samples RGB, reports the
     average over a 9×9 window, and prints the water-vs-land verdict
     under the current heuristic.
  3. Saves the frame + a marked-up copy (yellow dots at each candidate,
     green = "water" verdict, red = "land") to /tmp/uwo_water_probe/.
  4. If --tap is passed: taps each "water" candidate one by one,
     captures the resulting frame, OCRs the lat/lon readout, prints
     whether the tap actually brought up the readout.

This isolates the failure mode:

  • If the heuristic says "land" on actual water → the B-R threshold
    is too strict for the current zoom/tint; tighten it.
  • If the heuristic says "water" but the tap brings up no readout →
    the readout crop is wrong, or the game doesn't show the panel for
    that tap location.
  • If both work → the bug is elsewhere (e.g. candidate pixels happen
    to land on UI controls that don't trigger the panel).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np
from PIL import Image, ImageDraw

from capture.adb_capture import capture_screen
from actions.water_tap import (
    WATER_TAP_CANDIDATES, tap_water_and_read_latlon,
)
from actions.latlon_localize import (
    WATER_GR_DELTA, WATER_SAMPLE_RADIUS, is_pixel_water,
)


_OUT = Path("/tmp/uwo_water_probe")


def _rgb_at(frame: Image.Image, px: int, py: int, r: int = WATER_SAMPLE_RADIUS):
    w, h = frame.size
    left, top = max(0, px - r), max(0, py - r)
    right, bottom = min(w, px + r + 1), min(h, py + r + 1)
    crop = np.array(frame.crop((left, top, right, bottom)).convert("RGB"))
    return float(crop[..., 0].mean()), float(crop[..., 1].mean()), float(crop[..., 2].mean())


def _annotate(frame: Image.Image, verdicts: list) -> Image.Image:
    img = frame.copy()
    draw = ImageDraw.Draw(img)
    for (px, py, is_water, _rgb) in verdicts:
        color = (0, 255, 0) if is_water else (255, 0, 0)
        draw.ellipse([px - 18, py - 18, px + 18, py + 18], outline=color, width=4)
        draw.text((px + 22, py - 10), f"({px},{py})", fill=color)
    return img


def main(argv):
    do_tap = "--tap" in argv
    no_filter = "--no-filter" in argv
    _OUT.mkdir(parents=True, exist_ok=True)
    print(f"=== Water-tap probe ===")
    print(f"Heuristic: G > R + {WATER_GR_DELTA}  window radius={WATER_SAMPLE_RADIUS}")
    if no_filter:
        print(f"Mode: --no-filter — will tap every candidate regardless of verdict")
    print(f"Candidates: {len(WATER_TAP_CANDIDATES)}")
    print()

    frame = capture_screen()
    ts = time.strftime("%H%M%S")
    frame_path = _OUT / f"frame_{ts}.png"
    frame.save(frame_path)
    print(f"Saved frame to {frame_path}")

    print()
    print(f"{'pixel':>14s}  {'R':>6s} {'G':>6s} {'B':>6s}   {'G-R':>5s}   verdict")
    print(f"{'-'*60}")
    verdicts = []
    for (px, py) in WATER_TAP_CANDIDATES:
        r, g, b = _rgb_at(frame, px, py)
        is_w = is_pixel_water(frame, px, py)
        verdicts.append((px, py, is_w, (r, g, b)))
        print(f"  ({px:4d},{py:4d})  {r:6.1f} {g:6.1f} {b:6.1f}   {g-r:5.1f}   {'WATER' if is_w else 'land '}")

    annotated = _annotate(frame, verdicts)
    annotated.save(_OUT / f"annotated_{ts}.png")
    print()
    print(f"Annotated frame: {_OUT / f'annotated_{ts}.png'}")
    print(f"  green ring = heuristic says water,  red ring = land")

    if not do_tap:
        print()
        print(f"Pass --tap to also tap each 'water' candidate and OCR the readout.")
        return 0

    print()
    if no_filter:
        print(f"=== Tapping ALL candidates (--no-filter) ===")
    else:
        print(f"=== Tapping 'water' candidates only ===")
    print()
    for (px, py, is_w, _) in verdicts:
        if not no_filter and not is_w:
            continue
        verdict_str = "WATER" if is_w else "land"
        print(f"Tapping ({px}, {py})  [heuristic: {verdict_str}]…")
        result = tap_water_and_read_latlon(px, py, debug_dir=_OUT)
        if result is None:
            print(f"  ✗ no lat/lon readout — tap may have hit land/icon,")
            print(f"    or the readout crop is wrong.  See debug PNG in {_OUT}.")
        else:
            print(f"  ✓ readout: lat={result[0]}, lon={result[1]}")
        print()

    print(f"All debug crops + frames saved in {_OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
