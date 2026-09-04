"""Live runner for the ai_nav pipeline (Phase 1 of the migration plan).

Wraps `brain.ai_nav.AiNavPipeline` + an `ActionLayer` and produces a
session directory in the same layout as `tools/run_centerline_live.py`
— same `tick_NNNN.png` raw minimap crops, same `trace.jsonl` schema —
so `tools/tick_viewer.py` and `tools/diagnose_shore_mask.py` work
without modification.

Usage
─────

  # Live run against the phone, port-side hug, 500 ticks:
  python -m tools.run_ai_nav_live \\
      --max-ticks 500 --side port \\
      --start-lat 30.17 --start-lon 30.54

  # Dry-run replay against a saved session (no ADB, no tap firing):
  python -m tools.run_ai_nav_live \\
      --source file:data/sessions/live_centerline_2026-06-17T11-53-04 \\
      --action noop --no-sail-start --max-ticks 50

The runner runs alongside the legacy `run_centerline_live.py` — both
are kept available so we can A/B compare during the migration.

Layer swapping is exposed via flags (`--smoother`, …) and via
`PipelineConfig` for anything more involved.  See
`docs/ai_nav_architecture.md` §4 for the swap recipes.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import random
import sys
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain.ai_nav import (  # noqa: E402
    ActionLayer, AdbActionLayer, AdbVisionSource, AiNavPipeline,
    FileVisionSource, NavState, NoOpActionLayer, PipelineConfig,
    VisionSource,
)
from brain.ai_nav.layers.planner import ShoreHugPlanner  # noqa: E402
from brain.ai_nav.layers.tactical import (  # noqa: E402
    LookaheadTactical, MoondreamTactical, NoOpTactical,
)
from brain.ai_nav.pipeline import HeadingKalman  # noqa: E402

log = logging.getLogger("run_ai_nav_live")

KM_PER_NM = 1.852
KM_PER_DEG_LAT = 111.0
TICK_SECS_ESTIMATE = 1.0          # rough; refined per-tick by wall clock


# ── Helpers ────────────────────────────────────────────────────────────


def _advance_latlon(lat, lon, heading_deg, speed_kt, dt_hours):
    """Dead-reckon next (lat, lon)."""
    km = speed_kt * dt_hours * KM_PER_NM
    cos_lat = math.cos(math.radians(lat))
    r = math.radians(heading_deg)
    dlat = (km * math.cos(r)) / KM_PER_DEG_LAT
    dlon = (km * math.sin(r)) / (KM_PER_DEG_LAT * cos_lat)
    return lat + dlat, lon + dlon


def _build_note(state: NavState, action_result) -> str:
    parts: list[str] = []
    po = state.planner_output
    if po and po.skip_reason:
        parts.append(f"SKIP:{po.skip_reason}")
    if state.commit_direction:
        cd = state.commit_direction
        parts.append(f"commit={cd.bearing_deg:.0f}°({cd.reason})")
    if state.heading:
        parts.append(
            f"hdg={state.heading.bearing_deg:.0f}°"
            f"@{state.heading.confidence:.2f}"
        )
    if po and po.command:
        parts.append(f"{po.command}@{po.hold_ms}ms")
        if action_result and not action_result.ok:
            parts.append(f"FAIL:{action_result.detail}")
    else:
        parts.append("hold")
    return " ".join(parts)


def _build_trace_record(state: NavState, action_result, frame_path: Path,
                        timings: dict) -> dict:
    """Compose a trace.jsonl entry compatible with tick_viewer.py."""
    snap = state.snapshot()
    # tick_viewer's orange "cmd" arrow renders at (heading + cmd) so cmd
    # must be the *delta* (positive = right, negative = left), not the
    # absolute heading.  Convert from hold_left/hold_right + hold_ms.
    po = state.planner_output
    cmd_delta = 0.0
    if po and po.command and po.hold_ms:
        rate = 120.0   # RATE_DPS — keep in sync with planner.py
        mag = po.hold_ms * rate / 1000.0
        cmd_delta = mag if po.command == "hold_right" else -mag
    snap.update({
        "wall_iso": datetime.now().isoformat(),
        "crop": frame_path.name,
        "note": _build_note(state, action_result),
        "phase": "AI_NAV",
        # tick_viewer-compatible legacy field names.
        "commanded_deg_this": cmd_delta,
        "prev_action": snap.get("action"),
        "timings_ms": timings,
        "action_ok": (action_result.ok if action_result else None),
        "action_detail": (action_result.detail if action_result else ""),
    })
    return snap


def _resolve_source(spec: str) -> VisionSource:
    """`adb` (default) | `file:<path>` for replay."""
    if spec == "adb":
        return AdbVisionSource()
    if spec.startswith("file:"):
        return FileVisionSource(Path(spec[len("file:"):]))
    raise ValueError(f"unknown source spec: {spec!r}")


def _resolve_action_layer(spec: str) -> ActionLayer:
    if spec == "adb":
        return AdbActionLayer()
    if spec == "noop":
        return NoOpActionLayer()
    raise ValueError(f"unknown action layer: {spec!r}")


def _auto_calibrate_ui() -> None:
    """Live UI auto-calibration at startup.

    Updates module-level constants in place — safe because callers
    read them from module scope at call time, not captured at import
    (verified for actions.sea_actions and vision.minimap_navigation_view).

    Sources:
      MINIMAP_CROP L/R:  OmniParser compound-widget bbox (top-right
                         quadrant, height >= 180 px, tallest wins).
                         Y bounds kept from the module default —
                         they're stable per observation.
      wheel arrows:      bright-pixel (r,g,b > 200) centroid in the
                         wheel region y[870..1050] x[0..600], split at
                         the largest x-gap into L and R halves.
      rudder:            x = midpoint of L/R arrows, y kept from default.

    Missing detections fall back to the module defaults with a warning.
    """
    import subprocess
    from io import BytesIO
    import numpy as np
    from PIL import Image
    raw = subprocess.run(["adb", "exec-out", "screencap", "-p"],
                         capture_output=True, check=True).stdout
    img = Image.open(BytesIO(raw))

    # 1. Mini-map via OmniParser
    #
    # The detection and the disc-rim insets moved into
    # `vision.minimap_navigation_view.calibrate_minimap_crop` — this used to be an inline
    # copy that also had to assign the crop into TWO modules by hand, because the value was
    # mirrored in both. There is one value now, so one call sets it everywhere.
    import vision.minimap_navigation_view as mnv
    try:
        from vision.omniparser import parse_fast_cached
        if mnv.calibrate_minimap_crop(parse_fast_cached(img)) is None:
            log.warning("[calibrate] OmniParser found no mini-map compound; "
                        "keeping default %s", mnv.get_minimap_crop())
    except Exception as e:
        log.warning("[calibrate] mini-map calibration failed (%s); "
                    "keeping default %s", e, mnv.get_minimap_crop())

    # 2. Wheel arrows via bright-pixel centroid
    #
    # Scan region tightened to the wheel-disc bbox (x=180..500,
    # y=900..1010).  The prior 0..600 × 870..1050 window caught
    # the "Quick Resupply" banner at the bottom-left corner (x≈0-200)
    # whenever the ship was near a port, pulling the L-arrow centroid
    # 100 px left and mis-calibrating the rudder tap to empty space —
    # observed in ai_nav_2026-07-24T14-04-11 (rudder tapped at (282,715)
    # instead of ~(334,715), ship never engaged).  Known-good arrow
    # positions L=(234,974) R=(434,975) sit comfortably inside the
    # tighter window.
    import actions.sea_actions as sa
    try:
        arr = np.asarray(img.convert("RGB"))
        region = arr[900:1010, 180:500]
        r, g, b = region[..., 0], region[..., 1], region[..., 2]
        white = (r > 200) & (g > 200) & (b > 200)
        ys, xs = np.where(white)
        if len(xs) < 100:
            log.warning("[calibrate] wheel: only %d bright px in region — "
                        "keeping defaults L=%s R=%s rudder=%s",
                        len(xs), sa.ARROW_LEFT_XY, sa.ARROW_RIGHT_XY,
                        sa.RUDDER_TOGGLE_XY)
            return
        order = np.argsort(xs)
        xs_s = xs[order]; ys_s = ys[order]
        diffs = np.diff(xs_s)
        if diffs.max() < 50:
            log.warning("[calibrate] wheel: no clear L/R gap in bright pixels "
                        "— keeping defaults")
            return
        split_i = int(np.argmax(diffs))
        lx = int(xs_s[:split_i + 1].mean()) + 180
        ly = int(ys_s[:split_i + 1].mean()) + 900
        rx = int(xs_s[split_i + 1:].mean()) + 180
        ry = int(ys_s[split_i + 1:].mean()) + 900
        sa.ARROW_LEFT_XY  = (lx, ly)
        sa.ARROW_RIGHT_XY = (rx, ry)
        sa.RUDDER_TOGGLE_XY = ((lx + rx) // 2, sa.RUDDER_TOGGLE_XY[1])
        log.info("[calibrate] wheel L=%s R=%s rudder=%s",
                 sa.ARROW_LEFT_XY, sa.ARROW_RIGHT_XY, sa.RUDDER_TOGGLE_XY)
    except Exception as e:
        log.warning("[calibrate] wheel calibration failed (%s); "
                    "keeping defaults", e)


# ── Main ───────────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-ticks", type=int, default=500)

    # Pipeline configuration
    ap.add_argument("--heading", default="unet",
                   choices=("pca", "template", "cnn", "ensemble", "unet"),
                   help="L1 heading source.  unet = region-parts U-Net "
                        "(ship_parts; DEFAULT — most accurate, resolves "
                        "bow/stern flips, ~40ms).  ensemble = CNN + "
                        "reconstruct-match.  template = rotational match.  "
                        "pca = legacy PCA (known 180° flips on overlays).")
    ap.add_argument("--shadow-cnn", type=Path, default=None,
                   help="Path to a second CNN checkpoint that runs in "
                        "parallel with the primary CNN.  Its raw output "
                        "is logged as shadow_cnn_heading_deg on each "
                        "tick but does NOT affect runtime decisions.  "
                        "Ticks where primary and shadow disagree by "
                        ">30° get tagged with +cnn_shadow_disagree:<N> "
                        "in heading_source for easy grepping later.  "
                        "Only meaningful with --heading cnn.")
    ap.add_argument("--side", choices=("port", "starboard"), default="port")
    ap.add_argument("--planner", default="hug_path",
                   choices=("shore_hug", "centerline", "hybrid",
                            "learned", "hug_path"),
                   help="L3 planner.  hug_path (default) = walks the "
                        "tactical layer's water polyline and picks the "
                        "farthest ship-visible point (needs --tactical "
                        "lookahead — matches the default).  shore_hug = "
                        "legacy shore-walk; centerline = medial-axis "
                        "tree; hybrid = dispatch by skeleton topology; "
                        "learned = NavController checkpoint via "
                        "--learned-checkpoint.")
    ap.add_argument("--learned-checkpoint", type=Path, default=None,
                   help="Path to NavController .pt checkpoint (required "
                        "for --planner learned).")
    ap.add_argument("--pid", action="store_true", default=True,
                   help="Closed-loop PID heading control for --planner "
                        "hug_path (calibrated hold→turn curve inverse + "
                        "PID) instead of the feed-forward hold estimate. "
                        "ON BY DEFAULT (the linear RATE_DPS=120 estimate "
                        "chronically under-turns).  Live-only — use "
                        "--no-pid to disable (e.g. for the sim, whose turn "
                        "model is idealised).")
    ap.add_argument("--no-pid", dest="pid", action="store_false",
                   help="Disable closed-loop PID; use the feed-forward hold "
                        "estimate (needed for the sim).")
    ap.add_argument("--validate-planner", action="store_true",
                   help="Wrap the planner with ValidatedPlanner: reject "
                        "out-of-bounds, on-land, and path-crosses-land "
                        "waypoints and slide toward the ship until they "
                        "pass all three checks.  See "
                        "docs/agentic_navigation_harness.md item #2.")
    ap.add_argument("--shore-walk-px", type=int, default=80)
    ap.add_argument("--shore-safe-px", type=int, default=25)
    ap.add_argument("--lookahead-px", type=int, default=50,
                   help="Centerline lookahead distance in pixels.")
    ap.add_argument("--smoother", action="store_true",
                   help="Enable HeadingKalman smoother on L1.")
    ap.add_argument("--tactical", default="noop",
                   choices=("noop", "moondream", "lookahead",
                            "lookahead_starboard", "loop", "loop_starboard"),
                   help="L4 tactical layer.  moondream calls "
                        "Moondream via Ollama on heartbeat + "
                        "stuck-detector triggers.  lookahead runs "
                        "per-tick skeleton-based commit_direction "
                        "(port hug); lookahead_starboard hugs the "
                        "starboard shore instead.  See "
                        "docs/lookahead_tactical_design.md.")
    ap.add_argument("--tactical-heartbeat", type=float, default=60.0,
                   help="L4 heartbeat interval in seconds (default 60).")
    ap.add_argument("--tactical-no-shore-trigger", type=int, default=5,
                   help="Fire L4 after N consecutive no_shore ticks "
                        "(default 5).")
    ap.add_argument("--tactical-model", default=None,
                   help="Ollama model name for L4 (e.g. moondream, "
                        "llava:7b, qwen2.5vl:7b).  Default: use "
                        "vision.local_vision.get_vision() autodetect "
                        "(currently llava:7b).")

    # Source / action layer
    ap.add_argument("--source", default="adb",
                   help="`adb` (default) or `file:<session-dir>` for replay.")
    ap.add_argument("--action", default="adb", choices=("adb", "noop"),
                   help="adb = fire real taps, noop = log only.")
    ap.add_argument("--no-sail-start", action="store_true")
    ap.add_argument("--force-sail-start", action="store_true",
                   help="Tap the rudder toggle unconditionally on "
                        "startup, bypassing icon detection.  Use when "
                        "icon templates are stale and incorrectly "
                        "report 'ship already moving'.")

    # Strategic
    ap.add_argument("--strategic", default="noop",
                   choices=("noop", "loop_detect"),
                   help="L5 strategic.  noop = nothing; loop_detect "
                        "= LoopDetectorStrategic — fires commit-rotation "
                        "when trajectory has been bouncing in a small "
                        "region for ~50 ticks (catches the bend-circling "
                        "case mission can't see).")
    # Mission
    ap.add_argument("--mission", default="noop",
                   choices=("noop", "go", "explore", "deadend_memory"),
                   help="L5 mission.  noop = no commit updates "
                        "(needs --commit-bearing for direction); "
                        "go = PointToPointMission toward "
                        "--dest-lat/--dest-lon; explore = "
                        "RiverExploreMission using JunctionGraph + "
                        "Trémaux at forks (needs --commit-bearing "
                        "for default direction between junctions); "
                        "deadend_memory = record every tactical "
                        "LOCK dest the ship reaches (plumbing for "
                        "no-revisit; still needs --commit-bearing).")
    ap.add_argument("--dest-lat", type=float, default=None,
                   help="Destination latitude for --mission go.")
    ap.add_argument("--dest-lon", type=float, default=None,
                   help="Destination longitude for --mission go.")
    ap.add_argument("--arrival-km", type=float, default=5.0,
                   help="Arrival radius for --mission go.")
    ap.add_argument("--return-to-lat", type=float, default=None,
                   help="Round-trip: on arrival at --dest-lat/--dest-lon, "
                        "switch the destination to (--return-to-lat, "
                        "--return-to-lon) and keep sailing.  Yields "
                        "under-represented ship-pose directions during "
                        "the turnaround + return leg.")
    ap.add_argument("--return-to-lon", type=float, default=None)
    ap.add_argument("--commit-bearing", type=float, default=None,
                   help="Seed commit_direction to this compass "
                        "bearing on t1.  Useful with --mission noop "
                        "for single-direction voyages without a "
                        "destination.")

    # Position bootstrap + odometry
    ap.add_argument("--start-lat", type=float, default=None)
    ap.add_argument("--start-lon", type=float, default=None)
    ap.add_argument("--speed-kt", type=float, default=10.0)
    ap.add_argument("--dt-secs", type=float, default=None,
                   help="Fixed per-tick dt for dead-reckoning, in "
                        "seconds.  Default: use wall-clock between "
                        "ticks (correct for live runs, collapses to "
                        "microseconds in replay).  Pass ~1.0 for "
                        "replay against legacy sessions.")

    # Breadcrumb / loop closure
    ap.add_argument("--breadcrumb-km", type=float, default=5.0)
    ap.add_argument("--loop-close-km", type=float, default=3.0)
    ap.add_argument("--min-loop-km", type=float, default=50.0)
    ap.add_argument("--exit-on-loop", action="store_true")

    ap.add_argument("--out-dir", type=Path, default=None,
                   help="Session dir.  Default: data/sessions/ai_nav_<ts>/")
    ap.add_argument("--log-level", default="INFO",
                   choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    args = ap.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s | %(levelname)-7s | %(name)s: %(message)s",
    )

    # ── Build pipeline + action layer ──
    source = _resolve_source(args.source)
    actions = _resolve_action_layer(args.action)
    if args.tactical == "moondream":
        tactical = MoondreamTactical(
            side=args.side,
            heartbeat_sec=args.tactical_heartbeat,
            consecutive_no_shore_trigger=args.tactical_no_shore_trigger,
            model_name=args.tactical_model,
        )
    elif args.tactical == "lookahead":
        tactical = LookaheadTactical(hug_side="port",
                                     goal_bearing_deg=args.commit_bearing)
    elif args.tactical == "lookahead_starboard":
        tactical = LookaheadTactical(hug_side="starboard",
                                     goal_bearing_deg=args.commit_bearing)
    elif args.tactical in ("loop", "loop_starboard"):
        from brain.ai_nav.layers.loop_tactical import LoopTactical
        tactical = LoopTactical(
            hug_side="starboard" if args.tactical == "loop_starboard" else "port")
    else:
        tactical = NoOpTactical()
    if args.heading == "unet":
        # Region-parts U-Net (ship_parts) — production heading.
        from brain.ai_nav.layers.heading import ShipPartsHeading
        heading_layer = ShipPartsHeading()
    elif args.heading == "template":
        from brain.ai_nav.layers.heading import TemplateMatchHeading
        heading_layer = TemplateMatchHeading()
    elif args.heading == "cnn":
        from brain.ai_nav.layers.heading import CNNHeading
        heading_layer = CNNHeading(
            shadow_ckpt_path=(str(args.shadow_cnn)
                              if args.shadow_cnn else None))
    elif args.heading == "ensemble":
        # CNN when ship ≥70% visible, ReconstructMatch otherwise.
        # See EnsembleCNNReconstructHeading docstring for the
        # confidence semantics and latency profile.
        from brain.ai_nav.layers.heading import (
            EnsembleCNNReconstructHeading,
        )
        heading_layer = EnsembleCNNReconstructHeading(
            shadow_ckpt_path=(str(args.shadow_cnn)
                              if args.shadow_cnn else None))
    else:
        from brain.ai_nav.layers.heading import PCAHeading
        heading_layer = PCAHeading()
    if args.planner == "learned":
        if args.learned_checkpoint is None:
            raise SystemExit(
                "--planner learned requires --learned-checkpoint <path>"
            )
        from brain.ai_nav.learned.controller import LearnedController
        planner_layer = LearnedController(args.learned_checkpoint)
    elif args.planner == "centerline":
        from brain.ai_nav.layers.planner import CenterlinePlanner
        planner_layer = CenterlinePlanner(
            side=args.side, lookahead_px=args.lookahead_px,
        )
    elif args.planner == "hybrid":
        from brain.ai_nav.layers.planner import HybridPlanner
        planner_layer = HybridPlanner(
            side=args.side,
            walk_px=args.shore_walk_px,
            safe_px=args.shore_safe_px,
            lookahead_px=args.lookahead_px,
        )
    elif args.planner == "hug_path":
        from brain.ai_nav.layers.hug_path_planner import HugPathPlanner
        planner_layer = HugPathPlanner(use_pid=args.pid)
    else:
        planner_layer = ShoreHugPlanner(
            side=args.side,
            walk_px=args.shore_walk_px,
            safe_px=args.shore_safe_px,
        )
    if args.validate_planner:
        from brain.ai_nav.layers.planner_validator import ValidatedPlanner
        planner_layer = ValidatedPlanner(planner_layer)
    if args.mission == "go":
        if args.dest_lat is None or args.dest_lon is None:
            raise SystemExit(
                "--mission go requires --dest-lat and --dest-lon"
            )
        from brain.ai_nav.mission import PointToPointMission
        mission_layer = PointToPointMission(
            dest_lat=args.dest_lat,
            dest_lon=args.dest_lon,
            arrival_km=args.arrival_km,
            return_to_lat=args.return_to_lat,
            return_to_lon=args.return_to_lon,
        )
    elif args.mission == "explore":
        from brain.ai_nav.mission import RiverExploreMission
        default = (args.commit_bearing if args.commit_bearing is not None
                   else 180.0)
        mission_layer = RiverExploreMission(default_bearing_deg=default)
    elif args.mission == "deadend_memory":
        from brain.ai_nav.mission import DeadEndMemoryMission
        mission_layer = DeadEndMemoryMission()
    else:
        from brain.ai_nav.mission import NoOpMission
        mission_layer = NoOpMission()

    if args.strategic == "loop_detect":
        from brain.ai_nav.layers.strategic import LoopDetectorStrategic
        strategic_layer = LoopDetectorStrategic()
    else:
        from brain.ai_nav.layers.strategic import NoOpStrategic
        strategic_layer = NoOpStrategic()

    cfg = PipelineConfig(
        heading=heading_layer,
        planner=planner_layer,
        mission=mission_layer,
        tactical=tactical,
        strategic=strategic_layer,
        heading_smoother=HeadingKalman() if args.smoother else None,
    )
    pipe = AiNavPipeline(source=source, config=cfg)

    # ── Output session dir ──
    sess = args.out_dir or (
        ROOT / "data/sessions" / f"ai_nav_{datetime.now():%Y-%m-%dT%H-%M-%S}"
    )
    sess.mkdir(parents=True, exist_ok=True)
    trace_path = sess / "trace.jsonl"
    log.info("Session dir: %s", sess)
    log.info("Pipeline: L1=%s L2=%s L3=%s Mission=%s L4=%s L5=%s  "
             "smoother=%s  action=%s",
             cfg.heading.name, cfg.segmentation.name, cfg.planner.name,
             cfg.mission.name, cfg.tactical.name, cfg.strategic.name,
             "on" if cfg.heading_smoother else "off",
             actions.name)

    # Live UI auto-calibration: MINIMAP_CROP (OmniParser) + wheel arrows
    # (bright-pixel centroid).  The game UI has drifted mid-session in
    # the past, silently breaking taps and perception; calibrating at
    # startup makes the run robust to whatever position the widgets
    # are in right now.
    if args.source == "adb":
        _auto_calibrate_ui()

    # ── Init state ──
    state = NavState(
        tick=0,
        lat=args.start_lat,
        lon=args.start_lon,
    )
    if args.commit_bearing is not None:
        from brain.ai_nav.state import CommitDirection
        state.commit_direction = CommitDirection(
            bearing_deg=args.commit_bearing,
            reason="cli_flag",
            set_at_tick=0,
        )
        log.info("→ commit_direction seeded to %.0f° (cli flag)",
                 args.commit_bearing)
    breadcrumbs: list[tuple[float, float, float]] = []
    cumulative_km = 0.0
    last_breadcrumb_km = 0.0
    loop_closed = False
    last_tick_ts = time.time()

    # Warm up perception models BEFORE the sail-start tap.  First-tick
    # lazy loads (heading CNN JIT on MPS, family_classifier,
    # minimap_reader, shoreline_reader, obstruction detector) took
    # ~30 s in ai_nav_2026-07-24T12-52-08 — during which the ship was
    # already sailing uncontrolled because the rudder tap fired first.
    #
    # Two warm-up passes cover the two model families:
    #   1. pipe.tick() — L1 heading CNN + L2 mask + L3 planner tree
    #   2. brain.perceive() — family_classifier + minimap_reader +
    #      shoreline_reader + obstruction detector (used by
    #      sea_actions to verify sea state before each tap)
    # Both discard their state.  After the pipe.tick() we reset the
    # tactical heartbeat so the first real tick's tactical layer still
    # fires immediately.
    if args.source == "adb":
        log.info("→ warming up perception models")
        _warm_start = time.time()
        try:
            warm_state = NavState(
                tick=0, lat=args.start_lat, lon=args.start_lon,
            )
            pipe.tick(warm_state)
            if hasattr(pipe.config.tactical, "_last_fire_ts"):
                pipe.config.tactical._last_fire_ts = 0.0
            # The warm-up ran the SHARED tactical on an unseeded warm_state
            # and left its anchor bootstrapped in the default (south)
            # direction; clear it so the real, --commit-bearing-seeded run
            # bootstraps fresh (else it inherits the stale anchor and sails
            # the wrong way — Cairo-north runs went south, 2026-07-30).
            if hasattr(pipe.config.tactical, "reset"):
                pipe.config.tactical.reset()
        except Exception as e:
            log.warning("  pipe.tick warm-up raised %s — continuing", e)
        try:
            from brain.perceive import perceive
            perceive()
        except Exception as e:
            log.warning("  perceive() warm-up raised %s — continuing", e)
        log.info("  warmed up in %.1fs", time.time() - _warm_start)

    if not args.no_sail_start:
        if args.force_sail_start and args.action == "adb":
            # Bypass icon detection — directly tap the rudder toggle.
            # Use when icon templates are stale and incorrectly report
            # "already moving" (sail_start no-op'd 2026-06-27 run).
            from actions.sea_actions import RUDDER_TOGGLE_XY
            from actions.adb_actions import tap
            log.info("→ force tap rudder toggle @ %s", RUDDER_TOGGLE_XY)
            tap(*RUDDER_TOGGLE_XY)
            time.sleep(random.uniform(1.0, 1.5))
            r = "force-tapped"
        else:
            log.info("→ sail_start()")
            r = actions.sail_start()
        log.info("  %s", r)
        time.sleep(random.uniform(1.5, 2.5))

    # ── Tick loop ──
    skip_counts: Counter = Counter()

    try:
        with trace_path.open("w") as trace_f:
            for _ in range(args.max_ticks):
                t0 = time.perf_counter()
                # 1. Pipeline tick
                state = pipe.tick(state)
                t1 = time.perf_counter()

                # 2. Dead-reckon dt — wall-clock between ticks for live,
                # fixed --dt-secs override for replay (saved-frame ticks
                # run in microseconds otherwise).
                wall_now = time.time()
                if args.dt_secs is not None:
                    dt_hours = args.dt_secs / 3600.0
                else:
                    dt_hours = max(0.05, wall_now - last_tick_ts) / 3600.0
                last_tick_ts = wall_now

                hdg_for_dr = (
                    state.heading.bearing_deg
                    if state.heading and state.heading.confidence > 0.05
                    else None
                )
                if (state.lat is not None and state.lon is not None
                        and hdg_for_dr is not None):
                    prev_lat, prev_lon = state.lat, state.lon
                    state.lat, state.lon = _advance_latlon(
                        state.lat, state.lon, hdg_for_dr,
                        args.speed_kt, dt_hours,
                    )
                    cos_lat = math.cos(math.radians((state.lat + prev_lat) / 2))
                    d_km = math.hypot(
                        (state.lat - prev_lat) * KM_PER_DEG_LAT,
                        (state.lon - prev_lon) * KM_PER_DEG_LAT * cos_lat,
                    )
                    cumulative_km += d_km
                    state.cumulative_km = cumulative_km

                # 3. Save the raw minimap crop the pipeline saw.
                frame_path = sess / f"tick_{state.tick:04d}.png"
                if pipe.last_frame is not None:
                    pipe.last_frame.minimap().save(frame_path)

                # 4. Fire the steering command via the ActionLayer.
                action_result = None
                po = state.planner_output
                if po and po.command:
                    action_result = actions.fire(po.command, po.hold_ms)
                else:
                    skip_counts[po.skip_reason or "no_command"] += 1

                # 5. Breadcrumbs + loop closure (only when we have lat/lon).
                if (state.lat is not None and state.lon is not None):
                    if cumulative_km - last_breadcrumb_km >= args.breadcrumb_km:
                        breadcrumbs.append((state.lat, state.lon, cumulative_km))
                        last_breadcrumb_km = cumulative_km
                    if (not loop_closed and breadcrumbs
                            and cumulative_km >= args.min_loop_km):
                        for bc_lat, bc_lon, bc_km in breadcrumbs:
                            if cumulative_km - bc_km < args.min_loop_km:
                                continue
                            cos_lat = math.cos(math.radians(
                                (state.lat + bc_lat) / 2))
                            d = math.hypot(
                                (state.lat - bc_lat) * KM_PER_DEG_LAT,
                                (state.lon - bc_lon) * KM_PER_DEG_LAT * cos_lat,
                            )
                            if d < args.loop_close_km:
                                loop_closed = True
                                log.warning(
                                    "[LOOP CLOSED at tick %d] returned to "
                                    "breadcrumb (%.3f, %.3f) after %.1f km",
                                    state.tick, bc_lat, bc_lon,
                                    cumulative_km - bc_km,
                                )
                                break

                # 6. Trace entry.
                timings = {
                    "tick_total_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "pipeline_ms": round((t1 - t0) * 1000, 1),
                }
                if po and hasattr(po, "timings_ms"):
                    timings.update(po.timings_ms)
                rec = _build_trace_record(state, action_result, frame_path, timings)
                trace_f.write(json.dumps(rec) + "\n")
                trace_f.flush()

                # 7. Optional: stop on loop closure.
                if loop_closed and args.exit_on_loop:
                    log.info("[exit: loop closure + --exit-on-loop]")
                    break
    finally:
        if not args.no_sail_start:
            log.info("→ sail_stop()")
            log.info("  %s", actions.sail_stop())

    # ── Summary ──
    log.info("")
    log.info("Ran %d ticks.  Cumulative: %.1f km.", state.tick, cumulative_km)
    log.info("Skip counts: %s", dict(skip_counts))
    log.info("Trace:  %s", trace_path)
    log.info("Crops:  %s/tick_*.png", sess)


if __name__ == "__main__":
    main()
