"""Destination generator — produces the per-tick controller destination.

See `docs/destination_generator_design.md` for the full design.  In
short: the controller's destination is the shore-projected lookahead
point when Phase A's shore-tangent fit is fresh, the previous tick's
destination when it is stale (TangentBug boundary-following
continuation), and a bow-forward synthetic point in the cold-start
case where neither is available.

The endpoint never enters this module — it is a goal-layer concept
used only by the arrival check and the initial hug-side bias.

This module is a pure function of its inputs.  It owns no state; the
caller supplies `last_known_destination` from the previous tick.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Optional, Tuple

Side = Literal["port", "starboard"]


# ── Tunables ────────────────────────────────────────────────────────────────

# Lookahead distance, in game lat/lon delta units.  Matches the
# canonical pure-pursuit range of 2-5× vehicle length.  We share the
# value with the existing hug-mode waypoint helper rather than
# introducing a new knob.
LOOKAHEAD_DISTANCE = 0.10

# Lateral offset gain — when the bot has drifted off the hug line by
# `d − d_star`, the destination is nudged perpendicular toward the
# line so the controller pulls back to it.  0.5 matches the existing
# §13.17 hug-mode lateral gain.
LATERAL_GAIN = 0.5


# ── Inputs ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ShoreTangent:
    """Shore-tangent state from Phase A's LSQ fit.

    Pass `None` to `generate_destination` when the fit is stale,
    missing, or otherwise unreliable — the generator will fall back to
    the last-known destination.
    """
    tangent_compass_deg:    float    # compass bearing along the shore,
                                     # destination-anchored per §13.21
    perpendicular_distance: float    # current perpendicular distance
                                     # to the shore (the `d` in
                                     # `_lyapunov_state`)
    d_star:                 float    # desired stand-off distance from
                                     # the shore on the hug side


# ── Generator ───────────────────────────────────────────────────────────────

def generate_destination(
    pos:                    Tuple[float, float],
    heading_deg:            float,
    shore_tangent:          Optional[ShoreTangent],
    last_known_destination: Optional[Tuple[float, float]],
    hug_side:               Side,
    lookahead_distance:     float = LOOKAHEAD_DISTANCE,
    lateral_gain:           float = LATERAL_GAIN,
) -> Tuple[float, float]:
    """Compute the per-tick destination point for the controller.

    Priority:
      1. Fresh shore tangent — return the shore-projected lookahead.
      2. Last-known destination — held from the previous tick when
         shore perception is stale (TangentBug-style continuation).
      3. Bow-forward fallback — only at cold start when neither is
         available.

    The endpoint does not enter here.  It only influences the
    initial choice of `hug_side` (handled at the goal layer) and the
    arrival check (also at the goal layer).
    """
    if shore_tangent is not None:
        return _shore_lookahead(
            pos, shore_tangent, hug_side,
            lookahead_distance, lateral_gain,
        )
    if last_known_destination is not None:
        return last_known_destination
    return _bow_forward(pos, heading_deg, lookahead_distance)


# ── Geometry helpers ────────────────────────────────────────────────────────

def _shore_lookahead(
    pos:               Tuple[float, float],
    shore:             ShoreTangent,
    hug_side:          Side,
    lookahead:         float,
    lateral_gain:      float,
) -> Tuple[float, float]:
    """Project the lookahead point onto the hug line.

    The hug line is the line parallel to the shore tangent at
    perpendicular distance `d_star` on the configured hug side.  The
    target sits `lookahead` ahead along the tangent, plus a
    perpendicular nudge proportional to `(d − d_star)` so the
    controller converges to the line when drifted off.

    Compass / game-coord convention:
      lat = north (+x)   lon = east (+y)
      unit vector for compass θ = (cos θ, sin θ)
    """
    lat, lon = pos
    tan_deg = shore.tangent_compass_deg
    perp_deg = (
        (tan_deg + 90.0) % 360.0 if hug_side == "starboard"
        else (tan_deg - 90.0) % 360.0
    )
    perp_offset = (shore.perpendicular_distance - shore.d_star) * lateral_gain
    tan_rad  = math.radians(tan_deg)
    perp_rad = math.radians(perp_deg)
    target_lat = (lat
                  + perp_offset * math.cos(perp_rad)
                  + lookahead   * math.cos(tan_rad))
    target_lon = (lon
                  + perp_offset * math.sin(perp_rad)
                  + lookahead   * math.sin(tan_rad))
    return (target_lat, target_lon)


def _bow_forward(
    pos:         Tuple[float, float],
    heading_deg: float,
    lookahead:   float,
) -> Tuple[float, float]:
    """Cold-start fallback: a point one lookahead distance ahead of
    the bow.  No shore information, no last-known destination — just
    go forward.
    """
    lat, lon = pos
    rad = math.radians(heading_deg)
    return (lat + lookahead * math.cos(rad),
            lon + lookahead * math.sin(rad))
