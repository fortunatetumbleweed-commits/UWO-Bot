"""Closed-loop heading control for rudder steering.

Replaces the feed-forward `hold_ms = Δ / 120°/s` guess (which assumes a
linear 120°/s rudder and chronically under-turns) with:

  1. `hold_ms_for_turn` — invert the CALIBRATED, nonlinear hold→turn
     curve (`tools/calibrate_steering.py`, 2026-07-28).  The real
     response has a spin-up: ~dead below 300 ms, then ramps to a
     ~110–120°/s asymptote.  Roughly ship-independent (proa 10.5 kt vs
     a 27 kt ship shared the same curve), so one curve serves.
  2. `HeadingPID` — a discrete per-tick PID on the heading error toward
     the reflex-WP bearing.  P drives the bulk of the correction (via
     the curve inverse), I clears steady bias / persistent under-turn,
     D damps overshoot.  A deadband suppresses tiny corrections; the
     min-hold floor is 300 ms — the low edge of the rudder's spin-up.
     (Was 450 ms → ~22° minimum turn, which overshot a 12° deadband
     correction by ~10° and drove a divergent fishtail in narrow
     channels; 300 ms → ~3-10° lets small corrections settle without
     overshoot.  Trade-off: 300 ms holds are noisier — the ±7° heading
     noise can flip a tiny turn's sign ~20% of the time.)

Gains are starting values meant to be tuned on a live run — the plant
is coarse (min turn ~15°), slow (one command per ~2–4 s tick), and the
heading feedback carries ~±7° noise, so keep P modest and lean on I.
"""
from __future__ import annotations
import math
from typing import Optional


# Calibrated turn(hold_ms) curve — mean of the proa + fast-ship sweeps
# (data/steering_cal/*.json).  (hold_ms, turn_deg) knots.
_CURVE: list[tuple[float, float]] = [
    (0.0, 0.0),
    (300.0, 3.0),      # sub-300 ms is effectively dead (rudder spin-up)
    (500.0, 28.0),
    (800.0, 77.5),
    (1200.0, 124.0),
]
_DEAD_TURN_DEG = _CURVE[1][1]   # ≤ this much turn ⇒ no effective hold


def hold_ms_for_turn(turn_deg: float) -> int:
    """Invert the calibrated curve: hold (ms) that produces `turn_deg` of
    rotation.  Piecewise-linear between measured knots; extrapolates past
    1200 ms at the last segment's rate.  Input is magnitude (≥0)."""
    t = abs(turn_deg)
    if t <= _DEAD_TURN_DEG:
        return int(_CURVE[1][0])          # min meaningful hold (300 ms)
    for (h0, t0), (h1, t1) in zip(_CURVE, _CURVE[1:]):
        if t <= t1:
            return int(h0 + (t - t0) / (t1 - t0) * (h1 - h0))
    (h0, t0), (h1, t1) = _CURVE[-2], _CURVE[-1]
    rate = (h1 - h0) / (t1 - t0)           # ms per extra degree
    return int(h1 + (t - t1) * rate)


def _wrap180(deg: float) -> float:
    return ((deg + 540.0) % 360.0) - 180.0


class HeadingPID:
    """Discrete per-tick PID from heading error → (command, hold_ms).

    One rudder command is fired per tick, so `dt` is folded into the
    gains (units are per-tick).  D is computed on the *measurement*
    (heading), not the error, to avoid derivative kick when the target
    (reflex WP) jumps.
    """

    def __init__(self,
                 kp: float = 0.8,
                 ki: float = 0.15,
                 kd: float = 0.15,
                 deadband_deg: float = 12.0,
                 i_clamp_deg: float = 45.0,
                 min_hold_ms: int = 300,
                 max_hold_ms: int = 1600):
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.deadband = deadband_deg
        self.i_clamp = i_clamp_deg
        self.min_hold = min_hold_ms
        self.max_hold = max_hold_ms
        self._integral = 0.0
        self._prev_heading: Optional[float] = None

    def reset(self) -> None:
        self._integral = 0.0
        self._prev_heading = None

    def step(self, error_deg: float, heading_deg: float
             ) -> tuple[Optional[str], int, str]:
        """Return (command|None, hold_ms, note).

        `error_deg` = signed heading error toward the target
        (reflex-WP bearing − heading), positive ⇒ turn right.
        """
        # Deadband: within tolerance, don't fire (the actuation can't make
        # a correction smaller than the min effective hold anyway).  Bleed
        # the integral so it doesn't sit wound up.
        if abs(error_deg) < self.deadband:
            self._integral *= 0.5
            self._prev_heading = heading_deg
            return None, 0, f"pid_hold(e={error_deg:+.0f}<db)"

        p = self.kp * error_deg

        # Integral with clamp anti-windup.
        self._integral = max(-self.i_clamp,
                             min(self.i_clamp, self._integral + error_deg))
        i = self.ki * self._integral

        # Derivative on measurement (−kd·Δheading); damps overshoot.
        d = 0.0
        if self._prev_heading is not None:
            d = -self.kd * _wrap180(heading_deg - self._prev_heading)
        self._prev_heading = heading_deg

        u = p + i + d                       # desired turn this tick (deg)
        command = "hold_right" if u > 0 else "hold_left"
        hold = hold_ms_for_turn(abs(u))
        hold = int(max(self.min_hold, min(self.max_hold, hold)))
        note = (f"pid(e={error_deg:+.0f},u={u:+.0f}|"
                f"P{p:+.0f} I{i:+.0f} D{d:+.0f})")
        return command, hold, note
