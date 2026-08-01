"""Extract reference icon crops for sail_button_state classifier.

The toggle button at (201, 741) shows two states:
  - RUDDER: ship is stopped (tap to start sailing)
  - ANCHOR: ship is actively sailing (tap to drop anchor + stop)

Both icons look broadly similar (nautical brass-on-wood), so we
crop button regions across MANY samples and pick the two most
distinct examples manually based on observed ship speed.

Outputs all button crops to /tmp/button_grid/ so the user can
pick the two best references by eye, then save the chosen pair to
vision/assets/sail_button_{rudder,anchor}.png.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

BUTTON_CROP = (201 - 35, 741 - 35, 201 + 35, 741 + 35)   # 70×70


def main() -> int:
    crops_dir = _PROJECT_ROOT / "data/calibration/arc_20260530_163426/crops"
    log_path = _PROJECT_ROOT / "data/calibration/arc_20260530_163426.jsonl"
    samples = [json.loads(l) for l in open(log_path)]

    out_dir = Path("/tmp/button_grid")
    out_dir.mkdir(exist_ok=True)
    print(f"writing {len(samples)} button crops to {out_dir}")

    fulls = sorted(crops_dir.glob("*_full.png"))
    for i, path in enumerate(fulls):
        img = Image.open(path).convert("RGB")
        button = img.crop(BUTTON_CROP)
        spd = samples[i].get("speed_kt") if i < len(samples) else None
        spd_s = f"{spd:.1f}" if spd is not None else "None"
        # Embed spd into filename for easy sorting
        idx_str = path.stem.split("_")[0]
        phase = "_".join(path.stem.split("_")[1:-1])
        button.save(out_dir / f"{idx_str}_v={spd_s}_{phase}.png")
        print(f"  {idx_str}  speed={spd_s:6s}  phase={phase}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
