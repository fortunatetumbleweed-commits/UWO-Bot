"""Closed-loop simulator: drive the full AiNavPipeline against a
reference voyage and see where the bot WOULD go.

Pattern mirrors `tools/sim_centerline_waypoint.py` but routes
everything through the new ai_nav stack:

  - L1 TemplateMatchHeading (with antipodal + physics gates)
  - L2 V11Segmenter
  - L3 HybridPlanner (centerline / shore_hug dispatch)
  - Mission RiverExploreMission (junction graph + Trémaux)
  - L4/L5 no-op

Per sim tick:
  1. Find nearest reference frame by (sim_lat, sim_lon)
  2. Run AiNavPipeline.tick() on that frame
  3. Read planner's turn command (hold_left/right + hold_ms)
  4. Advance sim_heading by command delta
  5. Advance sim_lat/sim_lon by speed_kt × dt in heading direction

This is the validator for "does today's fix prevent the
bend-circling we saw in run #4?" without needing live phone time.

Usage
─────
  python -m tools.sim_ai_nav data/reference/standard_nile.json \\
      --max-ticks 800 --speed-kt 15

Output
──────
  sim_trace.jsonl    — per-tick (sim_lat, sim_lon, sim_heading, ref_tick,
                        cmd_delta, commit_deg, commit_reason, topology,
                        primitive, deviation_km)
  trajectory.png     — sim track vs reference track in lat/lon
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
from PIL import Image
import matplotlib.pyplot as plt

from brain.ai_nav import AiNavPipeline, NavState, PipelineConfig
from brain.ai_nav.layers.planner import HybridPlanner
from brain.ai_nav.mission import RiverExploreMission
from brain.ai_nav.state import CommitDirection, Heading
from brain.ai_nav.vision_input import FileVisionSource, VisionFrame
import time


class SimHeading:
    """Heading layer that returns the sim's authoritative bow direction,
    not the saved frame's.  Tests L2+L3+Mission in isolation from L1's
    perception."""
    name = "sim_heading"
    latency_budget_ms = 0.1

    def __init__(self):
        self.bearing_deg = 180.0

    def estimate(self, frame, prior):
        return Heading(self.bearing_deg, confidence=1.0, source="sim")


KM_PER_NM = 1.852
KM_PER_DEG_LAT = 111.0


@dataclass
class RefTick:
    tick: int
    lat: float
    lon: float


def load_ref(session_dir: Path, outbound_end_tick: int) -> list[RefTick]:
    """Load reference voyage from bot_replay.jsonl."""
    replay = session_dir / "bot_replay.jsonl"
    if not replay.exists():
        raise FileNotFoundError(replay)
    out = []
    for line in replay.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        t = r.get("tick")
        lat = r.get("lat")
        lon = r.get("lon")
        if t is None or lat is None or lon is None or t > outbound_end_tick:
            continue
        out.append(RefTick(tick=int(t), lat=float(lat), lon=float(lon)))
    return out


def nearest_ref_idx(refs: list[RefTick], lat: float, lon: float) -> int:
    return min(
        range(len(refs)),
        key=lambda i: (refs[i].lat - lat) ** 2 + (refs[i].lon - lon) ** 2,
    )


def cross_track_km(refs: list[RefTick], lat: float, lon: float) -> float:
    i = nearest_ref_idx(refs, lat, lon)
    cl = math.cos(math.radians((refs[i].lat + lat) / 2))
    return math.hypot(
        (refs[i].lat - lat) * KM_PER_DEG_LAT,
        (refs[i].lon - lon) * KM_PER_DEG_LAT * cl,
    )


def advance(lat, lon, heading_deg, speed_kt, dt_hours):
    km = speed_kt * dt_hours * KM_PER_NM
    cos_lat = math.cos(math.radians(lat))
    r = math.radians(heading_deg)
    dlat = (km * math.cos(r)) / KM_PER_DEG_LAT       # N = +lat
    dlon = (km * math.sin(r)) / (KM_PER_DEG_LAT * cos_lat)
    return lat + dlat, lon + dlon


class NearestRefSource:
    """VisionSource that returns the reference frame nearest to the
    sim's current (lat, lon).  Wraps FileVisionSource."""
    def __init__(self, refs: list[RefTick], session_dir: Path):
        self.refs = refs
        self._file = FileVisionSource(session_dir)
        self._latlon: tuple[float, float] | None = None
        self._last_ref_tick: int | None = None

    def set_position(self, lat: float, lon: float):
        self._latlon = (lat, lon)

    @property
    def last_ref_tick(self) -> int | None:
        return self._last_ref_tick

    def capture(self, tick: int) -> VisionFrame:
        ri = nearest_ref_idx(self.refs, *self._latlon) if self._latlon else 0
        ref_tick = self.refs[ri].tick
        self._last_ref_tick = ref_tick
        return self._file.capture(ref_tick)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ref_json", type=Path,
                   help="standard_nile.json or similar reference manifest")
    ap.add_argument("--max-ticks", type=int, default=800)
    ap.add_argument("--speed-kt", type=float, default=15.0)
    ap.add_argument("--dt-secs", type=float, default=4.0,
                   help="Per-tick dt for kinematic advance (sec).  Higher "
                        "= faster sim, less granular.")
    ap.add_argument("--side", choices=("port", "starboard"), default="port")
    ap.add_argument("--commit-bearing", type=float, default=180.0)
    ap.add_argument("--out-dir", type=Path,
                   default=Path("/tmp/sim_ai_nav"))
    args = ap.parse_args()

    ref_manifest = json.loads(args.ref_json.read_text())
    session_dir = (
        args.ref_json.parent.parent / "sessions" / ref_manifest["session"]
    )
    refs = load_ref(session_dir, ref_manifest["outbound_end_tick"])
    if not refs:
        raise SystemExit(f"no ref ticks loaded from {session_dir}")
    print(f"loaded {len(refs)} ref ticks from {session_dir}")
    print(f"  start: lat={refs[0].lat:.2f} lon={refs[0].lon:.2f}")
    print(f"    end: lat={refs[-1].lat:.2f} lon={refs[-1].lon:.2f}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    trace_path = args.out_dir / "sim_trace.jsonl"

    source = NearestRefSource(refs, session_dir)
    sim_hdg_layer = SimHeading()
    cfg = PipelineConfig(
        heading=sim_hdg_layer,
        planner=HybridPlanner(side=args.side),
        mission=RiverExploreMission(default_bearing_deg=args.commit_bearing),
    )
    pipe = AiNavPipeline(source=source, config=cfg)

    sim_lat, sim_lon = refs[0].lat, refs[0].lon
    sim_heading = args.commit_bearing
    state = NavState(tick=0, lat=sim_lat, lon=sim_lon)
    state.commit_direction = CommitDirection(
        bearing_deg=args.commit_bearing, reason="sim_init", set_at_tick=0,
    )

    dt_hours = args.dt_secs / 3600.0
    RATE_DPS = 120.0
    sim_track: list[tuple[float, float]] = []

    with trace_path.open("w") as tf:
        for step in range(args.max_ticks):
            source.set_position(sim_lat, sim_lon)
            # Inject sim heading so L1 returns it instead of the saved
            # frame's ship sprite (which is the human pilot's
            # orientation at capture time).
            sim_hdg_layer.bearing_deg = sim_heading
            state = pipe.tick(state)
            # Pipeline writes state.lat/.lon from HUD OCR on the ref
            # frame — overwrite with our sim's authoritative position.
            state.lat, state.lon = sim_lat, sim_lon

            po = state.planner_output
            cmd_delta = 0.0
            if po and po.command and po.hold_ms:
                mag = po.hold_ms * RATE_DPS / 1000.0
                cmd_delta = mag if po.command == "hold_right" else -mag

            # Advance heading + position.
            sim_heading = (sim_heading + cmd_delta) % 360.0
            sim_lat, sim_lon = advance(
                sim_lat, sim_lon, sim_heading,
                args.speed_kt, dt_hours,
            )
            sim_track.append((sim_lat, sim_lon))

            rec = {
                "step": step + 1,
                "ref_tick": source.last_ref_tick,
                "sim_lat": round(sim_lat, 4),
                "sim_lon": round(sim_lon, 4),
                "sim_heading": round(sim_heading, 1),
                "cmd_delta": round(cmd_delta, 1),
                "commit_deg": (round(state.commit_direction.bearing_deg, 1)
                              if state.commit_direction else None),
                "commit_reason": (state.commit_direction.reason
                                 if state.commit_direction else None),
                "topology": (po.topology if po else None),
                "primitive": (po.primitive if po else None),
                "deviation_km": round(cross_track_km(refs, sim_lat, sim_lon), 1),
                "skip_reason": (po.skip_reason if po else None),
            }
            tf.write(json.dumps(rec) + "\n")

            if step % 50 == 0:
                print(f"  step {step:>4}: ref_t{source.last_ref_tick:>3} "
                      f"sim=({sim_lat:.2f},{sim_lon:.2f}) "
                      f"hdg={sim_heading:.0f}° "
                      f"commit={rec['commit_deg']}°({rec['commit_reason']!s:.20}) "
                      f"dev={rec['deviation_km']:.1f}km")

    # Plot trajectory vs reference.
    fig, ax = plt.subplots(figsize=(10, 12))
    ax.plot([r.lon for r in refs], [r.lat for r in refs],
            "C0-", linewidth=2, label="reference (human pilot)")
    ax.plot([p[1] for p in sim_track], [p[0] for p in sim_track],
            "C3-", linewidth=1.5, label="sim (ai_nav)")
    ax.scatter([refs[0].lon], [refs[0].lat], c="g", s=80, zorder=10,
              label="start (Cairo)")
    ax.scatter([refs[-1].lon], [refs[-1].lat], c="r", s=80, zorder=10,
              label="ref end (Y-tip)")
    ax.scatter([sim_track[-1][1]], [sim_track[-1][0]], c="orange",
              s=80, marker="*", zorder=10, label="sim end")
    ax.set_xlabel("lon (°E)"); ax.set_ylabel("lat (°N)")
    ax.set_title(f"sim_ai_nav: {args.max_ticks} ticks @ {args.speed_kt}kt")
    ax.legend(); ax.grid(alpha=0.3); ax.invert_yaxis()
    fig.tight_layout()
    fig.savefig(args.out_dir / "trajectory.png", dpi=110, bbox_inches="tight")
    print(f"\n→ trace: {trace_path}")
    print(f"→ plot:  {args.out_dir / 'trajectory.png'}")
    print(f"final position: ({sim_lat:.2f}, {sim_lon:.2f})")
    print(f"final dev from ref endpoint: "
          f"{cross_track_km(refs, sim_lat, sim_lon):.1f} km")


if __name__ == "__main__":
    main()
