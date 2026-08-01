"""Live ADB runner for the centerline-waypoint prototype.

Captures the minimap each tick, runs M6f + picker, converts the
waypoint pixel direction to a world-bearing steering command, and
holds the rudder via `actions.sea_actions.hold_left/hold_right`.

Optionally accumulates a WorldMap (`sim/centerline_world_map.py`) from
the per-tick stable nodes (junctions + dead-ends) and uses it for
target-commitment navigation, so the picker doesn't lose orientation
at long bends.  Bot position is dead-reckoned from a --start-lat/lon
seed plus speed × dt × heading (no live lat/lon OCR for now).

Safety:
  - hard cap on tick count (default 30)
  - auto sail_stop() on exit (signal handler + try/finally)
  - per-tick hold capped to 800 ms (prevents over-rotation on bad
    heading reads)
  - aborts on no_tree or no_ship for N consecutive ticks
  - jittered sleeps between commands (anti-cheat rule)
  - per-tick log + composite written to
    data/sessions/live_centerline_<TS>/

Usage
─────
  python -m tools.run_centerline_live --max-ticks 30 --hug left
  python -m tools.run_centerline_live --max-ticks 1000 --hug left \\
      --use-world-commit --start-lat 30.0 --start-lon 30.5
"""
from __future__ import annotations

import argparse
import json
import math
import random
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from capture.adb_capture import capture_screen  # noqa: E402
from vision.minimap_navigation_view import MINIMAP_CROP  # noqa: E402
from actions.sea_actions import (  # noqa: E402
    sail_start, sail_stop, hold_left, hold_right,
)
from tools.centerline_extraction_prototype import _ship_xy  # noqa: E402
from tools.centerline_waypoint_prototype import (  # noqa: E402
    extract_tree, pick_waypoint, estimate_ship_heading, render,
    LOOKAHEAD_PX,
)
from sim.centerline_world_map import WorldMap  # noqa: E402
from tools.shore_waypoint_picker import (  # noqa: E402
    extract_shore_path,
)


RATE_DPS = 120.0          # rudder hold rate (deg/sec), per memory
MAX_HOLD_MS = 800         # cap any single hold (prevent over-rotation)
MIN_DELTA_DEG = 5.0       # don't rotate for tiny corrections
TICK_PERIOD_S = (0.9, 1.3)   # jittered inter-tick sleep
HUG_SIDE = "left"
ABORT_CONSECUTIVE_NO_TREE = 4

# Dead-reckoning constants (match sim/region_sim.py + sim_centerline_waypoint.py).
DT_HOURS = 0.115
DEFAULT_SPEED_KT = 20.0
KM_PER_DEG_LAT = 111.0
KM_PER_NM = 1.852


def _bearing_of_waypoint(ship_xy, dest_yx) -> float:
    dy = dest_yx[0] - ship_xy[1]
    dx = dest_yx[1] - ship_xy[0]
    return math.degrees(math.atan2(dx, -dy)) % 360.0


def _pca_bearing(heading_vec) -> float | None:
    if heading_vec is None:
        return None
    dy, dx = heading_vec
    return math.degrees(math.atan2(dx, -dy)) % 360.0


def _signed_delta(target_deg: float, current_deg: float) -> float:
    return ((target_deg - current_deg + 180) % 360) - 180


def _bearing_to_vec(bearing_deg: float) -> tuple[float, float]:
    r = math.radians(bearing_deg)
    return (-math.cos(r), math.sin(r))


def _bearing_between(lat1, lon1, lat2, lon2) -> float:
    cos_lat = math.cos(math.radians((lat1 + lat2) / 2))
    dy = (lat2 - lat1) * KM_PER_DEG_LAT
    dx = (lon2 - lon1) * KM_PER_DEG_LAT * cos_lat
    return math.degrees(math.atan2(dx, dy)) % 360.0


def _advance_latlon(lat, lon, heading_deg, speed_kt, dt_hours):
    """Dead-reckon next (lat, lon) given heading + speed × dt."""
    km = speed_kt * dt_hours * KM_PER_NM
    cos_lat = math.cos(math.radians(lat))
    r = math.radians(heading_deg)
    dlat = (km * math.cos(r)) / KM_PER_DEG_LAT
    dlon = (km * math.sin(r)) / (KM_PER_DEG_LAT * cos_lat)
    return lat + dlat, lon + dlon


def _build_note(skip, cmd, hold_ms, picker_bearing, pca_b, target_node_id,
                n_shore, n_path) -> str:
    parts: list[str] = []
    if skip:
        parts.append(f"SKIP:{skip}")
    if target_node_id is not None:
        parts.append(f"tgt={target_node_id}")
    if picker_bearing is not None:
        parts.append(f"pick={picker_bearing:.0f}°")
    if pca_b is not None:
        parts.append(f"pca={pca_b:.0f}°")
    if picker_bearing is not None and pca_b is not None:
        parts.append(f"Δ={_signed_delta(picker_bearing, pca_b):+.0f}°")
    parts.append(f"shore={n_shore}/path={n_path}px")
    if cmd:
        parts.append(f"{cmd}@{hold_ms}ms")
    else:
        parts.append("hold")
    return " ".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-ticks", type=int, default=30)
    ap.add_argument("--picker", choices=("centerline", "shore"),
                   default="centerline",
                   help="centerline = M6f tree (legacy).  "
                        "shore = bow-relative shore-hug.")
    ap.add_argument("--side", choices=("port", "starboard"), default="port",
                   help="Shore picker: which side of the bow to hug.")
    ap.add_argument("--hug", choices=("left", "right"), default=HUG_SIDE,
                   help="Centerline picker hug side (legacy).")
    ap.add_argument("--lookahead", type=int, default=LOOKAHEAD_PX)
    ap.add_argument("--shore-walk-px", type=int, default=80,
                   help="Shore picker: walk distance along shore per tick.")
    ap.add_argument("--shore-safe-px", type=int, default=25,
                   help="Shore picker: distance to stay off the shore (px).")
    # Breadcrumb / loop closure (works with both pickers).
    ap.add_argument("--breadcrumb-km", type=float, default=5.0,
                   help="Drop a breadcrumb every K km of cumulative travel.")
    ap.add_argument("--loop-close-km", type=float, default=3.0,
                   help="Distance to old breadcrumb that counts as 'returned'.")
    ap.add_argument("--min-loop-km", type=float, default=50.0,
                   help="Min travel before loop closure can fire.")
    ap.add_argument("--exit-on-loop", action="store_true",
                   help="Stop the run when loop closure fires.")
    ap.add_argument("--force-heading", type=float, default=None,
                   help="Override PCA heading with this world bearing.")
    ap.add_argument("--no-sail-start", action="store_true")
    # WorldMap-related flags (Piece B integration).
    ap.add_argument("--use-world-commit", action="store_true",
                   help="Accumulate WorldMap and use target commitment "
                        "for picker heading.")
    ap.add_argument("--start-lat", type=float, default=None,
                   help="Initial latitude for dead-reckoning "
                        "(required with --use-world-commit).")
    ap.add_argument("--start-lon", type=float, default=None,
                   help="Initial longitude for dead-reckoning.")
    ap.add_argument("--goal-lat", type=float, default=None,
                   help="Goal latitude for target-selection scoring. "
                        "Default: dead-reckon south, current heading.")
    ap.add_argument("--goal-lon", type=float, default=None)
    ap.add_argument("--speed-kt", type=float, default=DEFAULT_SPEED_KT)
    args = ap.parse_args()

    if args.use_world_commit and (args.start_lat is None or args.start_lon is None):
        sys.exit("--use-world-commit requires --start-lat and --start-lon")

    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    out_dir = ROOT / "data" / "sessions" / f"live_centerline_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    composites_dir = out_dir / "composites"
    composites_dir.mkdir(exist_ok=True)
    log_path = out_dir / "ticks.jsonl"
    trace_path = out_dir / "trace.jsonl"
    print(f"out dir   : {out_dir}")
    print(f"hug={args.hug}  lookahead={args.lookahead}px  "
          f"max_ticks={args.max_ticks}")
    if args.use_world_commit:
        print(f"WorldMap commit ON  start=({args.start_lat}, {args.start_lon})  "
              f"speed={args.speed_kt}kt")
        if args.goal_lat is not None:
            print(f"goal=({args.goal_lat}, {args.goal_lon})")

    # Initialize dead-reckoning state.
    sim_lat = args.start_lat if args.start_lat is not None else 0.0
    sim_lon = args.start_lon if args.start_lon is not None else 0.0
    last_sim_heading: float | None = None
    world_map = WorldMap() if args.use_world_commit else None

    # Breadcrumb buffer for loop-closure detection.
    # Each entry: (lat, lon, cumulative_travel_km_at_drop).
    breadcrumbs: list[tuple[float, float, float]] = []
    cumulative_travel_km = 0.0
    last_breadcrumb_km = 0.0
    loop_closed = False

    aborted = False
    def _abort(sig=None, frame=None):
        nonlocal aborted
        aborted = True
        print("\n[ABORT requested — stopping after this tick]")
    signal.signal(signal.SIGINT, _abort)

    try:
        if not args.no_sail_start:
            print("→ sail_start()")
            r = sail_start()
            print(f"  result: {r}")
            time.sleep(random.uniform(1.0, 1.5))

        consecutive_no_tree = 0
        target_events = {"acquired": 0, "arrived": 0}
        with log_path.open("w") as log_f, trace_path.open("w") as trace_f:
            for tick in range(args.max_ticks):
                if aborted:
                    break
                t_capture = time.perf_counter()
                frame = capture_screen()
                mm = frame.crop(MINIMAP_CROP)
                t_after_cap = time.perf_counter()
                rgb_raw = np.asarray(mm.convert("RGB"))
                tree, mask = extract_tree(rgb_raw)
                rgb = rgb_raw[6:, :-6]
                ship = _ship_xy(rgb)
                heading_vec = estimate_ship_heading(rgb)
                t_after_perc = time.perf_counter()

                skip = None
                dest = None; visited = []
                wp_bearing = None; picker_bearing = None; cmd = None
                hold_ms = 0
                target_event = None
                target_node_id = None
                shore_pts: list = []
                path_pts: list = []

                if not tree.nodes:
                    skip = "no_tree"; consecutive_no_tree += 1
                elif ship is None:
                    skip = "no_ship"
                else:
                    consecutive_no_tree = 0
                    pca_b = _pca_bearing(heading_vec)
                    # Integrate this tick's tree into the WorldMap.
                    if world_map is not None:
                        world_map.integrate(tree, sim_lat, sim_lon, ship,
                                            tick, mode="river")
                        # Arrival / transition logic.
                        if world_map.arrived_at_target(sim_lat, sim_lon):
                            world_map.bot_state.last_target_id = \
                                world_map.bot_state.target_node_id
                            world_map.bot_state.target_node_id = None
                            target_event = "arrived"
                            target_events["arrived"] += 1
                        if world_map.bot_state.target_node_id is None:
                            goal_lat = args.goal_lat if args.goal_lat is not None \
                                       else sim_lat - 5.0   # 5° south of current
                            goal_lon = args.goal_lon if args.goal_lon is not None \
                                       else sim_lon
                            picked = world_map.select_target(
                                sim_lat, sim_lon, goal_lat, goal_lon)
                            if picked is not None:
                                world_map.bot_state.target_node_id = picked.id
                                target_event = (target_event or "") + "acquired"
                                target_events["acquired"] += 1
                        if world_map.bot_state.target_node_id is not None:
                            t = world_map.nodes[world_map.bot_state.target_node_id]
                            target_node_id = t.id
                            picker_bearing = _bearing_between(
                                sim_lat, sim_lon, t.lat, t.lon)

                    # Pick the heading vector for the picker.
                    # Priority order:
                    #   1. --force-heading (CLI override)
                    #   2. bearing-to-WorldMap-target (Piece B commitment)
                    #   3. bearing-to-GOAL when WorldMap is active but no
                    #      target yet — biases the bot toward the goal from
                    #      tick 0, so initial observations are along the
                    #      intended path (otherwise PCA at parked-ship
                    #      orientation pulls the picker toward Mediterranean
                    #      / harbor branches at Cairo)
                    #   4. PCA heading (legacy fallback)
                    if args.force_heading is not None:
                        picker_bearing = args.force_heading
                    elif picker_bearing is None and world_map is not None \
                            and args.goal_lat is not None:
                        picker_bearing = _bearing_between(
                            sim_lat, sim_lon, args.goal_lat, args.goal_lon)
                    if picker_bearing is None:
                        picker_bearing = pca_b
                    hvec = _bearing_to_vec(picker_bearing) \
                           if picker_bearing is not None else heading_vec

                    if hvec is None:
                        skip = "no_heading"
                    elif args.picker == "shore":
                        # SHORE picker: ship anchored at frame center,
                        # walk along chosen-side shore from there.
                        H_mm, W_mm = mask.shape
                        ship_center = (W_mm // 2, H_mm // 2)
                        steering_heading = picker_bearing if picker_bearing \
                                          is not None else pca_b
                        if steering_heading is None:
                            skip = "no_heading"
                        else:
                            shore_pts, path_pts = extract_shore_path(
                                mask, ship_center, steering_heading,
                                side=args.side,
                                walk_distance_px=args.shore_walk_px,
                                safe_distance_px=args.shore_safe_px,
                            )
                            if path_pts:
                                # Waypoint = last point on the walked path
                                # (furthest ahead along the shore).
                                dest = path_pts[-1]
                                wp_bearing = _bearing_of_waypoint(
                                    ship_center, dest)
                                current_b = pca_b if pca_b is not None \
                                            else picker_bearing
                                delta = _signed_delta(wp_bearing, current_b)
                                if abs(delta) >= MIN_DELTA_DEG:
                                    hold_ms = int(min(MAX_HOLD_MS,
                                        abs(delta) / RATE_DPS * 1000))
                                    cmd = "hold_right" if delta > 0 else "hold_left"
                            else:
                                skip = "no_shore"
                    else:
                        # CENTERLINE picker (legacy M6f tree).
                        dest, _, visited = pick_waypoint(
                            tree, ship, hvec, args.hug, args.lookahead)
                        if dest is not None:
                            wp_bearing = _bearing_of_waypoint(ship, dest)
                            current_b = pca_b if pca_b is not None else picker_bearing
                            delta = _signed_delta(wp_bearing, current_b)
                            if abs(delta) >= MIN_DELTA_DEG:
                                hold_ms = int(min(MAX_HOLD_MS,
                                                 abs(delta) / RATE_DPS * 1000))
                                cmd = "hold_right" if delta > 0 else "hold_left"
                        else:
                            skip = "no_waypoint"

                # Dead-reckon position based on PCA heading (real ship motion).
                pca_b = _pca_bearing(heading_vec)
                if pca_b is not None:
                    prev_lat, prev_lon = sim_lat, sim_lon
                    sim_lat, sim_lon = _advance_latlon(
                        sim_lat, sim_lon, pca_b, args.speed_kt, DT_HOURS)
                    # Accumulate travel distance for breadcrumbs.
                    cos_lat = math.cos(math.radians((sim_lat + prev_lat) / 2))
                    d_km = math.hypot(
                        (sim_lat - prev_lat) * KM_PER_DEG_LAT,
                        (sim_lon - prev_lon) * KM_PER_DEG_LAT * cos_lat,
                    )
                    cumulative_travel_km += d_km
                    last_sim_heading = pca_b

                # Drop a breadcrumb every breadcrumb_km of travel.
                if cumulative_travel_km - last_breadcrumb_km >= args.breadcrumb_km:
                    breadcrumbs.append(
                        (sim_lat, sim_lon, cumulative_travel_km))
                    last_breadcrumb_km = cumulative_travel_km

                # Loop closure check: are we near a breadcrumb that's
                # at least min_loop_km of travel ago?
                if not loop_closed and breadcrumbs \
                        and cumulative_travel_km >= args.min_loop_km:
                    for bc_lat, bc_lon, bc_km in breadcrumbs:
                        if cumulative_travel_km - bc_km < args.min_loop_km:
                            continue
                        cos_lat = math.cos(math.radians((sim_lat + bc_lat) / 2))
                        d_km = math.hypot(
                            (sim_lat - bc_lat) * KM_PER_DEG_LAT,
                            (sim_lon - bc_lon) * KM_PER_DEG_LAT * cos_lat,
                        )
                        if d_km < args.loop_close_km:
                            loop_closed = True
                            print(f"\n[LOOP CLOSED at tick {tick}] "
                                  f"returned to breadcrumb at "
                                  f"({bc_lat:.3f},{bc_lon:.3f}) "
                                  f"after {cumulative_travel_km - bc_km:.1f} km")
                            break

                # Save raw minimap crop as tick_NNNN.png (what tick_viewer
                # expects).  Composite (with M6f tree + waypoint overlay)
                # goes to composites/ subdir for our own inspection.
                tick_png_path = out_dir / f"tick_{tick:04d}.png"
                mm.save(tick_png_path)
                composite_path = composites_dir / f"tick_{tick:04d}.png"
                if tree.nodes and ship is not None and heading_vec is not None:
                    render(rgb, mask, tree, ship, heading_vec,
                           dest, [], args.hug, composite_path)

                t_before_cmd = time.perf_counter()
                result = None
                if cmd == "hold_left":
                    result = hold_left(hold_ms)
                elif cmd == "hold_right":
                    result = hold_right(hold_ms)
                t_after_cmd = time.perf_counter()

                rec = {
                    "tick": tick,
                    "wall": datetime.now().isoformat(),
                    "skip": skip,
                    "tree_nodes": len(tree.nodes),
                    "ship_xy": ship,
                    "sim_lat": round(sim_lat, 4) if world_map else None,
                    "sim_lon": round(sim_lon, 4) if world_map else None,
                    "pca_bearing": _pca_bearing(heading_vec),
                    "picker_bearing": picker_bearing,
                    "waypoint_px": list(dest) if dest else None,
                    "waypoint_bearing": wp_bearing,
                    "cmd": cmd, "hold_ms": hold_ms,
                    "result_ok": getattr(result, "ok", None),
                    "visited": visited,
                    "target_node_id": target_node_id,
                    "target_event": target_event,
                    "wm_nodes": len(world_map.nodes) if world_map else None,
                    "capture_ms": round((t_after_cap - t_capture) * 1e3, 1),
                    "perc_ms": round((t_after_perc - t_after_cap) * 1e3, 1),
                    "cmd_ms": round((t_after_cmd - t_before_cmd) * 1e3, 1),
                }
                log_f.write(json.dumps(rec) + "\n")
                log_f.flush()

                # trace.jsonl entry in tick_viewer-compatible schema.
                pca_b = _pca_bearing(heading_vec)
                trace_rec = {
                    "tick": tick,
                    "wall_iso": rec["wall"],
                    "capture_ms": rec["capture_ms"],
                    "nav_read_ms": rec["perc_ms"],
                    "decision_ms": rec["cmd_ms"],
                    "heading_deg": pca_b,
                    "heading_deg_raw": pca_b,
                    "heading_strategy": "pca_from_ship_icon",
                    "heading_confidence": 0.5,
                    "speed_kt": args.speed_kt,
                    "lat": rec.get("sim_lat"),
                    "lon": rec.get("sim_lon"),
                    "commanded_deg_this": picker_bearing,
                    "prev_action": cmd,
                    "phase": ("WM_COMMIT" if target_node_id else
                              "GOAL_BOOTSTRAP" if args.goal_lat is not None else
                              "PCA_DRIVE"),
                    "action": cmd or "noop",
                    "note": _build_note(skip, cmd, hold_ms, picker_bearing,
                                        pca_b, target_node_id,
                                        len(shore_pts), len(path_pts)),
                    "wall_distance": None,
                    "endpoint_lat": args.goal_lat,
                    "endpoint_lon": args.goal_lon,
                    "picker_target": target_node_id,
                    "crop": tick_png_path.name,
                    # Shore-picker overlays (image-space [y, x] tuples).
                    "shore_pts": [list(p) for p in shore_pts],
                    "path_pts": [list(p) for p in path_pts],
                    "waypoint_px": list(dest) if dest else None,
                    "picker": args.picker,
                    "picker_side": args.side,
                    "skip_reason": skip,
                }
                trace_f.write(json.dumps(trace_rec) + "\n")
                trace_f.flush()

                hd_s = f"{_pca_bearing(heading_vec):.0f}°" \
                       if heading_vec is not None else "?"
                wp_s = f"{wp_bearing:.0f}°" if wp_bearing is not None else "?"
                tgt = f"→{target_node_id}" if target_node_id else ""
                latlon = f"({sim_lat:.2f},{sim_lon:.2f})" if world_map else ""
                wm_n = f"wm={len(world_map.nodes)}" if world_map else ""
                print(f"  t={tick:>3} {latlon} skip={skip!s:<11} "
                      f"hdg={hd_s:>5} wp={wp_s:>5} {tgt:<6} {wm_n} "
                      f"cmd={cmd or '-':<11} hold={hold_ms}ms")

                if consecutive_no_tree >= ABORT_CONSECUTIVE_NO_TREE:
                    print(f"[abort: {consecutive_no_tree} consecutive no_tree]")
                    break

                if loop_closed and args.exit_on_loop:
                    print(f"[exit: loop closure + --exit-on-loop]")
                    break

                time.sleep(random.uniform(*TICK_PERIOD_S))

    finally:
        print("\n→ sail_stop()")
        try:
            r = sail_stop()
            print(f"  result: {r}")
        except Exception as e:
            print(f"  sail_stop failed: {e}")
        if world_map is not None:
            print(f"\nWorldMap:")
            print(f"  nodes: {len(world_map.nodes)}")
            print(f"  edges: {len(world_map.edges)}")
            print(f"  targets acquired: {target_events.get('acquired', 0)}")
            print(f"  targets arrived:  {target_events.get('arrived', 0)}")
        print(f"\nLog:  {log_path}")
        print(f"Composites: {out_dir}/tick_*.png")


if __name__ == "__main__":
    main()
