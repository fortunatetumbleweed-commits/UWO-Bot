"""Replay an existing JSONL trace through the Lyapunov regulator, as
if Phase 1 code had been running when the trace was captured.

The motivating use case: traces captured BEFORE Phase 1 (commit
0e5ab31) don't have a `lyapunov` field, but we can reconstruct what
the regulator WOULD have proposed by re-running compute_desired_
heading_lyapunov() on each tick's nav state.  This is how we satisfy
acceptance criterion §12.5(4):

    > Replaying Lyapunov decisions through the t=80→t=95 spiral of
    > hug_debug_20260530_175557 does NOT reproduce the spiral.

Usage:
    python tools/replay_lyapunov.py \\
        data/sessions/hug_debug_<TS>/trace.jsonl \\
        --range 78 95
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from brain.goals.hug_shore import (
    compute_desired_heading_lyapunov,
    _heading_to_sector_index,
    _lyapunov_state,
    _SECTOR_REL_ANGLE,
)


class _FakeSectorReading:
    __slots__ = ("land_fraction", "nearest_dist", "is_observed", "bearing_deg")
    def __init__(self, frac, dist, bearing):
        self.land_fraction = frac
        self.nearest_dist  = dist
        self.is_observed   = True
        self.bearing_deg   = bearing


class _FakeNav:
    __slots__ = ("sectors", "ship_heading_deg")


def _reconstruct_nav(rec: dict):
    nav_dict = rec.get("nav", {})
    secs = nav_dict.get("sectors", [])
    if len(secs) != 8:
        return None
    sectors = tuple(
        _FakeSectorReading(
            frac=s["frac"], dist=s["dist"], bearing=float(i * 45),
        )
        for i, s in enumerate(secs)
    )
    nav = _FakeNav()
    nav.sectors = sectors
    nav.ship_heading_deg = rec.get("heading_deg")
    return nav


def _sector_to_category(sector_idx: int, side: str) -> str:
    angle = _SECTOR_REL_ANGLE[sector_idx]
    if abs(angle) < 22.5:
        return "hold"
    target_sign = +1 if side == "starboard" else -1
    if angle * target_sign > 0:
        return "turn_target"
    return "turn_away"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("trace")
    ap.add_argument("--range", nargs=2, type=int, metavar=("START", "END"),
                    help="tick range to replay (inclusive)")
    ap.add_argument("--side", default="starboard",
                    choices=("starboard", "port"))
    args = ap.parse_args()

    records = [json.loads(l) for l in open(args.trace) if l.strip()]
    if args.range:
        start, end = args.range
        records = [r for r in records if start <= r["tick"] <= end]

    print(f"replaying {len(records)} ticks from {args.trace}")
    print(f"side: {args.side}")
    print()
    print(f"{'tick':>4}  {'hdg':>5}  {'phase':<8}  "
          f"{'old_act':<20}  {'lyap_dh':>7}  {'lyap_sec':>4}  "
          f"{'lyap_cat':<11}  {'d':>5}  {'θerr':>5}  match?")
    print("-" * 110)

    same_cat = 0
    total_with_lyap = 0
    same_sector = 0
    diverged_ticks = []

    for r in records:
        nav = _reconstruct_nav(r)
        if nav is None:
            continue
        hdg = nav.ship_heading_deg
        if hdg is None:
            continue

        lyap_desired = compute_desired_heading_lyapunov(nav, args.side, hdg)
        lyap_state = _lyapunov_state(nav, args.side)

        # The old action's nominal sector
        old_action = (r.get("action") or "")[:20]
        if "hold_left" in old_action:
            old_sector = 7
        elif "hold_right" in old_action:
            old_sector = 1
        else:
            old_sector = 0
        old_cat = _sector_to_category(old_sector, args.side)

        if lyap_desired is None:
            print(f"{r['tick']:>4}  {hdg:>5.1f}  {r.get('phase','')[:8]:<8}  "
                  f"{old_action:<20}  {'None':>7}  {'-':>4}  "
                  f"{'(no shore)':<11}  {'-':>5}  {'-':>5}  -")
            continue

        total_with_lyap += 1
        lyap_sector = _heading_to_sector_index(lyap_desired, hdg)
        lyap_cat = _sector_to_category(lyap_sector, args.side)
        d_str = f"{lyap_state[0]:.2f}" if lyap_state else "-"
        th_str = f"{lyap_state[2]:+.1f}" if lyap_state else "-"

        cat_match = (old_cat == lyap_cat)
        sec_match = (old_sector == lyap_sector)
        if cat_match: same_cat += 1
        if sec_match: same_sector += 1
        marker = "✓" if cat_match else "✗"
        if not cat_match:
            diverged_ticks.append((r['tick'], old_cat, lyap_cat,
                                    old_sector, lyap_sector))

        print(f"{r['tick']:>4}  {hdg:>5.1f}  {r.get('phase','')[:8]:<8}  "
              f"{old_action:<20}  {lyap_desired:>7.1f}  {lyap_sector:>4}  "
              f"{lyap_cat:<11}  {d_str:>5}  {th_str:>5}  {marker}")

    print()
    print(f"summary: {total_with_lyap} ticks had a Lyapunov reading")
    if total_with_lyap > 0:
        print(f"  category match: {same_cat}/{total_with_lyap} "
              f"({same_cat/total_with_lyap*100:.0f}%)")
        print(f"  exact sector  : {same_sector}/{total_with_lyap} "
              f"({same_sector/total_with_lyap*100:.0f}%)")
    if diverged_ticks:
        print(f"\ndivergent ticks (old_cat → lyap_cat):")
        for t, old, new, old_s, new_s in diverged_ticks:
            print(f"  t={t}: {old} (sec {old_s}) → {new} (sec {new_s})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
