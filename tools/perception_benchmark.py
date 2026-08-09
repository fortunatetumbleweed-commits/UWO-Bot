"""Perception benchmark — score the current classifier over labeled frames.

Runs the real `brain.perceive._classify_nav_state` on frames from
`data/labels.jsonl` (last-wins per frame) and compares the predicted base
state to the human label. Reports per-class accuracy + a labeled-vs-predicted
confusion matrix + writes the misclassified list.

Doubles as a **label-quality audit**: a disagreement is either a model error or
a bad label, and both are worth reviewing. This is Step 1 of the perception
work — the baseline to beat and the regression guard for the arbitration
redesign (see docs/perception_backlog.md).

Run from a repo that has models + data (e.g. uwo_v2):
    python -m tools.perception_benchmark --classes village,port_overworld
    python -m tools.perception_benchmark --classes base            # all clean base states
    python -m tools.perception_benchmark --classes village,port_overworld --limit 40
"""
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

from PIL import Image

# label screen_type -> the classifier's base 'location' vocabulary.
# Dialog/overlay/transient label types are orthogonal (overlays) and excluded
# from the base-state benchmark for now.
BASE_MAP = {
    "village":           "village",
    "port_overworld":    "port_overworld",
    "sea":               "sea",
    "world_map":         "world_map",
    "sub_menu":          "sub_menu",
    "building_interior": "building",
    "main_menu":         "main_menu",
    "port_map":          "port_map",
    "loading":           "loading",
    "port_loading":      "loading",
}


def load_last_wins(labels_path: Path) -> dict:
    """Return {(session_id, file/frame_path): screen_type} with last row winning."""
    out = {}
    with open(labels_path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            k = (r.get("session_id"), r.get("file") or r.get("frame_path"))
            if k == (None, None):
                continue
            out[k] = r.get("screen_type")
    return out


def resolve_frame(sessions_dir: Path, sid, fil) -> Path | None:
    if fil and "/" in str(fil):          # frame_path style (absolute-ish)
        p = Path(fil)
        return p if p.exists() else None
    for c in (sessions_dir / str(sid) / "frames" / str(fil),
              sessions_dir / str(sid) / str(fil)):
        if c.exists():
            return c
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="data/labels.jsonl")
    ap.add_argument("--sessions", default="data/sessions")
    ap.add_argument("--classes", default="village,port_overworld",
                    help="comma list of label screen_types, or 'base' for all clean base states")
    ap.add_argument("--limit", type=int, default=0, help="cap frames per class (0 = all)")
    ap.add_argument("--out", default="/tmp/perception_benchmark_misclassified.jsonl")
    ap.add_argument("--allow-cache-writes", action="store_true",
                    help="allow the classifier's scene-cache writes — NON-deterministic / "
                         "self-poisoning across runs. Off by default so the benchmark is "
                         "reproducible (proven: village recall drifted 3->2->1 with writes on).")
    args = ap.parse_args()

    if not args.allow_cache_writes:
        # Perception must be a PURE READ for a stable benchmark. The classifier
        # otherwise writes memory/knowledge/scenes/*.json during classify, which
        # self-poisons subsequent runs. No-op the write (default).
        import vision.claude_vision as _cv
        _cv.save_inventory = lambda *a, **k: None
        print("[benchmark] deterministic: scene-cache writes disabled "
              "(use --allow-cache-writes to see the non-deterministic behavior)")

    if args.classes == "base":
        want = set(BASE_MAP)
    else:
        want = {c.strip() for c in args.classes.split(",")}

    labels = load_last_wins(Path(args.labels))
    sessions_dir = Path(args.sessions)

    # collect frames per class (resolvable only)
    per_class = collections.defaultdict(list)
    for (sid, fil), st in labels.items():
        if st not in want:
            continue
        p = resolve_frame(sessions_dir, sid, fil)
        if p is not None:
            per_class[st].append(p)
    if args.limit:
        for st in per_class:
            per_class[st] = per_class[st][:args.limit]

    total = sum(len(v) for v in per_class.values())
    print(f"scoring {total} frames across {len(per_class)} class(es): "
          f"{ {k: len(v) for k, v in per_class.items()} }")
    if total == 0:
        print("no resolvable frames — check --labels / --sessions paths")
        return

    from brain.perceive import _classify_nav_state

    confusion = collections.Counter()   # (labeled_base, predicted) -> n
    correct = collections.Counter()
    seen = collections.Counter()
    misclassified = []
    for st, paths in per_class.items():
        expected = BASE_MAP[st]
        for p in paths:
            try:
                img = Image.open(p).convert("RGB")
                pred = (_classify_nav_state(img) or {}).get("location", "unknown")
            except Exception as e:
                pred = f"<error:{type(e).__name__}>"
            seen[expected] += 1
            confusion[(expected, pred)] += 1
            if pred == expected:
                correct[expected] += 1
            else:
                misclassified.append({"label": st, "expected": expected,
                                      "predicted": pred, "frame": str(p)})

    print("\n=== per-class accuracy (base state) ===")
    for base in sorted(seen):
        n, c = seen[base], correct[base]
        print(f"  {base:16} {c:4}/{n:<4} = {100*c/n:5.1f}%")
    ov_n = sum(seen.values()); ov_c = sum(correct.values())
    print(f"  {'OVERALL':16} {ov_c:4}/{ov_n:<4} = {100*ov_c/ov_n:5.1f}%")

    print("\n=== confusion (labeled_base -> predicted) ===")
    for (lab, pred), n in sorted(confusion.items(), key=lambda x: (-x[1])):
        flag = "" if lab == pred else "   <-- miss"
        print(f"  {lab:16} -> {pred:16} {n:4}{flag}")

    Path(args.out).write_text("\n".join(json.dumps(m) for m in misclassified) + "\n")
    print(f"\nwrote {len(misclassified)} misclassified rows to {args.out}")


if __name__ == "__main__":
    main()
