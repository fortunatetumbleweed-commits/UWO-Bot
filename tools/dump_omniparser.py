#!/usr/bin/env python
"""Dump raw OmniParser detections for a frame.

Usage:
    python tools/dump_omniparser.py <frame.png> [<frame.png> ...]

For each frame, prints every DetectedElement (label, type, bbox, cx/cy,
confidence) sorted top-to-bottom, left-to-right.  Annotates which
elements fall inside the top-left title region used by the
state_fingerprints building_title / sub_menu_title signals.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from PIL import Image
from vision.omniparser import get_omniparser, parse_fast_cached


# Title region (top 10% × left 40%) — matches TOP_LEFT_TITLE in
# vision/state_fingerprints_data.py.
TITLE_REGION = (0.0, 0.0, 0.40, 0.10)


def _in_title_region(cx: int, cy: int, w: int, h: int) -> bool:
    l, t, r, b = TITLE_REGION
    return l * w <= cx <= r * w and t * h <= cy <= b * h


def dump(frame_path: Path) -> None:
    print(f"\n{'=' * 78}")
    print(f"Frame: {frame_path}")
    print(f"{'=' * 78}")
    img = Image.open(frame_path).convert("RGB")
    print(f"Dimensions: {img.width}×{img.height}")

    try:
        if not get_omniparser().yolo_available():
            print("OmniParser YOLO unavailable")
            return
        elements = parse_fast_cached(img)
    except Exception as e:
        print(f"OmniParser parse failed: {type(e).__name__}: {e}")
        return

    print(f"Detected {len(elements)} elements\n")

    elements_sorted = sorted(elements, key=lambda e: (e.cy, e.cx))

    print(f"  {'#':<3} {'type':<7} {'cx':>5} {'cy':>5} {'w':>4} {'h':>4} "
          f"{'conf':>4} {'title?':<7} label")
    print("  " + "-" * 75)
    for i, el in enumerate(elements_sorted):
        in_title = _in_title_region(el.cx, el.cy, img.width, img.height)
        marker = "TITLE" if in_title else ""
        label_short = (el.label or "")[:50]
        print(
            f"  {i:<3} {el.element_type:<7} {el.cx:>5} {el.cy:>5} "
            f"{el.width:>4} {el.height:>4} "
            f"{el.confidence:>.2f} {marker:<7} {label_short!r}"
        )

    # Highlight title-region elements
    title_els = [e for e in elements_sorted
                 if _in_title_region(e.cx, e.cy, img.width, img.height)]
    if title_els:
        print("\n  Title-region elements (used by building_title / "
              "sub_menu_title signals):")
        for el in title_els:
            print(f"    - {el.label!r}  type={el.element_type}  "
                  f"@({el.cx},{el.cy})  bbox=({el.x1},{el.y1})-({el.x2},{el.y2})")
        # Show what the LabelSetSignal would see:
        # - text concatenation in detection order (the OLD broken way)
        # - text concatenation spatially sorted (the NEW fix)
        old_text = " ".join((e.label or "").lower().strip() for e in title_els)
        new_text = " ".join(
            (e.label or "").lower().strip()
            for e in sorted(title_els, key=lambda e: (e.cy, e.cx))
        )
        print(f"\n  Title text (spatial-sorted): {new_text!r}")
        if old_text != new_text:
            print(f"  Title text (detection-order, OLD): {old_text!r}")


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__, file=sys.stderr)
        return 1
    for arg in argv[1:]:
        p = Path(arg)
        if not p.exists():
            print(f"error: {p} not found", file=sys.stderr)
            continue
        dump(p)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
