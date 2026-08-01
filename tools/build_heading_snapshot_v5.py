"""Build v5 training snapshot from v4 + human audit verdicts.

- Starts from data/heading_training_snapshot_v4_2026-06-30
- Applies label corrections from the two audit files
  (later verdict wins on collision).
- Drops the 58 duplicate Cairo-static sprites (606..663), keeping
  only 000604 and 000605 as prototypes.
- Optionally emits a holdout eval jsonl of the audited sprite IDs.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
V4 = REPO / "data" / "heading_training_snapshot_v4_2026-06-30"
V5 = REPO / "data" / "heading_training_snapshot_v5_2026-07-01"

CAIRO_KEEP = {"000604", "000605"}
CAIRO_DROP_RANGE = range(606, 664)  # 000606..000663

AUDIT_FILES = [
    REPO / "data/heading_labels_human/label_audit_v4.jsonl",
    REPO / "data/heading_labels_human/v4_suspects_audit.jsonl",
]


def load_verdicts() -> dict[str, dict]:
    """Later-file / later-line verdict wins on sprite_id collision."""
    out: dict[str, dict] = {}
    for fp in AUDIT_FILES:
        if not fp.exists(): continue
        for line in fp.open():
            r = json.loads(line)
            out[r["sprite_id"]] = r
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan without writing files")
    ap.add_argument("--holdout", type=str, default=None,
                    help="write the audited sprite IDs to this jsonl "
                         "as a clean human-verified eval set")
    args = ap.parse_args()

    v4_manifest = [json.loads(l) for l in
                   (V4 / "sprites" / "manifest.jsonl").open()]
    verdicts = load_verdicts()
    print(f"v4 sprites : {len(v4_manifest)}")
    print(f"audit verdicts: {len(verdicts)}")

    drop_cairo = {f"{i:06d}" for i in CAIRO_DROP_RANGE}

    kept, corrected, dropped = [], 0, 0
    for rec in v4_manifest:
        sid = rec["sprite_id"]
        if sid in drop_cairo:
            dropped += 1
            continue
        v = verdicts.get(sid)
        if v is not None:
            new_h = v.get("corrected_heading_deg")
            if new_h is not None:
                old = rec["heading_deg"]
                diff = abs(((old - new_h + 540) % 360) - 180)
                if diff >= 1.0:
                    rec = dict(rec)
                    rec["heading_deg"] = round(float(new_h), 2)
                    rec["_v5_source"] = f"human:{v['verdict']}"
                    corrected += 1
        kept.append(rec)

    print(f"\nsummary:")
    print(f"  kept       : {len(kept)}")
    print(f"  corrected  : {corrected}")
    print(f"  dropped    : {dropped}  (cairo-static duplicates)")
    print(f"  v5 total   : {len(kept)}")

    if args.dry_run:
        print("\n[dry-run — no files written]")
        # Show first few corrections
        print("\nfirst 5 corrections:")
        for r in kept:
            if r.get("_v5_source"):
                print(f"  {r['sprite_id']}  {r['heading_deg']:.1f}°  "
                      f"({r['_v5_source']})")
                if r == kept[-1]: break
        return

    # Create v5 dirs
    print(f"\nwriting → {V5}")
    (V5 / "sprites").mkdir(parents=True, exist_ok=True)

    # Copy sprite images (fast — hardlink where possible)
    src_dir = V4 / "sprites"
    dst_dir = V5 / "sprites"
    for rec in kept:
        sid = rec["sprite_id"]
        for name in (f"sprite_{sid}.png", f"sprite_{sid}_mask.png"):
            src = src_dir / name
            dst = dst_dir / name
            if not src.exists():
                print(f"  WARN: missing source {src}")
                continue
            if dst.exists(): dst.unlink()
            try:
                dst.hardlink_to(src)   # instant, saves disk
            except OSError:
                shutil.copy2(src, dst)

    # Write new manifest
    with (V5 / "sprites" / "manifest.jsonl").open("w") as f:
        for rec in kept:
            f.write(json.dumps(rec) + "\n")
    print(f"  wrote {len(kept)} sprites + manifest.jsonl")

    # Copy shared dirs untouched
    for sub in ("backgrounds", "labels"):
        src = V4 / sub
        dst = V5 / sub
        if src.exists() and not dst.exists():
            dst.symlink_to(src.resolve())
            print(f"  symlinked {sub}/ → v4/{sub}")

    if args.holdout:
        n = 0
        with Path(args.holdout).open("w") as f:
            for sid, v in verdicts.items():
                if v["verdict"] in ("accept", "flip", "motion", "pca", "manual"):
                    corr = v.get("corrected_heading_deg")
                    if corr is None: continue
                    f.write(json.dumps({
                        "sprite_id": sid,
                        "heading_deg": corr,
                        "verdict": v["verdict"],
                    }) + "\n")
                    n += 1
        print(f"  holdout eval set → {args.holdout}  ({n} sprites)")


if __name__ == "__main__":
    main()
