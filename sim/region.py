"""Real-world region backed by a captured reference voyage.

The simulator-as-world-lookup approach: a `Region` IS a reference
voyage's trace.  At any (lat, lon, heading), we synthesise the bot's
perception by:

  1. Finding the nearest reference tick by (lat, lon)
  2. Reading that tick's 16 bow-relative sectors + heading
  3. Rotating the sectors into the simulator's bow frame

This works for any simulator state within range of the reference path.
When the simulator wanders far from the path, sector readings become
stale — that's actually the signal we want (the bot is going where the
human never went; either the bot is wrong or we need more reference
data).

Why this approach for the first iteration:
  - No pixel rendering, no calibration step, no image-space stitching.
  - Bow-relative → world-frame rotation is a clean discrete shift
    (16 sectors, 22.5° each).
  - One Region = one reference voyage; adding the Amazon or Baltic
    later is just "capture another voyage and load it here."

See `memory/project_simulator_real_region_idea.md` for the design
context and `data/reference/standard_nile.json` for the canonical Nile
reference.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from vision.navigation_view import SECTOR_COUNT, SECTOR_WIDTH_DEG, SectorReading


@dataclass(frozen=True)
class RefTick:
    """One tick of reference-voyage perception, as needed by the sim."""
    tick:        int
    lat:         float
    lon:         float
    heading_deg: float
    speed_kt:    float
    # Bow-relative 16-sector land_fractions and nearest distances.
    # Same convention as `vision.navigation_view.SectorReading`.
    sector_land_fractions: Tuple[float, ...]
    sector_nearest_dists:  Tuple[Optional[float], ...]


@dataclass
class Region:
    """Real-world region defined by a captured reference voyage."""
    name:           str
    reference_dir:  Path
    standard_path:  List[Tuple[float, float, int]]   # (lat, lon, tick)
    ref_ticks:      List[RefTick]
    start:          Tuple[float, float]              # initial (lat, lon)
    end:            Tuple[float, float]              # destination (lat, lon)
    outbound_end_tick: int

    # ── Construction ──

    @classmethod
    def from_config(cls, config_path: Path) -> "Region":
        """Load a region from `data/reference/<name>.json`."""
        cfg = json.loads(config_path.read_text())
        session_dir = (Path(__file__).parent.parent / "data" / "sessions"
                       / cfg["session"])
        ref_ticks, standard_path = _load_reference_voyage(
            session_dir, outbound_end_tick=cfg["outbound_end_tick"],
        )
        if not ref_ticks:
            raise SystemExit(
                f"no usable reference ticks in {session_dir}"
            )
        start = (ref_ticks[0].lat, ref_ticks[0].lon)
        end = (ref_ticks[-1].lat, ref_ticks[-1].lon)
        return cls(
            name=cfg["name"],
            reference_dir=session_dir,
            standard_path=standard_path,
            ref_ticks=ref_ticks,
            start=start,
            end=end,
            outbound_end_tick=cfg["outbound_end_tick"],
        )

    # ── Lookups ──

    def nearest_ref(self, lat: float, lon: float) -> RefTick:
        """Nearest reference tick by Euclidean distance in (lat, lon).

        Linear scan is fine for ~1000 ticks per region.  Swap for a
        KD-tree if/when we add many regions.
        """
        best = self.ref_ticks[0]
        best_d2 = (best.lat - lat) ** 2 + (best.lon - lon) ** 2
        for r in self.ref_ticks[1:]:
            d2 = (r.lat - lat) ** 2 + (r.lon - lon) ** 2
            if d2 < best_d2:
                best_d2 = d2
                best = r
        return best

    def sectors_at(
        self,
        lat: float, lon: float, sim_heading_deg: float,
    ) -> Tuple[SectorReading, ...]:
        """Synthesise the 16 bow-relative sectors at (lat, lon) facing
        `sim_heading_deg`.  Uses the nearest reference tick's sectors,
        rotated by the heading delta between sim and reference.
        """
        ref = self.nearest_ref(lat, lon)
        # The bow-relative sector at index i in `ref` is at world bearing
        # ref.heading_deg + i*22.5°.  For the sim with heading
        # sim_heading_deg, the bow-relative sector at index j is at
        # world bearing sim_heading_deg + j*22.5°.  So sim's j-sector
        # equals ref's (j + k_offset) sector where:
        #     k_offset = round((sim_heading_deg - ref.heading_deg) / 22.5)
        # This is a discrete shift; sub-22.5° heading differences are
        # snapped to the nearest sector boundary.
        delta = (sim_heading_deg - ref.heading_deg) % 360.0
        k_offset = int(round(delta / SECTOR_WIDTH_DEG)) % SECTOR_COUNT
        out: List[SectorReading] = []
        for j in range(SECTOR_COUNT):
            src = (j + k_offset) % SECTOR_COUNT
            out.append(SectorReading(
                bearing_deg=j * SECTOR_WIDTH_DEG,
                land_fraction=ref.sector_land_fractions[src],
                nearest_dist=ref.sector_nearest_dists[src],
                is_observed=True,
            ))
        return tuple(out)

    def speed_at(self, lat: float, lon: float) -> float:
        """Reference speed (knots) at the nearest tick.  Falls back to
        20 kt when the reference didn't record one."""
        ref = self.nearest_ref(lat, lon)
        return ref.speed_kt if ref.speed_kt > 0 else 20.0

    def next_reference_target(
        self,
        lat: float, lon: float,
        lookahead_ticks: int = 20,
    ) -> Optional[Tuple[float, float]]:
        """Lookup the reference (lat, lon) `lookahead_ticks` ahead of
        the bot's current position.  Stand-in for live-bot skeleton
        tracing — answers "where on the channel should I head next?"
        each tick from current state.

        Method:
          1. Find the index of the nearest reference tick to current pos
          2. Advance by `lookahead_ticks` along the reference's recorded order
          3. Return that reference tick's (lat, lon)

        Returns None when current position is past the reference's end.
        """
        # Find nearest reference index by Euclidean distance.
        best_i = 0
        best_d2 = (self.ref_ticks[0].lat - lat) ** 2 \
                  + (self.ref_ticks[0].lon - lon) ** 2
        for i, r in enumerate(self.ref_ticks[1:], start=1):
            d2 = (r.lat - lat) ** 2 + (r.lon - lon) ** 2
            if d2 < best_d2:
                best_d2 = d2
                best_i = i
        target_i = best_i + lookahead_ticks
        if target_i >= len(self.ref_ticks):
            return None
        r = self.ref_ticks[target_i]
        return (r.lat, r.lon)


# ── Trace loading ───────────────────────────────────────────────────────────

def _load_corrections(corrections_path: Path) -> dict:
    """Load the sidecar `corrected_latlon.jsonl` if present.
    Returns {tick: (lat, lon)}.  Format defined by
    `tools/correct_reference_path.py`."""
    if not corrections_path.exists():
        return {}
    out: dict = {}
    for line in corrections_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            out[int(d["tick"])] = (float(d["lat"]), float(d["lon"]))
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            pass
    return out


def _load_reference_voyage(
    session_dir: Path,
    outbound_end_tick: int,
) -> Tuple[List[RefTick], List[Tuple[float, float, int]]]:
    """Read trace.jsonl, extract per-tick sector + heading + position.

    Applies corrections from sibling `corrected_latlon.jsonl` when
    present, so the simulator's notion of "the standard path" uses
    curated ground-truth lat/lon for any tick whose raw OCR was bad.
    Raw `trace.jsonl` is not modified.

    Filters to:
      - tick <= outbound_end_tick (drops the human's return leg)
      - records with lat, lon, heading_deg, and nav.sectors present
    """
    trace_path = session_dir / "trace.jsonl"
    if not trace_path.exists():
        raise SystemExit(f"trace.jsonl missing in {session_dir}")
    corrections = _load_corrections(session_dir / "corrected_latlon.jsonl")
    out: List[RefTick] = []
    path_points: List[Tuple[float, float, int]] = []
    for line in trace_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        tick = rec.get("tick")
        if tick is None or tick > outbound_end_tick:
            continue
        lat = rec.get("lat")
        lon = rec.get("lon")
        heading = rec.get("heading_deg")
        nav = rec.get("nav") or {}
        sectors = nav.get("sectors") or []
        if (lat is None or lon is None or heading is None
                or len(sectors) != SECTOR_COUNT):
            continue
        # Sidecar correction takes precedence over raw OCR.
        if int(tick) in corrections:
            lat, lon = corrections[int(tick)]
        fracs = tuple(float(s.get("frac", 0.0)) for s in sectors)
        dists = tuple(
            float(s["dist"]) if s.get("dist") is not None else None
            for s in sectors
        )
        out.append(RefTick(
            tick=int(tick), lat=float(lat), lon=float(lon),
            heading_deg=float(heading),
            speed_kt=float(rec.get("speed_kt") or 20.0),
            sector_land_fractions=fracs,
            sector_nearest_dists=dists,
        ))
        path_points.append((float(lat), float(lon), int(tick)))
    return out, path_points
