"""Join + diff a captured voyage's trace against the replay output.

Inputs (all in the session dir):
  trace.jsonl       — capture-time bot decisions + perception state.
  bot_replay.jsonl  — re-run planner decisions (from tools/replay_voyage.py).
  labels.jsonl      — user's verdict labels (ideal/acceptable/wrong).
  keystrokes.jsonl  — user's raw global keystrokes during capture.

Outputs:
  stdout            — human-readable summary report.
  <session>/diff_report.md  — same summary saved to disk for review.
  <session>/diff_report.json — machine-readable metric dump.

Exit code:
  0 if action-agreement >= --threshold (default 0.85)
  1 otherwise — usable as a future CI gate on the canonical reference
                voyage.

Metrics:
  - Overall action-agreement rate (trace.action_kind vs replay.action_kind).
  - Per-kind agreement bucketed by trace action.
  - Direction-flip count (trace=L↔replay=R) — the most interesting class.
  - Label cross-reference: for each user-labeled tick, whether the
    captured + replayed planner reproduced the user's expected
    behaviour.  IDEAL ticks where replay diverges = regression risk;
    WRONG ticks where replay matches trace = unfixed regression.
  - Hold-duration delta histogram on agreed-kind ticks (planner
    fidelity beyond just direction).

See `docs/exploration_navigation_layers.md` for the architecture
that makes this whole pipeline deterministic.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ── Helpers ─────────────────────────────────────────────────────────────────

def _action_kind(action: Optional[str]) -> str:
    """Strip the duration suffix.  'hold_left:875ms(...)' → 'hold_left'."""
    if not action:
        return ""
    return action.split(":")[0].split("(")[0].strip()


def _action_duration_ms(action: Optional[str]) -> Optional[int]:
    """Extract `N` from 'hold_left:Nms(...)'.  Returns None if no
    duration in the action string."""
    if not action or ":" not in action:
        return None
    try:
        tail = action.split(":", 1)[1]
        # tail like '875ms(~45°@60°/s)' or '875ms'
        ms_str = tail.split("ms")[0]
        return int(ms_str)
    except (ValueError, IndexError):
        return None


def _load_jsonl(path: Path) -> List[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def _physical_holds_from_keystrokes(keystrokes: List[dict]) -> List[dict]:
    """Collapse OS auto-repeat presses into one entry per physical
    press → release cycle.  Mirrors the tick_viewer reconstruction."""
    holds: List[dict] = []
    pending: Dict[str, Tuple[datetime, int]] = {}
    for ev in keystrokes:
        try:
            ts = datetime.fromisoformat(ev["wall_iso"])
        except Exception:
            continue
        k = ev.get("key")
        if ev.get("event") == "press":
            if k in pending:
                first, n = pending[k]
                pending[k] = (first, n + 1)
            else:
                pending[k] = (ts, 1)
        elif ev.get("event") == "release":
            if k in pending:
                first, n = pending.pop(k)
                holds.append({
                    "key": k,
                    "press_iso": first.isoformat(),
                    "release_iso": ts.isoformat(),
                    "duration_ms": int(
                        (ts - first).total_seconds() * 1000),
                })
    return holds


# ── Metric computation ─────────────────────────────────────────────────────

def compute_metrics(
    trace: List[dict],
    replay: List[dict],
    labels: List[dict],
    holds: List[dict],
) -> dict:
    """Compute the structured metric dict.  Pure function."""
    trace_by_tick = {t["tick"]: t for t in trace if "tick" in t}
    replay_by_tick = {r["tick"]: r for r in replay if "tick" in r}
    labels_by_tick = {l["tick"]: l for l in labels if "tick" in l}

    common_ticks = sorted(set(trace_by_tick) & set(replay_by_tick))
    n_ticks = len(common_ticks)

    # --- Action agreement ---
    n_match = 0
    per_kind_total: Counter = Counter()
    per_kind_match: Counter = Counter()
    direction_flips: List[dict] = []
    duration_deltas_ms: List[int] = []

    for tick in common_ticks:
        t = trace_by_tick[tick]
        r = replay_by_tick[tick]
        t_kind = _action_kind(t.get("action"))
        r_kind = _action_kind(r.get("action"))
        per_kind_total[t_kind] += 1
        if t_kind == r_kind:
            n_match += 1
            per_kind_match[t_kind] += 1
            # Compute duration delta when both kinds match + both have ms.
            t_ms = _action_duration_ms(t.get("action"))
            r_ms = _action_duration_ms(r.get("action"))
            if t_ms is not None and r_ms is not None:
                duration_deltas_ms.append(r_ms - t_ms)
        else:
            # Detect direction flips specifically.
            if {t_kind, r_kind} == {"hold_left", "hold_right"}:
                direction_flips.append({
                    "tick": tick,
                    "trace": t.get("action"),
                    "replay": r.get("action"),
                })

    # --- Label cross-reference ---
    label_buckets = defaultdict(lambda: {
        "n": 0, "replay_matches_trace": 0, "replay_differs": [],
    })
    for tick, lbl in labels_by_tick.items():
        t = trace_by_tick.get(tick)
        r = replay_by_tick.get(tick)
        if t is None or r is None:
            continue
        verdict = lbl.get("verdict")
        if verdict not in ("ideal", "acceptable", "wrong"):
            continue
        bucket = label_buckets[verdict]
        bucket["n"] += 1
        if _action_kind(t.get("action")) == _action_kind(r.get("action")):
            bucket["replay_matches_trace"] += 1
        else:
            bucket["replay_differs"].append({
                "tick": tick,
                "trace": t.get("action"),
                "replay": r.get("action"),
            })

    # --- Duration-delta histogram (ms) ---
    duration_histogram = {}
    if duration_deltas_ms:
        bins = [-2000, -1000, -500, -200, -50, 50, 200, 500, 1000, 2000]
        labels_str = ["≤-1000", "-1000..-500", "-500..-200",
                       "-200..-50", "-50..50", "50..200", "200..500",
                       "500..1000", "≥1000"]
        counts = [0] * len(labels_str)
        for d in duration_deltas_ms:
            for i, edge in enumerate(bins[1:-1]):
                if d <= edge:
                    counts[i] += 1
                    break
            else:
                counts[-1] += 1
        duration_histogram = dict(zip(labels_str, counts))

    return {
        "n_ticks":               n_ticks,
        "n_match":               n_match,
        "match_pct":             100.0 * n_match / max(1, n_ticks),
        "per_kind":              {
            kind: {
                "total":  per_kind_total[kind],
                "match":  per_kind_match[kind],
                "pct":    100.0 * per_kind_match[kind] /
                          max(1, per_kind_total[kind]),
            }
            for kind in sorted(per_kind_total)
        },
        "direction_flips":       direction_flips,
        "n_direction_flips":     len(direction_flips),
        "labels":                {k: dict(v) for k, v in label_buckets.items()},
        "duration_histogram":    duration_histogram,
        "duration_deltas_count": len(duration_deltas_ms),
        "n_holds_recorded":      len(holds),
    }


# ── Report formatting ──────────────────────────────────────────────────────

def render_report(session_name: str, m: dict) -> str:
    """Return a Markdown-style summary report."""
    lines = []
    bar = "═" * 75
    lines.append(bar)
    lines.append(f"Replay diff report — {session_name}")
    lines.append(bar)
    lines.append("")

    lines.append("OVERALL")
    lines.append(f"  Ticks compared : {m['n_ticks']}")
    lines.append(f"  Action match   : {m['n_match']} / {m['n_ticks']}  "
                 f"({m['match_pct']:.1f}%)")
    lines.append(f"  Direction flips: {m['n_direction_flips']}")
    lines.append("")

    lines.append("PER-KIND AGREEMENT (trace action → match rate)")
    for kind, stats in m["per_kind"].items():
        lines.append(f"  {kind:<30} {stats['match']:>5}/{stats['total']:<5} "
                     f"({stats['pct']:.1f}%)")
    lines.append("")

    if m["direction_flips"]:
        lines.append(f"DIRECTION FLIPS (first 15 of {m['n_direction_flips']})")
        for flip in m["direction_flips"][:15]:
            lines.append(f"  t{flip['tick']:>4}: trace={flip['trace']!s:<28}  "
                         f"replay={flip['replay']}")
        lines.append("")

    if m["labels"]:
        lines.append("LABEL CROSS-REFERENCE")
        for verdict, bucket in m["labels"].items():
            n_match = bucket["replay_matches_trace"]
            n = bucket["n"]
            n_diff = len(bucket["replay_differs"])
            lines.append(f"  verdict={verdict.upper()}  (n={n})")
            lines.append(f"    replay matches trace: {n_match}")
            lines.append(f"    replay differs      : {n_diff}")
            if verdict == "wrong" and n_match > 0:
                lines.append(f"      ⚠ unfixed regression — bot reproduces "
                             f"the wrong action at {n_match} tick(s)")
            if verdict == "ideal" and n_diff > 0:
                lines.append(f"      ⚠ regression risk — bot diverged from "
                             f"the IDEAL trace at {n_diff} tick(s)")
            for d in bucket["replay_differs"][:5]:
                lines.append(f"        t{d['tick']:>4}: "
                             f"trace={d['trace']!s:<28} "
                             f"replay={d['replay']}")
        lines.append("")

    if m["duration_histogram"]:
        lines.append(f"DURATION-DELTA HISTOGRAM (replay − trace, ms; "
                     f"only on matched-kind ticks; n={m['duration_deltas_count']})")
        for bucket, count in m["duration_histogram"].items():
            lines.append(f"  {bucket:<14} {count}")
        lines.append("")

    lines.append(bar)
    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────

def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("session_dir", type=Path)
    ap.add_argument("--threshold", type=float, default=0.85,
                    help="Action-agreement threshold for exit code. "
                         "Default 0.85.")
    args = ap.parse_args(argv)
    if not args.session_dir.is_dir():
        raise SystemExit(f"not a directory: {args.session_dir}")

    trace      = _load_jsonl(args.session_dir / "trace.jsonl")
    replay     = _load_jsonl(args.session_dir / "bot_replay.jsonl")
    labels     = _load_jsonl(args.session_dir / "labels.jsonl")
    keystrokes = _load_jsonl(args.session_dir / "keystrokes.jsonl")
    holds      = _physical_holds_from_keystrokes(keystrokes)

    if not trace:
        raise SystemExit(f"no trace.jsonl in {args.session_dir}")
    if not replay:
        raise SystemExit(
            f"no bot_replay.jsonl in {args.session_dir}  "
            f"(run `python -m tools.replay_voyage {args.session_dir}` first)")

    m = compute_metrics(trace, replay, labels, holds)
    report = render_report(args.session_dir.name, m)
    print(report)

    (args.session_dir / "diff_report.md").write_text(report + "\n")
    (args.session_dir / "diff_report.json").write_text(
        json.dumps(m, indent=2) + "\n")

    match_frac = m["match_pct"] / 100.0
    return 0 if match_frac >= args.threshold else 1


if __name__ == "__main__":
    sys.exit(main())
