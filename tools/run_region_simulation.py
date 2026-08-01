"""Run a trajectory-only simulation against a real-world Region.

Loads a Region (canonical Nile by default), drives the bot's
HugShoreGoal planner against synthesised perception (sectors looked
up from the reference voyage), and emits a trajectory comparable to
a live voyage's trace.

The simulator runs in ~seconds — no game, no ADB, no frame capture.
Useful for verifying steering / planner / picker changes without a
30-minute live cycle.

Outputs (in `data/sessions/sim_<ts>/`):
  trace.jsonl      Per-tick lat/lon/heading/action — same schema as a
                   live `explore_port_<ts>/trace.jsonl`, so the existing
                   evaluation tools (compare_paths, evaluate_voyage)
                   work unchanged.

Usage:
  python -m tools.run_region_simulation
  python -m tools.run_region_simulation --region nile --max-ticks 1000
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from sim.region import Region
from sim.region_sim import run_region_sim


DEFAULT_REFERENCE_DIR = (
    Path(__file__).parent.parent / "data" / "reference"
)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--region", default="nile",
                    help="region name; loads "
                         "data/reference/standard_<region>.json")
    ap.add_argument("--max-ticks", type=int, default=1500)
    ap.add_argument("--initial-heading-deg", type=float, default=180.0,
                    help="starting bow direction; 180 = south "
                         "(Nile descent default)")
    ap.add_argument("--side", default="port",
                    choices=("port", "starboard"))
    ap.add_argument("--junction-picker", default="tremaux",
                    choices=("none", "yamauchi", "tremaux"))
    ap.add_argument("--dt-hours", type=float, default=None,
                    help="game-time per tick; default is region-specific "
                         "(~0.115h for Nile)")
    ap.add_argument("--out-name", default=None,
                    help="session dir name under data/sessions/; "
                         "default sim_<region>_<ts>")
    ap.add_argument("--start-ref-tick", type=int, default=1,
                    help="Seed sim state from reference voyage tick N "
                         "(lat/lon/heading).  Useful to skip the post-"
                         "departure ticks where the ship still faces the "
                         "port direction.  Nile: t5=219° SSW, t7=181° S.")
    ap.add_argument("--perception-mode", default="reference",
                    choices=("reference", "live", "v11"),
                    help="reference: waypoint from Region.next_reference_target "
                         "(fast, doesn't test live perception).  live: load the "
                         "reference frame for each tick, run the real "
                         "MinimapNavigationView pipeline, derive the waypoint "
                         "via the same skeleton/bearing chain as a live voyage. "
                         "Writes divergence.jsonl comparing live waypoints to "
                         "the reference path so perception regressions are "
                         "visible without burning a 30-min live voyage.")
    args = ap.parse_args(argv)

    config_path = DEFAULT_REFERENCE_DIR / f"standard_{args.region}.json"
    if not config_path.exists():
        raise SystemExit(f"missing region config: {config_path}")

    print(f"[sim] loading region from {config_path.name} ...")
    region = Region.from_config(config_path)
    print(f"[sim] {len(region.ref_ticks)} reference ticks, "
          f"start=({region.start[0]:.2f}, {region.start[1]:.2f}), "
          f"end=({region.end[0]:.2f}, {region.end[1]:.2f})")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_name = args.out_name or f"sim_{args.region}_{ts}"
    out_dir = (Path(__file__).parent.parent / "data" / "sessions" / out_name)
    out_dir.mkdir(parents=True, exist_ok=True)

    kwargs = dict(
        max_ticks=args.max_ticks,
        initial_heading_deg=args.initial_heading_deg,
        side=args.side,
        junction_picker=args.junction_picker,
        perception_mode=args.perception_mode,
        divergence_log_path=out_dir / "divergence.jsonl",
        symlink_frames_dir=(out_dir if args.perception_mode == "live"
                            else None),
        start_ref_tick=args.start_ref_tick,
    )
    if args.dt_hours is not None:
        kwargs["dt_hours"] = args.dt_hours

    print(f"[sim] perception_mode={args.perception_mode}")
    print(f"[sim] running for up to {args.max_ticks} ticks ...")
    trace = run_region_sim(region, **kwargs)

    # Per-tick deviation analysis vs reference path.  Reports rolling
    # min/max/mean every 100 ticks so we see when (if) the sim
    # diverges from the reference, and by how much.
    print()
    print("[sim] tick-by-tick deviation vs reference (every 100 ticks):")
    print(f"  {'tick':>5}  {'pos':<22}  {'dev_km':>7}  {'window stats (km)':<32}")
    import math as _m
    rolling = []
    for r in trace:
        target = region.next_reference_target(r.lat, r.lon, lookahead_ticks=0)
        if target is None:
            d_km = float("nan")
        else:
            dlat = r.lat - target[0]
            dlon = (r.lon - target[1]) * _m.cos(_m.radians(r.lat))
            d_km = _m.hypot(dlat, dlon) * 111.0
        rolling.append(d_km)
        if r.tick % 100 == 0 or r.tick == len(trace):
            window = [d for d in rolling[-100:] if not _m.isnan(d)]
            if window:
                stats = (f"min={min(window):>5.1f} "
                         f"avg={sum(window)/len(window):>5.1f} "
                         f"max={max(window):>5.1f}")
            else:
                stats = "—"
            pos = f"({r.lat:>6.2f}, {r.lon:>6.2f})"
            print(f"  t{r.tick:>4}  {pos:<22}  {d_km:>7.1f}  {stats}")

    with (out_dir / "trace.jsonl").open("w") as f:
        for r in trace:
            f.write(json.dumps({
                "tick": r.tick,
                "wall_iso": datetime.now().isoformat(),
                "lat": r.lat,
                "lon": r.lon,
                "heading_deg": r.heading_deg,
                "heading_deg_raw": r.heading_deg_perceived,
                "heading_rejected": False,
                "speed_kt": 20.0,
                "action": r.action,
                "phase": r.phase,
                "note": r.note,
                # Stub fields downstream tools may consult:
                "endpoint_lat": region.end[0],
                "endpoint_lon": region.end[1],
                "side": "port",
                "coverage_verdict": "keep_following",
                "lyapunov": r.lyapunov,
            }) + "\n")

    final = trace[-1] if trace else None
    print()
    print(f"[sim] {len(trace)} ticks simulated.")
    if final is not None:
        print(f"[sim] final: lat={final.lat:.2f} lon={final.lon:.2f}  "
              f"hdg={final.heading_deg:.0f}  phase={final.phase}")
        d_to_end = ((final.lat - region.end[0]) ** 2
                    + (final.lon - region.end[1]) ** 2) ** 0.5
        print(f"[sim] distance to endpoint: {d_to_end * 111:.0f} km")
    print(f"[sim] trace written to {out_dir / 'trace.jsonl'}")
    print()
    print("Next: evaluate with the standard procedure:")
    print(f"  python -m tools.evaluate_voyage {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
