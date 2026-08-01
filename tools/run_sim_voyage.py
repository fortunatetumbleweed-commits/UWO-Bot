"""Run a simulated voyage against the stitched Nile canvas.

Uses the same AiNavPipeline as the live runner, but with:
  - `SimHeading`         instead of the heading CNN
  - `SimSegmentation`    instead of V11 brightness
  - `SimulatedNileSource` instead of ADB screencap
  - sim's lat / lon / speed injected into state each tick (no HUD OCR)

Physics + rendering live in `sim/nile_sim.py`.

Output layout matches a live session (`trace.jsonl` + `tick_NNNN.png`)
so `tools.tick_viewer` and the eval tools work unchanged.

Usage:
    python -m tools.run_sim_voyage --start-lat 30.13 --start-lon 30.58 \\
        --start-heading 180 --max-ticks 700
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

from brain.ai_nav.layers.planner import HybridPlanner
from brain.ai_nav.layers.planner_validator import ValidatedPlanner
from brain.ai_nav.mission import RiverExploreMission
from brain.ai_nav.pipeline import AiNavPipeline, PipelineConfig
from brain.ai_nav.state import CommitDirection, NavState
from sim.nile_sim import (
    NileSim, SimulatedNileSource, SimHeading, SimSegmentation, SimState,
)


ROOT = Path(__file__).parent.parent
log = logging.getLogger("run_sim_voyage")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--canvas-dir", type=Path,
                    default=ROOT / "data/nile_canvas")
    ap.add_argument("--start-lat", type=float, default=30.13)
    ap.add_argument("--start-lon", type=float, default=30.58)
    ap.add_argument("--start-heading", type=float, default=180.0)
    ap.add_argument("--commit-bearing", type=float, default=180.0)
    ap.add_argument("--cruise-kt", type=float, default=8.0)
    ap.add_argument("--dt-s", type=float, default=1.0,
                    help="Game-time seconds per tick.  1.0 → ~0.048°/tick "
                         "at 8kt (safe in narrow channels); 1.3 matches "
                         "live pace (0.063°/tick) but overshoots the "
                         "Y-tip isthmus.")
    ap.add_argument("--max-ticks", type=int, default=700)
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--rng-seed", type=int, default=42)
    ap.add_argument("--planner", choices=("hybrid", "validated", "hug_path"),
                    default="validated")
    ap.add_argument("--heading", default="ground_truth",
                    choices=("ground_truth", "cnn", "pca", "ensemble"),
                    help="ground_truth = SimHeading (perfect); "
                         "cnn/pca/ensemble = real production layers "
                         "run against the composited sprite.  Ground "
                         "truth is always logged as shadow_bearing_deg "
                         "so tick_viewer shows both arrows.")
    ap.add_argument("--tactical", default="noop",
                    choices=("noop", "lookahead", "lookahead_starboard"),
                    help="L4 tactical.  lookahead runs per-tick "
                         "skeleton-based commit_direction (see "
                         "docs/lookahead_tactical_design.md).")
    ap.add_argument("--mission", default="river_explore",
                    choices=("river_explore", "deadend_memory", "noop"),
                    help="Mission layer.  river_explore = default "
                         "junction-graph exploration; deadend_memory = "
                         "record LOCK'd dead-ends the ship reaches (paired "
                         "with LookaheadTactical's no-revisit rule).")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s | %(levelname)-7s | %(name)s: %(message)s",
    )

    sim = NileSim(
        args.canvas_dir,
        SimState(lat=args.start_lat, lon=args.start_lon,
                 heading_deg=args.start_heading,
                 speed_kt=args.cruise_kt, cruise_kt=args.cruise_kt),
        dt_s=args.dt_s,
        rng_seed=args.rng_seed,
    )
    source = SimulatedNileSource(sim)

    planner = HybridPlanner()
    if args.planner == "validated":
        planner = ValidatedPlanner(planner)
    elif args.planner == "hug_path":
        from brain.ai_nav.layers.hug_path_planner import HugPathPlanner
        planner = HugPathPlanner()
    if args.mission == "deadend_memory":
        from brain.ai_nav.mission import DeadEndMemoryMission
        mission = DeadEndMemoryMission()
    elif args.mission == "noop":
        from brain.ai_nav.mission import NoOpMission
        mission = NoOpMission()
    else:
        mission = RiverExploreMission(default_bearing_deg=args.commit_bearing)

    if args.heading == "ground_truth":
        heading_layer = SimHeading(sim)
    elif args.heading == "cnn":
        from brain.ai_nav.layers.heading import CNNHeading
        heading_layer = CNNHeading()
    elif args.heading == "ensemble":
        from brain.ai_nav.layers.heading import EnsembleCNNReconstructHeading
        heading_layer = EnsembleCNNReconstructHeading()
    else:  # pca
        from brain.ai_nav.layers.heading import PCAHeading
        heading_layer = PCAHeading()
    log.info("Heading layer: %s", heading_layer.name)

    if args.tactical == "lookahead":
        from brain.ai_nav.layers.tactical import LookaheadTactical
        tactical = LookaheadTactical(hug_side="port",
                                     goal_bearing_deg=args.commit_bearing)
    elif args.tactical == "lookahead_starboard":
        from brain.ai_nav.layers.tactical import LookaheadTactical
        tactical = LookaheadTactical(hug_side="starboard",
                                     goal_bearing_deg=args.commit_bearing)
    else:
        from brain.ai_nav.layers.tactical import NoOpTactical
        tactical = NoOpTactical()

    cfg = PipelineConfig(
        heading=heading_layer,
        segmentation=SimSegmentation(sim),
        planner=planner,
        mission=mission,
        tactical=tactical,
    )
    pipe = AiNavPipeline(source=source, config=cfg)

    out = args.out_dir or (
        ROOT / "data/sessions" / f"sim_{datetime.now():%Y-%m-%dT%H-%M-%S}"
    )
    out.mkdir(parents=True, exist_ok=True)
    trace_path = out / "trace.jsonl"

    # Seed commit_direction (planner otherwise starts with none)
    state = NavState(
        tick=0, lat=args.start_lat, lon=args.start_lon,
        commit_direction=CommitDirection(
            bearing_deg=args.commit_bearing,
            reason="cli_flag", set_at_tick=0,
        ),
    )

    log.info("Sim voyage → %s", out)
    log.info("Start: (%.2f, %.2f) hdg=%.0f°  cruise=%.1f kt  dt=%.1fs",
             args.start_lat, args.start_lon, args.start_heading,
             args.cruise_kt, args.dt_s)

    bounces = 0
    with open(trace_path, "w") as tf:
        for tick in range(1, args.max_ticks + 1):
            # 1. Inject sim ground truth into state before pipeline runs
            state.lat = sim.state.lat
            state.lon = sim.state.lon
            state.speed_kt = sim.state.speed_kt

            # 2. Run pipeline — it captures its own frame via SimulatedNileSource
            state = pipe.tick(state)

            # Log sim ground truth as shadow so tick_viewer shows both arrows.
            # Only meaningful when heading layer is NOT ground_truth.
            if args.heading != "ground_truth" and state.heading is not None:
                state.heading = state.heading.__class__(
                    bearing_deg=state.heading.bearing_deg,
                    confidence=state.heading.confidence,
                    source=state.heading.source + "+sim_gt_shadow",
                    raw_bearing_deg=state.heading.raw_bearing_deg,
                    shadow_bearing_deg=sim.state.heading_deg,
                    shadow_confidence=1.0,
                )

            # 3. Save mini-map crop (viewer-compatible)
            sim.render_minimap().save(out / f"tick_{tick:04d}.png")

            # 4. Apply the reflex layer's action to the sim
            po = state.planner_output
            action = po.command if po else None
            hold_ms = po.hold_ms if po else 0
            sim.apply_action(action, hold_ms)
            if sim.state.bounced_this_tick:
                bounces += 1

            # 5. Trace record (compatible with tick_viewer)
            snap = state.snapshot()
            snap.update({
                "wall_iso": datetime.now().isoformat(),
                "crop": f"tick_{tick:04d}.png",
                "phase": "SIM",
                "sim_bounced": sim.state.bounced_this_tick,
            })
            tf.write(json.dumps(snap, default=float) + "\n")

            if tick % 50 == 0:
                log.info("  t%d  lat=%.3f lon=%.3f hdg=%.0f° "
                         "spd=%.1f  bounces=%d",
                         tick, sim.state.lat, sim.state.lon,
                         sim.state.heading_deg, sim.state.speed_kt, bounces)

    log.info("Done.  %d ticks, %d bounces, ended at "
             "(%.3f, %.3f)  hdg=%.0f°  cruise=%.1f kt",
             args.max_ticks, bounces, sim.state.lat, sim.state.lon,
             sim.state.heading_deg, sim.state.cruise_kt)
    log.info("Trace: %s", trace_path)


if __name__ == "__main__":
    main()
