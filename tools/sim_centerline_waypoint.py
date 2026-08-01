"""Closed-loop sim of the centerline-waypoint picker on a reference voyage.

Drives a kinematic ship through (lat, lon, heading) space, each tick:
  1. Finds the nearest reference tick by (sim_lat, sim_lon).
  2. Loads that reference frame.
  3. Runs M6f centerline extraction + 50-px waypoint picker against the
     frame, using sim_heading_deg as the picker's ship heading (the
     actual ship sprite in the ref frame may be heading differently —
     we override).
  4. Computes the world bearing from ship_xy → waypoint pixel.
  5. Snaps sim_heading_deg to that bearing (no rate-limit — simplest
     model; a rate-limited variant is a TODO).
  6. Advances sim_lat, sim_lon by speed_kt * dt_hours in heading
     direction.

Emits:
  sim_trace.jsonl  — per-sim-tick (sim_lat, sim_lon, sim_heading_deg,
                     waypoint_bearing_deg, ref_tick_used, deviation_km,
                     skip_reason)
  composites/sim_step_NNNN.png  — every --stride sim ticks
  trajectory.png   — final sim track vs reference track in lat/lon
  Summary stats   — final distance from reference endpoint, mean and
                    p95 cross-track deviation, % valid waypoints,
                    fraction stuck.

Usage
─────
  python -m tools.sim_centerline_waypoint \\
      data/reference/standard_nile.json --max-ticks 800 --stride 50

Note
────
This is the centerline-waypoint counterpart of `tools.run_region_simulation`
(which sims HugShoreGoal).  Independent harnesses — pick whichever fits
the perception layer you want to evaluate.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.centerline_extraction_prototype import (  # noqa: E402
    channel_mask_from_rgb, _ship_xy,
)
from tools.centerline_waypoint_prototype import (  # noqa: E402
    extract_tree, pick_waypoint, render,
    LOOKAHEAD_PX, HUG_SIDE,
)
from sim.centerline_world_map import WorldMap  # noqa: E402


DT_HOURS = 0.115            # game time per tick (matches sim/region_sim.py)
DEFAULT_SPEED_KT = 20.0
KM_PER_DEG_LAT = 111.0
KM_PER_NM = 1.852
MAX_HEADING_STEP_DEG = 15.0  # cap heading change per tick to avoid
                             # noisy perpendicular-waypoint flips


# ── Reference loader ────────────────────────────────────────────────────


@dataclass(frozen=True)
class RefTick:
    tick: int
    lat: float
    lon: float
    heading_deg: float
    speed_kt: float


def load_ref(session_dir: Path, outbound_end_tick: int) -> list[RefTick]:
    out: list[RefTick] = []
    with (session_dir / "trace.jsonl").open() as f:
        for line in f:
            d = json.loads(line)
            t = d.get("tick")
            if t is None or t > outbound_end_tick:
                continue
            if d.get("lat") is None or d.get("lon") is None:
                continue
            out.append(RefTick(
                tick=int(t),
                lat=float(d["lat"]),
                lon=float(d["lon"]),
                heading_deg=float(d.get("heading_deg") or 0.0),
                speed_kt=float(d.get("speed_kt") or DEFAULT_SPEED_KT),
            ))
    return out


def nearest_ref_idx(refs: list[RefTick], lat: float, lon: float) -> int:
    arr = np.array([(r.lat, r.lon) for r in refs])
    d2 = (arr[:, 0] - lat) ** 2 + (arr[:, 1] - lon) ** 2
    return int(np.argmin(d2))


def cross_track_km_to_ref(refs: list[RefTick], lat: float, lon: float
                         ) -> float:
    """Distance in km from (lat, lon) to nearest reference point."""
    i = nearest_ref_idx(refs, lat, lon)
    r = refs[i]
    cos_lat = math.cos(math.radians((lat + r.lat) / 2))
    dlat_km = (r.lat - lat) * KM_PER_DEG_LAT
    dlon_km = (r.lon - lon) * KM_PER_DEG_LAT * cos_lat
    return math.hypot(dlat_km, dlon_km)


# ── Heading utilities ──────────────────────────────────────────────────


def bearing_to_image_vec(bearing_deg: float) -> tuple[float, float]:
    """World bearing (0=N, 90=E, CW) → image-space unit vector (dy, dx)
    where y points down and x points right.  N → (-1, 0), E → (0, +1)."""
    r = math.radians(bearing_deg)
    return (-math.cos(r), math.sin(r))


def image_vec_to_bearing(dy: float, dx: float) -> float:
    """Inverse of `bearing_to_image_vec`.  Returns degrees in [0, 360)."""
    return math.degrees(math.atan2(dx, -dy)) % 360.0


def bearing_between_latlon(lat1: float, lon1: float,
                          lat2: float, lon2: float) -> float:
    """World bearing from point 1 to point 2, in degrees (0=N, 90=E)."""
    cos_lat = math.cos(math.radians((lat1 + lat2) / 2))
    dy_km = (lat2 - lat1) * KM_PER_DEG_LAT             # north positive
    dx_km = (lon2 - lon1) * KM_PER_DEG_LAT * cos_lat   # east positive
    return math.degrees(math.atan2(dx_km, dy_km)) % 360.0


def rate_limit_bearing(current_deg: float, target_deg: float,
                      max_step_deg: float = MAX_HEADING_STEP_DEG) -> float:
    """Move `current_deg` toward `target_deg` by at most `max_step_deg`,
    on the shorter angular side."""
    diff = ((target_deg - current_deg + 180) % 360) - 180
    if abs(diff) <= max_step_deg:
        return target_deg % 360.0
    return (current_deg + math.copysign(max_step_deg, diff)) % 360.0


# ── Step ───────────────────────────────────────────────────────────────


@dataclass
class SimStep:
    step: int
    sim_lat: float
    sim_lon: float
    sim_heading_deg: float
    ref_tick: int
    waypoint_px: tuple[int, int] | None
    waypoint_bearing_deg: float | None
    deviation_km: float
    visited: list[str]
    skip_reason: str | None
    target_node_id: str | None = None
    target_event: str | None = None    # "acquired", "arrived", "transition"


def advance(lat: float, lon: float, heading_deg: float,
           speed_kt: float, dt_hours: float) -> tuple[float, float]:
    km = speed_kt * dt_hours * KM_PER_NM
    cos_lat = math.cos(math.radians(lat))
    r = math.radians(heading_deg)
    dlat = (-km * math.cos(r) * -1) / KM_PER_DEG_LAT     # north = +lat
    # Actually: heading N (0°) should INCREASE lat (north).  cos(0)=1.
    # dlat = km*cos(r)/111 ; dlon = km*sin(r)/(111*cos(lat))
    dlat = (km * math.cos(r)) / KM_PER_DEG_LAT
    dlon = (km * math.sin(r)) / (KM_PER_DEG_LAT * cos_lat)
    return lat + dlat, lon + dlon


def sim_step(refs: list[RefTick], sim_lat: float, sim_lon: float,
            sim_heading_deg: float, hug_side: str, lookahead: int,
            session_dir: Path, step: int,
            world_map: WorldMap | None = None,
            goal_lat: float | None = None,
            goal_lon: float | None = None,
            use_world_commit: bool = False) -> SimStep:
    """Run one sim tick.  Returns the step record + new (lat, lon, hdg)."""
    ri = nearest_ref_idx(refs, sim_lat, sim_lon)
    rt = refs[ri]
    frame_path = session_dir / f"tick_{rt.tick:04d}.png"
    if not frame_path.exists():
        return SimStep(
            step=step, sim_lat=sim_lat, sim_lon=sim_lon,
            sim_heading_deg=sim_heading_deg, ref_tick=rt.tick,
            waypoint_px=None, waypoint_bearing_deg=None,
            deviation_km=cross_track_km_to_ref(refs, sim_lat, sim_lon),
            visited=[], skip_reason="frame_missing",
        )
    rgb_raw = np.asarray(Image.open(frame_path).convert("RGB"))
    tree, mask = extract_tree(rgb_raw)
    rgb = rgb_raw[6:, :-6] if rgb_raw.shape[1] > 6 else rgb_raw
    ship = _ship_xy(rgb)
    # If WorldMap commitment is active, picker heading = bearing toward
    # current target node.  Otherwise just use sim heading.
    picker_heading_deg = sim_heading_deg
    target_event = None
    target_node_id = None
    if use_world_commit and world_map is not None and \
            goal_lat is not None and goal_lon is not None:
        # Maintain commitment + handle arrival/transition.
        if world_map.arrived_at_target(sim_lat, sim_lon):
            old = world_map.bot_state.target_node_id
            world_map.bot_state.last_target_id = old
            world_map.bot_state.target_node_id = None
            target_event = "arrived"
        if world_map.bot_state.target_node_id is None:
            picked = world_map.select_target(sim_lat, sim_lon,
                                            goal_lat, goal_lon)
            if picked is not None:
                world_map.bot_state.target_node_id = picked.id
                target_event = "acquired" if target_event is None \
                              else "transition"
        if world_map.bot_state.target_node_id is not None:
            t = world_map.nodes[world_map.bot_state.target_node_id]
            picker_heading_deg = bearing_between_latlon(
                sim_lat, sim_lon, t.lat, t.lon)
            target_node_id = t.id

    heading_vec = bearing_to_image_vec(picker_heading_deg)
    if not tree.nodes:    skip = "no_tree"; dest = None; vis = []
    elif ship is None:    skip = "no_ship"; dest = None; vis = []
    else:
        dest, _, vis = pick_waypoint(tree, ship, heading_vec,
                                     hug_side, lookahead)
        skip = None if dest is not None else "no_waypoint"
    wp_bearing = None
    if dest is not None and ship is not None:
        dy = dest[0] - ship[1]    # ship is (x, y); dest is (y, x)
        dx = dest[1] - ship[0]
        wp_bearing = image_vec_to_bearing(dy, dx)
    if world_map is not None and tree.nodes and ship is not None:
        # Use the REFERENCE tick's real lat/lon as the px-to-world origin,
        # not the sim's drifted lat/lon — the frame's content reflects
        # the real bot's position at capture time, not the sim's.
        # Otherwise the same junction observed across consecutive ticks
        # ends up at different world coords (one per drift step) and
        # the merge layer fragments instead of consolidating.
        world_map.integrate(tree, rt.lat, rt.lon, ship, step,
                            mode="river")
    return SimStep(
        step=step, sim_lat=sim_lat, sim_lon=sim_lon,
        sim_heading_deg=sim_heading_deg, ref_tick=rt.tick,
        waypoint_px=tuple(dest) if dest else None,
        waypoint_bearing_deg=wp_bearing,
        deviation_km=cross_track_km_to_ref(refs, sim_lat, sim_lon),
        visited=vis, skip_reason=skip,
        target_node_id=target_node_id, target_event=target_event,
    )


def render_composite(rgb_raw: np.ndarray, sim_step_rec: SimStep,
                    refs: list[RefTick], hug_side: str, lookahead: int,
                    out_path: Path):
    """Render the ref frame with sim ship pose + waypoint."""
    tree, mask = extract_tree(rgb_raw)
    rgb = rgb_raw[6:, :-6] if rgb_raw.shape[1] > 6 else rgb_raw
    ship = _ship_xy(rgb)
    heading_vec = bearing_to_image_vec(sim_step_rec.sim_heading_deg)
    dest, walked, visited = (None, [], [])
    if tree.nodes and ship is not None:
        dest, walked, visited = pick_waypoint(tree, ship, heading_vec,
                                             hug_side, lookahead)
    render(rgb, mask, tree, ship, heading_vec, dest, walked,
           hug_side, out_path)


# ── Driver ─────────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("region", type=Path,
                   help="path to region JSON (e.g. data/reference/standard_nile.json)")
    ap.add_argument("--max-ticks", type=int, default=800)
    ap.add_argument("--stride", type=int, default=50,
                   help="render composite every N sim ticks")
    ap.add_argument("--hug", choices=("left", "right"), default=HUG_SIDE)
    ap.add_argument("--lookahead", type=int, default=LOOKAHEAD_PX)
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--use-world-commit", action="store_true",
                   help="Use WorldMap-based target commitment (Piece B). "
                        "Default off — Piece A behavior only.")
    ap.add_argument("--loop-closure", action="store_true",
                   help="Each tick, snap sim_lat/sim_lon to nearest ref tick. "
                        "Cheat to isolate picker logic from dead-reckon drift.")
    args = ap.parse_args()

    cfg = json.loads(args.region.read_text())
    session_dir = ROOT / "data" / "sessions" / cfg["session"]
    out_dir = args.out_dir or (session_dir / "centerline_waypoint_sim")
    out_dir.mkdir(parents=True, exist_ok=True)
    composites_dir = out_dir / "composites"
    composites_dir.mkdir(exist_ok=True)
    trace_path = out_dir / "sim_trace.jsonl"

    refs = load_ref(session_dir, cfg["outbound_end_tick"])
    if not refs:
        sys.exit(f"no usable reference ticks in {session_dir}")
    print(f"region        : {cfg['name']}")
    print(f"session       : {session_dir}")
    print(f"refs loaded   : {len(refs)} ticks (1 .. {cfg['outbound_end_tick']})")
    print(f"start         : lat={refs[0].lat:.3f}  lon={refs[0].lon:.3f}")
    print(f"end (target)  : lat={refs[-1].lat:.3f}  lon={refs[-1].lon:.3f}")
    print(f"hug={args.hug}  lookahead={args.lookahead}px  max_ticks={args.max_ticks}\n")

    sim_lat, sim_lon = refs[0].lat, refs[0].lon
    # Initial heading = bearing from start to goal endpoint, not whatever
    # the reference ship happened to be facing at tick 1 (could be sideways
    # to the channel; biases the picker the wrong way).
    sim_heading = bearing_between_latlon(refs[0].lat, refs[0].lon,
                                         refs[-1].lat, refs[-1].lon)
    print(f"initial heading (start→goal bearing): {sim_heading:.1f}°")
    sim_path = [(sim_lat, sim_lon)]
    counters = {"valid": 0, "no_tree": 0, "no_ship": 0, "no_waypoint": 0,
                "frame_missing": 0}
    target_events = {"acquired": 0, "arrived": 0, "transition": 0}
    world_map = WorldMap()

    with trace_path.open("w") as log:
        for step in range(args.max_ticks):
            rec = sim_step(refs, sim_lat, sim_lon, sim_heading,
                          args.hug, args.lookahead, session_dir, step,
                          world_map=world_map,
                          goal_lat=refs[-1].lat, goal_lon=refs[-1].lon,
                          use_world_commit=args.use_world_commit)
            log.write(json.dumps(asdict(rec)) + "\n")
            if rec.skip_reason is None:
                counters["valid"] += 1
                sim_heading = rate_limit_bearing(
                    sim_heading, rec.waypoint_bearing_deg)
            else:
                counters[rec.skip_reason] += 1
            if rec.target_event is not None:
                target_events[rec.target_event] = \
                    target_events.get(rec.target_event, 0) + 1
            # Even on skip, advance by current heading (don't freeze).
            speed = DEFAULT_SPEED_KT
            sim_lat, sim_lon = advance(sim_lat, sim_lon, sim_heading,
                                       speed, DT_HOURS)
            if args.loop_closure:
                # Snap to nearest ref tick: cheat that the bot knows
                # exactly where it is.  Isolates picker logic from
                # dead-reckon drift.
                ri = nearest_ref_idx(refs, sim_lat, sim_lon)
                sim_lat, sim_lon = refs[ri].lat, refs[ri].lon
            sim_path.append((sim_lat, sim_lon))

            if args.stride and step % args.stride == 0:
                frame_path = session_dir / f"tick_{rec.ref_tick:04d}.png"
                if frame_path.exists():
                    rgb_raw = np.asarray(Image.open(frame_path).convert("RGB"))
                    render_composite(rgb_raw, rec, refs, args.hug,
                                    args.lookahead,
                                    composites_dir / f"sim_step_{step:04d}.png")

            if step % 100 == 0 and step > 0:
                print(f"  step {step}/{args.max_ticks}  "
                      f"sim=({sim_lat:.2f},{sim_lon:.2f})  "
                      f"hdg={sim_heading:.0f}°  "
                      f"deviation={rec.deviation_km:.1f}km")

    # ── Final metrics
    end_lat, end_lon = sim_path[-1]
    cos_lat = math.cos(math.radians((end_lat + refs[-1].lat) / 2))
    final_dist_km = math.hypot(
        (end_lat - refs[-1].lat) * KM_PER_DEG_LAT,
        (end_lon - refs[-1].lon) * KM_PER_DEG_LAT * cos_lat,
    )
    deviations = []
    with trace_path.open() as f:
        for line in f:
            d = json.loads(line)
            deviations.append(d["deviation_km"])
    devs = np.array(deviations)
    print(f"\nDone.  steps={args.max_ticks}")
    print(f"  end position   : lat={end_lat:.3f}  lon={end_lon:.3f}")
    print(f"  reference end  : lat={refs[-1].lat:.3f}  lon={refs[-1].lon:.3f}")
    print(f"  final distance : {final_dist_km:.1f} km from ref endpoint")
    print(f"  deviation mean : {devs.mean():.2f} km")
    print(f"  deviation p95  : {np.percentile(devs, 95):.2f} km")
    print(f"  deviation max  : {devs.max():.2f} km")
    print(f"  valid          : {counters['valid']} ({100*counters['valid']/args.max_ticks:.1f}%)")
    print(f"  no_tree        : {counters['no_tree']}")
    print(f"  no_ship        : {counters['no_ship']}")
    print(f"  no_waypoint    : {counters['no_waypoint']}")
    print(f"  frame_missing  : {counters['frame_missing']}")

    # ── WorldMap stats
    junc = [n for n in world_map.nodes.values() if n.kind == "junction"]
    dead = [n for n in world_map.nodes.values() if n.kind == "dead_end"]
    print(f"\nWorldMap (Piece A — observed-not-used):")
    print(f"  nodes total    : {len(world_map.nodes)}  "
          f"(junctions: {len(junc)}, dead-ends: {len(dead)})")
    print(f"  edges total    : {len(world_map.edges)}")
    obs_counts = [n.n_observations for n in world_map.nodes.values()]
    if obs_counts:
        print(f"  obs/node mean  : {np.mean(obs_counts):.1f}  "
              f"max: {max(obs_counts)}  "
              f"(nodes seen >=5 times: {sum(1 for c in obs_counts if c >= 5)})")
    if args.use_world_commit:
        print(f"\nTarget commitment (Piece B):")
        print(f"  acquired       : {target_events['acquired']}")
        print(f"  arrived        : {target_events['arrived']}")
        print(f"  transition     : {target_events['transition']}")

    # ── Trajectory plot
    fig, ax = plt.subplots(figsize=(12, 9))
    rp = np.array([(r.lat, r.lon) for r in refs])
    sp = np.array(sim_path)
    ax.plot(rp[:, 1], rp[:, 0], "b-", lw=2.5, alpha=0.7,
            label=f"reference ({len(refs)} ticks)")
    ax.plot(sp[:, 1], sp[:, 0], "g-", lw=2.0, alpha=0.85,
            label=f"sim ({len(sp)} ticks)")
    ax.scatter([rp[0, 1]], [rp[0, 0]], s=180, c="blue",
              marker="*", edgecolors="black", zorder=6, label="start")
    ax.scatter([rp[-1, 1]], [rp[-1, 0]], s=180, c="orange",
              marker="*", edgecolors="black", zorder=6, label="ref endpoint")
    ax.scatter([sp[-1, 1]], [sp[-1, 0]], s=180, c="red",
              marker="X", edgecolors="black", zorder=6, label="sim endpoint")
    # WorldMap nodes overlay.
    if world_map.nodes:
        jx = [n.lon for n in junc]; jy = [n.lat for n in junc]
        dx = [n.lon for n in dead]; dy = [n.lat for n in dead]
        sizes_j = [12 + 4*n.n_observations for n in junc]
        sizes_d = [12 + 4*n.n_observations for n in dead]
        if jx: ax.scatter(jx, jy, s=sizes_j, c="magenta",
                          edgecolors="black", linewidths=0.6,
                          alpha=0.75, zorder=5,
                          label=f"WorldMap junctions ({len(junc)})")
        if dx: ax.scatter(dx, dy, s=sizes_d, c="cyan",
                          edgecolors="black", linewidths=0.6,
                          alpha=0.75, zorder=5,
                          label=f"WorldMap dead-ends ({len(dead)})")
    ax.invert_yaxis()   # latitude decreases southward — show like a map
    ax.set_xlabel("Longitude  (°E)")
    ax.set_ylabel("Latitude  (°N, south down)")
    ax.set_title(f"Centerline-waypoint sim vs reference  —  "
                 f"final distance = {final_dist_km:.0f} km", fontsize=12)
    ax.legend(loc="best", fontsize=10)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_dir / "trajectory.png", dpi=130, bbox_inches="tight")
    print(f"\nTrajectory plot: {out_dir / 'trajectory.png'}")
    print(f"Sim trace:       {trace_path}")
    print(f"Composites:      {composites_dir}")


if __name__ == "__main__":
    main()
