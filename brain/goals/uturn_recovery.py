"""§13.27 — Stuck detector + U-turn recovery state machine.

Pure-function design: state is held in a `StuckDetector` instance and a
`UTurnState` dataclass, both owned by `HugShoreGoal`.  This module
exports the data structures and the predicate functions; the steering
glue (entering/exiting the phase, emitting `HugTickResult`) lives in
`hug_shore.py::tick()`.

Design rationale (see `feedback_no_goal_bearing_candidate_widening.md`):
canonical reactive planners (Nav2, MoveBase, TEB) never widen the
candidate set on goal bearing alone — Bug2/TangentBug commit to
boundary-following and let transient goal-behind moments pass.  The
only canonical reverse-motion mechanism is a separate recovery
behaviour gated by a *windowed* stuck detector.

Detector signals (both required, both windowed over `WINDOW_TICKS`):
  1. Position drift (euclidean over lat,lon) below `DRIFT_THRESHOLD_DEG`
  2. Forward-arc mean nearest-distance below `FWD_CLEAR_THRESHOLD`
     (sectors 14, 15, 0, 1, 2 — the forward 5 sectors at 22.5° each)

Single-frame triggers are explicitly avoided — the detector requires
the full window of samples before it can fire.  This matches Nav2's
ProgressChecker (`movement_time_allowance`) and the BARN literature.

Recovery: latch the goal-direction bearing at entry, then command
calibrated holds in whichever direction (left vs right) closes the
bow-to-target gap fastest, until |delta| <= `EXIT_TOLERANCE_DEG`.
During recovery the steering loop suspends waypoint computation and
collision checks — per the original problem statement (the U-turn must
not be interrupted by the same chaos that triggered it).

Exit hysteresis: after recovery completes, the detector is disarmed
until the bot has moved `REARM_DISTANCE_DEG` from the exit position.
Prevents flicker if the U-turn unwinds the bot into the same trap.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional, Tuple


# ── Tunables ────────────────────────────────────────────────────────────────
#
# Calibrated against the phase_a_dest8_20260602_170946 bend trace (ON
# run, 420-tick stall at the Nile bend) and the _noastern_174745 trace
# (OFF run, smooth bend traversal).  See replay analysis in the
# project_uturn_recovery_design memory entry.

# Window over which both signals must hold.  ~30 ticks ≈ 60-90 seconds
# given the 2-3 s capture cadence — long enough that a smooth bend
# (which clears in 50 ticks) doesn't trigger, short enough that a
# 420-tick stall is caught early.
WINDOW_TICKS = 30

# Position drift threshold (euclidean over lat,lon in degrees).  At
# cruise speed the bot covers ~0.3-1.0° per 30 ticks; at the bend stall
# the per-30-tick drift was ~0.05-0.10°.  0.15° catches the stall while
# leaving comfortable margin for "slow but progressing" sailing.
DRIFT_THRESHOLD_DEG = 0.15

# Forward-arc mean nearest-distance threshold.  Open water reads ~0.30+
# in the forward sectors; the bend stall averaged ~0.20.  0.22 is
# slightly above the bend average — fires when shore is genuinely
# crowding the bow, doesn't fire in open water with intermittent
# small obstacles.
FWD_CLEAR_THRESHOLD = 0.22

# Forward-arc sector indices (sectors 14, 15, 0, 1, 2 at 22.5° each =
# ±56° from bow).  Tighter than the avoider's CANDIDATE_SECTORS (which
# spans ±90°) because for the dead-end signal we want the *immediate*
# forward path, not the wider candidate space.
FORWARD_ARC_SECTORS = (14, 15, 0, 1, 2)

# Exit tolerance: U-turn declares success when bow is within this many
# degrees of the latched target heading.  25° matches the avoider's
# 22.5° sector granularity plus a small margin so a single hold can
# close the gap.
EXIT_TOLERANCE_DEG = 25.0

# Hysteresis re-arm distance.  After recovery exits, the detector
# refuses to re-fire until the bot has moved this far from the exit
# position.  3× the drift threshold = ~3 ship-lengths of progress per
# Nav2's recovery exit pattern.
REARM_DISTANCE_DEG = 3 * DRIFT_THRESHOLD_DEG

# Per-tick max turn magnitude during recovery.  Same cap the steering
# primitives use — keeps the U-turn from issuing illegal commands and
# matches the rate calibrator's expectations.
RECOVERY_HOLD_MAX_DEG = 90.0


# ── Data classes ────────────────────────────────────────────────────────────

@dataclass
class UTurnState:
    """Active recovery state.  None when not in recovery."""
    entry_tick:           int
    latched_target_deg:   float          # bow target, latched at entry
    entry_lat:            Optional[float] = None
    entry_lon:            Optional[float] = None


@dataclass
class StuckDetector:
    """Rolling-window detector with re-arm hysteresis.

    Maintains a deque of (tick, lat, lon, mean_fwd_clearance) samples
    and decides whether both signals have held for the full window.
    """
    samples: Deque[Tuple[int, float, float, float]] = field(
        default_factory=lambda: deque(maxlen=WINDOW_TICKS))
    # Position of the most recent recovery exit; while disarmed the
    # detector ignores all check() calls until the bot moves more than
    # REARM_DISTANCE_DEG from this position.
    disarmed_until_moved_from: Optional[Tuple[float, float]] = None

    def record(
        self, tick: int,
        lat: Optional[float], lon: Optional[float],
        forward_clearance: Optional[float],
    ) -> None:
        """Push one sample.  Skipped silently when any field is None —
        no point recording a half-sample, and the deque already shrinks
        on max-len so old data ages out naturally."""
        if lat is None or lon is None or forward_clearance is None:
            return
        self.samples.append((tick, lat, lon, forward_clearance))

    def check(self) -> bool:
        """True when both stuck signals have held over the full window
        AND the detector is armed.  Returns False during the disarmed
        post-recovery window."""
        if len(self.samples) < WINDOW_TICKS:
            return False
        # Disarm check — require movement away from the exit point
        # before re-firing.  Uses the *current* (latest) sample.
        if self.disarmed_until_moved_from is not None:
            cur_lat, cur_lon = self.samples[-1][1], self.samples[-1][2]
            exit_lat, exit_lon = self.disarmed_until_moved_from
            if _euclid_deg(cur_lat, cur_lon, exit_lat, exit_lon) \
                    < REARM_DISTANCE_DEG:
                return False
            # We've moved far enough — re-arm.
            self.disarmed_until_moved_from = None
        # Signal 1: position drift across the window.
        lats = [s[1] for s in self.samples]
        lons = [s[2] for s in self.samples]
        drift = max(
            _euclid_deg(lats[i], lons[i], lats[j], lons[j])
            for i in range(len(self.samples))
            for j in range(i + 1, len(self.samples))
        )
        if drift >= DRIFT_THRESHOLD_DEG:
            return False
        # Signal 2: mean forward clearance across the window.
        clearances = [s[3] for s in self.samples]
        if sum(clearances) / len(clearances) >= FWD_CLEAR_THRESHOLD:
            return False
        return True

    def disarm_at(self, lat: float, lon: float) -> None:
        """Called by the steering loop on U-turn exit.  Disarms the
        detector until the bot has moved REARM_DISTANCE_DEG."""
        self.disarmed_until_moved_from = (lat, lon)
        # Clear the window so the next eval starts fresh from this
        # point — otherwise the in-window samples from the stuck zone
        # would still report drift=low even after the bot escapes.
        self.samples.clear()


# ── Pure helpers ────────────────────────────────────────────────────────────

def forward_clearance_mean(nav) -> Optional[float]:
    """Average `nearest_dist` over the forward arc.  Returns None when
    no forward sector has a distance reading — caller should treat this
    as "perception not ready, don't update detector"."""
    dists = []
    for idx in FORWARD_ARC_SECTORS:
        if idx >= len(nav.sectors):
            continue
        d = nav.sectors[idx].nearest_dist
        if d is not None:
            dists.append(d)
    if not dists:
        return None
    return sum(dists) / len(dists)


def signed_bow_to_target(
    bow_heading_deg: float, target_heading_deg: float,
) -> float:
    """Signed compass difference target − bow, wrapped to [-180, 180].
    Positive means target is clockwise (starboard) of bow → turn right.
    """
    return ((target_heading_deg - bow_heading_deg + 180.0) % 360.0) - 180.0


def recovery_command(
    bow_heading_deg: float, target_heading_deg: float,
) -> Tuple[str, float]:
    """Pick the U-turn command for this tick.

    Returns (direction, magnitude_deg) where direction is "left" or
    "right" and magnitude is the commanded turn capped at
    RECOVERY_HOLD_MAX_DEG.  Caller translates this into a calibrated
    hold via the existing rate_dps machinery.
    """
    delta = signed_bow_to_target(bow_heading_deg, target_heading_deg)
    direction = "right" if delta >= 0 else "left"
    magnitude = min(abs(delta), RECOVERY_HOLD_MAX_DEG)
    return direction, magnitude


def should_exit(
    bow_heading_deg: float, target_heading_deg: float,
) -> bool:
    """True when bow is within EXIT_TOLERANCE_DEG of the latched target.
    """
    return abs(signed_bow_to_target(
        bow_heading_deg, target_heading_deg)) <= EXIT_TOLERANCE_DEG


def _euclid_deg(
    lat1: float, lon1: float, lat2: float, lon2: float,
) -> float:
    """Euclidean distance in degree-space.  Not great near the poles
    (longitude shrinks) but the game's operating range (Mediterranean,
    Atlantic, Nile) is well below ±60° so the small-angle approximation
    is fine.  Same approximation `_within_motion_budget` uses."""
    dlat = lat1 - lat2
    dlon = lon1 - lon2
    return (dlat * dlat + dlon * dlon) ** 0.5
