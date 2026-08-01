"""Real-world region simulator — drives HugShoreGoal against a Region.

Trajectory-only.  No pixel rendering, no ADB.  The bot's perception
each tick is synthesised from the nearest reference tick's sectors
(rotated to the sim's bow), with HUD lat/lon fed directly from the
sim's state.

Architecture mirrors `tools/replay_voyage.py`:
  - Monkey-patch `actions.sea_actions` to no-ops at module import time
    (BEFORE importing the goal), so HugShoreGoal.tick()'s internal
    sail_start/turn_left/etc. calls don't try to drive a phone.
  - Synthesise a stub nav (`SimNav`) per tick from the Region.
  - Apply the goal's chosen action to the sim state via simple
    kinematics (turning rate + forward sail at speed for dt).

What we DO test in this layer:
  - HugShoreGoal's planner / steering / picker / §13.21 commit
  - End-to-end "does the bot's trajectory match the standard path"

What we DON'T test yet:
  - OCR misreads (sim feeds clean lat/lon)
  - Heading-detector failures (sim feeds clean heading)
  - NPC sprites in sectors (sim feeds reference-clean sectors)
  - Anything pixel-level

The noise-injection layer (next iteration) lives on top of this loop:
take the clean SimNav / HUD reads and corrupt them via pluggable
NoiseSource hooks before passing to the goal.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple


# ── Monkey-patch sea_actions BEFORE the goal import ─────────────────────────

import actions.sea_actions as _sea_actions  # noqa: E402


def _make_stub(name: str):
    def stub(*args, **kwargs):
        try:
            from actions.sea_actions import SeaActionResult
            return SeaActionResult(
                ok=True, action=name, detail="sim: tap suppressed",
            )
        except Exception:
            return None
    stub.__name__ = name
    return stub


_STUBBED = ("sail_start", "sail_stop",
            "turn_left", "turn_right",
            "hold_left", "hold_right",
            "is_ship_moving")
for _name in _STUBBED:
    if hasattr(_sea_actions, _name):
        setattr(_sea_actions, _name, _make_stub(_name))
_sea_actions.is_ship_moving = lambda: True   # type: ignore


# Deferred imports — must happen AFTER the monkey-patch above.
from brain import observation as _obs  # noqa: E402
from brain.goals.hug_shore import HugShoreGoal, HugTickResult  # noqa: E402
from vision.navigation_view import SectorReading  # noqa: E402

from sim.region import Region  # noqa: E402


# ── Sim state ───────────────────────────────────────────────────────────────

@dataclass
class SimState:
    lat:         float
    lon:         float
    heading_deg: float
    speed_kt:    float = 20.0


# Game-time per tick.  Empirically calibrated from the Nile reference
# voyage: 30° lat travelled / 786 ticks at ~20 kt = ~0.115 hours per
# tick.  Adjustable per region.
DEFAULT_DT_HOURS = 0.115

# Approx turning rate when the bot says "hold_left:Nms".  Matches the
# live calibration EMA (~120°/sec); see hug_shore _update_rate_estimate.
DEFAULT_RATE_DPS = 120.0


# ── Action parser ───────────────────────────────────────────────────────────

_HOLD_RE = re.compile(r"^hold_(left|right):(\d+)ms")


def parse_action(action: str) -> Tuple[str, float]:
    """Map an action string to (kind, degrees_to_turn).

    Returns:
      ("hold", 0.0)              — no rudder, sail forward
      ("turn", +deg) | (-, deg)  — sign encodes side (left=-, right=+)
      ("stop", 0.0)              — no motion this tick
    """
    if not action:
        return ("hold", 0.0)
    if action in ("stop", "stopped_for_rejection"):
        return ("stop", 0.0)
    if action == "hold":
        return ("hold", 0.0)
    m = _HOLD_RE.match(action)
    if m:
        side = m.group(1)
        ms = int(m.group(2))
        deg = DEFAULT_RATE_DPS * ms / 1000.0
        return ("turn", -deg if side == "left" else +deg)
    return ("hold", 0.0)


# ── Kinematics ──────────────────────────────────────────────────────────────

def propagate(
    state: SimState, action: str, dt_hours: float = DEFAULT_DT_HOURS,
) -> SimState:
    """Apply `action` to `state` and step forward by `dt_hours`.

    Simplifications:
      - Turn happens instantaneously at the start of the tick (we then
        sail forward at the new heading for the full dt).  Good enough
        when individual turns are small (<= 90°); curving paths split
        into multiple sub-steps for big turns would be more accurate
        but adds complexity we don't need yet.
      - No game-time variance from speed/wind — constant dt_hours.
    """
    kind, deg = parse_action(action)
    if kind == "stop":
        return state
    new_heading = (state.heading_deg + deg) % 360.0
    # Forward integration in compass frame:
    #   dlat = speed_kt / 60 * dt_hours * cos(heading)
    #   dlon = speed_kt / 60 * dt_hours * sin(heading) / cos(lat)
    # 1 kt → 1 nm/h → 1/60 deg-lat/h.  Longitude shrinks toward poles.
    rad = math.radians(new_heading)
    nm = state.speed_kt * dt_hours
    dlat = nm * math.cos(rad) / 60.0
    cos_lat = max(math.cos(math.radians(state.lat)), 1e-6)
    dlon = nm * math.sin(rad) / 60.0 / cos_lat
    # Compass convention in this codebase: heading 0=N (+lat), 90=E
    # (+lon).  cos(0)=1 sends us north which is +lat.  But a heading of
    # 180 (south) gives cos(180)=-1 → dlat negative → moving south.
    # Matches `_compute_sectors`' atan2(dx, dy) convention.
    return SimState(
        lat=state.lat + dlat,
        lon=state.lon + dlon,
        heading_deg=new_heading,
        speed_kt=state.speed_kt,
    )


# ── Stub nav implementing the NavigationView protocol ──────────────────────

@dataclass
class SimNav:
    """Minimal NavigationView fed by Region lookup.

    Two perception modes, controlled by `SimNav.from_state(..., perception_mode=)`:

      "reference"  — original sim behaviour.  Sectors synthesised from the
                     nearest reference tick.  `water_mask`, `ship_xy`,
                     `village_overlap` are unset → the live perception
                     code paths (Phase 1 skeleton-tangent, Phase 2 skeleton-
                     bearing, village_overlap rejection-suppressor) are all
                     unreachable.  Fast, clean, but doesn't test perception.

      "live"       — load the saved minimap PNG for the nearest reference
                     tick, paste into a synthetic full-frame, run it through
                     `MinimapNavigationView.from_frame` exactly as the live
                     bot would.  Populates `water_mask`, `ship_xy`,
                     `village_overlap`, `desired_waypoint_bearing_deg` from
                     the real perception output.  This makes the goal's live
                     code path actually execute in sim — so a perception-
                     layer regression (like 2026-06-07's village_overlap-
                     suppressor + Cairo-marker interaction) shows up before
                     a live voyage burns 30 minutes.
    """
    sectors:           Tuple[SectorReading, ...]
    ship_heading_deg:  Optional[float]
    heading_strategy:  Optional[str] = "sail_pair"
    heading_confidence: Optional[float] = 0.95
    # Live-perception fields — populated only in perception_mode="live".
    water_mask:        Any = None
    ship_xy:           Any = None
    targets:           Tuple = ()
    village_overlap:   bool = False
    # Phase 2 per-tick waypoint hint.  "reference" mode populates this
    # from `Region.next_reference_target()`; "live" mode leaves it None
    # so the goal's `_project_desired_waypoint_from_bearing` path runs
    # against `desired_waypoint_bearing_deg` instead.
    desired_waypoint:  Optional[Tuple[float, float]] = None
    desired_waypoint_bearing_deg: Optional[float] = None

    def is_reachable(self, target) -> bool:
        return False

    @classmethod
    def from_reference(cls, state: SimState, region: Region) -> "SimNav":
        """Synthetic perception — sectors from reference voyage's
        recorded sectors (rotated to sim's bow), waypoint from the
        reference path lookahead.  Doesn't exercise the live
        perception code path."""
        return cls(
            sectors=region.sectors_at(
                state.lat, state.lon, state.heading_deg,
            ),
            ship_heading_deg=state.heading_deg,
            desired_waypoint=region.next_reference_target(
                state.lat, state.lon, lookahead_ticks=20,
            ),
        )

    @classmethod
    def from_live(cls, state: SimState, region: Region) -> "SimNav":
        """Live-faithful perception — load the saved minimap PNG for
        the nearest reference tick and run it through the real
        `MinimapNavigationView` pipeline.  Sectors still come from
        Region.sectors_at because the saved frame's sectors reflect
        the reference's bow, not the sim's; everything else
        (water_mask, ship_xy, village_overlap, bearing) is the
        unfiltered live perception output."""
        from vision.minimap_navigation_view import (
            MinimapNavigationView, MINIMAP_CROP,
        )
        from PIL import Image as _PILImage
        ref = region.nearest_ref(state.lat, state.lon)
        frame_path = region.reference_dir / f"tick_{ref.tick:04d}.png"
        water = ship_xy = None
        vo = False
        bearing = None
        if frame_path.exists():
            crop = _PILImage.open(frame_path).convert("RGB")
            full = _PILImage.new("RGB", (2400, 1080), (0, 0, 0))
            full.paste(crop, (MINIMAP_CROP[0], MINIMAP_CROP[1]))
            nav = MinimapNavigationView.from_frame(full)
            water = nav.water_mask
            ship_xy = nav.ship_xy
            vo = nav.village_overlap
            bearing = nav.desired_waypoint_bearing_deg
        return cls(
            sectors=region.sectors_at(
                state.lat, state.lon, state.heading_deg,
            ),
            ship_heading_deg=state.heading_deg,
            water_mask=water,
            ship_xy=ship_xy,
            village_overlap=vo,
            desired_waypoint=None,
            desired_waypoint_bearing_deg=bearing,
        )

    @classmethod
    def from_v11(cls, state: SimState, region: Region) -> "SimNav":
        """V11 perception — load the saved minimap PNG for the nearest
        reference tick, compute the V11 channel mask + V11-derived
        sectors, and let HugShoreGoal drive against those.  Contrasts
        with `from_live` which uses production-derived sectors.

        Key differences from `live`:
          - `sectors` come from V11's mask via `_compute_sectors`,
            NOT from `Region.sectors_at`.  This is the actual closed-
            loop test of V11 on navigation.
          - `water_mask` is V11's channel polygon (no sprite punch-
            holes, channel-only extraction).
          - `desired_waypoint` is left None — let the goal compute the
            waypoint from V11 perception just like live mode would.
        """
        from sim.v11_perception import v11_perception
        from PIL import Image as _PILImage
        ref = region.nearest_ref(state.lat, state.lon)
        frame_path = region.reference_dir / f"tick_{ref.tick:04d}.png"
        if not frame_path.exists():
            return cls.from_reference(state, region)
        crop = _PILImage.open(frame_path).convert("RGB")
        water, ship_xy, sectors = v11_perception(crop, state.heading_deg)
        return cls(
            sectors=sectors,
            ship_heading_deg=state.heading_deg,
            water_mask=water,
            ship_xy=ship_xy,
            desired_waypoint=None,
        )

    @classmethod
    def from_state(
        cls, state: SimState, region: Region,
        perception_mode: str = "reference",
    ) -> "SimNav":
        if perception_mode == "reference":
            return cls.from_reference(state, region)
        if perception_mode == "live":
            return cls.from_live(state, region)
        if perception_mode == "v11":
            return cls.from_v11(state, region)
        raise ValueError(
            f"perception_mode={perception_mode!r} not in "
            "('reference','live','v11')"
        )


def _perceived_heading_from_frame(
    state: "SimState", region: Region,
) -> Optional[float]:
    """Run MinimapNavigationView on the saved reference frame for this
    sim position and return what its heading detector reports.  Used
    only for diagnostics — sim's *tracked* heading is `state.heading_deg`.
    Returns None when the frame is missing or detection fails.
    """
    try:
        from vision.minimap_navigation_view import (
            MinimapNavigationView, MINIMAP_CROP,
        )
        from PIL import Image as _PILImage
        ref = region.nearest_ref(state.lat, state.lon)
        frame_path = region.reference_dir / f"tick_{ref.tick:04d}.png"
        if not frame_path.exists():
            return None
        crop = _PILImage.open(frame_path).convert("RGB")
        full = _PILImage.new("RGB", (2400, 1080), (0, 0, 0))
        full.paste(crop, (MINIMAP_CROP[0], MINIMAP_CROP[1]))
        nav = MinimapNavigationView.from_frame(full)
        return nav.ship_heading_deg
    except Exception:
        return None


# ── Driver ──────────────────────────────────────────────────────────────────

@dataclass
class SimTrace:
    """Per-tick record from a region simulation."""
    tick:         int
    lat:          float
    lon:          float
    heading_deg:  float
    action:       str
    phase:        str
    note:         str
    lyapunov:     dict = field(default_factory=dict)
    # Heading the live perception detected from the (saved) frame this
    # tick — different from `heading_deg` (sim's tracked value).
    # None in reference perception_mode.
    heading_deg_perceived: Optional[float] = None


def run_region_sim(
    region: Region,
    max_ticks: int = 1500,
    initial_heading_deg: float = 180.0,
    side: str = "port",
    junction_picker: str = "tremaux",
    dt_hours: float = DEFAULT_DT_HOURS,
    perception_mode: str = "reference",
    divergence_log_path: Optional[Path] = None,
    symlink_frames_dir: Optional[Path] = None,
    start_ref_tick: int = 1,
) -> List[SimTrace]:
    """Drive HugShoreGoal from the region's start point.

    Returns a per-tick trace.  Stops on goal.is_complete or max_ticks.

    `initial_heading_deg=180` (south) matches the Nile descent.  For
    other regions, pass the appropriate starting bow.
    """
    _obs.reset()
    # Optionally start from a specific reference tick rather than the
    # voyage start.  Useful for skipping post-departure ticks where
    # the ship is still pointing toward the port's facing direction —
    # at tick 1 of the Nile reference the ship faces 283° (WNW) which
    # is a meaningless "channel forward" hint for the upper Nile's
    # N-S geometry.  By tick 4 the ship is pointing south.
    if start_ref_tick > 1:
        seed = next(
            (r for r in region.ref_ticks if r.tick == start_ref_tick),
            None,
        )
        if seed is None:
            seed = region.ref_ticks[0]
        state = SimState(
            lat=seed.lat, lon=seed.lon,
            heading_deg=seed.heading_deg,
            speed_kt=region.speed_at(seed.lat, seed.lon),
        )
    else:
        state = SimState(
            lat=region.start[0], lon=region.start[1],
            heading_deg=initial_heading_deg,
            speed_kt=region.speed_at(region.start[0], region.start[1]),
        )
    # Match the Nile YAML config — point_pursuit is what the live
    # voyages use AND the only driver_mode that runs the picker /
    # pp_waypoint block we override in Phase 2.  Default "vfh" would
    # bypass that path entirely (early-2026-06-07 bug).
    goal = HugShoreGoal(
        side=side, max_ticks=max_ticks, junction_picker=junction_picker,
        driver_mode="point_pursuit",
    )

    div_lines: List[str] = []
    trace: List[SimTrace] = []
    for t in range(max_ticks):
        # Build BOTH perceptions every tick:
        #   • active_nav drives the goal (matches perception_mode)
        #   • shadow_nav is computed for comparison only
        # This lets a single sim run compare what each perception
        # mode produces at the SAME tick on the SAME frame.
        if perception_mode == "live":
            active_nav = SimNav.from_live(state, region)
            shadow_nav = SimNav.from_reference(state, region)
            shadow_mode = "reference"
        else:
            active_nav = SimNav.from_reference(state, region)
            shadow_nav = SimNav.from_live(state, region)
            shadow_mode = "live"
        nav = active_nav

        # Divergence record — captured BEFORE goal.tick() so we
        # log what each perception said this tick, plus the
        # steering deviation we'll compute after the goal runs.
        ref_at_pos = region.nearest_ref(state.lat, state.lon)
        # Symlink the reference frame this sim tick is mapped to into
        # the sim session dir, so `tools/tick_viewer.py` can show the
        # actual minimap pixels alongside the sim's per-tick decision.
        if symlink_frames_dir is not None:
            src = region.reference_dir / f"tick_{ref_at_pos.tick:04d}.png"
            dst = symlink_frames_dir / f"tick_{t+1:04d}.png"
            if src.exists() and not dst.exists():
                try:
                    dst.symlink_to(src.resolve())
                except OSError:
                    pass    # filesystem rejected symlink — silently skip
        ref_wp = region.next_reference_target(
            state.lat, state.lon, lookahead_ticks=20,
        )

        def _project(bearing_deg, lat, lon, deg=0.30):
            if bearing_deg is None or lat is None or lon is None:
                return None
            r = math.radians(bearing_deg)
            cos_lat = max(math.cos(math.radians(lat)), 1e-6)
            return (lat + deg * math.cos(r),
                    lon + deg * math.sin(r) / cos_lat)

        def _percep_snapshot(snav: "SimNav") -> dict:
            wp = snav.desired_waypoint
            if wp is None:
                wp = _project(snav.desired_waypoint_bearing_deg,
                              state.lat, state.lon)
            none_reason = None
            if wp is None:
                if snav.village_overlap:
                    none_reason = "village_overlap=True"
                elif snav.water_mask is None:
                    none_reason = "no water_mask"
                elif snav.ship_xy is None:
                    none_reason = "no ship_xy"
                elif snav.desired_waypoint_bearing_deg is None:
                    none_reason = "skeleton/trace returned None"
            return {
                "waypoint":              list(wp) if wp else None,
                "bearing_deg":           snav.desired_waypoint_bearing_deg,
                "village_overlap":       snav.village_overlap,
                "ship_xy":               (list(snav.ship_xy)
                                          if snav.ship_xy else None),
                "water_mask_px":         (int(snav.water_mask.sum())
                                          if snav.water_mask is not None
                                          else None),
                "none_reason":           none_reason,
            }

        active_snap = _percep_snapshot(active_nav)
        shadow_snap = _percep_snapshot(shadow_nav)

        # Cross-perception waypoint deviation (km) — does live's
        # waypoint agree with reference's at this same position?
        wp_a = active_snap["waypoint"]
        wp_s = shadow_snap["waypoint"]
        wp_dev_km = None
        if wp_a is not None and wp_s is not None:
            _dlat = wp_a[0] - wp_s[0]
            _dlon = (wp_a[1] - wp_s[1]) * math.cos(math.radians(state.lat))
            wp_dev_km = math.hypot(_dlat, _dlon) * 111.0

        # Stash for after-goal logging
        _pending_div = {
            "tick": t + 1,
            "sim_lat": state.lat, "sim_lon": state.lon,
            "sim_heading_deg_pre": state.heading_deg,
            "ref_voyage_at_pos": {
                "ref_tick":    ref_at_pos.tick,
                "ref_lat":     ref_at_pos.lat,
                "ref_lon":     ref_at_pos.lon,
                "ref_heading": ref_at_pos.heading_deg,
                "ref_next_waypoint": list(ref_wp) if ref_wp else None,
            },
            "active_mode":   perception_mode,
            "shadow_mode":   shadow_mode,
            "active":        active_snap,
            "shadow":        shadow_snap,
            "wp_dev_km_active_vs_shadow": wp_dev_km,
        }
        _obs.update(tick=t + 1, timestamp=datetime.now(), nav=nav)
        try:
            goal.set_hud(
                lat=state.lat, lon=state.lon,
                speed_kt=state.speed_kt,
                raw_heading=state.heading_deg,
                rejected=False,
            )
        except TypeError:
            goal.set_hud(lat=state.lat, lon=state.lon)
        result = goal.tick()

        # Perceived heading (from the live perception path) — diagnostic
        # only; the sim still steers on `state.heading_deg`.  Cheap
        # because MinimapNavigationView caches per-frame.
        _percep_hdg = _perceived_heading_from_frame(state, region)
        trace.append(SimTrace(
            tick=t + 1,
            lat=state.lat, lon=state.lon,
            heading_deg=state.heading_deg,
            action=result.action,
            phase=(result.phase.name
                   if hasattr(result.phase, "name") else str(result.phase)),
            note=result.note or "",
            lyapunov=dict(getattr(goal, "_last_lyap", {}) or {}),
            heading_deg_perceived=_percep_hdg,
        ))

        # Steering deviation — what did the goal pick this tick vs
        # what the reference voyage was doing at this same position?
        # Use the LSQ lyap.desired_heading_deg when available
        # (point_pursuit's chosen heading), otherwise fall back to
        # state.heading_deg.
        lyap = dict(getattr(goal, "_last_lyap", {}) or {})
        sim_desired_h = lyap.get("desired_heading_deg")
        ref_h = ref_at_pos.heading_deg
        steering_dev = None
        if sim_desired_h is not None and ref_h is not None:
            d = (sim_desired_h - ref_h) % 360.0
            steering_dev = min(d, 360.0 - d)
        _pending_div["sim_desired_heading_deg"] = sim_desired_h
        _pending_div["steering_dev_deg"] = steering_dev
        _pending_div["action"] = result.action
        _pending_div["phase"]  = (result.phase.name
                                  if hasattr(result.phase, "name")
                                  else str(result.phase))
        div_lines.append(json.dumps(_pending_div))

        state = propagate(state, result.action, dt_hours=dt_hours)

        if getattr(goal, "is_complete", False):
            break

    if divergence_log_path is not None and div_lines:
        divergence_log_path.parent.mkdir(parents=True, exist_ok=True)
        divergence_log_path.write_text("\n".join(div_lines) + "\n")

    return trace
