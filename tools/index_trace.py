"""Perceive every frame of a trace, so its stages can be found by state rather than by eye.

    python -m tools.index_trace data/sessions/trace_barter_cmd_2026-08-26T00-06-32

Writes `sim_index.json` beside the frames: one row per frame with the perceived state and the
tap that was recorded on it. The perceive results land in the shared cache, so this is paid
once per frame ever, not once per run of this script.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brain.replay import state_of      # noqa: E402


def main(trace_dir: str) -> None:
    d = Path(trace_dir)
    frames = sorted(p for p in d.glob("frame_*.png") if "_marked" not in p.name)

    taps = {}
    actions = d / "actions.jsonl"
    if actions.exists():
        for line in actions.read_text().splitlines():
            if not line.strip():
                continue
            a = json.loads(line)
            taps.setdefault(a.get("frame"), []).append(
                {"kind": a.get("kind"), "x": a.get("x"), "y": a.get("y"),
                 "label": a.get("label"), "t": a.get("t")})

    rows = []
    for i, f in enumerate(frames, 1):
        try:
            s = state_of(f)
            row = {"frame": f.name, "state": s.state, "port": s.port,
                   "detail": s.detail, "taps": taps.get(f.name, [])}
        except Exception as exc:
            row = {"frame": f.name, "state": None, "error": str(exc)[:120],
                   "taps": taps.get(f.name, [])}
        rows.append(row)
        print(f"{i:4}/{len(frames)}  {f.name:18} {str(row.get('state')):18} "
              f"{(row['taps'][0]['label'] if row['taps'] and row['taps'][0].get('label') else '')}",
              flush=True)

    out = d / "sim_index.json"
    out.write_text(json.dumps(rows, indent=1))
    print(f"\nwrote {out} ({len(rows)} frames)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else
         "data/sessions/trace_barter_cmd_2026-08-26T00-06-32")
