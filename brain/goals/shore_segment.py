"""§13.23 Phase A — Shore segment commitment state machine.

Maintains a world-frame identity for the shore the bot is currently
hugging.  Decouples "which physical wall am I following" from the
per-tick LSQ output, which fails at Y/T junctions and inlet mouths
because the 3 sparse port-side samples come from different walls.

See `docs/shore_segment_commitment_design.md` for design rationale,
tuning constants, and the failure-mode mapping against
hug_debug_20260601_164805 t338→t341.

Phase A interface:

    decision = evaluate_segment(memory, nav, side, heading, dest, tick)

`decision` is one of:

    SegmentDecision(action="act",     theta_err_override=..., memory=...)
    SegmentDecision(action="hold",    memory=...)
    SegmentDecision(action="no_data", memory=memory)   # shore not visible

The caller stores `decision.memory` for the next tick.  On "act", the
override theta_err (in degrees, body-frame, signed) replaces the
fresh LSQ value for downstream waypoint generation.  On "hold", the
caller skips the steering policy this tick (Phase A = stall; Phase B
will escalate to LLM consult).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Literal, Optional

Side = Literal["port", "starboard"]


# Tuning (see design doc §"Tuning constants").
ASSOC_THRESH_DEG     = 30.0    # world-frame Δ to consider "same segment"
RECOMMIT_AFTER_MISSES = 2       # consecutive disagreements before re-acquire
EWMA_ALPHA           = 0.3     # fresh tangent's weight in memory blend
HEALTHY_FRAC_MIN     = 0.5     # sample frac threshold for "commit-worthy"


@dataclass(frozen=True)
class ShoreSegmentMemory:
    """World-frame identity for the shore the bot is following.

    `tangent_world_deg` is the compass bearing of the shore line, already
    disambiguated against the destination LOS at commit time.  Memory
    survives heading-detector rejections and body-frame rotations — a
    180° bow flip does not change the segment's identity.
    """
    tangent_world_deg:  float
    anchor_lat:         float
    anchor_lon:         float
    committed_tick:     int
    last_update_tick:   int
    side_at_commit:     Side
    consecutive_misses: int = 0


@dataclass(frozen=True)
class SegmentDecision:
    action:             Literal["act", "hold", "no_data"]
    memory:             Optional[ShoreSegmentMemory]
    # Populated when action == "act":
    theta_err_override: Optional[float] = None
    # Always populated (for trace / debug):
    note:               str = ""
    fresh_tangent_world_deg: Optional[float] = None


def _angle_diff_deg(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def _angle_blend(a: float, b: float, alpha: float) -> float:
    """Weighted compass-angle average. alpha is the weight of `b`."""
    da = ((b - a + 180.0) % 360.0) - 180.0
    return (a + alpha * da) % 360.0


def _is_clean_fit(samples_frac: list[float]) -> bool:
    """Phase A commit criterion: at least 2 of the 3 port-side samples
    have land_fraction ≥ HEALTHY_FRAC_MIN.  This is intentionally loose —
    refining it deferred to Phase B (LLM resolves the rest of the
    ambiguity at HOLD moments)."""
    high = sum(1 for f in samples_frac if f >= HEALTHY_FRAC_MIN)
    return high >= 2


def evaluate_segment(
    memory: Optional[ShoreSegmentMemory],
    fresh_theta_err_deg: Optional[float],
    ship_heading_deg: Optional[float],
    samples_frac: list[float],
    current_lat: Optional[float],
    current_lon: Optional[float],
    endpoint_lat: Optional[float],
    endpoint_lon: Optional[float],
    side: Side,
    current_tick: int,
) -> SegmentDecision:
    """§13.23 state machine.  See module docstring for interface.

    `samples_frac` is the list of land_fraction values for the three
    port-side LSQ samples (sec 14, 12, 10), in any order.  Used only
    for the cleanness check.
    """
    # Bootstrap or no-data short-circuits.
    if (fresh_theta_err_deg is None
            or ship_heading_deg is None
            or current_lat is None
            or current_lon is None):
        return SegmentDecision(
            action="no_data", memory=memory,
            note="missing pose or fresh LSQ",
        )

    # Fresh world-frame tangent, then destination-anchor disambiguation
    # (mirrors §13.21 in _extract_shore_line — we need apples-to-apples
    # with what memory stored).
    fresh_tw = (ship_heading_deg + fresh_theta_err_deg) % 360.0
    if endpoint_lat is not None and endpoint_lon is not None:
        los = _bearing_compass(current_lat, current_lon,
                                endpoint_lat, endpoint_lon)
        if los is not None:
            alt = (fresh_tw + 180.0) % 360.0
            if _angle_diff_deg(alt, los) < _angle_diff_deg(fresh_tw, los):
                fresh_tw = alt

    clean = _is_clean_fit(samples_frac)

    # Bootstrap — no memory yet.
    if memory is None:
        if clean:
            new_mem = ShoreSegmentMemory(
                tangent_world_deg=fresh_tw,
                anchor_lat=current_lat, anchor_lon=current_lon,
                committed_tick=current_tick,
                last_update_tick=current_tick,
                side_at_commit=side,
                consecutive_misses=0,
            )
            return SegmentDecision(
                action="act", memory=new_mem,
                theta_err_override=fresh_theta_err_deg,
                fresh_tangent_world_deg=fresh_tw,
                note=f"init commit at {fresh_tw:.0f}°",
            )
        # No commit yet, but no choice — bootstrap with fresh.
        return SegmentDecision(
            action="act", memory=None,
            theta_err_override=fresh_theta_err_deg,
            fresh_tangent_world_deg=fresh_tw,
            note="bootstrap (no commit)",
        )

    # Have memory — compare.
    delta = _angle_diff_deg(fresh_tw, memory.tangent_world_deg)

    if delta < ASSOC_THRESH_DEG:
        # Same segment — EWMA update.
        new_tangent = _angle_blend(
            memory.tangent_world_deg, fresh_tw, EWMA_ALPHA)
        new_mem = replace(
            memory,
            tangent_world_deg=new_tangent,
            last_update_tick=current_tick,
            consecutive_misses=0,
        )
        # Override theta_err to drive downstream with the smoothed
        # world-frame tangent.
        override = _signed_deg(new_tangent - ship_heading_deg)
        return SegmentDecision(
            action="act", memory=new_mem,
            theta_err_override=override,
            fresh_tangent_world_deg=fresh_tw,
            note=(f"EWMA mem={new_tangent:.0f}° "
                  f"fresh={fresh_tw:.0f}° Δ={delta:.0f}°"),
        )

    # Disagreement.
    new_misses = memory.consecutive_misses + 1
    if clean and new_misses >= RECOMMIT_AFTER_MISSES:
        new_mem = ShoreSegmentMemory(
            tangent_world_deg=fresh_tw,
            anchor_lat=current_lat, anchor_lon=current_lon,
            committed_tick=current_tick,
            last_update_tick=current_tick,
            side_at_commit=side,
            consecutive_misses=0,
        )
        return SegmentDecision(
            action="act", memory=new_mem,
            theta_err_override=fresh_theta_err_deg,
            fresh_tangent_world_deg=fresh_tw,
            note=(f"recommit at {fresh_tw:.0f}° after "
                  f"{new_misses} misses (mem was "
                  f"{memory.tangent_world_deg:.0f}°)"),
        )

    # HOLD — fresh disagrees and either isn't clean OR we haven't yet
    # seen enough consecutive disagreements to re-acquire.
    new_mem = replace(memory, consecutive_misses=new_misses)
    return SegmentDecision(
        action="hold", memory=new_mem,
        fresh_tangent_world_deg=fresh_tw,
        note=(f"HOLD mem={memory.tangent_world_deg:.0f}° "
              f"fresh={fresh_tw:.0f}° Δ={delta:.0f}° "
              f"clean={clean} misses={new_misses}"),
    )


def _signed_deg(deg: float) -> float:
    """Wrap to [-180, 180]."""
    return ((deg + 180.0) % 360.0) - 180.0


def _bearing_compass(lat0: float, lon0: float, lat1: float, lon1: float
                      ) -> Optional[float]:
    dlat = lat1 - lat0
    dlon = lon1 - lon0
    if abs(dlat) < 1e-9 and abs(dlon) < 1e-9:
        return None
    b = math.degrees(math.atan2(dlon, dlat))
    return b + 360.0 if b < 0 else b


def memory_to_dict(m: Optional[ShoreSegmentMemory]) -> Optional[dict]:
    """Serialise memory state for trace JSONL.  Returns None when no
    memory committed."""
    if m is None:
        return None
    return {
        "tangent_world_deg":   m.tangent_world_deg,
        "anchor_lat":          m.anchor_lat,
        "anchor_lon":          m.anchor_lon,
        "committed_tick":      m.committed_tick,
        "last_update_tick":    m.last_update_tick,
        "side_at_commit":      m.side_at_commit,
        "consecutive_misses":  m.consecutive_misses,
    }
