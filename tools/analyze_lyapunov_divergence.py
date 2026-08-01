"""Phase 1 — Compare the Lyapunov regulator's per-tick decisions
against the actual `ideal_idx` chosen by `_ideal_sector`, across one
or more live JSONL traces.

Reads the `lyapunov` block from each trace record (logged by
brain/goals/hug_shore.py when Phase 1 code is in place) and produces:

  1. Per-tick comparison table: actual_ideal vs lyapunov_proposed,
     with diverging ticks marked
  2. Summary: agreement %, magnitude histogram, top N divergent ticks

Usage:
    python tools/analyze_lyapunov_divergence.py \\
        data/sessions/hug_debug_<TS>/trace.jsonl

    # Or multiple sessions at once for aggregate metrics:
    python tools/analyze_lyapunov_divergence.py \\
        data/sessions/hug_debug_*/trace.jsonl

Use the output to decide when Phase 1 acceptance criteria are met —
see docs/shore_following_design.md §12.5.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _load_trace(path: Path) -> list[dict]:
    return [json.loads(l) for l in open(path) if l.strip()]


def _sector_diff(a: int, b: int) -> int:
    """Cyclic distance between two sectors (0..4)."""
    if a is None or b is None:
        return 0
    d = abs(a - b) % 8
    return min(d, 8 - d)


def analyze_one(records: list[dict], label: str) -> dict:
    """Per-tick diff between actual ideal_idx and lyapunov proposed.

    Note: `ideal_idx` is recorded as `ideal_sector` in TickRecord but
    only in finalized records.  We pull from `chosen_sector` when
    `ideal_sector` isn't present; the lyapunov_proposed should be
    compared against the IDEAL the policy picked, not the cost-min
    choice the cost function actually committed to.
    """
    total = 0
    have_lyap = 0
    agree = 0
    diff_hist: Counter[int] = Counter()
    top_divergent: list[dict] = []
    no_action_diff_count = 0

    for r in records:
        lyap = r.get("lyapunov")
        if not lyap:
            continue
        total += 1
        # Prefer the shadow VFH+ pick (Phase 2+ — Lyapunov drives, so
        # `chosen_sector` would just echo `proposed_sector` and the
        # agreement number would be meaningless).  Fall back to
        # `chosen_sector` for Phase-1 traces that don't carry
        # `vfh_best_sector`.
        actual_chosen = (lyap.get("vfh_best_sector")
                         if lyap.get("vfh_best_sector") is not None
                         else r.get("nav", {}).get("chosen_sector"))
        proposed = lyap.get("proposed_sector")
        if proposed is None:
            continue
        have_lyap += 1
        actual = actual_chosen
        if actual is None:
            # Pre-Phase-1.5 trace fallback via action string.
            action = (r.get("action") or "").lower()
            if "hold_left" in action: actual = 7
            elif "hold_right" in action: actual = 1
            elif action.startswith("hold"): actual = 0
            else: actual = None
        if actual is None:
            continue
        d = _sector_diff(actual, proposed)
        diff_hist[d] += 1
        if d == 0:
            agree += 1
        else:
            top_divergent.append({
                "tick":       r.get("tick"),
                "actual":     actual,
                "proposed":   proposed,
                "delta_sec":  d,
                "lyap_d":     lyap.get("d"),
                "lyap_dstar": lyap.get("d_star"),
                "lyap_theta": lyap.get("theta_err"),
                "lyap_desired": lyap.get("desired_heading_deg"),
                "hdg":        r.get("heading_deg"),
                "phase":      r.get("phase"),
                "note":       (r.get("note") or "")[:80],
            })

    top_divergent.sort(key=lambda x: (-x["delta_sec"], x["tick"]))
    return {
        "label":       label,
        "total":       total,
        "have_lyap":   have_lyap,
        "agree":       agree,
        "agreement":   (agree / have_lyap * 100.0) if have_lyap else 0.0,
        "diff_hist":   dict(diff_hist),
        "top_divergent": top_divergent[:10],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("traces", nargs="+", help="JSONL trace files")
    ap.add_argument("--top-n", type=int, default=10,
                    help="how many most-divergent ticks to print")
    args = ap.parse_args()

    all_summaries = []
    for path in args.traces:
        p = Path(path)
        if not p.exists():
            print(f"[warn] missing {p}")
            continue
        records = _load_trace(p)
        summary = analyze_one(records, p.name)
        all_summaries.append(summary)

        print(f"\n=== {p.name} ===")
        print(f"  total ticks                   : {summary['total']}")
        print(f"  ticks with lyapunov logged    : {summary['have_lyap']}")
        print(f"  same-sector agreement         : {summary['agree']} "
              f"({summary['agreement']:.1f}%)")
        print(f"  sector-distance histogram     :")
        for d in sorted(summary["diff_hist"].keys()):
            n = summary["diff_hist"][d]
            bar = "█" * min(40, n // 2 + 1)
            print(f"    Δ={d}  {n:>4}  {bar}")
        print(f"  top divergent ticks (Δsec, tick, actual→proposed):")
        for row in summary["top_divergent"][:args.top_n]:
            print(f"    t={row['tick']:>3}  Δ={row['delta_sec']} "
                  f"actual={row['actual']} → lyap={row['proposed']}  "
                  f"d={row['lyap_d']!r}  θ={row['lyap_theta']!r}  "
                  f"phase={row['phase']}")
            if row["note"]:
                print(f"        note: {row['note']}")

    if len(all_summaries) > 1:
        total_have = sum(s["have_lyap"] for s in all_summaries)
        total_agree = sum(s["agree"]    for s in all_summaries)
        agg = (total_agree / total_have * 100.0) if total_have else 0.0
        print(f"\n=== AGGREGATE ACROSS {len(all_summaries)} TRACES ===")
        print(f"  total logged ticks    : {total_have}")
        print(f"  agreement             : {agg:.1f}%")
        print(f"  Phase-2 acceptance    : "
              f"{'✓ ≥75%' if agg >= 75 else '✗ <75% — investigate divergences'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
