"""Capture the current screen and annotate the sea-control tap targets.

Use this to verify that ARROW_LEFT_XY, ARROW_RIGHT_XY, and
RUDDER_TOGGLE_XY in actions/sea_actions.py actually hit the visible
rudder + arrow icons on the phone.  If the cross-hairs land on the
wrong UI elements, the coordinates need to be re-calibrated for this
phone.

Usage:
    python tools/check_rudder_coords.py
    open /tmp/rudder_coords.png
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def main() -> int:
    from PIL import Image, ImageDraw, ImageFont
    from capture.adb_capture import capture_screen
    from actions.sea_actions import (
        RUDDER_TOGGLE_XY, ARROW_LEFT_XY, ARROW_RIGHT_XY,
    )

    img = capture_screen()
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", 36)
    except Exception:
        font = ImageFont.load_default()

    def crosshair(xy, colour, label):
        x, y = xy
        r = 30
        draw.line([(x - r, y), (x + r, y)], fill=colour, width=4)
        draw.line([(x, y - r), (x, y + r)], fill=colour, width=4)
        draw.ellipse([x - 8, y - 8, x + 8, y + 8], outline=colour, width=3)
        draw.text((x + r + 8, y - 18), f"{label} ({x},{y})",
                  fill=colour, font=font)

    crosshair(ARROW_LEFT_XY,   "#33ff33", "LEFT")
    crosshair(RUDDER_TOGGLE_XY,"#ffff00", "RUDDER")
    crosshair(ARROW_RIGHT_XY,  "#ff5050", "RIGHT")

    out = Path("/tmp/rudder_coords.png")
    img.save(out)
    print(f"Saved annotated screen → {out}")
    print(f"  green cross  = ARROW_LEFT_XY   = {ARROW_LEFT_XY}")
    print(f"  yellow cross = RUDDER_TOGGLE_XY = {RUDDER_TOGGLE_XY}")
    print(f"  red cross    = ARROW_RIGHT_XY  = {ARROW_RIGHT_XY}")
    print()
    print("Open the PNG and check that each cross lands on the correct")
    print("icon.  If they're off, edit the constants in actions/sea_actions.py")
    print("to match what you see.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
