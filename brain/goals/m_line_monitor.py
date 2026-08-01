"""M-line progress monitor — Bug2-style endpoint-reach guarantee.

⚠ DEPRECATED — superseded by CoverageTracker + FrontierPicker.
================================================================

This module is kept as a reference implementation of the classical
Bug2 (Lumelsky-Stepanov 1987) leave-point rule but is no longer
wired into HugShoreGoal.  It will not be re-introduced for our
current voyage geometries.

Why deprecated (empirical evidence):

The M-line is the straight line from start to endpoint.  Bug2's
leave-point rule fires only at M-line *re-crossings*.  In our river
voyage from Cairo (30.14, 30.46) toward upper Nile (5.71, 32.12),
the M-line passes through Sudan.  Almost all of it is over dry
land — the bot can never physically reach it.

Trace from `explore_port_20260604_104620` (630 ticks):
  - 5 M-line crossings total, all before t251 (above the bend).
  - 152 ticks at the Y tip — zero crossings.
  - The monitor was silent through the bend detour and through the
    Y oscillation, which are exactly the situations the user wants
    to flag.

Why this is structural, not a bug:

Bug2 was designed for open-space navigation with discrete blocking
obstacles.  Our environment is the geometric inverse — a thin
channel of water bounded by land on both sides.  The M-line is
mostly in inaccessible terrain.  No variant of Bug-family algorithms
(TangentBug, DistBug, VisBug) rescues this — they all keep the
M-line as a globally-defined reference.

Replacement:

CoverageTracker (`brain/goals/coverage_tracker.py`) maintains a 2D
grid of visited cells and emits a STUCK verdict when the visited
set isn't growing.  FrontierPicker
(`brain/goals/frontier_picker.py`) uses the coverage state to
override the destination-generator when STUCK fires, steering
toward the nearest unvisited cell (Yamauchi 1997).  Together they
detect AND resolve the bend/Y-oscillation cases the M-line monitor
missed.

What was retained — historical reference and theoretical reference:

The original module docstring and implementation are kept verbatim
below for two reasons:
 1. The completeness proof of Bug2 is a useful conceptual reference
    when reasoning about progress checks.
 2. If a future voyage configuration is open-water with discrete
    blocking obstacles (e.g. open-sea routing between islands),
    Bug2 may be the right tool again.  The module is ready to
    re-wire in that case.

See `docs/destination_generator_design.md` § "M-line vs Coverage"
for the design discussion.

────────────────────────────────────────────────────────────────────

Original docstring:

The destination-generator + Lyapunov stack converges to the shore,
not to the endpoint.  This module supplies an endpoint-reach
guarantee by emitting a per-tick verdict on whether the voyage is
making progress along the M-line — the straight line from start to
endpoint.

The monitor never moves the bot.  It emits one of three verdicts
per tick; the goal layer above decides what to do with
`REGRESSING_AT_HIT`.

Used only when an endpoint is configured.  Pure-exploration voyages
do not construct the monitor.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple


class Verdict(str, Enum):
    """Per-tick progress verdict.

    `MAKING_PROGRESS`: bot just re-crossed the M-line at a point
        strictly closer to the endpoint than the last crossing.
    `KEEP_FOLLOWING`: bot is between M-line crossings.  Distance to
        endpoint may grow freely — this is the canonical Bug2
        boundary-follow allowance.
    `REGRESSING_AT_HIT`: bot just re-crossed the M-line but at a point
        no closer (or strictly farther) than the last crossing.
        Trigger for the recovery layer.
    """
    MAKING_PROGRESS   = "making_progress"
    KEEP_FOLLOWING    = "keep_following"
    REGRESSING_AT_HIT = "regressing_at_hit"


@dataclass
class MLineMonitor:
    """Bug2-style M-line monitor.

    Construction:
        MLineMonitor(start=(lat, lon), endpoint=(lat, lon))

    Per tick:
        verdict = monitor.record(current=(lat, lon))
    """
    start:    Tuple[float, float]
    endpoint: Tuple[float, float]

    # Internal state — set on construction, then updated by record().
    _d_at_last_hit:     float = field(init=False)
    _prev_signed_perp:  Optional[float] = field(default=None, init=False)

    def __post_init__(self) -> None:
        # Initial reference distance: the M-line's own length.  The
        # start point is, by definition, on the M-line — so it counts
        # as a "hit" at d_at_last_hit = |start - endpoint|.
        self._d_at_last_hit = _euclid(self.start, self.endpoint)

    def record(self, current: Tuple[float, float]) -> Verdict:
        """Push the current position and return the verdict."""
        signed_perp = _signed_perp_distance(
            current, self.start, self.endpoint,
        )
        prev = self._prev_signed_perp
        self._prev_signed_perp = signed_perp

        # First tick (or first valid sample): no prior sign to compare
        # against.  Voyage in motion — between-crossings state.
        if prev is None:
            return Verdict.KEEP_FOLLOWING

        # Both samples on the same side (both positive or both
        # negative) → no crossing.  Either sample exactly on the line
        # (signed_perp == 0) is treated as "no crossing" too — a
        # tick that grazes the line is not a hit; the next sign
        # change after grazing will be the real crossing.
        if prev * signed_perp >= 0:
            return Verdict.KEEP_FOLLOWING

        # Strict sign change → M-line crossing.
        d_current = _euclid(current, self.endpoint)
        if d_current < self._d_at_last_hit:
            self._d_at_last_hit = d_current
            return Verdict.MAKING_PROGRESS
        # d_current ≥ d_at_last_hit — don't update the reference, so
        # the *next* crossing still compares against the same hit
        # distance.  This is Bug2's monotone-at-crossings rule.
        return Verdict.REGRESSING_AT_HIT

    @property
    def d_at_last_hit(self) -> float:
        """Smallest endpoint distance recorded at any M-line crossing
        so far (or the initial M-line length if no crossing has
        happened yet)."""
        return self._d_at_last_hit


# ── Geometry helpers ────────────────────────────────────────────────────────

def _euclid(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _signed_perp_distance(
    point: Tuple[float, float],
    line_a: Tuple[float, float],
    line_b: Tuple[float, float],
) -> float:
    """Signed perpendicular distance from `point` to the line through
    `line_a` and `line_b`.

    Sign convention: positive means `point` is on the "left" side of
    the directed line from `line_a` to `line_b` (the side where the
    2D cross product is positive).  Sign-change between consecutive
    samples means the line was crossed.

    Returns 0 when the line is degenerate (start == endpoint), so
    every tick reads as "on the line" — caller's responsibility to
    catch the degenerate construction.
    """
    ax, ay = line_a
    bx, by = line_b
    px, py = point
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy)
    if length < 1e-12:
        return 0.0
    # 2D cross product / |line vector| = signed perpendicular distance.
    return ((px - ax) * dy - (py - ay) * dx) / length
