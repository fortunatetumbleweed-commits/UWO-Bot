"""Run the L4 tactical prompt across multiple VLMs on the same set
of saved minimap frames, side-by-side.  Used to A/B local VLMs
(llava, moondream, qwen2.5vl, ...) without spinning up a live run.

For each (model, tick) pair:
  - load the saved minimap crop
  - ask the model the same L4 prompt that MoondreamTactical uses
  - parse the answer with brain.ai_nav.layers.tactical.parse_direction
  - record raw answer, parsed direction, and wall time

Reports per-model histograms + the per-tick answer matrix so you
can see where models agree / disagree.

Usage
─────
  # All installed VLMs on every 10th tick of a saved session:
  python -m tools.compare_l4_vlms \\
      data/sessions/ai_nav_live_palma  --stride 10

  # Specific models on specific ticks:
  python -m tools.compare_l4_vlms \\
      data/sessions/ai_nav_live_palma  \\
      --models moondream,llava:7b  --ticks 1,35,85,89,149,187
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from brain.ai_nav.layers.tactical import (  # noqa: E402
    _MOONDREAM_PROMPT_TEMPLATE, _bearing_to_compass_dir, parse_direction,
)


def _installed_vlms() -> list[str]:
    """List models that look like they support vision (heuristic:
    name contains 'vl', 'vision', 'llava', or 'moondream').
    Fallback to all if none match."""
    from vision.local_vision import list_installed_models
    installed = list_installed_models()
    vision_like = [
        m for m in installed
        if any(tag in m.lower() for tag in
               ("vl", "vision", "llava", "moondream"))
    ]
    return vision_like or installed


def _query_one(model_name: str, frame_png: Path, prompt: str
               ) -> tuple[str, str | None, float]:
    """Returns (raw_answer, parsed_direction, wall_seconds)."""
    from PIL import Image
    from vision.local_vision import LocalVision

    vision = LocalVision(model=model_name)
    if not vision.check_available():
        return ("", None, 0.0)

    img = Image.open(frame_png).convert("RGB")
    img.thumbnail((800, 400))
    t0 = time.perf_counter()
    raw = vision.ask(prompt, frame=img)
    dt = time.perf_counter() - t0
    return (raw.strip(), parse_direction(raw), dt)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session_dir", type=Path,
                   help="Session dir containing tick_NNNN.png + trace.jsonl")
    ap.add_argument("--models", default=None,
                   help="Comma-separated Ollama model names.  Default: "
                        "auto-detect vision-capable installed models.")
    ap.add_argument("--ticks", default=None,
                   help="Comma-separated tick numbers.  Default: "
                        "use --stride.")
    ap.add_argument("--stride", type=int, default=10,
                   help="When --ticks not given, sample every Nth tick.")
    ap.add_argument("--side", choices=("port", "starboard"), default="port")
    ap.add_argument("--out", type=Path, default=None,
                   help="Output JSON path.  Default: "
                        "<session_dir>/l4_vlm_compare.json")
    args = ap.parse_args()

    if args.models:
        models = [m.strip() for m in args.models.split(",")]
    else:
        models = _installed_vlms()
        print(f"[auto] using installed vision models: {models}")

    # Pick ticks
    recs = [json.loads(l) for l in
            (args.session_dir / "trace.jsonl").read_text().splitlines()
            if l.strip()]
    by_tick = {r["tick"]: r for r in recs}
    if args.ticks:
        ticks = [int(t) for t in args.ticks.split(",")]
    else:
        all_ticks = sorted(by_tick.keys())
        ticks = all_ticks[::args.stride]
    ticks = [t for t in ticks
             if (args.session_dir / f"tick_{t:04d}.png").exists()]
    if not ticks:
        sys.exit("No matching tick PNGs found.")
    print(f"querying {len(models)} model(s) × {len(ticks)} tick(s) = "
          f"{len(models) * len(ticks)} VLM calls")

    # Run
    results: dict[str, dict[int, dict]] = {m: {} for m in models}
    for model in models:
        print(f"\n=== {model} ===")
        for t in ticks:
            r = by_tick.get(t, {})
            heading_deg = r.get("heading_deg") or 0.0
            prompt = _MOONDREAM_PROMPT_TEMPLATE.format(
                heading_dir=_bearing_to_compass_dir(heading_deg),
                heading_deg=heading_deg,
                side=args.side,
            )
            frame = args.session_dir / f"tick_{t:04d}.png"
            raw, parsed, dt = _query_one(model, frame, prompt)
            results[model][t] = {
                "raw": raw, "parsed": parsed, "wall_s": round(dt, 2),
                "heading_deg": round(heading_deg, 1),
            }
            print(f"  t{t:3d}  hdg={heading_deg:5.0f}°  "
                  f"parsed={str(parsed):8s} ({dt:4.1f}s)  raw={raw!r}")

    # Summary
    print("\n" + "=" * 70)
    print("Summary — direction histogram per model:")
    print(f"{'model':25s} {'F':>4s} {'L':>4s} {'R':>4s} {'B':>4s} {'?':>4s} "
          f"{'mean_s':>7s}")
    for m in models:
        cnt = Counter(v["parsed"] for v in results[m].values())
        mean_s = (sum(v["wall_s"] for v in results[m].values())
                 / max(1, len(results[m])))
        print(f"  {m:23s} {cnt['forward']:>4d} {cnt['left']:>4d} "
              f"{cnt['right']:>4d} {cnt['back']:>4d} {cnt[None]:>4d} "
              f"{mean_s:>7.2f}")

    # Per-tick agreement matrix
    print("\nPer-tick answer matrix (rows=tick, cols=model):")
    header = "  tick | " + " | ".join(f"{m[:12]:12s}" for m in models)
    print(header)
    print("  " + "-" * (len(header) - 2))
    for t in ticks:
        row = f"  t{t:3d}  | " + " | ".join(
            f"{str(results[m][t]['parsed'] or 'NONE'):12s}"
            for m in models)
        print(row)

    out_path = args.out or (args.session_dir / "l4_vlm_compare.json")
    out_path.write_text(json.dumps({
        "models": models, "ticks": ticks, "side": args.side,
        "results": results,
    }, indent=2))
    print(f"\n→ {out_path}")


if __name__ == "__main__":
    main()
