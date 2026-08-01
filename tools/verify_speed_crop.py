"""Verify SPEED_CROP against the saved full-frame PNGs from a
calibration run.  Prints read_speed for each tick so we can confirm
the new coordinates pick up the correct value across motion + flips.

Usage:
    python tools/verify_speed_crop.py data/calibration/<RUN>/crops
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from vision.sea_hud import read_speed, read_latlon, SPEED_CROP, LATLON_CROP


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    crops_dir = Path(sys.argv[1])
    if not crops_dir.is_dir():
        raise SystemExit(f"not a dir: {crops_dir}")

    # Re-save the speed and latlon crops with the new coords so the
    # user can eyeball them, then run the readers.
    out_dir = crops_dir / "verify"
    out_dir.mkdir(exist_ok=True)

    fulls = sorted(crops_dir.glob("*_full.png"))
    print(f"verifying against {len(fulls)} full frames")
    print(f"{'tick':<28} {'spd':>6}  {'lat':>7}  {'lon':>7}")
    for path in fulls:
        img = Image.open(path).convert("RGB")
        img.crop(SPEED_CROP).save(out_dir / f"{path.stem}_speed_v2.png")
        img.crop(LATLON_CROP).save(out_dir / f"{path.stem}_latlon_v2.png")
        spd = read_speed(img)
        ll = read_latlon(img)
        spd_s = f"{spd:.1f}" if spd is not None else "None"
        lat_s = f"{ll[0]:.2f}" if ll else "None"
        lon_s = f"{ll[1]:.2f}" if ll else "None"
        print(f"{path.stem[:28]:<28} {spd_s:>6}  {lat_s:>7}  {lon_s:>7}")
    print(f"\nre-cropped to {out_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
