"""Run the sail-button classifier across all saved full-frame crops
in a calibration run and print the per-tick result.

Cross-check against the observed speed readings: speed=0 ↔ rudder,
speed>0 ↔ anchor.

Usage:
    python tools/verify_button_classifier.py data/calibration/<RUN>/crops
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from PIL import Image

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from vision.sea_button_state import classify_sail_button
from vision.sea_hud import read_speed


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    crops_dir = Path(sys.argv[1])
    fulls = sorted(crops_dir.glob("*_full.png"))
    print(f"{'tick':<32} {'spd':>5}  {'btn':>6}  {'conf':>5}")
    for path in fulls:
        img = Image.open(path).convert("RGB")
        spd = read_speed(img)
        spd_s = f"{spd:.1f}" if spd is not None else "None"
        t0 = time.time()
        state, conf = classify_sail_button(img)
        dt = (time.time() - t0) * 1000
        state_s = state or "None"
        # Sanity: if speed=0, btn should be rudder; if speed>1, btn should be anchor
        expected = None
        if spd is not None:
            expected = "anchor" if spd > 1.0 else "rudder"
        ok = "" if expected is None or expected == state else f"  ← MISMATCH (expected {expected})"
        print(f"{path.stem[:32]:<32} {spd_s:>5}  {state_s:>6}  {conf:>.2f}  ({dt:.0f}ms){ok}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
