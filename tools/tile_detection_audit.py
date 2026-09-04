"""How often does OmniParser give a row its thumbnail tile?

The reader models a list row as a THUMBNAIL BOX in a fixed x-band with a fixed height range.
That is an identification by dimension, and Guarana showed it failing: the row was fully
visible and perfectly legible, but its artwork came back as one wide flat box merged with the
category chip, so the row did not exist as far as the reader was concerned.

This audits that model against a structural one. A row really is a NAME with a CATEGORY CHIP
beneath it — that pairing is what makes it a row, and the thumbnail is decoration to its left.
Counting rows both ways over saved frames says whether the tile model is reliable, and whether
it fails differently between villages.

    python -m tools.tile_detection_audit <frames_dir> [<frames_dir> ...]
"""

from __future__ import annotations

import glob
import re
import sys

from PIL import Image

from actions.village_check import _CATEGORY_WORDS
import tools.learn_village_barter as L

NAME_X_MIN = 1800          # names and chips sit right of the thumbnail column


def _rows_by_association(els, vp):
    """[(name_element, chip_element)] — a row is a name with a category chip under it."""
    if not vp:
        return []
    inside = [e for e in els if e.y1 >= vp[0] - 40 and e.y2 <= vp[1] + 40]
    chips, names = [], []
    for e in inside:
        lab = (getattr(e, "label", "") or "").strip()
        if not lab or e.x1 < NAME_X_MIN:
            continue
        if lab.lower() in _CATEGORY_WORDS:
            chips.append(e)
        elif not re.fullmatch(r"[\d,.]+", lab) and len(lab) > 1:
            names.append(e)
    rows = []
    for chip in chips:
        above = [n for n in names if n.y2 <= chip.y1 + 12 and chip.y1 - n.y2 < 60]
        if above:
            rows.append((max(above, key=lambda n: n.y2), chip))
    return rows


def audit(frames_dir):
    from vision.omniparser import parse_fast_cached

    files = sorted(glob.glob(f"{frames_dir}/tick_*.png"))
    tot_rows = tot_tiled = 0
    orphans = {}
    for f in files:
        img = Image.open(f).convert("RGB")
        els = list(parse_fast_cached(img))
        vp = L._list_viewport(els)
        tiles = L._row_tiles(els)
        for name, chip in _rows_by_association(els, vp):
            tot_rows += 1
            top, bot = name.y1, chip.y2
            hit = any(t.y1 < bot and t.y2 > top for t in tiles)
            tot_tiled += hit
            if not hit:
                orphans[(getattr(name, "label", "") or "").strip()] = \
                    orphans.get((getattr(name, "label", "") or "").strip(), 0) + 1
    return files, tot_rows, tot_tiled, orphans


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    for d in sys.argv[1:]:
        files, rows, tiled, orphans = audit(d)
        name = d.rstrip("/").split("/")[-2]
        pct = 100.0 * tiled / rows if rows else 0.0
        print(f"\n=== {name} === ({len(files)} frames)")
        print(f"rows seen by name+chip : {rows}")
        print(f"rows that got a tile   : {tiled}  ({pct:.0f}%)")
        print(f"rows with NO tile      : {rows - tiled}")
        if orphans:
            print("  never tiled: " + ", ".join(
                f"{k} x{v}" for k, v in sorted(orphans.items(), key=lambda kv: -kv[1])))
