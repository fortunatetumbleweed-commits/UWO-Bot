# brain/goals/hug_shore.py
#
# Hug a coastline on the chosen side of the ship while sailing forward.
#
# Steering policy: VFH+ (Vector Field Histogram Plus, Ulrich & Borenstein
# 1998) adapted to our 8-sector mini-map navigation view.  Each tick:
#
#   1. Build a per-sector cost combining obstacle density (land
#      fraction + proximity to closest land patch) and angular distance
#      from an "ideal" heading.
#   2. Pick the lowest-cost sector among the forward-arc candidates
#      (sectors 6, 7, 0, 1, 2 — port beam through starboard beam).
#   3. Apply VFH+ hysteresis: keep the previous choice unless the new
#      best is meaningfully better.  Kills tick-to-tick oscillation.
#   4. Steer to bring the bow toward the chosen sector via one
#      calibrated press-and-hold gesture, capped at 60° per tick so the
#      mini-map check happens at sub-1s cadence.
#
# Replaces an earlier rule-cascade policy (6 hand-tuned branches) that
# had recurring "wrong-direction in tight channel" and "blind-hold past
# channel reversal" failures.  See the design lessons memory in
# memory/feedback_hug_shore_design_lessons.md.

from __future__ import annotations

import json
import math
import random
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Literal, Optional, Deque

from loguru import logger

from brain import observation as _obs
from brain.goals.destination_generator import (
    ShoreTangent,
    generate_destination,
)
from brain.goals.coverage_tracker import (
    CoverageTracker,
    Verdict as CoverageVerdict,
)
from brain.goals.frontier_picker import TremauxPicker, YamauchiPicker
# M-line monitor — superseded by CoverageTracker.  See
# docs/destination_generator_design.md § "Phase 2 update".  Kept
# importable in case a future open-water voyage wants Bug2.
# from brain.goals.m_line_monitor import (
#     MLineMonitor,
#     Verdict as MLineVerdict,
# )
from brain.goals.shore_segment import (
    SegmentDecision,
    ShoreSegmentMemory,
    evaluate_segment,
    memory_to_dict,
)
from brain.goals.uturn_recovery import (
    StuckDetector,
    UTurnState,
    forward_clearance_mean,
    recovery_command,
    should_exit as uturn_should_exit,
)
from brain.steering import (
    ConfigSelector,
    NARROW_SAFETY_DIST,
    VFHPlusAvoider,
    VFHPlusConfig,
)
from brain.goals.junction_detector import TopologyKind
from vision.water_skeleton import (
    classify_topology,
    compute_skeleton_tangent_deg,
    extract_skeleton,
)


Side = Literal["port", "starboard"]


# ── Phase enum ──────────────────────────────────────────────────────────────

class HugPhase(Enum):
    INIT             = auto()    # haven't run a tick yet
    HUGGING          = auto()    # tracking shore on the chosen side
    BLOCKED          = auto()    # all forward sectors flagged obstructed
    OFFSHORE         = auto()    # no shore visible on the target side
    UTURN_RECOVERY   = auto()    # §13.27 — stuck detector fired, U-turning
    FAILED           = auto()    # could not steer (no nav, etc.)
    COMPLETE         = auto()    # max_ticks reached without failure


# ── Tick result ─────────────────────────────────────────────────────────────

@dataclass
class HugTickResult:
    action: str
    phase:  HugPhase
    note:   str = ""
    delay:  float = 0.0


# ── Memory (Phase A) ────────────────────────────────────────────────────────
#
# Per-tick observation + decision record.  Stored on a rolling deque so
# the policy can derive temporal signals: "shore was just here", "I've
# been turning the same direction for N ticks", "world bearing from
# accumulated lat/lon".  See docs/hug_shore_predictive_pipeline.md.
#
# All fields are optional so a record is well-defined even when
# perception is partial (e.g., lat/lon unreadable, sectors None).

HISTORY_WINDOW = 10        # how many ticks of memory we keep
SHORE_RECENT_TICKS = 3     # "shore was just here" window


@dataclass
class TickRecord:
    tick:          int
    wall_time:     float
    heading:       Optional[float]   # accepted heading after sanity check
    raw_heading:   Optional[float]   # what the detector returned
    rejected:      bool
    lat:           Optional[float]
    lon:           Optional[float]
    speed_kt:      Optional[float]
    sectors:       tuple              # tuple of 8 SectorReading-like (frac, dist)
    commanded_deg: float              # signed angle command issued THIS tick
    actual_delta:  Optional[float]    # rotation between this tick and the next
    phase:         "HugPhase"
    chosen_sector: Optional[int]
    ideal_sector:  Optional[int]
    cost_dump:     dict               # candidate-sector → cost


@dataclass
class HistorySignals:
    """Derived signals from the history deque.  Computed once per tick,
    consumed by the policy.  Pure function of recent observations."""
    # Tick index of last observation where the target-side had real shore.
    # None if we've never seen it (fresh start or perpetual offshore).
    target_shore_last_seen_at_tick: Optional[int] = None
    # Which target-side sector (1, 2, or 3) was loaded at that tick.
    target_shore_last_seen_sector: Optional[int] = None
    # Same for opposite-side.
    opposite_shore_last_seen_at_tick: Optional[int] = None
    # Sign of cumulative heading-delta over the last 3 ticks.
    # +1 = trending right, -1 = left, 0 = neutral or insufficient data.
    recent_rotation_direction: int = 0
    # Run length of consecutive same-sign commanded turns (excluding hold).
    consecutive_same_direction_turns: int = 0
    # World-frame bearing derived from accumulated lat/lon over the
    # window.  None when displacement is below the quantization floor.
    world_motion_bearing: Optional[float] = None
    # Median of last 5 readable speed_kt readings.  Robust against
    # OCR outlier spikes.
    median_speed_kt: Optional[float] = None
    # Per-tick rate of change for the bow-target sector (Δfrac, Δdist).
    # Positive Δfrac = shore loading up; negative Δdist = shore closing in.
    # Used by approach-velocity peel trigger.  None when fewer than 2 ticks
    # in history or readings missing.
    bow_target_velocity: Optional[tuple] = None
    # Same for the ahead sector.
    ahead_velocity: Optional[tuple] = None
    # Signed rate-of-change of target-beam nearest_dist, averaged over the
    # last few tick transitions for noise immunity.
    #     > 0  → shore is receding from the corridor = DRIFTING AWAY
    #     < 0  → shore is closing in on the corridor = APPROACHING
    #     ~ 0  → corridor stable
    # None when fewer than 2 history ticks have a target-sector distance.
    target_drift_rate: Optional[float] = None

    # §13.14 — signed mean Δfrac per tick on target-beam, over recent
    # history.  Distinguishes "tight hug with thin shore" (stable low
    # frac, rate ≈ 0) from "shore receding from view" (frac dropping
    # tick-over-tick, rate << 0).  Used to gate the frac-aware
    # d_effective.
    target_frac_drift_rate: Optional[float] = None


# ── VFH+ tuning ─────────────────────────────────────────────────────────────
#
# Candidate sectors = forward arc + beams.  Astern sectors (5-11) are
# excluded — the ship sails forward only.
#
# Sector index map (ship-relative, NavigationView convention) §13.20:
#   16 sectors of 22.5° each.  Forward arc (-90°..+90°):
#   0 = ahead          1 = +22.5°       2 = +45° (bow-stbd)
#   3 = +67.5°         4 = +90° (stbd beam)
#  12 = -90° (port)   13 = -67.5°      14 = -45° (bow-port)
#  15 = -22.5°
#   Astern: 5-11 (excluded from candidates).

CANDIDATE_SECTORS = (12, 13, 14, 15, 0, 1, 2, 3, 4)

# §13.26 — Astern-expanded candidate set.  Used when the waypoint
# bearing falls more than 90° from the bow: the forward arc literally
# cannot point at the goal, so the avoider must be allowed to consider
# rear sectors.  Without this expansion the bot would pick the cheapest
# *forward* sector (a beam) and grind sideways for tens of ticks until
# the world rotated under it enough to bring the goal back into the
# forward arc.  Observed at phase_a_dest8_20260602_162537 t184-t260:
# the bot was heading N with wp_brg≈170° behind it and never picked a
# southward sector for 76 ticks.  The μ2=2 inertia term still damps
# single-tick noise (a spurious 180° wp flip can't beat μ1·160° + μ2·0
# vs μ1·0° + μ2·180°), so a U-turn only wins when the goal is
# genuinely opposite for sustained ticks.
ALL_SECTORS = tuple(range(16))

_SECTOR_REL_ANGLE: dict[int, float] = {
    0:    0.0,
    1:   22.5,
    2:   45.0,
    3:   67.5,
    4:   90.0,
    5:  112.5,    # §13.26 astern arc (admitted when wp is behind bow)
    6:  135.0,
    7:  157.5,
    8:  180.0,    # dead astern
    9: -157.5,
    10: -135.0,
    11: -112.5,
    12: -90.0,
    13: -67.5,
    14: -45.0,
    15: -22.5,
}

# Cost weights.  Obstacle dominates (we must never knowingly steer into
# a wall); ideal pulls toward "follow the shore"; hysteresis keeps the
# previous choice unless a clearly better one appears.
W_OBSTACLE        = 1.0
W_IDEAL           = 0.35
# Hysteresis margin — keep `last_chosen_sector` only when an alternative
# beats it by less than this much.  Calibrated 2026-05-28 against live
# log: at our ~1–2s sample rate, real per-tick cost changes are
# typically 0.10–0.30 while perception noise is ~0.01–0.03.  A 0.02
# margin filters noise without swallowing real signal.  The old value
# 0.10 was 3–5× the noise floor and routinely kept stale (wrong-
# direction or wall-pointing) decisions.  See `docs/hug_shore_scenarios.md`
# scenario `facing_wall_peel_via_hysteresis_off` for the canonical
# failure mode this fixes.
HYSTERESIS_MARGIN = 0.02

# Obstacle-cost blend: land_fraction is the bulk signal, but a tiny
# patch very close still scores high (e.g. 0.02 frac at 0.05 dist).
PROXIMITY_DANGER  = 0.15   # nearest_dist below this → proximity-dominated

# Virtual-wall model — see header docstring.
#
# Concept: real shore is one wall of a corridor; we add a virtual wall on
# the opposite side at the calibrated `wall_distance`.  Ship sails the
# corridor.  Drift correction, hug-tightness, and stay-near-shore
# pressure all emerge from a single tunable parameter.
#
# When ahead is loaded, the virtual wall is *suppressed* — this lets the
# bot peel away from the real shore (classic wall-following turn) without
# the virtual wall fighting it.

# §13.20 16-sector indices — these reference the SAME COMPASS BEARINGS as
# the previous 8-sector constants: bow-stbd/-port (±45°) is now idx 2/14
# (was 1/7), beams (±90°) are now idx 4/12 (was 2/6).
_AHEAD_TARGET_SECTOR   = {"starboard": 2,  "port": 14}
_AHEAD_OPPOSITE_SECTOR = {"starboard": 14, "port": 2}
_BEAM_OPPOSITE_SECTOR  = {"starboard": 12, "port": 4}   # §13.9 escalation

# Wall-follow override: when ahead is loaded AND opposite-beam is
# clear (genuine escape exists), force ideal to ahead-opposite so the
# cost function peels AWAY from target — classic right-hand-on-the-
# wall rule.  Only fires when opposite-beam is below this frac AND
# not collision-imminent.
OPP_BEAM_CLEAR_FRAC = 0.30
OPP_BEAM_CLEAR_DIST = 0.10

# Wrong-side trap: target side empty but opposite side has substantial
# shore.  Peel toward opp-shore to initiate U-turn.  Threshold above
# which the opposite-shore is "substantive enough to U-turn for."
WRONG_SIDE_OPP_FRAC = 0.30
# Target sector counts as "empty" if frac is below this even at close
# range — a tiny sliver close-by (e.g., a rocky outcrop or noise spike)
# does not constitute real hug-shore.  Without this guard, the wrong-
# side detector misses the case where shore is solidly on the opposite
# side but a small patch close on the target side keeps target_empty
# False.  Live ticks 1-5 of 2026-05-29_143711 run: S=0.01/0.07,
# P=0.83/0.12 → bot stayed HUGGING sailing north on the wrong side.
WRONG_SIDE_TINY_FRAC = 0.05
# In wrong-side mode the cost function must accept a leftward sector
# that has real shore in it (we're peeling INTO the shore to start the
# U-turn).  Boost ideal weight so the directional pull (toward opp-
# bow) dominates the obstacle cost on the opp-side sectors.  1.8 is
# slightly above the threshold needed to flip sector-0 vs sector-7
# at typical opp-shore loads (~0.40 frac).
W_IDEAL_WRONG_SIDE = 2.5

# Same boost for astern-pull mode: when the hug-line is BEHIND us (aS
# loaded) and the bot must commit to a target-ward turn even though
# bow-target has a substantial obstacle, the directional pull toward
# the ideal sector needs to dominate the obstacle cost.  Without this,
# the cost min picks the obstacle-free opposite-side sector and the
# bot peels AWAY from the hug-line it just lost.  Live t=29 of
# 2026-05-29_205124: ideal=bow-stbd (astern-pull fired), but bS=.45/.35
# (substantial close shore on bow-stbd) → cost picked bow-port → ship
# rotated away from the shore wrap-around behind it.
W_IDEAL_ASTERN_PULL = 0.35   # §13.10 — turned off (was 2.5).  The
                             # principled tangent-bias term (added by
                             # §13.10) replaces astern-pull's role
                             # using the continuous tangent estimate
                             # from _shore_tangent_angle().  Code path
                             # preserved for potential re-enable; with
                             # this value = W_IDEAL, it's a no-op
                             # multiplier.  See §13.10 in the design doc.

# Shore-present detection: shore is visible if EITHER target sector
# (T or bT) has meaningful land_fraction OR has land at close range.
# Both conditions must fail before we declare "shore lost."
SHORE_VISIBLE_FRAC   = 0.15  # frac on T or bT counts as shore
SHORE_VISIBLE_DIST   = 0.20  # OR dist on T or bT this close counts as shore

# Astern-target pull: when the hug-line has migrated behind the ship
# (shore on the astern-target sector), the policy treats this as a
# memory of "we just passed our hug-line."  Pulls ideal toward
# bow-target so the bot curves back into the hug instead of
# continuing offshore or chasing the cheapest forward sector.
# Empirically (live tick 95 of 2026-05-29_113451 run): aS=0.46/0.30
# was a clear "you sailed past" signal that was being ignored.
ASTERN_PULL_FRAC = 0.30
ASTERN_PULL_DIST = 0.20

# Drift threshold = wall_distance × this.  Uses T.nearest_dist
# specifically (the target BEAM — direction we want shore on).
# bT being close means something is ahead-target, but the actual
# shore could still be far away on the beam.
# Used as a fallback when history is too short for a rate-based check.
DRIFT_THRESHOLD_MULT = 1.5

# Temporal drift: signed mean Δdist on target-beam sector, averaged over
# DRIFT_RATE_WINDOW transitions.  When the rate exceeds
# DRIFT_RATE_THRESHOLD (positive = shore receding), the corridor is
# actually opening up over time — not just one noisy frame.  This
# replaces the single-tick threshold check whenever signals are
# available, since a single tick can show big T.dist purely from
# corner geometry / perception noise without any real drift.
DRIFT_RATE_WINDOW    = 3      # average over up to 3 most recent Δdist
DRIFT_RATE_THRESHOLD = 0.02   # per-tick Δdist; ~2% of mini-map radius/tick

# Virtual wall: cost added to opposite-side sectors.  As wall_distance
# shrinks (tighter corridor), the wall cost grows.
WALL_DIST_INIT       = 0.20  # 1/5 of mini-map radius (user spec 2026-05-28)
WALL_DIST_MIN        = 0.12  # safety floor — tighter than this and the
                             # bot's typical ~75°/s rotation can't avoid
                             # contact during a perception tick
WALL_DIST_MAX        = 0.30  # safety ceiling
WALL_DIST_EMA_ALPHA  = 0.05  # very slow — many samples before adapting
WALL_COST_MAX        = 0.50  # at wall_distance = 0
WALL_COST_RANGE      = 0.40  # at wall_distance = this, cost = 0

# Lateral-balance term — see docs/shore_following_design.md §13.7.
# The fix for "bot drifts offshore until shore is fully lost."
# When the target-side beam reading exceeds the calibrated
# wall_distance, the bot has drifted past the corridor midline.
# We charge sec 0 a proportional penalty so the cost gap to sec 1
# shrinks smoothly with drift — replacing the previous categorical
# `_ideal_sector` flip at the SHORE_VISIBLE_FRAC threshold.
LATERAL_VALID_FRAC = 0.05    # ignore T.nearest_dist when frac is below
                             # noise floor (defends against 1-pixel
                             # artifacts mimicking shore at d_target)
K_LATERAL          = 1.5     # weight on lateral drift in sec 0's cost

# §13.13 — EMA smoothing on T.nearest_dist.  Live observation:
# port-marker UI elements briefly misclassify as land pixels,
# producing single-tick dist spikes (hug_debug_20260531_095814 t=20:
# 0.36 → 0.26 → 0.29 around a stray reading).  The Lyapunov literature
# always pairs a state estimator with the controller; this is the
# classical low-pass filter version.  See §13.13.
T_DIST_EMA_ALPHA   = 0.4     # weight on the new reading.  ~3-tick
                             # response window: high enough to track
                             # real geometry changes, low enough to
                             # reject single-tick noise spikes.

# §13.13 — Barrier function on lateral drift.  Quadratic penalty
# beyond a safe threshold so the bot reacts more strongly as it
# approaches the maximum operational drift.  Matches the control-
# barrier-function pattern from modern robotics: linear control in
# the normal regime, super-linear amplification near the safety
# boundary.  Pre-§13.13 the bot let drift accumulate to 0.20+ before
# meaningful response — with ample maneuver room remaining but
# trajectory committed.
LATERAL_BARRIER_THRESHOLD = 0.05   # drift > this fires the barrier
K_LATERAL_BARRIER         = 20.0   # quadratic gain on (drift − threshold)

# §13.14 — frac-aware d_effective.  Lyapunov's `d` is supposed to be
# perpendicular distance to wall, but T.nearest_dist only tracks the
# nearest pixel within the sector — which can stay small (single
# stray pixel) even as the wall geometrically recedes from view.
# Observed live in hug_debug_20260531_121728 t=18-24: T.frac collapsed
# 0.37 → 0.00 (wall lost) while T.dist stayed 0.04-0.19 (noise pixels
# still nearby).  Drift signal stayed at zero across all 7 ticks
# despite the bot completely losing the coast.  Bot kept holding
# until shore was fully gone, then went into chaotic recovery.
#
# Fix: use frac as the principled signal for "is the wall there."
# When T.frac drops below FRAC_HEALTHY, inflate d_effective by the
# deficit × K_FRAC_FADE.  The bulk-presence signal complements the
# nearest-pixel-distance signal, matching what wall-followers in the
# literature do with continuous lidar sweeps.
FRAC_HEALTHY  = 0.30        # T.frac at clean parallel hug
K_FRAC_FADE   = 1.5         # weight on frac deficit in d_effective
FRAC_FADING_RATE_THRESHOLD = -0.02   # mean Δfrac per tick below this
                                     # counts as "shore receding".
                                     # 0.02 per tick × 3-tick window
                                     # = a clear monotonic drop, not
                                     # noise.

# §13.14 — search-mode max turn cap.  When shore is invisible, §13.11
# bypasses margin scaling (full nominal turns).  But full 45°-per-tick
# rotation over multiple ticks produces chaotic search behaviour —
# the bot has no time to perceive between taps.  The classical
# wall-following recovery is a *controlled* search at a moderate
# turn rate (spiral or sweep).  This cap implements that.
SEARCH_MAX_TURN_DEG = 22.0  # half of nominal; allows ~135° sweep over 6 ticks

# ── §13.15 VFH* lookahead ───────────────────────────────────────────────────
# Replaces the static BOW_T_CLOSE_FRAC/DIST + APPROACH_* threshold patches
# (which AND'd frac & dist and missed thin protrusions — see t=101-103 of
# 2026-05-31_130527 where a peninsula at sec1.frac=0.21-0.37, d=0.27-0.29
# slipped through every static gate).  VFH* (Ulrich & Borenstein 2000)
# extends VFH+ with N-step tree-search: for each candidate sector, predict
# the next-tick polar histogram via a kinematic model, re-score, and add
# the discounted future cost to the current cost.
LOOKAHEAD_DEPTH         = 2     # 2-tick rollout: catches single-tick bow
                                # approaches (issue 1) AND wrap-around
                                # peninsulas (issue 3).  64+512 = 576
                                # _score_sectors calls per tick — trivial
                                # at 1 Hz.
LOOKAHEAD_DISCOUNT      = 0.7   # future-cost weight per step.  0.7 means
                                # depth-1 cost contributes 70%, depth-2
                                # contributes 49%.  Picked at the canonical
                                # MDP rule-of-thumb; tune if lookahead
                                # under-fires (raise) or over-fires (lower).
V_DT_PER_KT_PER_TICK    = 0.014 # Forward translation per tick in normalized
                                # mini-map units, per knot.  Calibrated
                                # 2026-05-31 from t=105→t=106 of
                                # hug_debug_20260531_130527: peninsula
                                # moved ~0.158 × max_r at 10.9 kt.  Used to
                                # advect obstacles toward the bow in the
                                # kinematic predictor.
MAX_TURN_PER_TICK_DEG   = 120.0 # Physical rotation ceiling per 3-sec tick:
                                # 1200ms hold cap × ~70-100°/sec measured
                                # in trace yields ~120° max single-tick
                                # rotation (t=22→23 = +116°, t=26→27 = −110°
                                # both close to ceiling).  Used to cap the
                                # predicted Δψ in the kinematic model so a
                                # commanded +180° doesn't lie about
                                # producing instant U-turn geometry.
# Cruise-speed fallback when nav.speed_kt is missing (sea-HUD OCR can fail).
LOOKAHEAD_SPEED_FALLBACK_KT = 11.0

LATERAL_DEADBAND   = 0.03    # ignore drifts below this — small natural
                             # fluctuations around target hug distance
                             # shouldn't fire turns.  Without it, the
                             # Cairo→Anatolia run t=160 oscillated 45°
                             # left↔right because a 0.05 drift produced
                             # enough lateral cost to flip sec 0↔sec 1.

# Fix B was tried (override astern-block when ahead is definitely
# walled at >= 0.45) but reverted: it broke the
# `astern_pull_beats_wall_follow_after_corner` regression scenario
# without actually fixing the Cairo t=167 collision, because in that
# tick opp_beam.frac = 0.38 wasn't < OPP_BEAM_CLEAR_FRAC anyway, so
# the override wouldn't have fired even with B applied.  The Cairo
# collision needs a different mechanism (likely sec 0 obstacle-cost
# urgency when ahead.frac is climbing tick-over-tick).  Deferred.

# §13.7 fix C: variable-duration holds.  Scale the commanded angle
# by the cost margin (sec 0 cost − best sector cost) so a small
# justification for turning produces a small turn, not a full 45°
# tap.  Smooths the bang-bang oscillation observed in t=160-163 of
# the same trace.  Bypassed when escape_fired (emergencies always
# commit full magnitude).
MARGIN_MIN_SCALE   = 0.25    # never scale below 25% of nominal — when the
                             # policy picks a non-zero sector even with a
                             # tied margin, honour the direction with at
                             # least a small turn
MARGIN_SATURATION  = 0.30    # at this margin or above, commit full nominal

# §13.8 — Inverse-square ahead-wall cost (Variant A1g).  The standard
# `_obstacle_cost` saturates at 1.0, so when virtual_wall (~0.28),
# rotation_penalty (~0.30), and ideal_distance push other sectors
# above 1.0, sec 0 stays cheapest even with a wall directly ahead.
# Observed live in hug_debug_20260531_072226 t=1-7: bot held into a
# wall at frac=0.83 / dist=0.10 because all turn options had cost
# >1.0 while sec 0 capped at 0.83.  Collision at t=6.
#
# Fix: when sec 0 has substantial shore ahead (frac > AHEAD_WALL_GATE)
# at moderate-or-closer range (dist < OBSTACLE_RANGE), use an
# inverse-square cost that can exceed 1.0 without a cap.  Matches the
# canonical VFH+ / APF formulation where close obstacles dominate by
# 1/r² scaling.  Other sectors and the noise-floor case fall through
# to the original `_obstacle_cost`.
#
# Pre-ship simulation on actual trace data (Cairo→Anatolia clean
# stretch, Cairo tricky area, NEW collision) showed:
#   • 4% disruption on clean hugs (negligible)
#   • 11% on Cairo mid-section — all on sec 0 frac > 0.28 ticks,
#     likely catching shore-approach the old code missed
#   • 3/3 catch on the "held into wall" collision ticks
AHEAD_WALL_GATE    = 0.20    # require real ahead-shore before triggering
OBSTACLE_RANGE     = 0.30    # range at which the inverse-square cost
                             # equals the linear baseline; below this,
                             # cost grows quadratically with proximity
OBSTACLE_MIN_DIST  = 0.03    # floor to avoid divide-by-zero at d→0

# §13.9 — when the wall-follow override fires AND the bow-opposite
# sector (the direction we'd peel toward) ALSO has obstacle, escalate
# from a 45° peel to a 90° peel.  Pinched-passage geometry — see live
# hug_debug_20260531_075222 t=153 for the motivating collision.
BOW_OPP_PINCH_FRAC = 0.10    # land_fraction threshold to count as
                             # "obstacle in peel direction" — much
                             # lower than AHEAD_WALL_GATE because
                             # obstacles on the opposite-bow are
                             # always surprising; we want to react
                             # before they're as solid as a wall

# §13.10 — Lyapunov tangent-bias weight.  Adds a continuous angular
# preference to the cost function based on the estimated shore tangent
# direction.  Replaces astern-pull's role with a principled mechanism
# matching the Lyapunov wall-following literature.  See §13.10 in
# docs/shore_following_design.md.
K_TANGENT_BIAS = 0.6         # cost added per unit (angular-distance/180°)
                             # from desired bearing.  0.6 chosen so a
                             # 45° misalignment adds ~0.15 — comparable
                             # to W_IDEAL × ideal_distance, less than
                             # the obstacle terms.

# Per-tick action shaping.
TURN_DEADBAND_DEG  = 5.0
MAX_TURN_PER_TICK  = 90.0

# Heading-smoothing weight (classic VFH+ μ₂ term).  Small per-sector
# penalty proportional to |sector_angle| / 180.  Acts as a tiebreaker so
# equally costed sectors are decided in favour of the gentler rotation.
#
# Sized to clear a true tie without disturbing real preferences.  The
# tightest correct-decision margin in pinned scenarios is
# `river_channel_opens_right`: sector 1 wins over sector 0 by only
# 0.009.  Smoothing penalty added to sector 1 (45°) is W × 0.25 — must
# be < 0.009 → W < 0.037.  W=0.02 is safely under that bound and
# resolves the t=15 over-steer of 2026-05-29_235717 (sectors 0 and 2
# tied exactly at 0.09 → 90° hard right overshoot; now sector 0 wins).
W_HEADING_SMOOTH      = 0.02
# Diagnostic mirror at a higher weight, kept in `costs_heading_smooth`
# so the A/B trace continues to log what a stronger smoothing would
# have picked.
W_HEADING_SMOOTH_DIAG = 0.30

# Dead-end escape direction bias.  When all forward sectors are bad
# and the policy must pick the cheapest non-zero one to break the
# standoff, prefer a target-side direction (toward where shore
# "should be") if it's within this margin of the absolute cheapest.
# Keeps the bot pointed near its hug-side during emergencies; doesn't
# fire when a clearly cheaper escape exists on the other side.
ESCAPE_TARGET_BIAS = 0.12

# ── Ship arc model (provisional, calibrated 2026-05-30) ──────────────────────
# From data/calibration/arc_20260530_174722/ — measured in open water.
# Used by future Stop-Think-Act planning to project where the ship
# will be next tick.
#
# Acceleration is near-instant: first sample ~1.6 s after sail_start
# already shows speed at >85% of v_max.  Treat τ as "very short" rather
# than the ~1 s I initially guessed.
#
# In-turn speed behaviour is UNCERTAIN: one calibration sample showed
# 0.3 kt mid-turn (Phase C 006), but the very next sample (~7 s later,
# during a second rudder hold) read 11.9 kt — so the slowdown is NOT
# sustained.  Possibly an HUD display transient caught at one frame.
# Don't model rudder-as-brake until we have multi-sample confirmation.
SHIP_V_MAX_KT          = 12.7    # top observed speed
SHIP_ACCEL_TAU_S       = 0.4     # v(t) = v_max × (1 − exp(−t/τ))
SHIP_TURN_RATE_DPS     = 25.0    # degrees per second at top speed,
                                 # from Phase D seg0 (172° in ~7 s)

# ── Lyapunov shore-following regulator (Phase 1 — parallel logging) ──────────
# Computes a desired heading angle for shore-following as if the policy
# were a Lyapunov-based wall-follower.  This phase does NOT drive any
# actions — the output is logged alongside the existing _ideal_sector
# choice so we can measure divergence across live runs before any
# behaviour change.  See docs/shore_following_design.md §12.
#
# State variables:
#   d         — distance to shore on target beam (0..1 mini-map units)
#   d_star    — desired clearance
#   theta_err — current bow vs. shore tangent (deg, signed)
# Lyapunov function: V = ½(d − d*)² + ½kθ · θ_err²
# Control law (Phase 1):
#   turn_correction = K_D × SIDE_SIGN × (d − d_star)
#                   + K_THETA × theta_err
#   desired_heading = current_heading + turn_correction  (capped)
LYAPUNOV_D_TARGET   = 0.07    # desired shore-on-beam distance (normalised).
                              # Tuned 2026-05-30 from replay against
                              # hug_debug_175557 t=79: original 0.15 made
                              # the regulator want to turn away from a
                              # perfectly tight hug (d=0.04).  Live hug-
                              # shore stays much closer to shore than 0.15
                              # in practice.
LYAPUNOV_K_D        = 30.0    # degrees turn per unit d-error
LYAPUNOV_K_THETA    = 1.0     # tangent-error gain (already in degrees)
LYAPUNOV_K_TANGENT  = 45.0    # translates bow-vs-astern dist imbalance → deg

# Phase 1.5 — memory projection caps (see docs/shore_following_design.md
# §12.5.2).  When the current tick has no shore visible but a recent
# cached state is fresh enough, project it forward by the heading
# delta and let the regulator keep running through brief gaps.
LYAPUNOV_MEM_MAX_AGE_TICKS         = 5
LYAPUNOV_MEM_MAX_HEADING_DELTA_DEG = 45.0

# Phase 2 driver-mode A/B switch (see docs/shore_following_design.md
# §12.5.3).  Three modes:
#   "vfh"           — VFH+ drives (today's baseline; safe rollback)
#   "lyapunov"      — Lyapunov drives unconditionally when it has a
#                     proposal; VFH+ only when proposal is None.
#                     No obstacle safety net.
#   "lyapunov_safe" — Lyapunov drives, but VFH+ vetoes when Lyapunov's
#                     sector has obstacle cost ≥ LYAPUNOV_VETO_OBSTACLE_COST.
# The mode can be overridden at instantiation OR via the env var
# UWO_HUG_SHORE_DRIVER, which lets the runner flip between voyages
# without touching code.
LYAPUNOV_VETO_OBSTACLE_COST = 0.50   # same threshold used by phase=BLOCKED
HUG_SHORE_DRIVER_MODES = ("vfh", "lyapunov", "lyapunov_safe", "point_pursuit")
HUG_SHORE_DRIVER_DEFAULT = "vfh"   # 2026-05-31: switched from
# "lyapunov_safe" back to VFH+ after adding the lateral-balance
# term that fixes the channel-follower's missing corridor-centering
# signal.  See docs/shore_following_design.md §13.7.  Lyapunov modes
# remain available for A/B testing via UWO_HUG_SHORE_DRIVER but are
# no longer the path forward.
HUG_SHORE_DRIVER_ENV     = "UWO_HUG_SHORE_DRIVER"

# §13.23 — waypoint generator strategy.  "pre_phase_a" is the legacy
# path: fresh LSQ θ_err every tick, no segment commitment, no HOLD.
# "phase_a" runs `evaluate_segment` to maintain a world-frame shore-
# segment identity, may override θ_err with the committed tangent, and
# may short-circuit to a hold action on ambiguous geometry (see
# docs/shore_segment_commitment_design.md).  Default is "pre_phase_a"
# until Phase A's recommit-on-pure-rotation issue is resolved; Phase A
# is opt-in by env var or constructor arg.
WAYPOINT_GENERATOR_MODES   = ("pre_phase_a", "phase_a")
WAYPOINT_GENERATOR_DEFAULT = "pre_phase_a"
WAYPOINT_GENERATOR_ENV     = "UWO_WAYPOINT_GENERATOR"

# ── Online rate calibration (P-control with rate identification) ────────────
#
# We don't trust a fixed °/sec.  Each tick we measure how much the
# previous hold actually rotated the ship (heading_after - heading_before)
# and update an EMA estimate.  The next hold's duration is computed from
# the *live* estimate, not the static calibration.
#
# This is a discrete-time P controller with online plant identification
# — the rate estimate is essentially an integral of past observations,
# so the system tolerates ship-class / cargo / weather drift without
# manual recalibration.

RATE_INIT_DPS      = 120.0   # initial guess; replaced after a few ticks
RATE_EMA_ALPHA     = 0.25    # EMA learning rate (higher = faster adapt)
RATE_MIN_DPS       = 10.0    # safety floor — never trust slower than this
RATE_MAX_DPS       = 300.0   # safety ceiling

# Outlier rejection — heading observations outside this band are
# ignored for calibration (noise / collision bounces).
RATE_OBS_MIN_DEG   = 5.0     # below = noise
RATE_OBS_MAX_DEG   = 120.0   # above = likely collision bounce


# ── Debug formatting ────────────────────────────────────────────────────────

def _fmt_sector(sec) -> str:
    if not sec.is_observed:
        return "?/?"
    d = "-" if sec.nearest_dist is None else f"{sec.nearest_dist:.2f}"
    return f"{sec.land_fraction:.2f}/{d}"


def _fmt_heading(deg: Optional[float]) -> str:
    if deg is None:
        return "hdg=?"
    pts = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    idx = int((deg % 360 + 22.5) // 45) % 8
    return f"hdg={deg:5.1f}°({pts[idx]})"


def _fmt_sense(nav, side: Side) -> str:
    if nav is None or len(nav.sectors) < 8:
        return "sense=<no nav>"
    t_idx  = 4 if side == "starboard" else 12
    o_idx  = 12 if side == "starboard" else 4
    bt_idx = 2 if side == "starboard" else 14
    bo_idx = 14 if side == "starboard" else 2
    s = nav.sectors
    return (
        f"{_fmt_heading(nav.ship_heading_deg)} "
        f"hug={'stbd' if side == 'starboard' else 'port'} | "
        f"ahead={_fmt_sector(s[0])} "
        f"bT={_fmt_sector(s[bt_idx])} "
        f"T={_fmt_sector(s[t_idx])} "
        f"bO={_fmt_sector(s[bo_idx])} "
        f"O={_fmt_sector(s[o_idx])}"
    )


def _fmt_sense_all(nav) -> str:
    """All 8 sectors, indexed 0..7 — ahead, bow-stbd, stbd, astern-stbd,
    astern, astern-port, port, bow-port.  For diagnostic logging."""
    if nav is None or len(nav.sectors) < 8:
        return "all=<no nav>"
    parts = []
    labels = ["A", "bS", "S", "aS", "B", "aP", "P", "bP"]
    for lab, s in zip(labels, nav.sectors):
        parts.append(f"{lab}={_fmt_sector(s)}")
    return "[" + " ".join(parts) + "]"


def _nav_record(nav):
    """Serialise nav into a JSON-friendly dict for the trace JSONL."""
    if nav is None:
        return None
    return {
        "heading_deg": getattr(nav, "ship_heading_deg", None),
        "sectors": [
            {
                "i": i,
                "frac": s.land_fraction,
                "dist": s.nearest_dist,
                "obs":  s.is_observed,
            }
            for i, s in enumerate(nav.sectors)
        ],
    }


# ── VFH+ scoring ────────────────────────────────────────────────────────────

def _obstacle_cost(sec) -> float:
    """0 = clear, 1 = fully obstructed.

    Combines land_fraction (bulk signal) with a proximity boost so a
    tiny sliver of land very close still registers as obstructed.
    """
    if not sec.is_observed:
        return 1.0
    frac = sec.land_fraction
    if sec.nearest_dist is not None and sec.nearest_dist < PROXIMITY_DANGER:
        proximity = 1.0 - sec.nearest_dist / PROXIMITY_DANGER
        return max(frac, proximity)
    return frac


def _ahead_obstacle_cost(sec) -> float:
    """Inverse-square obstacle cost for sec 0 (straight ahead).

    `_obstacle_cost` caps at 1.0 — fine for "is there an obstacle?"
    decisions, wrong for "is straight ahead the cheapest sector?"
    decisions when virtual wall / rotation penalty / ideal-distance
    can push OTHER sectors above 1.0.  When ahead is genuinely walled
    (frac > AHEAD_WALL_GATE) at close range (dist < OBSTACLE_RANGE),
    we want the cost to grow without bound so sec 0 self-disqualifies.

    This is the canonical VFH+/APF formulation: 1/r² scaling so close
    obstacles dominate.  See §13.8.

    Falls through to `_obstacle_cost` when the gate doesn't fire so
    noise-pixel cases (frac ≤ 0.20) don't get artificially inflated.
    """
    if not sec.is_observed:
        return 1.0
    frac = sec.land_fraction
    if (frac > AHEAD_WALL_GATE
            and sec.nearest_dist is not None
            and sec.nearest_dist < OBSTACLE_RANGE):
        d = max(sec.nearest_dist, OBSTACLE_MIN_DIST)
        return frac * (OBSTACLE_RANGE / d) ** 2
    return _obstacle_cost(sec)


def _wall_cost(wall_distance: float) -> float:
    """Virtual-wall obstacle cost (added to opposite-side sectors).

    Linear: at wall_distance = 0, cost = WALL_COST_MAX.
    At wall_distance = WALL_COST_RANGE, cost = 0.  Beyond that, 0.
    """
    return WALL_COST_MAX * max(0.0, 1.0 - wall_distance / WALL_COST_RANGE)


def _derive_signals(history: "Deque[TickRecord]", side: Side) -> HistorySignals:
    """Compute derived temporal signals from the history deque.

    Pure function: same input → same output, no side effects.  Returns
    an all-defaults `HistorySignals` if history is empty.
    """
    sig = HistorySignals()
    if not history:
        return sig

    # Map side → which sectors count as target / opposite
    if side == "starboard":
        target_sectors = (1, 2, 3)   # bow-stbd, stbd, astern-stbd
        opp_sectors    = (5, 6, 7)
    else:
        target_sectors = (5, 6, 7)
        opp_sectors    = (1, 2, 3)

    # "Shore on side X" = max frac across that side's sectors
    # exceeds SHORE_VISIBLE_FRAC.  Walk backwards from most recent.
    for rec in reversed(history):
        if rec.sectors is None:
            continue
        # find the most-loaded target sector in this record
        best_t_sec = None
        best_t_frac = 0.0
        for i in target_sectors:
            frac = rec.sectors[i][0]
            if frac > best_t_frac:
                best_t_frac = frac
                best_t_sec = i
        if best_t_frac >= SHORE_VISIBLE_FRAC and sig.target_shore_last_seen_at_tick is None:
            sig.target_shore_last_seen_at_tick = rec.tick
            sig.target_shore_last_seen_sector = best_t_sec
        # opposite
        best_o_frac = max((rec.sectors[i][0] for i in opp_sectors), default=0.0)
        if best_o_frac >= SHORE_VISIBLE_FRAC and sig.opposite_shore_last_seen_at_tick is None:
            sig.opposite_shore_last_seen_at_tick = rec.tick
        if (sig.target_shore_last_seen_at_tick is not None
                and sig.opposite_shore_last_seen_at_tick is not None):
            break

    # Recent rotation direction over last 3 ticks (signed cumulative
    # actual_delta where available, else commanded_deg).
    last_n = list(history)[-3:]
    cum = 0.0
    for r in last_n:
        if r.actual_delta is not None:
            cum += r.actual_delta
        else:
            cum += r.commanded_deg
    if cum > 15.0:
        sig.recent_rotation_direction = +1
    elif cum < -15.0:
        sig.recent_rotation_direction = -1

    # Consecutive same-sign commanded turns (walk backwards from latest)
    last_sign = 0
    run = 0
    for r in reversed(history):
        if r.commanded_deg == 0:
            break   # a hold breaks the streak
        cur_sign = 1 if r.commanded_deg > 0 else -1
        if last_sign == 0:
            last_sign = cur_sign
            run = 1
        elif cur_sign == last_sign:
            run += 1
        else:
            break
    sig.consecutive_same_direction_turns = run

    # Speed median over last 5 readable
    speeds = [r.speed_kt for r in list(history)[-5:] if r.speed_kt is not None]
    if speeds:
        speeds.sort()
        sig.median_speed_kt = speeds[len(speeds) // 2]

    # World-motion bearing from accumulated lat/lon over the full deque.
    ll = [(r.lat, r.lon) for r in history if r.lat is not None]
    if len(ll) >= 2:
        dlat = ll[-1][0] - ll[0][0]
        dlon = ll[-1][1] - ll[0][1]
        if math.hypot(dlat, dlon) >= 0.02:   # above quantization floor
            sig.world_motion_bearing = (
                math.degrees(math.atan2(dlon, dlat)) + 360
            ) % 360

    # Per-tick velocity for ahead and bow-target sectors — used by
    # approach-detection triggers.  Catches "shore loading up fast" /
    # "distance shrinking rapidly" before static thresholds would fire.
    # Live tick 10→11 of 2026-05-29_165018: bS.frac jumped 0.02→0.25
    # and bS.dist shrank 0.13→0.07 in one tick — clear collision
    # course, but neither current value crossed AHEAD_WALL_FRAC or
    # BOW_T_CLOSE_FRAC thresholds.
    bt_idx = 2 if side == "starboard" else 14
    sig.bow_target_velocity = _sector_velocity(history, bt_idx)
    sig.ahead_velocity      = _sector_velocity(history, 0)

    # Target-beam drift rate — signed mean Δdist over the last few tick
    # transitions, on the target-beam sector.  Used by the temporal
    # drift check (replaces the single-tick t_dist > threshold rule).
    # Positive = receding (drifting away); negative = approaching.
    t_idx = 4 if side == "starboard" else 12
    sig.target_drift_rate = _sector_dist_rate(history, t_idx,
                                              window=DRIFT_RATE_WINDOW)

    # §13.14 — Target-beam frac drift rate.  Distinguishes "tight hug,
    # thin shore" (stable low frac) from "shore receding" (frac
    # dropping over multiple ticks).  Signed: negative = shore fading
    # from view.
    sig.target_frac_drift_rate = _sector_frac_rate(
        history, t_idx, window=DRIFT_RATE_WINDOW,
    )

    return sig


def _sector_velocity(history, sector_idx):
    """Compute per-tick Δ(frac, dist) for the given sector.

    Looks at the two most recent ticks.  Returns None when fewer than
    2 ticks are available or readings are missing.  Distance is
    treated as 1.0 when None (= no land detected).
    """
    if len(history) < 2:
        return None
    cur = history[-1].sectors[sector_idx]
    prev = history[-2].sectors[sector_idx]
    # SectorReading-like records are stored as (frac, dist) tuples.
    cur_frac, cur_dist = cur
    prev_frac, prev_dist = prev
    cur_d  = 1.0 if cur_dist  is None else cur_dist
    prev_d = 1.0 if prev_dist is None else prev_dist
    return (cur_frac - prev_frac, cur_d - prev_d)


def _sector_dist_rate(history, sector_idx, window: int = 3):
    """Signed mean Δdist per tick on `sector_idx`, over up to `window`
    most recent tick transitions.

    Returns:
        > 0  → distance has been growing on this sector = shore RECEDING
        < 0  → distance has been shrinking = shore APPROACHING
        ~ 0  → stable
        None → fewer than 2 history ticks available

    Averaging smooths the per-tick noise inherent in the mini-map
    distance readings.  Up to `window` transitions are averaged; fewer
    are used if history is short.
    """
    n = len(history)
    if n < 2:
        return None
    # Walk backward across up to `window` transitions; need n-1 deltas max.
    n_deltas = min(window, n - 1)
    deltas = []
    for i in range(1, n_deltas + 1):
        cur = history[-i].sectors[sector_idx]
        prev = history[-i - 1].sectors[sector_idx]
        cur_d  = 1.0 if cur[1]  is None else cur[1]
        prev_d = 1.0 if prev[1] is None else prev[1]
        deltas.append(cur_d - prev_d)
    return sum(deltas) / len(deltas)


def _sector_frac_rate(history, sector_idx, window: int = 3):
    """Signed mean Δfrac per tick on `sector_idx`, over up to `window`
    most recent tick transitions.

    Returns:
        > 0  → frac has been growing = shore COMING INTO VIEW
        < 0  → frac has been shrinking = shore FADING FROM VIEW
        ~ 0  → stable (either tight hug or open water steady)
        None → fewer than 2 history ticks

    Mirror of `_sector_dist_rate`.  Used by §13.14 to distinguish
    tight-hug-thin-shore (stable low frac) from shore-receding
    (frac dropping over multiple ticks).
    """
    n = len(history)
    if n < 2:
        return None
    n_deltas = min(window, n - 1)
    deltas = []
    for i in range(1, n_deltas + 1):
        cur = history[-i].sectors[sector_idx]
        prev = history[-i - 1].sectors[sector_idx]
        deltas.append(cur[0] - prev[0])  # cur_frac - prev_frac
    return sum(deltas) / len(deltas)


# ── Lyapunov shore-following regulator (Phase 1) ────────────────────────────
#
# Pure-math regulator that produces a desired heading angle for
# shore-following.  Runs in PARALLEL with _ideal_sector for Phase 1 —
# the output is logged but does NOT drive any actions.  Designed so
# we can measure divergence from the existing rule tangle before any
# behaviour change.  See docs/shore_following_design.md.

def _shore_tangent_angle(nav, side: Side) -> Optional[float]:
    """Estimate the angle (degrees) between ship's bow and the local
    shore tangent on the hug side.

    Returns None when too few shore points are visible to fit a line.

    Positive return value = shore tangent rotated CW from bow → bot
    should turn RIGHT to align (for starboard hug); LEFT for port hug.

    Implementation: weighted least-squares fit of a line through the
    three hug-side shore sample points (bow-target, beam-target,
    astern-target).  This is the textbook tangent estimator from
    Lyapunov wall-following — the 2-point bow-vs-astern differential
    we used before gave wrong signs in corner geometries (live
    `astern_pull_beats` scenario: returned -8.6° when correct was
    +20.2°).

    See docs/shore_following_design.md §13.10 for the principled-fix
    rationale (replaces astern-pull's role with continuous tangent
    tracking, matching the Lyapunov wall-following literature).
    """
    # §13.20: 16-sector indices preserving the same compass bearings
    # as the original 8-sector fit (bow-stbd 45°, stbd-beam 90°,
    # aft-stbd 135°; port mirrors).
    if side == "starboard":
        sectors_with_bearing = [(2, 45.0), (4, 90.0), (6, 135.0)]
    else:
        sectors_with_bearing = [(14, -45.0), (12, -90.0), (10, -135.0)]

    # Collect ship-frame shore points: +x is starboard, +y is forward.
    points: list[tuple[float, float, float]] = []
    for sec_idx, bearing_deg in sectors_with_bearing:
        sec = nav.sectors[sec_idx]
        if (sec.nearest_dist is None
                or sec.land_fraction < SHORE_VISIBLE_FRAC):
            continue
        bearing_rad = math.radians(bearing_deg)
        x = sec.nearest_dist * math.sin(bearing_rad)
        y = sec.nearest_dist * math.cos(bearing_rad)
        points.append((x, y, sec.land_fraction))

    if len(points) < 2:
        return None

    # Weighted least-squares fit: x = a × y + b (line nearly parallel
    # to bow direction is the "good hug" case → slope_dxdy small).
    total_w = sum(p[2] for p in points)
    mean_x  = sum(p[0] * p[2] for p in points) / total_w
    mean_y  = sum(p[1] * p[2] for p in points) / total_w
    sxy = sum(p[2] * (p[0] - mean_x) * (p[1] - mean_y) for p in points)
    syy = sum(p[2] * (p[1] - mean_y) ** 2              for p in points)

    if syy < 1e-6:
        # All points at same y → shore perpendicular to bow direction.
        return 90.0 if mean_x > 0 else -90.0

    slope_dxdy = sxy / syy
    return math.degrees(math.atan(slope_dxdy))


def _lyapunov_state(nav, side: Side) -> Optional[tuple[float, float, float]]:
    """Read the Lyapunov state variables `(d, d_star, theta_err)` from
    the current sector observation.

    Returns None when shore is not visible on the target side — in
    that case the regulator can't regulate and the caller falls back
    to other behaviour (the FSM in later phases; the existing rules
    in Phase 1).
    """
    # §13.20: 16-sector indices preserving the same compass bearings
    # (target beam ±90°, bow-target ±45°, aft-target ±135°).
    target_idx = 4 if side == "starboard" else 12
    bow_t_idx  = 2 if side == "starboard" else 14
    astern_idx = 6 if side == "starboard" else 10
    target = nav.sectors[target_idx]
    bow_t  = nav.sectors[bow_t_idx]
    astern = nav.sectors[astern_idx]

    # Shore-visible gate: same conditions used elsewhere.
    target_visible = (
        target.land_fraction >= SHORE_VISIBLE_FRAC
        or (target.nearest_dist is not None
            and target.nearest_dist < SHORE_VISIBLE_DIST)
    )
    bow_t_visible = (
        bow_t.land_fraction >= SHORE_VISIBLE_FRAC
        or (bow_t.nearest_dist is not None
            and bow_t.nearest_dist < SHORE_VISIBLE_DIST)
    )
    if not (target_visible or bow_t_visible):
        return None

    d = target.nearest_dist if target.nearest_dist is not None else 1.0

    # §13.10 — Tangent estimation via multi-point least-squares fit on
    # the hug-side shore points.  Replaces the Phase-1 2-point
    # bow-vs-astern differential, which gave wrong signs in corner
    # geometries.  Returns a signed degrees value; +ve = shore tangent
    # rotated CW from bow (for starboard) → turn RIGHT.
    theta_err = _shore_tangent_angle(nav, side)
    if theta_err is None:
        # Fall back to old 2-point estimator when fit can't be made
        # (only 1 sector visible).  Still useful in degraded cases.
        bt_dist = bow_t.nearest_dist  if bow_t.nearest_dist  is not None else 1.0
        at_dist = astern.nearest_dist if astern.nearest_dist is not None else 1.0
        theta_err = LYAPUNOV_K_TANGENT * (at_dist - bt_dist)

    return (d, LYAPUNOV_D_TARGET, theta_err)


def _lyapunov_correction(
    state: tuple[float, float, float], side: Side,
) -> float:
    """Shared control law — returns the capped per-tick turn correction
    in degrees from a `(d, d_star, theta_err)` state.  Used by both the
    live-state path in `compute_desired_heading_lyapunov` and the
    projected-state path in HugShoreGoal.tick()."""
    d, d_star, theta_err = state
    side_sign = +1.0 if side == "starboard" else -1.0
    turn_correction = (LYAPUNOV_K_D * side_sign * (d - d_star)
                       + LYAPUNOV_K_THETA * theta_err)
    return max(-MAX_TURN_PER_TICK,
               min(MAX_TURN_PER_TICK, turn_correction))


def _project_lyap_state(
    memory: Optional[dict],
    current_heading_deg: Optional[float],
    current_tick: int,
) -> Optional[tuple[float, float, float]]:
    """Project a cached `_lyapunov_state` forward by the heading delta
    since it was recorded.  Phase 1.5 — see docs/shore_following_design.md
    §12.5.2.

    Returns (d, d_star, theta_err_projected) or None when the cache is
    stale or the projection's preconditions don't hold.

    Caps:
    - age   ≤ LYAPUNOV_MEM_MAX_AGE_TICKS
    - |Δhdg| ≤ LYAPUNOV_MEM_MAX_HEADING_DELTA_DEG
    """
    if memory is None or current_heading_deg is None:
        return None
    age = current_tick - memory["tick"]
    if age <= 0 or age > LYAPUNOV_MEM_MAX_AGE_TICKS:
        return None
    heading_delta = ((current_heading_deg - memory["heading"] + 180.0)
                     % 360.0 - 180.0)
    if abs(heading_delta) > LYAPUNOV_MEM_MAX_HEADING_DELTA_DEG:
        return None
    # Theta_err rotates with the ship: if we turned +30°, the cached
    # tangent now sits 30° to our LEFT relative to the new bow.
    theta_err_now = memory["theta_err"] - heading_delta
    return (memory["d"], memory["d_star"], theta_err_now)


def compute_desired_heading_lyapunov(
    nav, side: Side, current_heading_deg: Optional[float],
) -> Optional[float]:
    """Lyapunov-based desired heading for shore-following.

    Returns the compass-frame desired heading in degrees, or None when
    shore is not visible on the target side (caller falls back).

    Control law:
        turn_correction = K_D × SIDE_SIGN × (d − d_star)
                        + K_THETA × theta_err
    capped at MAX_TURN_PER_TICK.

    For starboard hug:
        d > d_star (too far)  → SIDE_SIGN × (d-d*) > 0 → turn RIGHT
        d < d_star (too close)→ SIDE_SIGN × (d-d*) < 0 → turn LEFT
    For port hug: mirror.
    """
    if current_heading_deg is None:
        return None
    state = _lyapunov_state(nav, side)
    if state is None:
        return None
    return (current_heading_deg + _lyapunov_correction(state, side)) % 360.0


def _heading_to_sector_index(desired_heading_deg: float,
                             current_heading_deg: float) -> int:
    """Convert a desired-heading-in-compass-frame to a sector index in
    the bot's local frame.  Sector 0 = directly ahead; sectors
    1,2,3 = +45°, +90°, +135° from bow (clockwise); sectors 7,6,5 =
    −45°, −90°, −135°.

    Used by the Phase 1 divergence analysis to compare Lyapunov's
    proposed direction against the actual `ideal_idx` from
    `_ideal_sector`.
    """
    delta = ((desired_heading_deg - current_heading_deg + 540.0) % 360.0) - 180.0
    # Round to nearest sector (each sector spans 45°).
    sector_float = delta / 45.0
    sector_int = int(round(sector_float)) % 8
    return sector_int


def _is_wrong_side(nav, side: Side) -> bool:
    """True iff target side is empty AND opposite side has substantial
    shore — the U-turn-needed trap.

    Target counts as "empty" when EITHER:
      - frac is essentially noise (< WRONG_SIDE_TINY_FRAC) — a single
        rocky outcrop or noise sliver close by doesn't constitute real
        hug-shore, even if its `nearest_dist` reads small.
      - OR frac is moderate (< SHORE_VISIBLE_FRAC) AND nothing is at
        close range (dist > SHORE_VISIBLE_DIST) — weak distant signal.

    Without the first clause, the bot stayed HUGGING for 5+ ticks
    while sailing north with shore *clearly* on its port side
    (P=0.83+) — the tiny S=0.01/0.07 reading kept target_empty=False.
    Live ticks 1-5 of 2026-05-29_143711 run.
    """
    t_idx  = 4 if side == "starboard" else 12
    o_idx  = 12 if side == "starboard" else 4
    bo_idx = 14 if side == "starboard" else 2
    ao_idx = 10 if side == "starboard" else 6   # astern-opposite
    target = nav.sectors[t_idx]
    opp    = nav.sectors[o_idx]
    bow_o  = nav.sectors[bo_idx]
    astern_o = nav.sectors[ao_idx]
    target_empty = (
        target.land_fraction < WRONG_SIDE_TINY_FRAC
        or (
            target.land_fraction < SHORE_VISIBLE_FRAC
            and (target.nearest_dist is None
                 or target.nearest_dist > SHORE_VISIBLE_DIST)
        )
    )
    # Opposite-side shore can sit in beam, bow, OR astern.  Astern-opp
    # is the "just bounced/overshot past the coast" signature — shore
    # was on port, the ship overshot a left turn, and shore is now
    # behind on the port side.  Without this check the detector waits
    # for shore to rotate around to bow-port before firing.  Live ticks
    # 115-122 of 2026-05-29_145608: aP=0.31/0.34 sat for 7 ticks while
    # the bot drifted around in wrong-side configuration.
    opp_has_shore = (
        opp.land_fraction      >= WRONG_SIDE_OPP_FRAC
        or bow_o.land_fraction  >= WRONG_SIDE_OPP_FRAC
        or astern_o.land_fraction >= WRONG_SIDE_OPP_FRAC
    )
    return target_empty and opp_has_shore


def _ideal_sector(
    nav, side: Side, wall_distance: float,
    signals: Optional[HistorySignals] = None,
) -> int:
    """Pick the heading the cost function biases toward this tick.

    Wall-follow override (highest priority): when ahead is walled AND
    the opposite-beam is clear, peel AWAY from target — classic right-
    hand-on-the-wall rule.  This overrides the recovery pull so the
    bot doesn't rotate INTO the shore it's trying to hug.

    Otherwise:
      - shore present (visible OR just behind) → ahead-target.
        The cost function decides via obstacle + lateral + tangent
        terms whether to actually turn.  At steady-state hug the
        cost margin to sec 0 is tiny → variable-duration scaling
        produces ~no turn ("hold" emerges naturally).  At drift the
        margin grows proportionally and the bot turns appropriately.
        Matches the continuous-correction shape of textbook Lyapunov
        wall-following — there's no discrete "hold" state.

      - shore truly lost (no T frac, no astern hint) →
        bow-target as the default search bearing.

    The `signals` argument is optional for backward compatibility (single-
    tick tests pass None).  When provided, the rate-of-change and shore-
    just-lost triggers can fire.
    """
    ahead = nav.sectors[0]
    o_idx  = 12 if side == "starboard" else 4
    o_sec  = nav.sectors[o_idx]

    bt_idx_pre = 2 if side == "starboard" else 14   # bow-target
    bow_t_pre  = nav.sectors[bt_idx_pre]
    bt_dist_pre = bow_t_pre.nearest_dist if bow_t_pre.nearest_dist is not None else 1.0

    # Approach-velocity peel trigger (Phase A.2).
    # If bow-target is loading up fast AND closing fast across the last
    # two ticks, we're on collision course even if the current readings
    # haven't crossed the static BOW_T_CLOSE_FRAC threshold.  Live tick
    # 10→11 of 2026-05-29_165018: bS jumped 0.02→0.25, dist 0.13→0.07.
    # Static check missed it (0.25 < 0.40); velocity check catches it.
    bt_approaching_fast = False
    if signals is not None and signals.bow_target_velocity is not None:
        dfrac, ddist = signals.bow_target_velocity
        if dfrac >= APPROACH_DFRAC_DANGER and ddist <= APPROACH_DDIST_DANGER:
            bt_approaching_fast = True

    # Astern-target check — computed BEFORE wall-follow so the override
    # can be gated on it.  When astern-target is heavily loaded, the
    # ship just rounded a corner and the shore we want is behind us; a
    # blanket "peel away from any obstacle ahead" is wrong because it
    # abandons the hug-line.  Live t=29/t=30 of 2026-05-29_205124:
    # aS=0.86-0.92 while bS=0.41/.06 — wall-follow fired and pulled the
    # bot LEFT (away from target) into open water instead of curving
    # back toward the shore we'd just lost.
    at_idx = 6 if side == "starboard" else 10
    astern_t = nav.sectors[at_idx]
    at_dist_pre = astern_t.nearest_dist if astern_t.nearest_dist is not None else 1.0
    astern_t_loaded = (
        astern_t.land_fraction >= ASTERN_PULL_FRAC
        or (at_dist_pre < ASTERN_PULL_DIST
            and astern_t.land_fraction >= WRONG_SIDE_TINY_FRAC)
    )

    # Wall-follow override.  Fires whenever ahead is loaded AND the
    # opposite beam is genuinely clear.  When the opposite side has
    # real obstacles (tick-25 wrong-side trap), we let the normal
    # cost min decide — that case has no good answer.
    #
    # Three triggers, same response:
    #   1. Ahead is loaded (the original — shore curving across our path)
    #   2. Bow-target is loaded AND close (the collision-approach signature
    #      — shore is on the hug-side but moving forward into the bow,
    #      typical of a coast curving inward).  Live tick 14 of the
    #      2026-05-29_133115 run: bS=0.53/0.28 → bot held → tick 16 was
    #      in contact.
    #   3. Bow-target is approaching fast (Phase A.2) — catches the case
    #      where static thresholds haven't fired yet but trajectory is
    #      obvious from the last tick's reading.
    #
    # Gate: when astern-target is loaded AND bow-target isn't IMMINENT
    # (very close, <BOW_T_IMMINENT_DIST), suppress the override and let
    # astern-pull (below) decide.  Bow-target IMMINENT still wins because
    # collision avoidance must beat hug-line recovery at very close range.
    bow_t_close_loaded = (
        bow_t_pre.land_fraction >= BOW_T_CLOSE_FRAC
        and bt_dist_pre < BOW_T_CLOSE_DIST
    )
    bow_t_imminent = (
        bow_t_pre.land_fraction >= BOW_T_CLOSE_FRAC
        and bt_dist_pre < BOW_T_IMMINENT_DIST
    )
    astern_blocks_wall_follow = astern_t_loaded and not bow_t_imminent

    if (not astern_blocks_wall_follow
            and (ahead.land_fraction >= AHEAD_WALL_FRAC
                 or bow_t_close_loaded
                 or bt_approaching_fast)):
        opp_beam_clear = (
            o_sec.land_fraction < OPP_BEAM_CLEAR_FRAC
            and (o_sec.nearest_dist is None
                 or o_sec.nearest_dist >= OPP_BEAM_CLEAR_DIST)
        )
        if opp_beam_clear:
            # §13.9 — escalate peel from bow-opposite (45°) to beam-
            # opposite (90°) when bow-opposite ALSO has substantial
            # obstacle.  Operationally: "the bot is in a pinch with
            # shore on both sides, treat them as one continuous mass
            # on the hug side by peeling past the bow-opposite obstacle
            # so BOTH end up on starboard after the turn."
            # Geometry verified for hug_debug_20260531_075222 t=153:
            # 90° left peel puts right shore astern and the island
            # bow-right — both on starboard, normal hug resumes.
            bow_opp_sec = nav.sectors[_AHEAD_OPPOSITE_SECTOR[side]]
            if (bow_opp_sec.land_fraction >= BOW_OPP_PINCH_FRAC
                    and bow_opp_sec.nearest_dist is not None
                    and bow_opp_sec.nearest_dist < OBSTACLE_RANGE):
                return _BEAM_OPPOSITE_SECTOR[side]
            return _AHEAD_OPPOSITE_SECTOR[side]

    t_idx  = 4 if side == "starboard" else 12
    bt_idx = bt_idx_pre
    target = nav.sectors[t_idx]
    bow_t  = bow_t_pre

    t_dist  = target.nearest_dist if target.nearest_dist is not None else 1.0
    bt_dist = bow_t.nearest_dist  if bow_t.nearest_dist  is not None else 1.0
    at_dist = at_dist_pre

    # Close-distance hits count as shore only if frac is non-trivial.
    # A tiny sliver (frac < WRONG_SIDE_TINY_FRAC) close-by is noise or a
    # rocky outcrop — not real hug-shore.  Without this, a small
    # close reading masks genuine wrong-side situations (see
    # _is_wrong_side docstring for the live failure).
    t_close_real = t_dist < SHORE_VISIBLE_DIST and target.land_fraction >= WRONG_SIDE_TINY_FRAC
    bt_close_real = bt_dist < SHORE_VISIBLE_DIST and bow_t.land_fraction >= WRONG_SIDE_TINY_FRAC
    shore_present = (
        target.land_fraction >= SHORE_VISIBLE_FRAC
        or bow_t.land_fraction  >= SHORE_VISIBLE_FRAC
        or t_close_real
        or bt_close_real
        or astern_t_loaded   # hug-line just behind us → still "present"
    )

    # Wrong-side trap: target side empty but opposite side has shore.
    # Peel toward opp-shore to initiate U-turn.
    if not shore_present:
        if _is_wrong_side(nav, side):
            return _AHEAD_OPPOSITE_SECTOR[side]
        return _AHEAD_TARGET_SECTOR[side]        # truly lost

    # Astern-target pull: shore is behind on the hug side.  Don't
    # hold ahead — pull toward bow-target so the bot curves back.
    # Fires only when T and bT don't already see the shore clearly
    # (otherwise the normal drift logic handles it).
    if astern_t_loaded and t_dist > wall_distance:
        return _AHEAD_TARGET_SECTOR[side]

    # Drift detection.  Fires when the target-BEAM (T) shows that shore
    # is actually moving AWAY from the corridor — recover toward target.
    #
    # Two ways to detect drift:
    #   1) Temporal (preferred, signals available): mean Δdist on the
    #      target-beam sector over the last few transitions.  Positive =
    #      receding.  This is the discriminator that distinguishes real
    #      drift from a single noisy frame or corner geometry.
    #   2) Single-tick fallback: |T.nearest_dist| > wall × MULT.  Used
    #      when history is too short for a rate-based read.
    #
    # CRITICAL GUARD (both modes): bow-target must ALSO be empty.
    # Without this, drift can pull ideal toward bow-target even when
    # bow-target already has close shore — exactly what happened at
    # t11 of 2026-05-29_165018 (bS=0.25/0.07).
    bow_t_empty_for_drift = (
        bow_t.land_fraction < SHORE_VISIBLE_FRAC
        and (bow_t.nearest_dist is None
             or bow_t.nearest_dist >= SHORE_VISIBLE_DIST)
    )
    if bow_t_empty_for_drift:
        rate = signals.target_drift_rate if signals is not None else None
        if rate is not None:
            # Temporal: shore is actually receding over multiple ticks.
            if rate > DRIFT_RATE_THRESHOLD:
                return _AHEAD_TARGET_SECTOR[side]
            # rate <= threshold (stable or approaching) → don't pull.
        else:
            # Fallback: rely on the single-tick threshold.
            if t_dist > wall_distance * DRIFT_THRESHOLD_MULT:
                return _AHEAD_TARGET_SECTOR[side]

    # §13.12 — Option A.  The fall-through used to return sec 0
    # ("shore visible + in corridor → hold").  That hand-rolled
    # categorical rule conflicts with the proportional Lyapunov
    # controller (§13.7 lateral term + §13.10 tangent bias): the
    # rule would override the proportional drift signal with a
    # discrete "hold" decision, collapsing the cost margin and
    # making the bot under-turn in moderate-drift cases.
    #
    # Replaced with: always return bow-target as the "ideal" direction.
    # The cost function then decides via obstacle + lateral + tangent
    # terms whether to actually turn.  At steady-state hug the margin
    # is tiny (variable-duration scaling produces ~no turn); at drift
    # the margin grows proportionally and the bot turns appropriately.
    # Matches the textbook Lyapunov wall-following formulation where
    # the controller emits a continuous correction with no discrete
    # "hold" state.
    #
    # Hand-rolled "hold" rule was a compensation for the cost
    # function's missing drift signal pre-§13.7 — once §13.7 added
    # the proper proportional signal, the rule became redundant for
    # steady-state and harmful for drift.
    return _AHEAD_TARGET_SECTOR[side]


# Bow-target close+loaded trigger — fires the wall-follow override
# when shore is loading on the bow-target sector at close range, before
# it reaches the ahead sector.  Catches the typical collision-approach
# geometry where a coast curves inward and the bow points at it for
# multiple ticks before A.frac crosses AHEAD_WALL_FRAC.
# Tuned from t14 of the 2026-05-29_133115 run: bS=0.53/0.28.
# Normal tight hugging has either bS.frac<0.30 or bS.dist>0.30, so
# this threshold pair doesn't trigger during legitimate hugging.
BOW_T_CLOSE_FRAC = 0.40
BOW_T_CLOSE_DIST = 0.30
# Bow-target IMMINENT — distance threshold at which collision avoidance
# trumps astern-pull.  When bow-target is BOW_T_CLOSE_FRAC-loaded AND
# closer than this, we peel away even if astern-pull would otherwise
# fire (because hitting the close shore is worse than abandoning the
# hug-line for one tick).  Set well below BOW_T_CLOSE_DIST so normal
# close-corner situations let astern-pull win.  Live t=30 of
# 2026-05-29_205124: bS=0.41/0.06 was a close-corner case the user
# confirmed wall-follow was the right call (very close shore on bow-
# stbd, peeling left bought clearance).
BOW_T_IMMINENT_DIST = 0.10

# Opp-bow rotation safety distance.  When the commanded rotation would
# pivot the bow into close opp-shore, scale rotation magnitude by
# `opp_bow.dist / OPP_BOW_SAFE_DIST` so a 45° peel becomes (e.g.) 22°
# when the shore is at half this distance.  Picked at 0.20 so we still
# turn meaningfully when there's any space, but throttle when shore is
# at point-blank.  Live t=24 of 2026-05-30_114024: bP=0.92/0.11 → scale
# = 0.55 → 45° commanded becomes ~25°, less collision-bouncy.
OPP_BOW_SAFE_DIST = 0.20

# Bow-target approach-velocity trigger — fires the wall-follow override
# when bow-target shore is *loading fast* even though static thresholds
# haven't crossed yet.  Catches the t10→t11 case from 2026-05-29_165018:
# bS went (0.02, 1.00) → (0.25, 0.07) in a single tick — frac jumped
# +0.23 and dist crashed −0.93.  Static BOW_T_CLOSE_FRAC=0.40 didn't
# fire, but the approach rate clearly does.  Thresholds are intentionally
# conservative — anything looser would fire during normal corridor
# tightening.
APPROACH_DFRAC_DANGER = 0.15
APPROACH_DDIST_DANGER = -0.04

AHEAD_WALL_FRAC = 0.30   # ahead is "genuinely walled" — shore curving
                         # across our path.  Suppress virtual wall so
                         # cost function can peel away through the
                         # opposite-side opening.  Note: based on FRAC
                         # only, not dist — a close *sliver* ahead (low
                         # frac + close dist) is fine, we just want to
                         # avoid sailing into a real wall.

# ── §13.16 Bug2/Trémaux wall-ahead commit ─────────────────────────────────
# Classical right-hand-on-wall discipline (Trémaux 1882; Lumelsky &
# Stepanov 1987 Bug2): when an obstacle blocks the forward path, commit
# to a *fixed* turn direction and STAY with it until the obstacle clears.
# For a starboard hugger this means turn LEFT (sec 7); for port hug,
# turn RIGHT (sec 1).  No weights, no cost trade-offs — the commitment
# is absolute while ahead is walled.
#
# Motivated by t=46-50 of hug_debug_20260531_150415 where the bot,
# facing Port Said coast, flip-flopped sec 1 ↔ sec 7 every tick because
# their VFH+ costs were nearly tied.  The Bug rule deterministically
# breaks this tie — same direction every tick → escape in one consistent
# sweep instead of zero net progress.
#
# Entry threshold = AHEAD_WALL_FRAC (0.30).  Exit threshold lower for
# hysteresis — must see clear-ahead frame to release commit, not just a
# transient dip.
WALL_AHEAD_EXIT_FRAC = 0.15

# ── §13.17 Lyapunov point-pursuit (driver_mode="point_pursuit") ───────────
# Goal-point-based steering per `docs/lyapunov_point_pursuit_design.md`.
# Replaces the §13.16 Bug2/Trémaux stack with:
#   Goal-Point Generator → Lyapunov point-stabilization → canonical VFH+
#   exclusion-based candidate masking.
#
# `POINT_PURSUIT_L` — forward step in game-units (lat/lon delta magnitude)
# used to project the next target point ahead of current position.  Chosen
# small enough to keep the target in observable space, large enough that
# Lyapunov V is dominated by heading error rather than discretization noise.
POINT_PURSUIT_L                = 0.10   # ~one tick of cruise distance

# Phase 2: FrontierPicker commit window.  After the picker fires on
# a STUCK verdict, its target point overrides the destination-
# generator for this many ticks.  Long enough to break the
# oscillation that triggered the picker; short enough that the
# bot doesn't get yanked off a real shore-following path if the
# picker's target turns out to be infeasible.
PICKER_COMMIT_WINDOW_TICKS     = 30


def _make_frontier_picker(name: str):
    """Factory for the configured FrontierPicker backend.

    Production callers get a TremauxPicker whose JunctionDetector is
    configured with `persistence_n=3` — Brunskill-2007-style multi-frame
    hysteresis that requires three consecutive matching classifications
    before the picker acts.  This is the canonical fix for the Z-bend
    false-positive class: mid-turn the geometry briefly opens what looks
    like a JUNCTION, then closes again; with persistence=1 the picker
    would fire and corrupt the topology graph.

    Tests construct `TremauxPicker()` directly (persistence_n=1 default)
    to exercise per-tick semantics without the hysteresis layer.
    """
    if name == "yamauchi":
        return YamauchiPicker()
    if name == "tremaux":
        from brain.goals.junction_detector import JunctionDetector
        return TremauxPicker(detector=JunctionDetector(persistence_n=3))
    return None
# `POINT_PURSUIT_ACCEPTANCE_DEG` — per-axis arrival tolerance.  When BOTH
# `|current_lat − endpoint_lat| < this` AND
# `|current_lon − endpoint_lon| < this`, the voyage transitions to
# COMPLETE.  Lenient by default (1° game-unit) — game coordinates are
# coarse and "close enough to dock at the destination port" is what
# the user actually cares about.
POINT_PURSUIT_ACCEPTANCE_DEG   = 1.0
# `POINT_PURSUIT_SAFETY_DIST`  — candidate-mask threshold.  Sectors whose
# nearest_dist (normalized 0..1) is below this are excluded from the
# survivor set, regardless of how well they align with the desired heading.
# Reuses §13.15 collision concept — anything this close ahead is "imminent."
POINT_PURSUIT_SAFETY_DIST      = 0.12


def _make_vfh_config(
    safety_dist: float,
    graduated: bool = False,
) -> VFHPlusConfig:
    """Build a `VFHPlusConfig` for the avoider.

    `graduated=True` enables §13.30 graduated density (raised
    hard-mask ceiling 0.6→0.85 + quadratic penalty in the (0.6, 0.85]
    soft band).  When False, legacy binary masking at frac > 0.6
    applies (the stoprej_dest8 baseline behaviour).

    Forced on via `UWO_GRADUATED_DENSITY=1` env var regardless of the
    `graduated` argument — kept for diagnostic A/B runs that flip
    everything (open + narrow) on at once.  Production callers should
    use the kwarg to selectively enable graduated only in the
    narrow_cfg (§13.34): graduated density helps in tight channels
    (t251 / t307 / t495 borderline-mask traps) and hurts in open water
    (admits noisy near-shore candidates that pull the bot off course
    — see explore_port_20260603_143438 trajectory)."""
    import os
    if graduated or os.environ.get("UWO_GRADUATED_DENSITY") == "1":
        return VFHPlusConfig(
            safety_dist=safety_dist,
            land_fraction_ceiling=0.85,
            density_soft_floor=0.6,
            density_penalty_weight=10000.0,
        )
    return VFHPlusConfig(safety_dist=safety_dist)
# Lateral correction gain for hug-mode target.  When the bot is off the
# hug-line (d ≠ d_star), bias the target perpendicular to the shore
# tangent toward d_star.  Small value — keeps the correction gentle.
POINT_PURSUIT_HUG_LATERAL_GAIN = 0.5
# Combined-mode blend weight (destination influence vs hug influence).
# 0.5 = equal contribution.  Future: cross-track-error-driven per
# Lekkas-Fossen, but a fixed 0.5 covers the common case where the
# bot is reasonably hugging while heading toward a destination.
POINT_PURSUIT_COMBINED_DEST_W  = 0.5

# Shore intrusion: when the bot is hugging TIGHT (huge T.frac), the
# right-beam landmass leaks into adjacent sectors' nearest_dist
# readings.  The bot would interpret "ahead.dist=0.07 because of
# right-shore corner" as "obstacle ahead" and spuriously turn away.
# Detect this and discount the proximity contribution on sector 0.
#
# Three conditions ALL must hold:
#  - Strong shore confidence on target side (T.frac high)
#  - Ahead has essentially zero MASS (frac near zero — anything
#    measurable means real obstacle, not edge-bleed)
#  - Ahead-dist is "moderate close" but not collision-imminent
#    (a close-touching wall is real, even with low frac)
INTRUSION_T_FRAC      = 0.70   # T strongly loaded
INTRUSION_T_CLOSE_DIST = 0.05  # OR T very close (shore touching beam,
                               # frac may be small if only a corner is
                               # visible — observed tick 1 of Tripoli run
                               # 2026-05-28: T.dist=0.03, T.frac=0.06)
INTRUSION_AHEAD_FRAC  = 0.05   # ahead frac essentially zero
INTRUSION_AHEAD_MIN_DIST = 0.06  # below this, ahead is a REAL wall


# ── §13.15 VFH* swept-volume lookahead ────────────────────────────────────
# Per the t=101-103 case of hug_debug_20260531_130527: VFH+ alone misses
# slow bow approaches because sec0 is unobserved (no land *currently*
# ahead) and sec1's land doesn't trigger AND-gated static thresholds.
# This swept-volume lookahead — also known as the "collision cone" in
# marine-USV literature (Fiorini & Shiller 1998 Velocity Obstacles,
# Fox/Burgard/Thrun 1997 DWA trajectory scoring) — addresses this by
# projecting the bow's forward path for each candidate command and
# scoring proximity to every observed shore pixel along that path.
#
# Why this is in VFH* spirit but not classical-tree-search VFH*:
# - Classical VFH* tracks the full polar histogram and re-bins after
#   each rollout step.  At our binning resolution (8 sectors × nearest-
#   pixel only) we can't predict a peninsula's tip into sec0 from sec1's
#   observation (different pixels, not the same one rotated).
# - Bow-path projection sidesteps this: we ask "does the bow's future
#   path pass within COLLISION_SAFETY of any *currently observed*
#   shore?" — this catches the t=101 case because sec1's closest pixel
#   at (β=45°, d=0.29) is ~0.20 from the straight-ahead bow path.
#
# Distance at which the bow path "feels" shore.  Picked at 0.30 (just
# above WALL_DIST_INIT=0.20) so the term kicks in as proximity becomes
# concerning, with quadratic ramp to zero at the threshold.
COLLISION_SAFETY        = 0.30
# Weight on the swept-volume penalty.  Tuned so per-tick contribution
# is same order as W_OBSTACLE — competes with VFH+ obstacle term
# without overwhelming directional / lateral-balance terms.
K_LOOKAHEAD             = 2.0
# Tight-shore gate: when any forward sector (7, 0, 1) already has shore
# closer than this, suppress the lookahead.  Rationale: at tight
# distances every forward path passes close to shore by definition;
# the swept-volume term degenerates into "everywhere is dangerous"
# and overrides VFH+'s carefully-tuned rotation_penalty / lateral-
# barrier / ideal-sector logic that handles tight hugging.  All 5
# logged regression scenarios (close_T_rotation_hazard,
# close_T_low_frac_intrusion, river_channel_opens_right,
# wrong_side_u_turn, lost_shore_tied_cost_holds_course) fall in
# this regime.  The t=101 peninsula-approach case is sec1.d=0.29,
# well above this gate — the lookahead's intended domain is
# "approaching, not yet arrived."
LOOKAHEAD_TIGHT_SHORE_DIST = 0.15


def _segment_point_dist_sq(ax: float, ay: float,
                            bx: float, by: float,
                            px: float, py: float) -> float:
    """Squared distance from point (px, py) to segment (a)–(b)."""
    dx, dy = bx - ax, by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq < 1e-12:
        return (px - ax) ** 2 + (py - ay) ** 2
    t = ((px - ax) * dx + (py - ay) * dy) / seg_len_sq
    t = max(0.0, min(1.0, t))
    cx = ax + t * dx
    cy = ay + t * dy
    return (px - cx) ** 2 + (py - cy) ** 2


def _extract_obstacles(nav) -> list[tuple[float, float]]:
    """Ship-relative Cartesian (x=ahead, y=starboard) of each observed
    sector's nearest-pixel.  Sectors below LATERAL_VALID_FRAC are
    skipped — a sector with frac≈0 may carry a sentinel `nearest_dist`
    value (e.g. 0.21) without any real shore there, and using it as
    an obstacle produces phantom collisions.
    """
    out = []
    for sec in nav.sectors:
        if not sec.is_observed or sec.nearest_dist is None:
            continue
        if sec.land_fraction < LATERAL_VALID_FRAC:
            continue
        beta_rad = math.radians(sec.bearing_deg)
        out.append((sec.nearest_dist * math.cos(beta_rad),
                    sec.nearest_dist * math.sin(beta_rad)))
    return out


def _segment_collision_cost(ax: float, ay: float,
                             bx: float, by: float,
                             obstacles: list[tuple[float, float]]) -> float:
    """Sum of quadratic ramp penalties: K × ((SAFETY − d) / SAFETY)²
    for each obstacle within SAFETY of the segment.  Bounded: zero at
    the safety threshold, K at zero distance.  Quadratic shape
    discourages collisions smoothly rather than the harsh 1/d² spike."""
    cost = 0.0
    safety_sq = COLLISION_SAFETY * COLLISION_SAFETY
    for ox, oy in obstacles:
        dsq = _segment_point_dist_sq(ax, ay, bx, by, ox, oy)
        if dsq >= safety_sq:
            continue
        d = math.sqrt(dsq)
        ratio = (COLLISION_SAFETY - d) / COLLISION_SAFETY
        cost += K_LOOKAHEAD * ratio * ratio
    return cost


def _lookahead_collision_cost(nav,
                               side: Side,
                               speed_kt: Optional[float],
                               depth: int = LOOKAHEAD_DEPTH,
                               ) -> dict[int, float]:
    """For each candidate sector, compute the *min*-trajectory swept-
    volume collision cost over a depth-step rollout.

    Discounted by γ per step inside a trajectory.  Outer caller applies
    one more γ when blending with the VFH+ snapshot cost.

    Returns {sector_idx: lookahead_cost}.  Sectors not in
    CANDIDATE_SECTORS get 0 (they're not evaluated as commands today).
    """
    # Tight-shore gate — see LOOKAHEAD_TIGHT_SHORE_DIST docstring.
    # §13.20: forward bow-arc sectors (-45°, 0°, +45°) are now idx
    # 14/0/2 in the 16-sector grid (was 7/0/1).
    for idx in (14, 0, 2):
        if idx < len(nav.sectors):
            sec = nav.sectors[idx]
            if (sec.is_observed and sec.nearest_dist is not None
                    and sec.nearest_dist < LOOKAHEAD_TIGHT_SHORE_DIST):
                return {idx: 0.0 for idx in CANDIDATE_SECTORS}

    # Wrong-side / lost-shore gate: if the *target* side (T sector) has
    # no observable shore, the bot is in a recovery mode (wrong-side
    # U-turn or lost-shore drift) whose strategy is driven by existing
    # VFH+ logic that may *deliberately* peel into the opposite shore.
    # Letting the collision-cone term overrule that picks the wrong
    # action — see scenarios wrong_side_u_turn,
    # lost_shore_tied_cost_holds_course.
    target_idx = 4 if side == "starboard" else 12
    if target_idx < len(nav.sectors):
        target = nav.sectors[target_idx]
        if (not target.is_observed
                or target.land_fraction < SHORE_VISIBLE_FRAC):
            return {idx: 0.0 for idx in CANDIDATE_SECTORS}

    # Astern-pull gate: when the astern-target sector is heavily loaded
    # (we just rounded a corner — shore wraps from beam through astern),
    # existing logic invokes astern-pull which *wants* the bot to turn
    # INTO the wrap to re-engage the hug-line.  Collision-cone scoring
    # would override and peel away — see
    # astern_pull_beats_wall_follow_after_corner.
    astern_target_idx = 6 if side == "starboard" else 10
    if astern_target_idx < len(nav.sectors):
        ast = nav.sectors[astern_target_idx]
        if ast.is_observed and ast.land_fraction >= ASTERN_PULL_FRAC:
            return {idx: 0.0 for idx in CANDIDATE_SECTORS}

    obstacles = _extract_obstacles(nav)
    if not obstacles:
        return {idx: 0.0 for idx in CANDIDATE_SECTORS}

    spd = speed_kt if speed_kt is not None else LOOKAHEAD_SPEED_FALLBACK_KT
    vdt = V_DT_PER_KT_PER_TICK * spd
    cap = math.radians(MAX_TURN_PER_TICK_DEG)

    def trajectory_cost(turn_seq: list[float]) -> float:
        x, y, psi = 0.0, 0.0, 0.0
        total = 0.0
        for k, dpsi in enumerate(turn_seq):
            psi += dpsi
            nx = x + vdt * math.cos(psi)
            ny = y + vdt * math.sin(psi)
            total += (LOOKAHEAD_DISCOUNT ** k) * _segment_collision_cost(
                x, y, nx, ny, obstacles,
            )
            x, y = nx, ny
        return total

    def clamp_psi(deg: float) -> float:
        psi = math.radians(deg)
        return max(-cap, min(cap, psi))

    out: dict[int, float] = {}
    for i in CANDIDATE_SECTORS:
        psi_i = clamp_psi(_SECTOR_REL_ANGLE[i])
        if depth <= 1:
            out[i] = trajectory_cost([psi_i])
            continue
        best = float("inf")
        for j in CANDIDATE_SECTORS:
            psi_j = clamp_psi(_SECTOR_REL_ANGLE[j])
            cost = trajectory_cost([psi_i, psi_j])
            if cost < best:
                best = cost
        out[i] = best
    return out


def _score_with_lookahead(nav, side: Side, wall_distance: float,
                          signals: Optional[HistorySignals] = None,
                          t_dist_smoothed: Optional[float] = None,
                          speed_kt: Optional[float] = None,
                          ) -> tuple[dict[int, float], int, bool, dict]:
    """`_score_sectors` augmented with §13.15 swept-volume lookahead.

    Same return shape.  The lookahead is mixed in as
    γ × min_trajectory_collision_cost(i) added to each candidate i's
    snapshot cost — i.e. one γ-factor outside the trajectory's internal
    γ-discounted sum, matching the canonical r(s,a) + γ·V(s') form.
    """
    costs, ideal_idx, wall_active, alt_costs = _score_sectors(
        nav, side, wall_distance, signals, t_dist_smoothed,
    )
    look = _lookahead_collision_cost(nav, side, speed_kt, depth=LOOKAHEAD_DEPTH)
    for idx, extra in look.items():
        if idx in costs:
            costs[idx] += LOOKAHEAD_DISCOUNT * extra
    return costs, ideal_idx, wall_active, alt_costs


# ── §13.17 Point-pursuit helpers ──────────────────────────────────────────

# §13.21 — Commitment warm-up parameters.  Number of consecutive
# valid heading samples and the maximum bearing spread across them
# required before commitment is allowed to initialise.  Standard
# robotics warm-up filter — never trust the first reading after a
# state change (boot, departure).
COMMITMENT_WARMUP_WINDOW = 3
COMMITMENT_STABILITY_DEG = 15.0


def _max_angular_spread_deg(bearings: list) -> float:
    """Maximum pairwise angular distance (wrap-aware) across the list.

    Returns 0 for lists of fewer than two elements.  Used as the
    stability statistic for the commitment warm-up filter: small
    spread → heading detector has converged, safe to commit.
    """
    if len(bearings) < 2:
        return 0.0
    max_spread = 0.0
    for i in range(len(bearings)):
        for j in range(i + 1, len(bearings)):
            d = abs(((bearings[j] - bearings[i] + 180.0) % 360.0) - 180.0)
            if d > max_spread:
                max_spread = d
    return max_spread


# §13.21 — Coverage-aware tangent scoring tunables.
#
# Forward look-ahead distance (degrees lat/lon) for projecting cells
# along each candidate tangent.  At CoverageTracker's default 0.1°
# cells, 0.5° = ~5 cells = ~50 km — far enough to capture real
# coverage state, short enough that intervening shore curvature
# doesn't poison the count.
COVERAGE_LOOKAHEAD_DEG = 0.5

# Number of evenly-spaced samples along the look-ahead arc.
COVERAGE_LOOKAHEAD_SAMPLES = 5

# Veto margin — flip the tangent if the alt direction has at least
# this many more unvisited forward cells than the committed/LOS-chosen
# direction.  Forward-information-gain analog of Julian-Karaman-How
# 2014 in discrete form: only act when the signal is overwhelming so
# we don't second-guess commitment on borderline cases.
COVERAGE_VETO_MARGIN = 2


def _try_skeleton_tangent(
    nav,
    bow_hint_deg: Optional[float],
    committed_direction: Optional[float],
) -> Optional[float]:
    """World-frame tangent from the minimap medial-axis skeleton.

    Returns None on open water (LAKE topology), insufficient water
    mask, missing ship_xy, or degenerate local skeleton — caller then
    falls back to the original shoreline-LSQ tangent.

    The returned bearing is undirected (axis only); orientation is
    seeded by `committed_direction` when present, otherwise by
    `bow_hint_deg`.  Downstream §13.21 commit/EWMA logic re-snaps to
    `committed_direction` every tick, so a transient bow-hint seed
    only matters until the first commit lands.

    See `memory/project_skeleton_steering_migration.md` for the why.
    """
    water_mask = getattr(nav, "water_mask", None)
    ship_xy = getattr(nav, "ship_xy", None)
    if water_mask is None or ship_xy is None:
        return None
    try:
        analysis = extract_skeleton(water_mask)
    except Exception as e:
        logger.debug(f"[hug_shore] skeleton extract failed: {e}")
        return None
    topo = classify_topology(analysis)
    if topo not in (TopologyKind.CHANNEL,
                    TopologyKind.JUNCTION,
                    TopologyKind.DEAD_END):
        return None
    seed_hint = (committed_direction
                 if committed_direction is not None
                 else bow_hint_deg)
    return compute_skeleton_tangent_deg(
        analysis, ship_xy, bow_hint_deg=seed_hint,
    )


def _extract_shore_line(
    nav,
    side: Side,
    ship_heading_deg: Optional[float],
    current_lat: Optional[float] = None,
    current_lon: Optional[float] = None,
    endpoint_lat: Optional[float] = None,
    endpoint_lon: Optional[float] = None,
    committed_direction: Optional[float] = None,
    coverage=None,
) -> Optional[tuple[float, float, float]]:
    """§13.17.2 — Slice 1: extract the shore line bot wants to hug.

    Returns `(tangent_compass_deg, perpendicular_distance, d_star)` when
    a usable shore line is observed on the configured hug-side; None
    otherwise.

    Uses the existing `_lyapunov_state` for distance + tangent
    estimation — it does the multi-sector LSQ fit and gives us the
    parameters we need.  We only enrich it with a confidence check
    that the configured side actually has shore (not just bow-target
    fallback — see §13.17.1 for the failure mode that caused).

    §13.21 — Direction-anchored tangent selection.  A shore line has
    two valid tangent directions 180° apart; the LSQ fit alone can't
    tell which way along the shore to travel.  Default behaviour
    (bow + θ_err) picks whichever side of the shore is closer to the
    current bow — but bow can drift, and once it drifts past 90° from
    the goal, hug locks in the wrong direction (observed at the big
    bend in hug_debug_20260601_161713 t192-t197 where the bot rotated
    progressively from south toward west; replayed in 151459 t360-367
    where the bot bounced into the bend and the wp flipped 180° as
    bow rotated west).

    Tangent direction is picked, in priority order:

    1. `committed_direction` — Bug2-style temporal commitment carried
       across ticks by the caller.  Memorises "what direction the bot
       has been hugging" and prevents single-tick bow flips from
       reversing the wp.  Only set on topology events (junction,
       dead-end, picker-driven backtrack), not per tick.
    2. LOS-to-endpoint — the "leave point" rule from the Bug family,
       used when an endpoint is set but no committed direction yet.
    3. Bow + θ_err — fallback when no temporal context is available
       (first tick, fresh voyage start).

    After (1)-(3) have picked a direction, a coverage-aware veto
    runs (discrete forward-information-gain — Julian-Karaman-How
    2014).  When the picked direction has substantially fewer
    unvisited forward cells than its 180° alternative, the choice is
    flipped.  This catches the "commitment is stuck on a direction
    that's already been fully explored" failure mode that pure
    commitment can't break out of.
    """
    if nav is None or ship_heading_deg is None:
        return None
    # Village-overlap dead-reckon gate.  When a village icon/label is
    # overlaying the local water mask + sectors AND we have a prior
    # §13.21 commit to fall back to, return None so the existing
    # committed direction persists (the bot dead-reckons through the
    # village on that prior direction).
    #
    # IMPORTANT precondition: ONLY apply this when `committed_direction`
    # is set.  Without a prior commit there is nothing to dead-reckon
    # TO — returning None just freezes the segment in an undefined
    # state.  Live voyage `explore_port_20260607_151743` t1-t500
    # showed why: village_overlap fires at Cairo (right next to the
    # port marker) when no commit exists yet, the gate fired, the
    # commit never bootstrapped, and the bot oscillated lat 27-29 for
    # 500 ticks.  The precondition fixes that.
    if (getattr(nav, "village_overlap", False)
            and committed_direction is not None):
        return None
    lyap = _lyapunov_state(nav, side)
    if lyap is None:
        return None
    d, d_star, theta_err_deg = lyap
    # Confidence: require shore actually on the configured side.
    target_idx = 4 if side == "starboard" else 12
    if (target_idx >= len(nav.sectors)
            or not nav.sectors[target_idx].is_observed
            or nav.sectors[target_idx].land_fraction < SHORE_VISIBLE_FRAC):
        return None
    # Tangent source — skeleton (world-frame) for narrow channels,
    # shoreline LSQ (bow-frame) for open water.  See
    # `memory/project_skeleton_steering_migration.md`.
    #
    # When the skeleton is a CHANNEL/JUNCTION/DEAD_END (i.e. there's a
    # well-defined medial axis), use the world-frame skeleton tangent.
    # That tangent is computed from the water mask alone — it does NOT
    # read `ship_heading_deg`, so a corrupted bow heading cannot
    # poison it.  On open water (LAKE / sparse skeleton) fall through
    # to the original shoreline-tangent path: `ship_heading_deg +
    # theta_err_deg`.
    skeleton_tangent = _try_skeleton_tangent(
        nav, ship_heading_deg, committed_direction,
    )
    if skeleton_tangent is not None:
        tangent_compass = skeleton_tangent
    else:
        # theta_err is in DEGREES from _shore_tangent_angle /
        # _lyapunov_state (matches what _lyapunov_correction and
        # _project_lyap_state consume).
        tangent_compass = (ship_heading_deg + theta_err_deg) % 360.0
    tangent_alt = (tangent_compass + 180.0) % 360.0
    if committed_direction is not None:
        # Bug2 temporal commitment: pick the candidate aligned with
        # the direction the bot has been hugging — ignore bow.
        if (_angle_diff_deg(tangent_alt, committed_direction)
                < _angle_diff_deg(tangent_compass, committed_direction)):
            tangent_compass = tangent_alt
            tangent_alt = (tangent_compass + 180.0) % 360.0
    elif (current_lat is not None and current_lon is not None
            and endpoint_lat is not None and endpoint_lon is not None):
        los_bearing = _point_pursuit_desired_heading(
            current_lat, current_lon, endpoint_lat, endpoint_lon,
        )
        if los_bearing is not None:
            if (_angle_diff_deg(tangent_alt, los_bearing)
                    < _angle_diff_deg(tangent_compass, los_bearing)):
                tangent_compass = tangent_alt
                tangent_alt = (tangent_compass + 180.0) % 360.0
    # Coverage-aware veto.  Discrete forward-information-gain:
    # project COVERAGE_LOOKAHEAD_SAMPLES cells along each candidate
    # tangent and count which are unvisited.  When the chosen
    # direction has substantially fewer unvisited cells than the
    # alternative, the chosen direction is "into already-explored
    # space" and we flip.  Threshold (COVERAGE_VETO_MARGIN = 2)
    # is intentionally conservative so commitment is preserved on
    # borderline cases.
    if (coverage is not None
            and current_lat is not None
            and current_lon is not None):
        unvisited_main = _unvisited_ahead(
            current_lat, current_lon, tangent_compass, coverage)
        unvisited_alt  = _unvisited_ahead(
            current_lat, current_lon, tangent_alt, coverage)
        if (unvisited_alt - unvisited_main) >= COVERAGE_VETO_MARGIN:
            tangent_compass = tangent_alt
    return (tangent_compass, d, d_star)


def _unvisited_ahead(
    current_lat: float, current_lon: float,
    bearing_deg: float, coverage,
) -> int:
    """Count unvisited cells along the bearing, out to
    COVERAGE_LOOKAHEAD_DEG.  Uses CoverageTracker.visited as the
    "explored" set (the same set frontier-based exploration uses).
    """
    bearing_rad = math.radians(bearing_deg)
    cos_b = math.cos(bearing_rad)
    sin_b = math.sin(bearing_rad)
    cell_size = coverage.cell_size_deg
    unvisited = 0
    seen_cells = set()
    for k in range(1, COVERAGE_LOOKAHEAD_SAMPLES + 1):
        frac = k / COVERAGE_LOOKAHEAD_SAMPLES
        lat = current_lat + frac * COVERAGE_LOOKAHEAD_DEG * cos_b
        lon = current_lon + frac * COVERAGE_LOOKAHEAD_DEG * sin_b
        cell = (math.floor(lat / cell_size), math.floor(lon / cell_size))
        if cell in seen_cells:
            continue
        seen_cells.add(cell)
        if cell not in coverage.visited:
            unvisited += 1
    return unvisited


def _hug_line_waypoint(
    current_lat: float,
    current_lon: float,
    tangent_compass_deg: float,
    perpendicular_distance: float,
    side: Side,
    d_star: float,
    L: float = POINT_PURSUIT_L,
) -> tuple[float, float]:
    """§13.17.2 — Slice 2: compute target point on the hug line.

    The hug line is the line parallel to the shore tangent, at
    perpendicular distance `d_star` on the bot's configured side.
    Algorithm:

      1. shore_anchor = bot + d · unit_toward_shore
      2. hug_line_anchor = shore_anchor − d_star · unit_toward_shore
                         = bot + (d − d_star) · unit_toward_shore
         (this IS bot's perpendicular projection onto the hug line)
      3. target = hug_line_anchor + L · unit_along_tangent

    When bot is exactly on the hug line (`d == d_star`), the target is
    `L` ahead along the tangent — pure forward motion.  When bot has
    drifted, the target sits off-axis from bot's bow, producing the
    bearing-to-target = atan2(perp_offset, L) that ILOS / pure-pursuit
    use for asymptotic convergence to the line.
    """
    # Perpendicular direction toward the shore (compass).
    perp_to_shore_compass = (
        (tangent_compass_deg + 90.0) % 360.0
        if side == "starboard"
        else (tangent_compass_deg - 90.0) % 360.0
    )
    perp_offset = perpendicular_distance - d_star
    perp_rad = math.radians(perp_to_shore_compass)
    tan_rad  = math.radians(tangent_compass_deg)
    # Game coords: lat = north (+x), lon = east (+y).  Unit vector for
    # compass θ: (cos θ, sin θ).
    target_lat = (current_lat
                  + perp_offset * math.cos(perp_rad)
                  + L * math.cos(tan_rad))
    target_lon = (current_lon
                  + perp_offset * math.sin(perp_rad)
                  + L * math.sin(tan_rad))
    return (target_lat, target_lon)


def _hug_mode_waypoint(
    current_lat: float,
    current_lon: float,
    ship_heading_deg: float,
    lyap_state: Optional[tuple[float, float, float]],
    side: Side,
    nav=None,
    L: float = POINT_PURSUIT_L,
    endpoint_lat: Optional[float] = None,
    endpoint_lon: Optional[float] = None,
) -> Optional[tuple[float, float]]:
    """§13.17 — Generate a target point for hug-mode (shore on `side`).

    Uses the existing wall-following Lyapunov state `(d, d_star, θ_err)`
    to compute the next ideal position: forward step `L` along the shore
    tangent, plus a small lateral nudge toward `d_star` if drifted off.
    Returns None when there's no current Lyapunov reading (caller falls
    back to existing behaviour for the bootstrap tick).

    Additional gate: if `nav` is provided AND the target sector (sec 2 for
    starboard, sec 6 for port) has frac < SHORE_VISIBLE_FRAC, return None.
    The Lyapunov state could still be defined off `bow_target` shore alone,
    but that's a "shore approaching the bow" signal — not "shore is on
    the configured side."  Treating it as the latter produces wrong-side
    targets (observed at Cairo departure t=1, hug_debug_20260531_200352).
    """
    # §13.17.2 — Slice 1+2: hug-line target generator.  When `nav` is
    # available, use the classical hug-line formulation (target = point
    # on the line parallel to shore at d_star, advanced L along
    # tangent).  Lateral correction is implicit in the geometry — no
    # separate term needed.
    if nav is not None:
        shore = _extract_shore_line(
            nav, side, ship_heading_deg,
            current_lat=current_lat, current_lon=current_lon,
            endpoint_lat=endpoint_lat,
            endpoint_lon=endpoint_lon,
        )
        if shore is None:
            return None
        tangent_compass, d, d_star = shore
        return _hug_line_waypoint(
            current_lat, current_lon, tangent_compass, d, side, d_star, L,
        )

    # Legacy fallback (no `nav` provided — tests, etc.).  Tangent-
    # projection target with small lateral correction; semantically the
    # same as the §13.17 v1 helper, kept so old test fixtures pass.
    if lyap_state is None:
        return None
    _d, _d_star, theta_err_deg = lyap_state
    tangent_compass = (ship_heading_deg + theta_err_deg) % 360.0
    rad = math.radians(tangent_compass)
    target_lat = current_lat + L * math.cos(rad)
    target_lon = current_lon + L * math.sin(rad)
    drift = _d - _d_star
    if abs(drift) > 0.01:
        perp_bias_deg = 90.0 if side == "starboard" else -90.0
        perp_compass = (tangent_compass + perp_bias_deg) % 360.0
        perp_rad = math.radians(perp_compass)
        lateral = POINT_PURSUIT_HUG_LATERAL_GAIN * drift * L
        target_lat += lateral * math.cos(perp_rad)
        target_lon += lateral * math.sin(perp_rad)
    return (target_lat, target_lon)


def _destination_mode_waypoint(
    current_lat: float,
    current_lon: float,
    endpoint_lat: float,
    endpoint_lon: float,
    L: float = POINT_PURSUIT_L,
) -> tuple[float, float]:
    """§13.17 — Generate a target point for destination-mode.

    Project a point of distance `L` from current along the LOS to the
    destination.  If destination is within `L`, return the destination
    itself.  (v1: no minimap clipping — VFH+ candidate masking handles
    obstacle avoidance.  Clipping is a follow-up.)
    """
    dlat = endpoint_lat - current_lat
    dlon = endpoint_lon - current_lon
    dist = math.hypot(dlat, dlon)
    if dist < L:
        return (endpoint_lat, endpoint_lon)
    return (current_lat + (dlat / dist) * L,
            current_lon + (dlon / dist) * L)


def _generate_waypoint(
    current_lat: Optional[float],
    current_lon: Optional[float],
    ship_heading_deg: Optional[float],
    side: Optional[Side],
    endpoint_lat: Optional[float],
    endpoint_lon: Optional[float],
    lyap_state: Optional[tuple[float, float, float]],
    nav=None,
) -> Optional[tuple[float, float]]:
    """§13.17 — Goal-point generator with mode dispatch.

    Modes:
      - side ✓, dest ✗: hug-mode shore-tangent target
      - side ✗, dest ✓: destination-mode LOS target
      - side ✓, dest ✓: blend (weighted average)
      - neither: None (caller should refuse to launch)
    """
    if current_lat is None or current_lon is None:
        return None
    has_side = side is not None
    has_dest = (endpoint_lat is not None
                and endpoint_lon is not None)
    if not has_side and not has_dest:
        return None

    hug_t = None
    if has_side and ship_heading_deg is not None:
        hug_t = _hug_mode_waypoint(
            current_lat, current_lon, ship_heading_deg,
            lyap_state, side, nav=nav,
            endpoint_lat=endpoint_lat,
            endpoint_lon=endpoint_lon,
        )
    dest_t = None
    if has_dest:
        dest_t = _destination_mode_waypoint(
            current_lat, current_lon,
            endpoint_lat, endpoint_lon,
        )
    if hug_t is None and dest_t is None:
        return None
    if hug_t is None:
        return dest_t
    if dest_t is None:
        return hug_t
    # §13.33 — Adaptive blend.  When the shore tangent and the
    # destination LOS disagree sharply, the 50/50 default produces a
    # bisector that points at neither — the picker then chases that
    # useless direction, often the wrong way around an obstacle.
    # Observed at explore_port_20260603_143438 t495: hug bearing 108°
    # (E), dest_los bearing 213° (SSW), 50/50 blend → 167° (S), which
    # made the avoider commit to a +22° RIGHT turn when the correct
    # answer was a left turn toward destination.
    #
    # Schema-blend pattern (Arkin motor schemas, Rosenblatt DAMN, TEB
    # hybrid weights): when behaviors disagree past a threshold, defer
    # to the more reliable one — here, the destination.  Shore-tangent
    # confidence drops when it disagrees strongly because that's exactly
    # the "ambiguous joint" case Phase A's segment-commitment was built
    # to handle.
    #
    # Thresholds (30°/60°) and weights (0.5/0.7/0.9) are heuristic —
    # see `project_adaptive_blend_future` memory for the principled
    # next-step (confidence-aware fusion via Phase A signals).
    hug_bearing = (math.degrees(math.atan2(
        hug_t[1] - current_lon, hug_t[0] - current_lat,
    )) + 360.0) % 360.0
    dest_bearing = (math.degrees(math.atan2(
        dest_t[1] - current_lon, dest_t[0] - current_lat,
    )) + 360.0) % 360.0
    disagreement = abs(((hug_bearing - dest_bearing + 180.0) % 360.0) - 180.0)
    if disagreement > 60.0:
        w = 0.9
    elif disagreement > 30.0:
        w = 0.7
    else:
        w = POINT_PURSUIT_COMBINED_DEST_W   # default 0.5
    return (w * dest_t[0] + (1 - w) * hug_t[0],
            w * dest_t[1] + (1 - w) * hug_t[1])


def _point_pursuit_desired_heading(
    current_lat: float,
    current_lon: float,
    target_lat: float,
    target_lon: float,
) -> Optional[float]:
    """§13.17 — Lyapunov point-stabilization: desired heading is the
    bearing from current position to the target point.  Returns None
    when already at the target (within numerical precision)."""
    dlat = target_lat - current_lat
    dlon = target_lon - current_lon
    if math.hypot(dlat, dlon) < 1e-9:
        return None
    deg = math.degrees(math.atan2(dlon, dlat))
    return deg + 360.0 if deg < 0 else deg


def _point_pursuit_pick_sector(
    nav,
    candidate_sectors: tuple[int, ...],
    desired_heading_deg: float,
) -> tuple[Optional[int], list[int]]:
    """§13.17 — Canonical VFH+ candidate-masking pick.

    Mask out any sector whose nearest-distance is below
    `POINT_PURSUIT_SAFETY_DIST` (imminent collision).  Among the
    surviving free sectors, pick the one whose absolute compass
    bearing is closest to `desired_heading_deg`.

    Returns (chosen_sector, list_of_unmasked_sectors).  When all
    candidates are masked, returns (None, []) and the caller should
    fall back to escape behaviour.
    """
    if nav.ship_heading_deg is None:
        return None, []
    free = []
    for idx in candidate_sectors:
        if idx >= len(nav.sectors):
            continue
        sec = nav.sectors[idx]
        if (sec.is_observed and sec.nearest_dist is not None
                and sec.nearest_dist < POINT_PURSUIT_SAFETY_DIST):
            continue
        free.append(idx)
    if not free:
        return None, []
    def misalign(idx: int) -> float:
        sec_bearing_rel = _SECTOR_REL_ANGLE.get(idx, 0.0)
        sec_compass = (nav.ship_heading_deg + sec_bearing_rel) % 360.0
        return _angle_diff_deg(sec_compass, desired_heading_deg)
    chosen = min(free, key=misalign)
    return chosen, free


def _obstacle_centroid_relative_bearing(nav) -> Optional[float]:
    """§13.16.1 — Weighted circular mean of observed obstacle bearings,
    returned ship-relative in degrees (−180, +180].  Used by Bug2 commit
    to pick the turn direction that brings the obstacle mass toward the
    hug-side beam, instead of using a fixed direction (Trémaux).

    Weight per sector = land_fraction × max(0, 1 − nearest_dist).
    Linear distance decay (not 1/d²) so a thin sliver dead ahead at
    `d=0.13` doesn't drown out a broader landmass at `d=0.25` — the
    user's t=1 Nile case showed the broader mass is the dominant
    contributor to "where the shore IS overall."

    Returns None when no qualifying obstacles, or when the weighted
    vectors cancel (centroid undefined).
    """
    x_sum = 0.0
    y_sum = 0.0
    has_any = False
    for sec in nav.sectors:
        if not sec.is_observed or sec.nearest_dist is None:
            continue
        if sec.land_fraction < LATERAL_VALID_FRAC:
            continue
        weight = sec.land_fraction * max(0.0, 1.0 - sec.nearest_dist)
        if weight <= 0:
            continue
        beta_rad = math.radians(sec.bearing_deg)
        x_sum += weight * math.cos(beta_rad)
        y_sum += weight * math.sin(beta_rad)
        has_any = True
    if not has_any:
        return None
    if x_sum * x_sum + y_sum * y_sum < 1e-9:
        return None
    bearing_deg = math.degrees(math.atan2(y_sum, x_sum))
    # Normalize to (-180, 180].
    while bearing_deg > 180.0:
        bearing_deg -= 360.0
    while bearing_deg <= -180.0:
        bearing_deg += 360.0
    return bearing_deg


# Hysteresis band around the hug-side beam.  While committed, the commit
# only SWITCHES when the centroid is clearly past target on the *opposite*
# side (|delta| > this).  While entering a fresh commit, within this
# window we default to the Trémaux fixed direction.
#
# Why a band instead of strict equality: if we re-evaluated every tick
# without hysteresis, the bot would oscillate at the Port Said-style
# scenario where delta hovers near 0.  Why we re-evaluate at all:
# without re-eval, the bot at hug_debug_20260531_163015 stayed sticky-
# committed to RIGHT for 9+ ticks while it rotated 200°+ past target,
# ending up doing a full circle.  Re-eval with hysteresis solves both.
CENTROID_ALIGNED_TOLERANCE_DEG = 45.0

# ── §13.16.3 — Goal-direction (VFH+ μ₁) commit ────────────────────────────
# Step 1 of the goal-management rollout (see project_goal_management_
# architecture memory).  When the HugShoreGoal is configured with a
# `goal_heading_deg`, the wall-ahead commit picks the sector whose post-
# turn heading is closer to that goal direction — canonical VFH+ μ₁
# term.  This dominates the centroid-alignment heuristic when set;
# falls back to centroid alignment when no goal is provided.
#
# Hysteresis: switch only when one candidate is ≥ this many degrees
# closer to goal than the other.  Smaller than the full ±45° candidate
# spread so we still react to meaningful asymmetry, larger than
# typical heading-jitter so we don't flip on noise.
GOAL_HEADING_HYSTERESIS_DEG = 20.0

# ── §13.16.4 — Step 2 sequencer: history-derived goal heading ──────────────
# Auto-infer the goal heading from recent successful cruise headings
# when no explicit goal_heading_deg is set.  Canonical pattern:
# weighted circular mean of recent headings during HUGGING phase,
# with phase / speed / age weighting, and Schmitt-trigger hysteresis
# so the inferred goal doesn't update on noise.
GOAL_INFERENCE_WINDOW         = 20    # last N ticks of history
GOAL_INFERENCE_MIN_SAMPLES    = 5     # need at least this many qualifying ticks
GOAL_INFERENCE_MIN_SPEED_KT   = 3.0   # ticks slower than this don't count (stuck)
GOAL_UPDATE_HYSTERESIS_DEG    = 15.0  # only update active goal when inferred
                                      # moves more than this from current value


def _infer_goal_heading_from_history(history) -> Optional[float]:
    """§13.16.4 — Weighted circular mean of recent cruise headings.

    Per the canonical history-smoothing approach (one of the three goal-
    signal sources in the three-layer architecture, see project_goal_
    management_architecture memory), compute a goal-direction estimate
    from recent successful navigation.

    Weights:
      - Phase: HUGGING=1.0, OFFSHORE=0.5, BLOCKED/INIT/FAILED=0.0
      - Speed: below GOAL_INFERENCE_MIN_SPEED_KT → 0.0 (stuck)
      - Age:   linear decay (most recent = 1.0, oldest of window = ~0)

    Returns None when too few qualifying ticks are available — the
    caller should fall back to the explicit goal or to no-goal mode.
    """
    if len(history) < GOAL_INFERENCE_MIN_SAMPLES:
        return None
    # Iterate over the most recent GOAL_INFERENCE_WINDOW records.
    recent = list(history)[-GOAL_INFERENCE_WINDOW:]
    if not recent:
        return None
    x_sum = 0.0
    y_sum = 0.0
    qualifying = 0
    n = len(recent)
    for idx, rec in enumerate(recent):
        if rec.heading is None:
            continue
        if rec.speed_kt is None or rec.speed_kt < GOAL_INFERENCE_MIN_SPEED_KT:
            continue
        if rec.phase == HugPhase.HUGGING:
            phase_w = 1.0
        elif rec.phase == HugPhase.OFFSHORE:
            phase_w = 0.5
        else:
            continue
        # Age weight: 0.5 (oldest) → 1.0 (newest).  Gentle 2x ramp so
        # newer ticks lean the estimate without erasing older ones —
        # otherwise a brief OFFSHORE excursion would overwrite a long
        # HUGGING tail at the same heading.  The window itself bounds
        # the recency horizon; the ramp provides smooth tracking when
        # the heading slowly evolves (e.g. river curve).
        age_w = 0.5 + 0.5 * (idx + 1) / n
        weight = phase_w * age_w
        if weight <= 0:
            continue
        rad = math.radians(rec.heading)
        x_sum += weight * math.cos(rad)
        y_sum += weight * math.sin(rad)
        qualifying += 1
    if qualifying < GOAL_INFERENCE_MIN_SAMPLES:
        return None
    if x_sum * x_sum + y_sum * y_sum < 1e-9:
        return None  # vectors canceled (e.g. bot was doing a U-turn)
    bearing = math.degrees(math.atan2(y_sum, x_sum))
    if bearing < 0:
        bearing += 360.0
    return bearing


def _angle_diff_deg(a: float, b: float) -> float:
    """Smallest absolute compass difference between two bearings, in degrees.
    Result is in [0, 180]."""
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def _bug2_commit_sector(nav,
                         side: Side,
                         current_commit: Optional[int] = None,
                         goal_heading_deg: Optional[float] = None,
                         ) -> int:
    """§13.16.1/13.16.3 — Pick the wall-ahead commit sector.

    **Primary signal (§13.16.3, VFH+ μ₁):** when `goal_heading_deg` is
    set and ship heading is known, pick the sector whose post-turn
    heading is closer to the goal direction.  This is the canonical
    target-direction term from VFH+ (Ulrich & Borenstein 1998) —
    default weight 5, expected to dominate the other terms.
    Hysteresis: switch only when one candidate is ≥
    GOAL_HEADING_HYSTERESIS_DEG closer than the other.

    **Fallback signal (§13.16.1):** when no goal is set, fall back to
    obstacle-centroid alignment — pick the turn that brings the
    weighted shore centroid toward the hug-side beam.
    """
    tremaux_default = 14 if side == "starboard" else 2

    # §13.16.3 — goal-direction (VFH+ μ₁) takes priority when available.
    if (goal_heading_deg is not None
            and getattr(nav, "ship_heading_deg", None) is not None):
        h = nav.ship_heading_deg
        hdg_right = (h + 45.0) % 360.0   # sec 1 commit
        hdg_left  = (h - 45.0) % 360.0   # sec 7 commit
        err_right = _angle_diff_deg(hdg_right, goal_heading_deg)
        err_left  = _angle_diff_deg(hdg_left,  goal_heading_deg)
        # Positive diff → sec 1 (right) is better.
        diff = err_left - err_right
        if abs(diff) >= GOAL_HEADING_HYSTERESIS_DEG:
            return 1 if diff > 0 else 7
        # Within hysteresis: stay current if committed, else Trémaux default.
        if current_commit is not None:
            return current_commit
        return tremaux_default

    # §13.16.1 — centroid-alignment fallback (no goal set).
    centroid_rel = _obstacle_centroid_relative_bearing(nav)
    if centroid_rel is None:
        return current_commit if current_commit is not None else tremaux_default
    target_rel = 90.0 if side == "starboard" else -90.0
    delta = centroid_rel - target_rel
    while delta > 180.0:
        delta -= 360.0
    while delta <= -180.0:
        delta += 360.0
    if delta > CENTROID_ALIGNED_TOLERANCE_DEG:
        return 1
    if delta < -CENTROID_ALIGNED_TOLERANCE_DEG:
        return 7
    # Within hysteresis band: stay with current commit, or use Trémaux
    # default if entering fresh.  The asymmetric "default to LEFT for
    # starboard" matches the classical right-hand-on-wall rule.
    if current_commit is not None:
        return current_commit
    return tremaux_default


def _score_sectors(nav, side: Side, wall_distance: float,
                   signals: Optional[HistorySignals] = None,
                   t_dist_smoothed: Optional[float] = None,
                   ) -> tuple[dict[int, float], int, bool, dict]:
    """Compute per-sector total cost.

    Returns (costs, ideal_sector_index, wall_active).  Virtual wall is
    on for normal cruising — provides ambient pressure toward shore.
    Suppressed when `ahead.frac >= AHEAD_WALL_FRAC` so genuine wall-
    follow situations can freely peel toward the opposite side.

    `t_dist_smoothed`: EMA-smoothed value of `T.nearest_dist` from the
    caller (HugShoreGoal.tick maintains it).  When provided, it
    replaces the raw reading in the lateral_drift computation only —
    rotation_penalty, intrusion check etc. still use the raw value
    because they're about *current* proximity (kinematic safety),
    while lateral drift is about *trajectory* (slow-moving state).
    """
    ideal_idx = _ideal_sector(nav, side, wall_distance, signals)
    ideal_angle = _SECTOR_REL_ANGLE[ideal_idx]

    # Wrong-side mode: cost function needs to accept a leftward
    # sector that has real shore (we're peeling INTO opp-shore to
    # start the U-turn).  Boost W_IDEAL so the directional pull
    # toward opp-bow dominates the obstacle cost there, AND suppress
    # the virtual wall (which normally pushes away from opp-side).
    wrong_side = _is_wrong_side(nav, side)
    # `astern_t_loaded_sw` is computed below; we need it here, so peek
    # ahead.  Same definition as the gating block further down.
    at_idx_peek = 6 if side == "starboard" else 10
    at_peek = nav.sectors[at_idx_peek]
    at_dist_peek = at_peek.nearest_dist if at_peek.nearest_dist is not None else 1.0
    astern_loaded_peek = (
        at_peek.land_fraction >= ASTERN_PULL_FRAC
        or (at_dist_peek < ASTERN_PULL_DIST
            and at_peek.land_fraction >= WRONG_SIDE_TINY_FRAC)
    )
    if wrong_side:
        w_ideal_effective = W_IDEAL_WRONG_SIDE
    elif astern_loaded_peek:
        w_ideal_effective = W_IDEAL_ASTERN_PULL
    else:
        w_ideal_effective = W_IDEAL

    ahead_walled = nav.sectors[0].land_fraction >= AHEAD_WALL_FRAC
    # Bow-target close+loaded also suppresses the virtual wall — same
    # rationale as ahead_walled: when shore is closing in on a forward
    # sector, the wall should not push the bot back toward shore.
    # Mirrors the trigger in _ideal_sector.
    bt_idx_sw = 2 if side == "starboard" else 14
    bt_sw = nav.sectors[bt_idx_sw]
    bt_dist_sw = bt_sw.nearest_dist if bt_sw.nearest_dist is not None else 1.0
    bow_t_close_loaded = (
        bt_sw.land_fraction >= BOW_T_CLOSE_FRAC
        and bt_dist_sw < BOW_T_CLOSE_DIST
    )
    # Approach-velocity mirror — same suppression rationale as the
    # static close-loaded trigger.  When bow-target shore is loading
    # fast (frac jumping up + dist shrinking), the wall must not
    # push the bot back toward it.
    bt_approaching_fast_sw = False
    if signals is not None and signals.bow_target_velocity is not None:
        dfrac, ddist = signals.bow_target_velocity
        if dfrac >= APPROACH_DFRAC_DANGER and ddist <= APPROACH_DDIST_DANGER:
            bt_approaching_fast_sw = True
    # Astern-pull gating mirror: when astern-target is loaded AND bow-
    # target isn't IMMINENT, the wall-follow override is suppressed in
    # `_ideal_sector` — so the virtual wall here must NOT be suppressed
    # for that case (the wall pressure helps the bot stay on hug-side
    # while astern-pull rotates it back).  Only suppress wall when
    # wall-follow would actually fire.
    at_idx_sw = 6 if side == "starboard" else 10
    astern_t_sw = nav.sectors[at_idx_sw]
    at_dist_sw = astern_t_sw.nearest_dist if astern_t_sw.nearest_dist is not None else 1.0
    astern_t_loaded_sw = (
        astern_t_sw.land_fraction >= ASTERN_PULL_FRAC
        or (at_dist_sw < ASTERN_PULL_DIST
            and astern_t_sw.land_fraction >= WRONG_SIDE_TINY_FRAC)
    )
    bow_t_imminent_sw = (
        bt_sw.land_fraction >= BOW_T_CLOSE_FRAC
        and bt_dist_sw < BOW_T_IMMINENT_DIST
    )
    astern_blocks_sw = astern_t_loaded_sw and not bow_t_imminent_sw

    suppress_wall = (
        not astern_blocks_sw
        and (ahead_walled or wrong_side
             or bow_t_close_loaded or bt_approaching_fast_sw)
    )
    wall_cost = 0.0 if suppress_wall else _wall_cost(wall_distance)
    o_idx  = 12 if side == "starboard" else 4
    bo_idx = 14 if side == "starboard" else 2

    # Shore intrusion: tight hug = target sector spilling into adjacent
    # bins.  Detected when T is heavily loaded, ahead has no real mass,
    # and ahead-dist is not collision-imminent (a close-touching wall
    # is real even with low frac).
    t_idx = 4 if side == "starboard" else 12
    ahead = nav.sectors[0]
    target = nav.sectors[t_idx]
    t_close = (
        target.nearest_dist is not None
        and target.nearest_dist < INTRUSION_T_CLOSE_DIST
    )
    intrusion = (
        (target.land_fraction >= INTRUSION_T_FRAC or t_close)
        and ahead.land_fraction  < INTRUSION_AHEAD_FRAC
        and (ahead.nearest_dist is None
             or ahead.nearest_dist >= INTRUSION_AHEAD_MIN_DIST)
    )

    # Rotation-into-shore penalty: when T (target beam) has close land,
    # turning target-ward rotates that land into the bow.  Pure polar
    # histograms can't reason about "what's at T will be at bT after
    # the turn" — so we explicitly penalize the bow-target sector
    # proportional to T's proximity.  Observed 2026-05-28: a tick
    # picked sector 1 (turn right) because bT=0.02/0.14 looked clean,
    # but T=0.14/0.08 — bot rotated into its own hug-shore.
    target_bow_idx = 2 if side == "starboard" else 14
    rotation_penalty = 0.0
    if (target.nearest_dist is not None
            and target.nearest_dist < PROXIMITY_DANGER):
        # §13.12 — weight raised from 0.30 to 0.60.  With Option A
        # (no categorical "shore visible → hold"), this kinematic
        # penalty is now load-bearing: it's what prevents the bot
        # from rotating into close beam shore when bow-target itself
        # looks clean.  Pre-§13.12 the categorical rule did this work
        # by forcing ideal=0; now the proportional rotation_penalty
        # must overcome the ideal-distance shift on its own.
        # 0.65 tuned so the close_T_rotation_hazard scenario picks
        # hold while river_channel_opens_right still picks turn-target
        # (the bot has to navigate through a partially-loaded sec 1).
        rotation_penalty = (1.0 - target.nearest_dist / PROXIMITY_DANGER) * 0.65

    # Lateral drift — the corridor-centering signal the channel-follower
    # was missing.  Only trust T.nearest_dist when frac is above the
    # noise floor (defense against the 1-pixel phantom issue).  Positive
    # drift means bot is farther from real shore than wall_distance →
    # has drifted toward the virtual wall.  See §13.7.
    lateral_drift = 0.0
    # Gate at SHORE_VISIBLE_FRAC, not LATERAL_VALID_FRAC.  The latter
    # (0.05) is the perception noise floor; the former (0.15) is
    # "real shore visible."  At noise-floor-crossing frac (0.05–0.15)
    # the `nearest_dist` reading is too uncertain to treat as drift —
    # see t=43 of hug_debug_20260531_150415 where sec2.f=0.07 d=0.39
    # triggered the barrier at K_BARRIER × (0.11)² = 0.24 cost and
    # caused a spurious +90° turn into open water.
    if (target.land_fraction >= SHORE_VISIBLE_FRAC
            and target.nearest_dist is not None):
        # §13.13 — use the smoothed T.dist when provided.  Rejects
        # single-tick noise spikes (port-marker leakage etc.).
        t_dist_for_lateral = (
            t_dist_smoothed if t_dist_smoothed is not None
            else target.nearest_dist
        )
        # §13.14 — frac-aware d_effective: trust T.frac as the signal
        # for "is the wall there."  When T.frac drops below
        # FRAC_HEALTHY, inflate d_effective by the deficit.
        # GATED on `signals.target_frac_drift_rate` being significantly
        # negative — shore has been actively *fading from view* over
        # recent ticks.  Without this gate, a tight-hug-with-thin-shore
        # (close_T_low_frac scenario, frac stable at 0.06) would
        # incorrectly trigger "drift" recovery — the frac is low but
        # geometrically the bot is hugging fine.  Using the rate
        # distinguishes the two — receding shore has frac dropping
        # tick-over-tick; thin-shore-tight-hug has frac stable.
        frac_deficit = 0.0
        frac_rate = (signals.target_frac_drift_rate
                     if signals is not None else None)
        frac_receding = (frac_rate is not None
                         and frac_rate < FRAC_FADING_RATE_THRESHOLD)
        if frac_receding:
            frac_deficit = max(0.0, FRAC_HEALTHY - target.land_fraction)
        d_effective = t_dist_for_lateral + K_FRAC_FADE * frac_deficit
        raw_drift = max(0.0, d_effective - wall_distance)
        # Deadband — see LATERAL_DEADBAND for rationale.
        lateral_drift = max(0.0, raw_drift - LATERAL_DEADBAND)
    # §13.13 — Barrier function: linear control in the safe regime,
    # super-linear amplification near the safety boundary.  Bounds
    # the bot's willingness to "burn maneuver room" while drifting.
    lateral_barrier = 0.0
    if lateral_drift > LATERAL_BARRIER_THRESHOLD:
        excess = lateral_drift - LATERAL_BARRIER_THRESHOLD
        lateral_barrier = K_LATERAL_BARRIER * excess * excess
    lateral_cost_sec0 = K_LATERAL * lateral_drift + lateral_barrier

    # §13.10 — Tangent bias: principled replacement for astern-pull.
    # Estimate the shore tangent angle and add a per-sector cost
    # proportional to angular distance from that bearing.  The cost
    # function is biased toward heading along the wall.  When the
    # tangent estimator can't fit (insufficient shore points), the
    # bias term is skipped (no signal, no penalty).
    tangent_target_deg = _shore_tangent_angle(nav, side)

    costs: dict[int, float] = {}
    costs_no_ideal: dict[int, float] = {}
    costs_heading_smooth: dict[int, float] = {}
    for idx in CANDIDATE_SECTORS:
        sec = nav.sectors[idx]
        if not sec.is_observed:
            continue
        if idx == 0 and intrusion:
            # Discount proximity — the close-ahead reading is the
            # edge of the right-beam landmass, not an obstacle.
            obs = sec.land_fraction
        elif idx == 0:
            # §13.8 — inverse-square ahead-wall cost: when sec 0 has
            # substantial shore (frac > AHEAD_WALL_GATE) close ahead,
            # let the cost grow past 1.0 so it can outweigh the
            # virtual_wall + rotation_penalty + ideal_distance terms
            # that constrain turn sectors.  Otherwise sec 0 capped at
            # 1.0 stayed cheapest even when straight = collision.
            obs = _ahead_obstacle_cost(sec)
        else:
            obs = _obstacle_cost(sec)
        ideal = abs(_SECTOR_REL_ANGLE[idx] - ideal_angle) / 180.0
        heading_dist = abs(_SECTOR_REL_ANGLE[idx]) / 180.0   # distance from straight-ahead (current heading)
        wall  = wall_cost if idx in (o_idx, bo_idx) else 0.0
        rot   = rotation_penalty if idx == target_bow_idx else 0.0
        # §13.7 lateral-balance term: applied to sec 0 only.  Real-shore
        # obstacle costs already handle the "too close" half of corridor
        # centering; this restores the symmetric "too far" half.
        lateral = lateral_cost_sec0 if idx == 0 else 0.0
        # §13.10 — Tangent bias: cost proportional to angular distance
        # from the estimated shore-tangent bearing.  When tangent
        # estimator is silent (None), no bias applied.
        if tangent_target_deg is not None:
            tangent_bias = K_TANGENT_BIAS * abs(
                _SECTOR_REL_ANGLE[idx] - tangent_target_deg
            ) / 180.0
        else:
            tangent_bias = 0.0
        base = W_OBSTACLE * obs + wall + rot + lateral + tangent_bias
        # Live cost includes a small heading-smoothing tiebreaker so
        # equally costed sectors prefer the gentler turn (see
        # W_HEADING_SMOOTH for the live t=15 case this catches).
        costs[idx]                = (base + w_ideal_effective * ideal
                                     + W_HEADING_SMOOTH * heading_dist)
        costs_no_ideal[idx]       = base
        costs_heading_smooth[idx] = (base + w_ideal_effective * ideal
                                     + W_HEADING_SMOOTH_DIAG * heading_dist)
    alts = {"no_ideal": costs_no_ideal, "heading_smooth": costs_heading_smooth}
    return costs, ideal_idx, (wall_cost > 0.0), alts


def _select_sector(costs: dict[int, float],
                   last_idx: Optional[int]) -> Optional[int]:
    """Pick the min-cost sector with VFH+ hysteresis."""
    if not costs:
        return None
    min_idx = min(costs, key=costs.get)
    if last_idx is not None and last_idx in costs:
        # Stick with last choice unless new is meaningfully better.
        if costs[last_idx] <= costs[min_idx] + HYSTERESIS_MARGIN:
            return last_idx
    return min_idx


# ── HugShoreGoal ────────────────────────────────────────────────────────────

@dataclass
class HugShoreGoal:
    """Track a coastline on `side` using a VFH+ steering policy.

    The goal does NOT call perceive() itself — the loop driver (or
    caller) is responsible for writing `nav` into BotObservation
    before each tick.  Pure policy: one observation in, one action
    out.
    """

    side: Side
    max_ticks: int = 120
    # See module-level docs on HUG_SHORE_DRIVER_MODES.  Constructor
    # arg takes precedence; pass None (the default) to honour
    # UWO_HUG_SHORE_DRIVER in the environment, falling back to
    # HUG_SHORE_DRIVER_DEFAULT.  Tests pass an explicit mode to
    # avoid any env-var inheritance affecting them.
    driver_mode: Optional[str] = None

    # §13.23 — waypoint generator strategy ("pre_phase_a" | "phase_a").
    # Constructor arg takes precedence; None means honour
    # UWO_WAYPOINT_GENERATOR env var, falling back to
    # WAYPOINT_GENERATOR_DEFAULT.  "pre_phase_a" = legacy θ_err every
    # tick, no segment commitment.  "phase_a" = Bug-style world-frame
    # segment-identity tracking + HOLD on ambiguity (see
    # docs/shore_segment_commitment_design.md).
    waypoint_generator: Optional[str] = None

    # §13.26 — Astern-expanded candidate set toggle.  Default False
    # (forward-arc only) after the phase_a_dest8_20260602_170946 vs
    # _noastern_174745 A/B revealed the gate causes bend oscillation:
    # at river bends the goal bearing transiently passes behind the bow
    # while the canonical Bug2-style behaviour is to commit to following
    # the boundary, not to turn back.  Admitting rear sectors gave the
    # avoider permission to pick ~180° turns at the bend (live data
    # showed 27 of 81 expansions producing rear-sector picks, several at
    # the ±90°/tick max-cap), which fed back into heading-detector noise
    # and produced a 420-tick oscillation.  The canonical fix for genuine
    # dead-ends (Y/T topology) is a stuck-detector + dedicated UTurn
    # recovery primitive — not goal-bearing-based candidate widening.
    # See Nav2 recovery behaviors and Bug2/TangentBug commitment rules.
    # Set True to re-enable for diagnostic A/B against this baseline.
    astern_expansion: bool = False

    # Phase 2: FrontierPicker selection.  "none" = no override;
    # "yamauchi" = YamauchiPicker fires when CoverageTracker reports
    # STUCK.  Default "none" so the wire-in lands without behavior
    # change; flipped to "yamauchi" after voyage validation.  See
    # docs/destination_generator_design.md § "Phase 2 update".
    junction_picker: str = "none"

    # §13.16.3 — Step 1 of goal-management rollout (see
    # project_goal_management_architecture memory).  When set, the
    # wall-ahead commit uses VFH+ μ₁ (target-direction) cost to pick
    # the sector whose post-turn heading is closer to this bearing.
    # Compass degrees, 0=N, 90=E.
    goal_heading_deg: Optional[float] = None

    # §13.16.5 — Step 4: destination-based LOS guidance.  When both are
    # set, `_los_bearing_deg()` returns the compass bearing from the
    # bot's current HUD position to this destination point, recomputed
    # each tick.  This becomes the *primary* deliberative anchor for
    # `_effective_goal_heading_deg()` (above the §13.16.4 history-
    # inferred sequencer signal).  Game coordinates: `lat` increases
    # north, `lon` increases east.  Standard Lekkas-Fossen marine-USV
    # LOS guidance — the heading-autopilot then chases the bearing.
    endpoint_lat: Optional[float] = None
    endpoint_lon: Optional[float] = None

    phase: HugPhase = field(default=HugPhase.INIT, init=False)
    tick_count: int = field(default=0, init=False)
    last_steer: Optional[str] = field(default=None, init=False)
    last_chosen_sector: Optional[int] = field(default=None, init=False)
    last_ideal_sector: Optional[int] = field(default=None, init=False)

    # Dead-end detection — when phase=BLOCKED for ≥ this many ticks
    # AND best is sector 0, force a turn into the next-cheapest sector
    # to break the standoff (otherwise the ship grinds forward into the
    # wall while the cost function keeps picking "least-bad straight").
    consecutive_blocked: int = field(default=0, init=False)

    # §13.16 — Bug2/Trémaux wall-ahead commitment.  When ahead is
    # walled, the bot commits to one fixed turn direction (sec 7 for
    # starboard, sec 1 for port) and stays with it until ahead clears.
    # `None` = not currently committed.  Persists across ticks.
    _wall_ahead_commit: Optional[int] = field(default=None, init=False)

    # §13.27 — Stuck-detector + U-turn recovery.  Detector accumulates
    # position + forward-clearance samples; uturn_state is non-None
    # while a recovery is in flight.  See brain/goals/uturn_recovery.py.
    _stuck_detector: StuckDetector = field(
        default_factory=StuckDetector, init=False)
    _uturn_state: Optional[UTurnState] = field(default=None, init=False)

    # Destination-generator state: the previous tick's destination
    # point, threaded back in so the generator can fall back to it
    # when the shore-tangent fit is stale.  See
    # docs/destination_generator_design.md.
    _last_destination: Optional[tuple[float, float]] = field(
        default=None, init=False)

    # CoverageTracker — 2D visited-cell grid; observer-only.
    # Constructed lazily on the first valid position read regardless
    # of endpoint (works in both endpoint and exploration modes).
    # Emits MAKING_PROGRESS / KEEP_FOLLOWING / STUCK into the trace
    # each tick; STUCK will drive FrontierPicker in a later phase.
    # Replaces the deprecated m_line_monitor — see
    # docs/destination_generator_design.md § "Phase 2 update".
    _coverage_tracker: Optional[CoverageTracker] = field(
        default=None, init=False)
    _coverage_verdict: Optional[str] = field(default=None, init=False)

    # Phase 2: FrontierPicker override state.  When the picker fires
    # on a STUCK verdict, it returns a target point that overrides
    # the destination-generator's lookahead for the next
    # `PICKER_COMMIT_WINDOW_TICKS` ticks.  Reset when the window
    # expires or when CoverageTracker reports MAKING_PROGRESS.
    _picker_target:            Optional[tuple[float, float]] = field(
        default=None, init=False)
    _picker_commit_remaining:  int = field(default=0, init=False)
    _picker_invocations:       int = field(default=0, init=False)
    # The picker instance — held across STUCK events so stateful
    # backends (TremauxPicker carries a JunctionGraph) retain memory
    # across the voyage.  Lazy-initialized on first STUCK.
    _frontier_picker:          Optional[object] = field(
        default=None, init=False)

    # §13.21 — Bug2-style temporal commitment for shore-tangent
    # direction.  Lazy-initialised from the first valid tangent
    # extraction; subsequent ticks pick the candidate aligned with
    # this direction (ignoring bow), so bow-bounce-induced wp flips
    # don't happen.  Updated by the caller on TremauxPicker mode
    # transitions to topology events (take_exit, dead_end backtrack,
    # reversal_backtrack), and cleared on arrived_from_backtrack so
    # the next valid tangent re-initialises it.
    _committed_tangent_direction: Optional[float] = field(
        default=None, init=False)
    _prev_picker_mode:            Optional[str] = field(
        default=None, init=False)
    # §13.21 commitment warmup — the first tick after departure has
    # an unreliable heading reading (UI still animating, ship icon
    # partially obscured).  Voyage 170442 t1 misread bow as 138°
    # (actual ≈ 290°), committed init to 173° south, then §13.23
    # HOLD fired for 8 ticks because every fresh reading disagreed.
    # We defer commitment init until the recent heading history is
    # stable (max-min spread within COMMITMENT_STABILITY_DEG over
    # COMMITMENT_WARMUP_WINDOW samples).  Standard robotics warm-up
    # filter pattern.
    _recent_headings: list = field(default_factory=list, init=False)

    # §13.29 — sail_stop tracking for segment-HOLD bursts.  Mirrors the
    # §13.28 heading_rejected anchor mechanism: on the first HOLD tick
    # of a burst, drop sail so the ship loses momentum instead of
    # drifting blind.  Cleared when segment HOLD clears.
    _hold_anchor_active: bool = field(default=False, init=False)

    # §13.16.4 — Step 2 sequencer: cached inferred goal heading from
    # recent history.  Updated each tick by
    # `_update_inferred_goal_heading()` with hysteresis so it doesn't
    # chatter on noise.  Consumed by `_effective_goal_heading_deg()`
    # when no explicit `goal_heading_deg` is set.
    _inferred_goal_heading_deg: Optional[float] = field(default=None, init=False)

    # Online steering calibration — see RATE_* constants above.
    rate_dps: float = field(default=RATE_INIT_DPS, init=False)
    rate_samples: int = field(default=0, init=False)
    # (heading_before, side, duration_ms) for the hold fired LAST tick.
    # We measure the rotation it produced on the next mini-map read and
    # update rate_dps from that.
    _pending_hold: Optional[tuple[float, str, int]] = field(default=None, init=False)

    # Virtual-wall calibration — converges on the natural hug distance
    # observed in stable hugging mode (shore visible, ahead clear).
    # See WALL_* constants for bounds and learning rate.
    wall_distance: float = field(default=WALL_DIST_INIT, init=False)
    wall_samples: int = field(default=0, init=False)

    # Phase A — multi-tick state memory.  Rolling deque of TickRecord;
    # see _derive_signals() for the derived temporal signals consumed
    # by the policy.
    history: Deque[TickRecord] = field(
        default_factory=lambda: deque(maxlen=HISTORY_WINDOW), init=False,
    )
    # Latest derived signals; refreshed in tick() before policy logic.
    signals: HistorySignals = field(default_factory=HistorySignals, init=False)
    # Track lat/lon/speed that the runner provides on each tick (set by
    # the loop, read by tick() at recording time).  The runner is
    # responsible for populating these before calling tick().
    _hud_lat:      Optional[float] = field(default=None, init=False)
    _hud_lon:      Optional[float] = field(default=None, init=False)
    _hud_speed_kt: Optional[float] = field(default=None, init=False)
    # raw_heading + rejected flag from the loop's sanity check
    _last_raw_heading: Optional[float] = field(default=None, init=False)
    _last_rejected:    bool = field(default=False, init=False)

    # Phase 1.5 — last valid Lyapunov state for short shore-blind gaps.
    # Refreshed every tick that has live shore visibility; consumed by
    # _project_lyap_state() on ticks that don't.  See docs/shore_
    # following_design.md §12.5.2.
    _lyap_memory: Optional[dict] = field(default=None, init=False)

    # §13.23 Phase A — world-frame shore segment commitment.  See
    # docs/shore_segment_commitment_design.md.  Maintains the identity
    # of the physical shore being hugged; survives body-frame rotations
    # and short heading-detector outages.  Updated by `evaluate_segment`
    # each acted tick.
    _segment_memory: Optional[ShoreSegmentMemory] = field(
        default=None, init=False)

    # §13.13 — EMA-smoothed T.nearest_dist for the lateral controller.
    # First sample initialises the smoother; subsequent samples blend
    # at T_DIST_EMA_ALPHA.
    _t_dist_smooth: Optional[float] = field(default=None, init=False)

    def __post_init__(self):
        if self.driver_mode is None:
            import os
            self.driver_mode = os.environ.get(
                HUG_SHORE_DRIVER_ENV, HUG_SHORE_DRIVER_DEFAULT,
            )
        if self.driver_mode not in HUG_SHORE_DRIVER_MODES:
            raise ValueError(
                f"driver_mode={self.driver_mode!r} not in "
                f"{HUG_SHORE_DRIVER_MODES}"
            )
        logger.info(f"[hug_shore] driver_mode={self.driver_mode}")
        # §13.23 — resolve the waypoint-generator strategy.  Same
        # precedence as driver_mode: constructor arg > env var > default.
        if self.waypoint_generator is None:
            import os
            self.waypoint_generator = os.environ.get(
                WAYPOINT_GENERATOR_ENV, WAYPOINT_GENERATOR_DEFAULT,
            )
        if self.waypoint_generator not in WAYPOINT_GENERATOR_MODES:
            raise ValueError(
                f"waypoint_generator={self.waypoint_generator!r} not in "
                f"{WAYPOINT_GENERATOR_MODES}"
            )
        logger.info(
            f"[hug_shore] waypoint_generator={self.waypoint_generator}")
        # §13.18 phase 2: canonical VFH+ defaults (μ1=5, μ2=2, μ3=0)
        # from Ulrich & Borenstein 1998.  The μ2 inertia term damps the
        # ±45° per-tick flip-flop our 8-sector grid is prone to in
        # narrow corridors — the canonical fix for narrow-passage
        # oscillation per the BARN Challenge 2022 winners.
        self._avoider: VFHPlusAvoider = VFHPlusAvoider()
        # §13.18 phase 4: adaptive safety_dist via narrow-passage
        # detector with hysteresis.  Open-water keeps the existing
        # safety_dist=0.12; narrow passages drop to 0.06 so sec 0
        # stops being masked when the bend wall is ~0.08-0.10 away,
        # letting the bot creep forward instead of spiraling on ±45°
        # turns.  See docs/steering_architecture.md and the 2026-06-01
        # trace analysis (hug_debug_20260601_114709 t360-378).
        self._config_selector: ConfigSelector = ConfigSelector(
            # §13.34 — graduated density is restricted to the narrow
            # config.  In open water the binary mask is correct
            # (graduated admitted noisy near-shore candidates and pulled
            # the bot off course, see explore_port_20260603_143438).
            # In narrow channels (the Schmitt-trigger fires at 5+ dense
            # sectors) the binary cliff at frac=0.6 systematically masks
            # the right-direction sector — see t251, t307, t495 borderline
            # cases.  Graduated density admits them with a small penalty
            # so the angular cost can still pick the right side.
            open_cfg=_make_vfh_config(POINT_PURSUIT_SAFETY_DIST,
                                       graduated=False),
            narrow_cfg=_make_vfh_config(NARROW_SAFETY_DIST,
                                         graduated=True),
        )

    def set_hud(self, lat=None, lon=None, speed_kt=None,
                raw_heading=None, rejected=False):
        """Hook for the loop driver to push HUD readings into the goal
        before tick() runs.  Used by Phase A memory recording."""
        self._hud_lat = lat
        self._hud_lon = lon
        self._hud_speed_kt = speed_kt
        self._last_raw_heading = raw_heading
        self._last_rejected = rejected
        # CoverageTracker — observer wire-in.  Lazy-initialized on
        # the first valid position read.  Works in both endpoint and
        # exploration modes (endpoint is not required).  STUCK
        # verdicts will drive FrontierPicker in a later phase.
        if lat is not None and lon is not None:
            if self._coverage_tracker is None:
                self._coverage_tracker = CoverageTracker()
            verdict = self._coverage_tracker.record(
                self.tick_count, lat, lon,
            )
            self._coverage_verdict = verdict.value

    def _record_observation(self, nav) -> None:
        """Append a TickRecord for this tick's observation.  Decision
        fields (commanded_deg, chosen_sector, etc.) are set to defaults
        and updated later in tick() via _finalize_record()."""
        # Update prior record's actual_delta now that we have a new
        # heading reading.
        if self.history and nav.ship_heading_deg is not None:
            prev = self.history[-1]
            if prev.heading is not None:
                prev.actual_delta = self._wrap_signed(
                    nav.ship_heading_deg - prev.heading
                )
        # Sectors are stored as a tuple of (frac, dist) tuples so we
        # don't hold references to SectorReading objects that may be
        # mutated elsewhere.
        sec_tuple = tuple(
            (s.land_fraction, s.nearest_dist) for s in nav.sectors
        )
        record = TickRecord(
            tick=self.tick_count,
            wall_time=time.monotonic(),
            heading=nav.ship_heading_deg,
            raw_heading=self._last_raw_heading,
            rejected=self._last_rejected,
            lat=self._hud_lat,
            lon=self._hud_lon,
            speed_kt=self._hud_speed_kt,
            sectors=sec_tuple,
            commanded_deg=0.0,         # filled by _finalize_record
            actual_delta=None,         # filled NEXT tick when heading is known
            phase=self.phase,
            chosen_sector=None,
            ideal_sector=None,
            cost_dump={},
        )
        self.history.append(record)

    def _finalize_record(
        self, commanded_deg: float, chosen_sector: Optional[int],
        ideal_sector: Optional[int], phase: "HugPhase",
        cost_dump: dict,
    ) -> None:
        """Fill in the decision fields on the current tick's record."""
        if not self.history:
            return
        cur = self.history[-1]
        cur.commanded_deg = commanded_deg
        cur.chosen_sector = chosen_sector
        cur.ideal_sector  = ideal_sector
        cur.phase         = phase
        cur.cost_dump     = cost_dump

    @property
    def is_complete(self) -> bool:
        return self.phase in (HugPhase.COMPLETE, HugPhase.FAILED)

    # ── Helpers ──

    @staticmethod
    def _wrap_signed(delta: float) -> float:
        return (delta + 180.0) % 360.0 - 180.0

    def _uturn_target_heading(self) -> Optional[float]:
        """§13.27 — pick the heading the U-turn should align to.
        Prefers destination-LOS (stable) over inferred goal heading.
        Returns None if neither signal is available — caller skips
        recovery in that case."""
        if (self.endpoint_lat is not None
                and self.endpoint_lon is not None
                and self._hud_lat is not None
                and self._hud_lon is not None):
            from brain.goals.shore_segment import _bearing_compass
            return _bearing_compass(
                self._hud_lat, self._hud_lon,
                self.endpoint_lat, self.endpoint_lon,
            )
        return self.goal_heading_deg

    def _uturn_step(self, nav) -> Optional[HugTickResult]:
        """§13.27 — record sample, then either continue an in-flight
        U-turn, enter one if the detector fires, or return None to let
        normal steering take over.

        Suspends waypoint computation and collision checks during
        recovery — the U-turn must not be interrupted by the same
        signals that triggered it (otherwise the recovery would
        oscillate just like the steering it's supposed to fix)."""
        # Record this tick's sample regardless of phase.
        fwd_clear = forward_clearance_mean(nav)
        self._stuck_detector.record(
            self.tick_count, self._hud_lat, self._hud_lon, fwd_clear,
        )

        # In-flight recovery: check exit, else command another turn.
        if self._uturn_state is not None:
            if nav.ship_heading_deg is None:
                # No heading reading — wait one tick, don't crash.
                return HugTickResult(
                    action="wait", phase=self.phase,
                    note="§13.27 uturn: waiting for heading",
                    delay=0.5,
                )
            # Refresh the latched target each tick — destination-LOS
            # rotates as the bot moves, so we want the latest bearing
            # to align to.  Skipping this would make the U-turn aim at
            # a stale point.
            tgt = self._uturn_target_heading()
            if tgt is not None:
                self._uturn_state.latched_target_deg = tgt
            tgt = self._uturn_state.latched_target_deg
            if uturn_should_exit(nav.ship_heading_deg, tgt):
                exit_lat, exit_lon = self._hud_lat, self._hud_lon
                if exit_lat is not None and exit_lon is not None:
                    self._stuck_detector.disarm_at(exit_lat, exit_lon)
                logger.info(
                    f"[hug_shore] §13.27 uturn complete after "
                    f"{self.tick_count - self._uturn_state.entry_tick} ticks "
                    f"— bow={nav.ship_heading_deg:.0f}° target={tgt:.0f}°"
                )
                self._uturn_state = None
                self.phase = HugPhase.OFFSHORE
                return None  # fall through to normal steering
            # Command the calibrated hold.
            direction, magnitude = recovery_command(
                nav.ship_heading_deg, tgt,
            )
            duration_ms = int(round(
                (magnitude / max(self.rate_dps, 1.0)) * 1000.0
            ))
            duration_ms = max(80, min(1200, duration_ms))
            action = (f"hold_{direction}:{duration_ms}ms"
                      f"(~{magnitude:.0f}°@{self.rate_dps:.0f}°/s)")
            return HugTickResult(
                action=action, phase=HugPhase.UTURN_RECOVERY,
                note=(f"§13.27 uturn: bow={nav.ship_heading_deg:.0f}° → "
                      f"target={tgt:.0f}° Δ={magnitude:.0f}°"),
                delay=0.3,
            )

        # Not in recovery — check the detector.
        if self._stuck_detector.check():
            tgt = self._uturn_target_heading()
            if tgt is None or nav.ship_heading_deg is None:
                # Detector fired but no target / heading — log and
                # let normal steering try.  We'll re-evaluate next
                # tick once the signals arrive.
                logger.warning(
                    "[hug_shore] §13.27 stuck detector fired but "
                    "no target heading available — skipping recovery"
                )
                return None
            self._uturn_state = UTurnState(
                entry_tick=self.tick_count,
                latched_target_deg=tgt,
                entry_lat=self._hud_lat,
                entry_lon=self._hud_lon,
            )
            self.phase = HugPhase.UTURN_RECOVERY
            logger.info(
                f"[hug_shore] §13.27 ENTERING uturn recovery: "
                f"bow={nav.ship_heading_deg:.0f}° target={tgt:.0f}° "
                f"Δ={self._wrap_signed(tgt - nav.ship_heading_deg):.0f}°"
            )
            direction, magnitude = recovery_command(
                nav.ship_heading_deg, tgt,
            )
            duration_ms = int(round(
                (magnitude / max(self.rate_dps, 1.0)) * 1000.0
            ))
            duration_ms = max(80, min(1200, duration_ms))
            action = (f"hold_{direction}:{duration_ms}ms"
                      f"(~{magnitude:.0f}°@{self.rate_dps:.0f}°/s)")
            return HugTickResult(
                action=action, phase=HugPhase.UTURN_RECOVERY,
                note=(f"§13.27 ENTER uturn: bow={nav.ship_heading_deg:.0f}° "
                      f"→ target={tgt:.0f}° Δ={magnitude:.0f}°"),
                delay=0.3,
            )

        return None  # no recovery active or needed

    def _update_inferred_goal_heading(self) -> None:
        """§13.16.4 — Recompute the inferred goal heading from recent
        history each tick.  Applies Schmitt-trigger hysteresis at the
        sequencer level: only update `_inferred_goal_heading_deg` when
        the new estimate differs from the current cached value by more
        than GOAL_UPDATE_HYSTERESIS_DEG."""
        new_estimate = _infer_goal_heading_from_history(self.history)
        if new_estimate is None:
            return
        if self._inferred_goal_heading_deg is None:
            self._inferred_goal_heading_deg = new_estimate
            logger.info(
                f"[hug_shore] §13.16.4 sequencer: initial goal heading "
                f"inferred = {new_estimate:.0f}°"
            )
            return
        if _angle_diff_deg(new_estimate, self._inferred_goal_heading_deg) \
                >= GOAL_UPDATE_HYSTERESIS_DEG:
            logger.info(
                f"[hug_shore] §13.16.4 sequencer: goal heading update "
                f"{self._inferred_goal_heading_deg:.0f}° → "
                f"{new_estimate:.0f}°"
            )
            self._inferred_goal_heading_deg = new_estimate

    def _los_bearing_deg(self) -> Optional[float]:
        """§13.16.5 — Compass bearing from current HUD position to the
        configured destination.  Returns None when either the
        destination or the current HUD position is unavailable.

        Flat-earth approximation: the game's lat/lon are local game-
        coordinate units (not geodesic), so a simple atan2 of (Δlon,
        Δlat) gives the compass bearing.  Convention: 0=N, 90=E,
        increasing clockwise.
        """
        if (self.endpoint_lat is None or self.endpoint_lon is None
                or self._hud_lat is None or self._hud_lon is None):
            return None
        dlon = self.endpoint_lon - self._hud_lon   # east displacement
        dlat = self.endpoint_lat - self._hud_lat   # north displacement
        # atan2(dlon, dlat) yields the angle from +N (north) measured
        # CW (east-positive), exactly the compass convention.
        bearing = math.degrees(math.atan2(dlon, dlat))
        if bearing < 0:
            bearing += 360.0
        return bearing

    def _effective_goal_heading_deg(self) -> Optional[float]:
        """Active goal heading consumed by the reactive layer.

        Priority (canonical 3-layer architecture, see
        project_goal_management_architecture memory):
          1. Explicit manual override (`goal_heading_deg`) — operator wins
          2. Deliberative LOS bearing to destination (§13.16.5, Step 4)
          3. Sequencer-inferred from history (§13.16.4, Step 2)
          4. None → reactive falls back to centroid alignment (§13.16.1)
        """
        if self.goal_heading_deg is not None:
            return self.goal_heading_deg
        los = self._los_bearing_deg()
        if los is not None:
            return los
        return self._inferred_goal_heading_deg

    def _project_desired_waypoint_from_bearing(
        self, bearing_deg: Optional[float],
        lookahead_deg: float = 0.30,
    ) -> Optional[tuple[float, float]]:
        """Phase 2 live path — turn a perception-supplied bearing into a
        synthetic (lat, lon) waypoint by projecting from the current HUD
        position.

        Returns None when bearing is None or HUD lat/lon are unavailable.
        The projected distance (~33 km at 0.30°) is large enough that the
        §13.21 commit and arrival logic don't trip on it, but the
        bearing — which is what `_point_pursuit_desired_heading` actually
        consumes — is preserved exactly.
        """
        if bearing_deg is None:
            return None
        if self._hud_lat is None or self._hud_lon is None:
            return None
        rad = math.radians(bearing_deg)
        dlat = lookahead_deg * math.cos(rad)
        cos_lat = max(math.cos(math.radians(self._hud_lat)), 1e-6)
        dlon = lookahead_deg * math.sin(rad) / cos_lat
        return (self._hud_lat + dlat, self._hud_lon + dlon)

    def _update_rate_estimate(self, current_heading: Optional[float]) -> None:
        """If a hold was fired last tick, compare commanded vs observed
        rotation and update the EMA rate estimate.  Outlier-filtered."""
        if self._pending_hold is None or current_heading is None:
            self._pending_hold = None
            return
        heading_before, side, duration_ms = self._pending_hold
        self._pending_hold = None

        raw_delta = self._wrap_signed(current_heading - heading_before)
        # Expected sign: right (CW) increases compass heading.
        expected_sign = 1.0 if side == "right" else -1.0
        signed_delta = raw_delta * expected_sign

        if signed_delta < RATE_OBS_MIN_DEG:
            return  # too small or wrong-way (drift / collision recoil)
        if signed_delta > RATE_OBS_MAX_DEG:
            return  # likely collision bounce
        observed_rate = signed_delta / (duration_ms / 1000.0)
        if not (RATE_MIN_DPS <= observed_rate <= RATE_MAX_DPS):
            return

        self.rate_dps = (
            RATE_EMA_ALPHA * observed_rate
            + (1.0 - RATE_EMA_ALPHA) * self.rate_dps
        )
        self.rate_samples += 1
        logger.info(
            f"[hug_shore] calibration: hold {side} {duration_ms}ms → "
            f"{signed_delta:+.1f}° = {observed_rate:.0f}°/s  "
            f"(EMA now {self.rate_dps:.0f}°/s, n={self.rate_samples})"
        )

    def _update_wall_distance(self, nav) -> None:
        """Sample T.nearest_dist into the wall_distance EMA — but only
        from STABLE HUGGING ticks.

        Sampling discipline (else the EMA learns from collisions, drifts
        too low, drives the bot tighter, more collisions, …):
          - Shore must be substantive: T.frac >= 0.20
          - Ship must be in a clean hug range: 0.05 <= T.dist <= 0.30
            (below = scraping/collision; above = drifting offshore)
          - Ahead must be clear: ahead.frac < AHEAD_LOAD_FRAC AND
            (ahead.dist is None or ahead.dist > 0.15).  In emergency
            ticks, "T.dist" doesn't reflect intended hug distance.
        """
        t_idx = 4 if self.side == "starboard" else 12
        T = nav.sectors[t_idx]
        ahead = nav.sectors[0]

        if T.nearest_dist is None or T.land_fraction < 0.20:
            return
        if T.nearest_dist < 0.05 or T.nearest_dist > 0.30:
            return
        if ahead.land_fraction >= 0.25:
            return
        if ahead.nearest_dist is not None and ahead.nearest_dist < 0.15:
            return

        observed = T.nearest_dist
        new_wd = (
            WALL_DIST_EMA_ALPHA * observed
            + (1.0 - WALL_DIST_EMA_ALPHA) * self.wall_distance
        )
        new_wd = max(WALL_DIST_MIN, min(WALL_DIST_MAX, new_wd))
        if abs(new_wd - self.wall_distance) > 0.005:
            logger.info(
                f"[hug_shore] wall calibration: T.dist={observed:.2f} → "
                f"wall_distance {self.wall_distance:.2f}→{new_wd:.2f} "
                f"(wall_cost={_wall_cost(new_wd):.2f}, n={self.wall_samples+1})"
            )
        self.wall_distance = new_wd
        self.wall_samples += 1

    def _hold_for_angle(self, side: Literal["left", "right"],
                        angle_deg: float,
                        current_heading: Optional[float]) -> str:
        """Press-and-hold the rudder using the LIVE rate estimate.

        Stores (heading_before, side, ms) so the next tick can measure
        the actual rotation and update the rate.  P-control: duration
        is computed from |angle_deg| / rate_dps, clamped for safety.
        """
        from actions import sea_actions
        ms = int(round(1000.0 * angle_deg / self.rate_dps))
        # Cap = the lesser of (the duration MAX_TURN_PER_TICK degrees
        # would take at the live rate) and a HARD per-tick blind-window
        # ceiling.  This lets a slow-turning ship hold longer to still
        # reach the per-tick goal angle without ever blinding the loop
        # for more than HARD_CAP_MS.
        HARD_CAP_MS = 1200
        rate_cap_ms = int(MAX_TURN_PER_TICK * 1000.0 / max(self.rate_dps, RATE_MIN_DPS))
        max_ms = min(HARD_CAP_MS, rate_cap_ms)
        ms = max(60, min(ms, max_ms))
        if side == "left":
            sea_actions.hold_left(ms)
        else:
            sea_actions.hold_right(ms)
        self.last_steer = side
        if current_heading is not None:
            self._pending_hold = (current_heading, side, ms)
        return f"hold_{side}:{ms}ms(~{angle_deg:.0f}°@{self.rate_dps:.0f}°/s)"

    # ── Tick ──

    def tick(self) -> HugTickResult:
        """Run one VFH+ steering iteration."""
        self.tick_count += 1
        if self.tick_count >= self.max_ticks:
            self.phase = HugPhase.COMPLETE
            return HugTickResult(
                action="stop",
                phase=self.phase,
                note=f"reached max_ticks={self.max_ticks}",
            )

        cur = _obs.current()
        if cur is None or cur.nav is None:
            return HugTickResult(
                action="wait",
                phase=self.phase,
                note="no nav view this tick",
                delay=2.0,
            )

        nav = cur.nav
        if len(nav.sectors) < 8:
            self.phase = HugPhase.FAILED
            return HugTickResult(
                action="wait",
                phase=self.phase,
                note=f"unexpected sector count {len(nav.sectors)}",
            )

        # Online steering calibration — measure the rotation produced
        # by the previous tick's hold and update rate_dps.
        self._update_rate_estimate(nav.ship_heading_deg)

        # Virtual-wall calibration — when we're in stable hugging
        # (shore visible, ahead clear), let wall_distance EMA-track the
        # observed T.nearest_dist.  Slow alpha + safety bounds prevent
        # the calibration from chasing transient outliers.
        self._update_wall_distance(nav)

        # ── Phase A: memory recording ──
        # Update the prior tick's actual_delta now that we have the new
        # heading, then append this tick's record with placeholder
        # decision fields (filled in below).  Signals derived next.
        self._record_observation(nav)
        self.signals = _derive_signals(self.history, self.side)

        # §13.17 — arrival check.  When destination is set and the bot's
        # current HUD position is within POINT_PURSUIT_ACCEPTANCE_DEG of
        # the destination on BOTH axes, mark phase=COMPLETE and return.
        if (self.endpoint_lat is not None
                and self.endpoint_lon is not None
                and self._hud_lat is not None
                and self._hud_lon is not None):
            dlat = abs(self._hud_lat - self.endpoint_lat)
            dlon = abs(self._hud_lon - self.endpoint_lon)
            if (dlat < POINT_PURSUIT_ACCEPTANCE_DEG
                    and dlon < POINT_PURSUIT_ACCEPTANCE_DEG):
                self.phase = HugPhase.COMPLETE
                note = (
                    f"§13.17 destination reached: "
                    f"|dlat|={dlat:.2f} < {POINT_PURSUIT_ACCEPTANCE_DEG}, "
                    f"|dlon|={dlon:.2f} < {POINT_PURSUIT_ACCEPTANCE_DEG}"
                )
                logger.info(f"[hug_shore] {note}")
                return HugTickResult(
                    action="destination_reached",
                    phase=self.phase, note=note, delay=0.0,
                )

        # §13.27 — Stuck-detector + U-turn recovery.  Record sample,
        # then either continue an in-flight recovery, enter one if
        # the detector fires, or fall through to normal steering.
        uturn_result = self._uturn_step(nav)
        if uturn_result is not None:
            return uturn_result

        # §13.16.4 — Step 2 sequencer: refresh inferred goal heading
        # from history (used by §13.16.3 VFH+ μ₁ when no explicit
        # goal_heading_deg is set).
        self._update_inferred_goal_heading()

        # §13.13 — update EMA-smoothed T.dist before scoring sectors.
        # Used only by the lateral-drift term inside _score_sectors;
        # other consumers (rotation_penalty, intrusion check) keep
        # using the raw value because they target current proximity
        # rather than trajectory state.
        t_idx_for_smooth = 4 if self.side == "starboard" else 12
        target_for_smooth = nav.sectors[t_idx_for_smooth]
        # Gate at SHORE_VISIBLE_FRAC for consistency with the
        # lateral_drift gate inside _score_sectors — only accumulate
        # smoothed T.dist when there's *real* shore to smooth, not
        # noise-floor flicker.
        if (target_for_smooth.land_fraction >= SHORE_VISIBLE_FRAC
                and target_for_smooth.nearest_dist is not None):
            if self._t_dist_smooth is None:
                self._t_dist_smooth = target_for_smooth.nearest_dist
            else:
                self._t_dist_smooth = (
                    T_DIST_EMA_ALPHA * target_for_smooth.nearest_dist
                    + (1.0 - T_DIST_EMA_ALPHA) * self._t_dist_smooth
                )
        # When perception loses shore entirely, drop the smoother
        # state so the next acquisition starts fresh rather than
        # carrying stale data through the silent period.
        elif target_for_smooth.land_fraction < SHORE_VISIBLE_FRAC:
            self._t_dist_smooth = None

        # ── VFH+ core + §13.15 swept-volume lookahead ──
        # Speed source: HUD reading when available; HistorySignals median
        # as a smoother fallback; LOOKAHEAD_SPEED_FALLBACK_KT otherwise.
        speed_for_lookahead = (
            self._hud_speed_kt
            if self._hud_speed_kt is not None
            else (self.signals.median_speed_kt if self.signals else None)
        )
        costs, ideal_idx, wall_active, alt_costs = _score_with_lookahead(
            nav, self.side, self.wall_distance, self.signals,
            t_dist_smoothed=self._t_dist_smooth,
            speed_kt=speed_for_lookahead,
        )

        # ── §13.16 Bug2/Trémaux wall-ahead commit ──
        # §13.17 gate: point-pursuit mode (the new Lyapunov + canonical
        # VFH+ pipeline) does not use the wall-ahead commit machinery
        # at all — it computes its own steering via the goal-point
        # generator downstream.  Skip the entire §13.16 block so
        # `costs` stays clean for the point-pursuit picker.
        ahead_sec = nav.sectors[0] if len(nav.sectors) > 0 else None
        ahead_frac = (ahead_sec.land_fraction
                      if ahead_sec is not None and ahead_sec.is_observed
                      else 0.0)
        _skip_bug2 = (self.driver_mode == "point_pursuit")
        if _skip_bug2:
            pass  # §13.17 point-pursuit owns steering
        elif self._wall_ahead_commit is not None:
            if ahead_frac < WALL_AHEAD_EXIT_FRAC:
                logger.info(
                    f"[hug_shore] §13.16 wall-ahead release: ahead.frac="
                    f"{ahead_frac:.2f} < {WALL_AHEAD_EXIT_FRAC}"
                )
                self._wall_ahead_commit = None
            else:
                # §13.16.2 — re-evaluate while committed.  As the ship
                # rotates, the centroid in ship-relative coords shifts;
                # if it crosses past target on the opposite side
                # (|delta| > tolerance, see hysteresis band), switch the
                # commit.  Without this, hug_debug_20260531_163015 t=1-12
                # drove a full circle because the original sec 1 commit
                # never updated as the bot rotated 200°+.
                prev = self._wall_ahead_commit
                self._wall_ahead_commit = _bug2_commit_sector(
                    nav, self.side, current_commit=prev,
                    goal_heading_deg=self._effective_goal_heading_deg(),
                )
                if self._wall_ahead_commit != prev:
                    cr = _obstacle_centroid_relative_bearing(nav)
                    logger.info(
                        f"[hug_shore] §13.16.2 wall-ahead switch: "
                        f"goal_hdg={self.goal_heading_deg} "
                        f"centroid_rel={cr if cr is None else f'{cr:.0f}°'} "
                        f"→ sector {prev} → {self._wall_ahead_commit}"
                    )
        elif ahead_frac >= AHEAD_WALL_FRAC:
            # §13.16.1/13.16.3 — wall-ahead commit (entry).  Goal-direction
            # (VFH+ μ₁) if goal_heading_deg is set; otherwise centroid
            # alignment fallback.
            self._wall_ahead_commit = _bug2_commit_sector(
                nav, self.side,
                goal_heading_deg=self._effective_goal_heading_deg(),
            )
            cr = _obstacle_centroid_relative_bearing(nav)
            logger.info(
                f"[hug_shore] §13.16 wall-ahead commit: ahead.frac="
                f"{ahead_frac:.2f} ≥ {AHEAD_WALL_FRAC}, "
                f"goal_hdg={self.goal_heading_deg} "
                f"centroid_rel={cr if cr is None else f'{cr:.0f}°'} "
                f"→ sector {self._wall_ahead_commit}"
            )
        if (not _skip_bug2
                and self._wall_ahead_commit is not None
                and self._wall_ahead_commit in costs):
            costs[self._wall_ahead_commit] = -1e6

        # ── Phase 2 of the Lyapunov migration: regulator DRIVES ──
        # When the regulator returns a heading (live state or short-gap
        # memory projection, §12.5.2), it overrides VFH+'s best_idx
        # below.  When it returns None (genuinely no signal, caps
        # tripped, no recent shore at all), VFH+ falls through as
        # before.  See docs/shore_following_design.md §12.5.3.
        # VFH+ obstacle costs are still computed and used to (a) pick
        # an escape sector if BLOCKED and (b) populate the trace for
        # shadow comparison — we just don't gate on `_ideal_sector`'s
        # bang-bang preference anymore.
        live_state = _lyapunov_state(nav, self.side)
        projected = False
        if live_state is not None:
            lyap_state = live_state
            if nav.ship_heading_deg is not None:
                self._lyap_memory = {
                    "tick":      self.tick_count,
                    "heading":   nav.ship_heading_deg,
                    "d":         live_state[0],
                    "d_star":    live_state[1],
                    "theta_err": live_state[2],
                }
        else:
            lyap_state = _project_lyap_state(
                self._lyap_memory, nav.ship_heading_deg, self.tick_count,
            )
            projected = lyap_state is not None

        # §13.23 Phase A — shore segment commitment state machine.  Run
        # after the legacy lyap state is established so a HOLD decision
        # only short-circuits the steering policy (lyap memory and trace
        # diagnostics are unaffected).  See
        # docs/shore_segment_commitment_design.md.
        #
        # Gated on `waypoint_generator == "phase_a"`.  Default
        # "pre_phase_a" leaves the legacy θ_err untouched, the segment
        # memory unused, and emits no HOLD path.
        #
        # §13.24 — dest-LOS-on-HOLD.  When Phase A HOLDs (LSQ
        # disagreement at a junction), instead of emitting `action="hold"`
        # and drifting, suppress the hug component and let the avoider
        # pick a sector aligned purely with destination-LOS.  This is
        # the §13.21-pattern extension: the goal direction supersedes
        # the unreliable local-sensing signal until the shore-fit
        # stabilises.  Only available when a destination is set;
        # otherwise we fall back to the legacy hard hold.
        pp_use_dest_los_only = False
        segment_decision: Optional[SegmentDecision] = None
        if (self.waypoint_generator == "phase_a"
                and live_state is not None):
            samples_frac = [
                nav.sectors[i].land_fraction
                for i in (14, 12, 10) if i < len(nav.sectors)
            ]
            segment_decision = evaluate_segment(
                memory=self._segment_memory,
                fresh_theta_err_deg=live_state[2],
                ship_heading_deg=nav.ship_heading_deg,
                samples_frac=samples_frac,
                current_lat=self._hud_lat,
                current_lon=self._hud_lon,
                endpoint_lat=self.endpoint_lat,
                endpoint_lon=self.endpoint_lon,
                side=self.side,
                current_tick=self.tick_count,
            )
            self._segment_memory = segment_decision.memory
            if segment_decision.action == "act" \
                    and segment_decision.theta_err_override is not None:
                # Override the LSQ θ_err with the committed/EWMA tangent.
                lyap_state = (live_state[0], live_state[1],
                               segment_decision.theta_err_override)
                # §13.29 — resume sailing after the HOLD burst clears.
                if self._hold_anchor_active:
                    try:
                        import actions.sea_actions as _sea_actions
                        _sea_actions.sail_start()
                        logger.info(
                            "[hug_shore] §13.29 resuming sail after "
                            "segment-HOLD burst cleared"
                        )
                    except Exception as e:
                        logger.warning(
                            f"[hug_shore] sail_start failed after "
                            f"segment-HOLD: {e}"
                        )
                    self._hold_anchor_active = False
            elif segment_decision.action == "hold":
                # §13.24 — dest-LOS-on-HOLD if a destination + HUD
                # position are available.  §13.29 (this commit) —
                # exploration-mode fall-through so VFH+ still runs.
                #
                # The pre-§13.29 else-branch hard-returned action="hold"
                # which (a) did not call sail_stop, so the ship kept
                # sailing on inertia, and (b) bypassed VFH+ entirely,
                # so no obstacle avoidance ran.  Voyage 170442 t2-t9
                # drifted ~25 km NW and beached for exactly this
                # reason: 8 consecutive HOLD ticks with no steering
                # and no avoidance.
                #
                # New behaviour: set pp_use_dest_los_only and clear
                # lyap_state (same as the endpoint path), then fall
                # through.  Downstream `generate_destination` returns
                # `last_known_destination` when shore_tangent is None,
                # giving VFH+ a target.  If `last_known_destination`
                # is also None (very first tick), `pp_waypoint` is
                # None and the existing `vfh_fallback` path runs
                # avoidance in pure-react mode.  Either way, avoidance
                # no longer gets bypassed.
                #
                # As a defence in depth, the first HOLD tick of a
                # burst also drops sail — mirrors §13.28's behaviour
                # for heading_rejected.  The runner restarts the
                # rudder when the segment HOLD clears.
                pp_use_dest_los_only = True
                lyap_state = None
                if not self._hold_anchor_active:
                    try:
                        import actions.sea_actions as _sea_actions
                        _sea_actions.sail_stop()
                        self._hold_anchor_active = True
                        logger.warning(
                            f"[hug_shore] §13.29 STOPPING SHIP for "
                            f"segment-HOLD burst — will resume on next "
                            f"accepted tangent reading"
                        )
                    except Exception as e:
                        logger.warning(
                            f"[hug_shore] sail_stop failed during "
                            f"segment-HOLD: {e}"
                        )

        if lyap_state is not None and nav.ship_heading_deg is not None:
            lyap_desired = (
                nav.ship_heading_deg
                + _lyapunov_correction(lyap_state, self.side)
            ) % 360.0
            lyap_proposed_sector: Optional[int] = _heading_to_sector_index(
                lyap_desired, nav.ship_heading_deg,
            )
        else:
            lyap_desired = None
            lyap_proposed_sector = None

        mem_age = mem_hdg_delta = None
        if projected and self._lyap_memory is not None:
            mem_age = self.tick_count - self._lyap_memory["tick"]
            if nav.ship_heading_deg is not None:
                mem_hdg_delta = self._wrap_signed(
                    nav.ship_heading_deg - self._lyap_memory["heading"]
                )
        self._last_lyap = {
            "desired_heading_deg": lyap_desired,
            "proposed_sector":     lyap_proposed_sector,
            "d":          lyap_state[0]  if lyap_state else None,
            "d_star":     lyap_state[1]  if lyap_state else LYAPUNOV_D_TARGET,
            "theta_err":  lyap_state[2]  if lyap_state else None,
            "diverged":   (lyap_proposed_sector is not None
                           and lyap_proposed_sector != ideal_idx),
            "projected":         projected,
            "mem_age_ticks":     mem_age,
            "mem_heading_delta": mem_hdg_delta,
        }
        # Diagnostic: what would the policy pick under alternative cost
        # weightings?  Doesn't affect this tick's decision.
        alt_best = {
            name: (min(d, key=d.get) if d else None)
            for name, d in alt_costs.items()
        }
        if alt_best:
            def _dump(d):
                return " ".join(f"{i}={d.get(i, float('nan')):.2f}" for i in CANDIDATE_SECTORS)
            logger.info(
                f"[hug_shore] ALT  no_ideal_best={alt_best.get('no_ideal')}  "
                f"heading_smooth_best={alt_best.get('heading_smooth')}  "
                f"no_ideal=[{_dump(alt_costs['no_ideal'])}]  "
                f"heading_smooth=[{_dump(alt_costs['heading_smooth'])}]"
            )
        # Stash on the goal so the runner can fold into JSONL trace.
        self._last_alt_costs = alt_costs
        self._last_alt_best = alt_best

        # Regime change — when the ideal sector shifts (shore drifted
        # in or out), the previous best is no longer the right anchor
        # for hysteresis.  Drop stickiness this tick so the policy
        # reacts immediately to the regime change.  Observed
        # 2026-05-28: hysteresis held the bot on sector 0 ("on course")
        # for 6+ ticks after ideal flipped to 1 ("shore lost") — even
        # with HYSTERESIS_MARGIN=0.02, a regime change is categorical,
        # not a margin question.
        regime_changed = (
            self.last_ideal_sector is not None
            and self.last_ideal_sector != ideal_idx
        )
        if regime_changed:
            vfh_best_idx = min(costs, key=costs.get) if costs else None
        else:
            vfh_best_idx = _select_sector(costs, self.last_chosen_sector)
        self.last_ideal_sector = ideal_idx

        # ── Policy switch (A/B driver_mode) ──
        # The downstream dead-end escape still runs against best_idx,
        # so a Lyapunov pick of sector 0 while BLOCKED will still be
        # bumped off the wall.  See §12.5.3 for mode semantics.
        lyap_obs_cost = (_obstacle_cost(nav.sectors[lyap_proposed_sector])
                         if lyap_proposed_sector is not None else None)
        lyap_available = (
            lyap_proposed_sector is not None
            and lyap_proposed_sector in costs
        )

        if self.driver_mode == "point_pursuit":
            # §13.17 — Waypoint generator → Lyapunov bearing → canonical
            # VFH+ candidate masking.  Bypasses VFH+ cost min, Lyapunov-
            # wall-follower override, and the entire §13.16 wall-ahead
            # commit stack.
            #
            # Diagnostic logging: separately compute the hug-line and
            # destination-LOS components BEFORE blending, so the trace
            # makes it possible to tell which signal drove each tick.
            # When hug returns None, the §13.17.1 gate fired and the
            # combined waypoint == destination LOS waypoint (the bot is
            # in open water or has no observed shore on the configured
            # side).  When hug is non-None, both components are real
            # and `pp_waypoint` is their 50/50 blend.
            pp_waypoint_hug = None
            if (not pp_use_dest_los_only
                    and self.side is not None
                    and nav.ship_heading_deg is not None
                    and self._hud_lat is not None
                    and self._hud_lon is not None):
                pp_waypoint_hug = _hug_mode_waypoint(
                    current_lat=self._hud_lat,
                    current_lon=self._hud_lon,
                    ship_heading_deg=nav.ship_heading_deg,
                    lyap_state=live_state,
                    side=self.side, nav=nav,
                    endpoint_lat=self.endpoint_lat,
                    endpoint_lon=self.endpoint_lon,
                )
            pp_waypoint_dest_los = None
            if (self.endpoint_lat is not None
                    and self.endpoint_lon is not None
                    and self._hud_lat is not None
                    and self._hud_lon is not None):
                pp_waypoint_dest_los = _destination_mode_waypoint(
                    self._hud_lat, self._hud_lon,
                    self.endpoint_lat, self.endpoint_lon,
                )
            # Destination-generator path.  Replaces the legacy
            # `_generate_waypoint` blend.  When Phase A's shore tangent
            # is fresh, the destination is the shore-projected lookahead.
            # When stale, fall back to the previous tick's destination
            # (TangentBug boundary-following continuation).  See
            # docs/destination_generator_design.md.
            #
            # The endpoint does not enter the per-tick generator — it
            # only feeds the destination-anchored tangent direction
            # inside `_extract_shore_line` (§13.21) and the arrival
            # check elsewhere.
            shore_tangent = None
            if (not pp_use_dest_los_only
                    and self.side is not None
                    and nav.ship_heading_deg is not None
                    and self._hud_lat is not None
                    and self._hud_lon is not None):
                shore = _extract_shore_line(
                    nav, self.side, nav.ship_heading_deg,
                    current_lat=self._hud_lat,
                    current_lon=self._hud_lon,
                    endpoint_lat=self.endpoint_lat,
                    endpoint_lon=self.endpoint_lon,
                    committed_direction=self._committed_tangent_direction,
                    coverage=self._coverage_tracker,
                )
                if shore is not None:
                    tangent_compass, d, d_star = shore
                    # §13.21 commitment — initialise on the first
                    # tangent extraction whose bow reading has been
                    # stable across the warm-up window.  Without the
                    # stability gate, voyage 170442 init'd commitment
                    # at t1 from a 138° misread (actual ≈ 290°) and
                    # the §13.23 segment-HOLD then fired for every
                    # subsequent tick because fresh tangent disagreed
                    # with the wrong commitment.
                    if nav.ship_heading_deg is not None:
                        self._recent_headings.append(nav.ship_heading_deg)
                        if len(self._recent_headings) > COMMITMENT_WARMUP_WINDOW:
                            self._recent_headings = (
                                self._recent_headings[-COMMITMENT_WARMUP_WINDOW:])
                    heading_stable = (
                        len(self._recent_headings) >= COMMITMENT_WARMUP_WINDOW
                        and (_max_angular_spread_deg(self._recent_headings)
                             <= COMMITMENT_STABILITY_DEG)
                    )
                    if (self._committed_tangent_direction is None
                            and heading_stable):
                        self._committed_tangent_direction = tangent_compass
                        logger.info(
                            f"[hug_shore] §13.21 commitment init: "
                            f"tangent={tangent_compass:.1f}° "
                            f"(after {len(self._recent_headings)}-tick "
                            f"warm-up)"
                        )
                    shore_tangent = ShoreTangent(
                        tangent_compass_deg=tangent_compass,
                        perpendicular_distance=d,
                        d_star=d_star,
                    )
            if (self._hud_lat is not None
                    and self._hud_lon is not None
                    and nav.ship_heading_deg is not None
                    and self.side is not None):
                pp_waypoint = generate_destination(
                    pos=(self._hud_lat, self._hud_lon),
                    heading_deg=nav.ship_heading_deg,
                    shore_tangent=shore_tangent,
                    last_known_destination=self._last_destination,
                    hug_side=self.side,
                )
                self._last_destination = pp_waypoint
            else:
                pp_waypoint = None

            # FrontierPicker override.  When CoverageTracker has
            # reported STUCK (and the picker is enabled), invoke the
            # picker to pick a frontier cell as a recovery target.
            # The picker's target overrides the destination-generator
            # for PICKER_COMMIT_WINDOW_TICKS ticks, breaking the
            # oscillation.  Reset on MAKING_PROGRESS (the picker
            # successfully unstuck us — return to normal flow) or
            # when the commit window expires.  See
            # docs/destination_generator_design.md § "Phase 2 update".
            if (self.junction_picker != "none"
                    and self._coverage_tracker is not None
                    and self._hud_lat is not None
                    and self._hud_lon is not None):
                if self._coverage_verdict == CoverageVerdict.MAKING_PROGRESS.value:
                    # New cell entered while in commit window → reset.
                    self._picker_target = None
                    self._picker_commit_remaining = 0
                if self._frontier_picker is None:
                    self._frontier_picker = _make_frontier_picker(
                        self.junction_picker)
                picker = self._frontier_picker
                endpoint = None
                if (self.endpoint_lat is not None
                        and self.endpoint_lon is not None):
                    endpoint = (self.endpoint_lat, self.endpoint_lon)
                # Trémaux maintains stateful history (trajectory log,
                # backtracker, reversal detection) and must be polled
                # every tick.  Yamauchi remains STUCK-gated with a
                # commit window.
                is_tremaux = self.junction_picker == "tremaux"
                if is_tremaux:
                    if picker is not None:
                        target = picker.pick(
                            current_pos=(self._hud_lat, self._hud_lon),
                            coverage=self._coverage_tracker,
                            endpoint=endpoint,
                            nav=nav,
                        )
                        if target is not None:
                            if self._picker_target != target:
                                logger.info(
                                    f"[hug_shore] TremauxPicker target="
                                    f"{target} mode="
                                    f"{getattr(picker, 'last_mode', '?')}"
                                )
                            self._picker_target = target
                            self._picker_invocations += 1
                        else:
                            self._picker_target = None
                        # §13.21 commitment update on topology events.
                        # When the picker enters a mode that changes
                        # the bot's intended direction, re-anchor the
                        # tangent commitment from the new target's LOS
                        # bearing.  On arrived_from_backtrack, clear so
                        # the next valid tangent re-initialises.
                        mode = getattr(picker, "last_mode", None)
                        _TOPOLOGY_EVENT_MODES = {
                            "at_junction_take_exit",
                            "at_junction_backtrack",
                            "dead_end_start_backtrack",
                            "reversal_backtrack",
                        }
                        if (mode != self._prev_picker_mode
                                and mode in _TOPOLOGY_EVENT_MODES
                                and target is not None):
                            new_dir = _point_pursuit_desired_heading(
                                self._hud_lat, self._hud_lon,
                                target[0], target[1],
                            )
                            if new_dir is not None:
                                old = self._committed_tangent_direction
                                self._committed_tangent_direction = new_dir
                                logger.info(
                                    f"[hug_shore] §13.21 commitment "
                                    f"re-anchored by {mode}: "
                                    f"{old}° → {new_dir:.1f}°"
                                )
                        elif (mode == "arrived_from_backtrack"
                                and self._prev_picker_mode != mode):
                            self._committed_tangent_direction = None
                            logger.info(
                                "[hug_shore] §13.21 commitment cleared "
                                "(arrived_from_backtrack) — will re-init"
                            )
                        self._prev_picker_mode = mode
                elif (self._picker_commit_remaining == 0
                        and self._coverage_verdict
                        == CoverageVerdict.STUCK.value):
                    if picker is not None:
                        target = picker.pick(
                            current_pos=(self._hud_lat, self._hud_lon),
                            coverage=self._coverage_tracker,
                            endpoint=endpoint,
                            nav=nav,
                        )
                        if target is not None:
                            self._picker_target = target
                            self._picker_commit_remaining = (
                                PICKER_COMMIT_WINDOW_TICKS
                            )
                            self._picker_invocations += 1
                            logger.info(
                                f"[hug_shore] FrontierPicker fired: "
                                f"target={target} "
                                f"commit={PICKER_COMMIT_WINDOW_TICKS} "
                                f"(invocation #{self._picker_invocations})"
                            )
                if is_tremaux:
                    # Phase 2 — per-tick waypoint hint overrides any
                    # stale picker target.  Two perception flavours:
                    #
                    # 1. `nav.desired_waypoint` — a (lat, lon) target.
                    #    `SimNav` provides this directly from the
                    #    reference trace.
                    #
                    # 2. `nav.desired_waypoint_bearing_deg` — a compass
                    #    bearing along the skeleton from the live
                    #    `MinimapNavigationView`.  We project the
                    #    HUD lat/lon forward along this bearing by
                    #    a fixed distance to synthesise a lat/lon
                    #    waypoint.  Distance doesn't matter for the
                    #    desired-heading math — only the bearing
                    #    does — but it has to be reasonable for the
                    #    §13.21 commit / arrival gates.
                    #
                    # See `memory/project_skeleton_steering_migration.md`
                    # for the why; sim_nile_20260607_162303 validates
                    # the lat/lon path; live wire-in 2026-06-07.
                    desired_wp = getattr(nav, "desired_waypoint", None)
                    if desired_wp is None:
                        desired_wp = self._project_desired_waypoint_from_bearing(
                            getattr(nav, "desired_waypoint_bearing_deg", None),
                        )
                    if desired_wp is not None:
                        pp_waypoint = desired_wp
                        self._last_destination = pp_waypoint
                    elif self._picker_target is not None:
                        pp_waypoint = self._picker_target
                        self._last_destination = pp_waypoint
                elif (self._picker_commit_remaining > 0
                        and self._picker_target is not None):
                    pp_waypoint = self._picker_target
                    self._last_destination = pp_waypoint
                    self._picker_commit_remaining -= 1
            self._last_lyap["pp_waypoint"]          = pp_waypoint
            self._last_lyap["pp_waypoint_hug"]      = pp_waypoint_hug
            self._last_lyap["pp_waypoint_dest_los"] = pp_waypoint_dest_los
            if pp_waypoint is None:
                # Can't compute waypoint — fall back to VFH+ (defensive).
                best_idx = vfh_best_idx
                driver   = "vfh_fallback"
                self._last_lyap["pp_waypoint_bearing"] = None
                self._last_lyap["pp_free_sectors"]     = None
                self._last_lyap["pp_picked_for_target"] = None
            else:
                desired_h = _point_pursuit_desired_heading(
                    self._hud_lat, self._hud_lon,
                    pp_waypoint[0], pp_waypoint[1],
                )
                self._last_lyap["pp_waypoint_bearing"] = desired_h
                if desired_h is None:
                    best_idx = vfh_best_idx
                    driver   = "vfh_fallback"
                    self._last_lyap["pp_free_sectors"]      = None
                    self._last_lyap["pp_picked_for_target"] = None
                else:
                    cfg = self._config_selector.pick(nav)
                    # §13.26 — when wp is more than 90° from bow,
                    # admit astern sectors so the avoider can pick a
                    # genuine U-turn.  See ALL_SECTORS docstring.
                    if (self.astern_expansion
                            and nav.ship_heading_deg is not None
                            and abs(self._wrap_signed(
                                desired_h - nav.ship_heading_deg)) > 90.0):
                        _candidates = ALL_SECTORS
                    else:
                        _candidates = CANDIDATE_SECTORS
                    avoider_result = self._avoider.select(
                        nav, desired_h, _candidates, cfg,
                    )
                    pp_idx = avoider_result.chosen_sector
                    _free  = list(avoider_result.free_sectors)
                    self._last_lyap["pp_free_sectors"]  = _free
                    # Per-tick diag carries both the avoider's per-call
                    # info AND the selector's regime decision so the
                    # trace shows when narrow-mode kicked in.
                    self._last_lyap["pp_avoider_diag"]  = {
                        **avoider_result.diagnostics,
                        **self._config_selector.diagnostics(),
                    }
                    if pp_idx is None:
                        # All forward candidates masked — fall through.
                        best_idx = vfh_best_idx
                        driver   = "vfh_fallback"
                        self._last_lyap["pp_picked_for_target"] = None
                    else:
                        best_idx = pp_idx
                        driver   = "point_pursuit"
                        # Was the chosen sector forced by masking (only
                        # one survivor) or genuinely target-aligned?
                        # If all 5 CANDIDATE_SECTORS were free, the
                        # picker's choice was purely target-driven.
                        # If only 1 was free, picker had no choice —
                        # collision avoidance dominated.
                        self._last_lyap["pp_picked_for_target"] = (
                            len(_free) >= 2
                        )
        elif self.driver_mode == "vfh":
            best_idx = vfh_best_idx
            driver   = "vfh"
        elif self.driver_mode == "lyapunov":
            if lyap_available:
                best_idx = lyap_proposed_sector
                driver   = "lyapunov"
            else:
                best_idx = vfh_best_idx
                driver   = "vfh_fallback"
        else:  # "lyapunov_safe"
            if (lyap_available
                    and lyap_obs_cost is not None
                    and lyap_obs_cost < LYAPUNOV_VETO_OBSTACLE_COST):
                best_idx = lyap_proposed_sector
                driver   = "lyapunov"
            elif lyap_available:
                # Lyapunov had a proposal but it pointed into an
                # obstacle — let VFH+ veto.
                best_idx = vfh_best_idx
                driver   = "vfh_veto"
            else:
                best_idx = vfh_best_idx
                driver   = "vfh_fallback"

        self._last_lyap["mode"]              = self.driver_mode
        self._last_lyap["driver"]            = driver
        self._last_lyap["vfh_best_sector"]   = vfh_best_idx
        self._last_lyap["vfh_ideal_sector"]  = ideal_idx
        self._last_lyap["lyap_obs_cost"]     = lyap_obs_cost
        # §13.23 — surface segment-commit state on act-path ticks too.
        if segment_decision is not None:
            # §13.24 — distinguish HOLD-with-dest-LOS-fallback from a
            # normal "act" so traces show when local-sensing was
            # superseded by the goal-direction signal.
            self._last_lyap["segment_action"]   = (
                "hold_dest_los" if pp_use_dest_los_only
                else segment_decision.action
            )
            self._last_lyap["segment_note"]     = segment_decision.note
            self._last_lyap["segment_mem"]      = memory_to_dict(
                self._segment_memory)
            self._last_lyap["segment_fresh_TW"] = \
                segment_decision.fresh_tangent_world_deg

        if best_idx is None:
            # No observed candidates — perception is partial.
            return HugTickResult(
                action="wait",
                phase=self.phase,
                note="no observed forward sectors",
                delay=0.5,
            )

        # Phase label.
        best_obs_cost = _obstacle_cost(nav.sectors[best_idx])
        if best_obs_cost >= 0.50:
            self.phase = HugPhase.BLOCKED
            self.consecutive_blocked += 1
        else:
            if ideal_idx != 0:
                self.phase = HugPhase.OFFSHORE
            else:
                self.phase = HugPhase.HUGGING
            self.consecutive_blocked = 0

        # Dead-end escape: if BLOCKED for several ticks AND the cheapest
        # sector is straight ahead (which means "sail into the wall"),
        # force a turn into the next-cheapest sector.  Better to scrape
        # the side than grind the bow into a dead end.
        #
        # §13.18 phase 4 follow-up (2026-06-01): only applies to the
        # legacy VFH+ driver.  point_pursuit's CollisionAvoider already
        # masks sectors where nearest_dist < safety_dist, so sec 0 in
        # the avoider's chosen list IS safe by construction.  Letting
        # this escape override sec 0 anyway produced the over-rotation
        # observed at t9-t10 of hug_debug_20260601_135446 (commanded
        # -90° → bot ended up facing N instead of continuing south).
        # The new μ2=2 inertia makes sec 0 the lowest-cost choice more
        # often, so the trigger fired routinely in BLOCKED phases that
        # were actually safe-to-traverse.
        escape_fired = False
        if (self.driver_mode != "point_pursuit"
                and self.consecutive_blocked >= 3
                and best_idx == 0
                and len(costs) > 1):
            non_zero = {i: c for i, c in costs.items() if i != 0}
            if non_zero:
                escape_idx = min(non_zero, key=non_zero.get)
                # Target-side bias: when emergencies present a choice
                # between similar-cost directions, prefer the one toward
                # the hug-target side.  Keeps the bot oriented near
                # where shore is "supposed" to be after escape.  Only
                # fires when target-side option is within
                # ESCAPE_TARGET_BIAS of cheapest.
                target_bow_idx  = 2 if self.side == "starboard" else 14
                target_beam_idx = 4 if self.side == "starboard" else 12
                target_candidates = [
                    i for i in (target_bow_idx, target_beam_idx) if i in non_zero
                ]
                if target_candidates:
                    best_target = min(target_candidates, key=non_zero.get)
                    if (non_zero[best_target] - non_zero[escape_idx]
                            <= ESCAPE_TARGET_BIAS):
                        escape_idx = best_target
                logger.info(
                    f"[hug_shore] dead-end escape: blocked {self.consecutive_blocked} "
                    f"ticks, sector 0 cost={costs[0]:.2f} → forcing turn to sector "
                    f"{escape_idx} (cost={non_zero[escape_idx]:.2f})"
                )
                best_idx = escape_idx
                escape_fired = True

        self.last_chosen_sector = best_idx
        target_angle = _SECTOR_REL_ANGLE[best_idx]

        # §13.17 — variable-duration steering for point-pursuit mode.
        # The picker's sector tells us the DIRECTION (left / right / hold);
        # the magnitude should be the *actual* heading error, capped at
        # the sector's full bearing.  This avoids overshooting: if the
        # picker chose sec 1 (+45°) but the true error is only +20°,
        # command +20° not +45°.  Smaller corrections → smoother voyage.
        if (self.driver_mode == "point_pursuit"
                and self._last_lyap.get("pp_desired_heading") is not None
                and nav.ship_heading_deg is not None):
            pp_desired = self._last_lyap["pp_desired_heading"]
            actual_diff = self._wrap_signed(
                pp_desired - nav.ship_heading_deg
            )
            sec_bearing = _SECTOR_REL_ANGLE.get(best_idx, 0.0)
            if sec_bearing == 0:
                # Picker says hold.  Tiny correction only if desired is
                # meaningfully off — otherwise truly hold.
                if abs(actual_diff) >= 10.0:
                    # Small nudge in the actual error's direction.
                    cap = 15.0
                    target_angle = max(-cap, min(cap, actual_diff))
                else:
                    target_angle = 0.0
            elif sec_bearing > 0 and actual_diff > 0:
                # Direction agreement; don't overshoot.
                target_angle = min(actual_diff, sec_bearing)
            elif sec_bearing < 0 and actual_diff < 0:
                target_angle = max(actual_diff, sec_bearing)
            else:
                # Sign disagreement: picker chose this direction despite
                # actual error pointing the other way — typically because
                # the desired-direction sector was masked for safety.
                # Trust the picker; commit at the sector's full bearing.
                target_angle = sec_bearing

        # §13.7 fix C: variable-duration scaling.  Scale the commanded
        # angle by the cost margin (how much cheaper the chosen sector
        # is than just holding).  Small margin → small turn; large
        # margin → full nominal.  Bypassed in emergencies.
        # When margin is non-positive (sec 0 tied with best, or hysteresis
        # picked best despite sec 0 being slightly cheaper), commit at
        # MIN_SCALE rather than zeroing: if the policy chose to turn,
        # honour the direction with a small magnitude.
        # §13.11 — Also bypass when shore is NOT VISIBLE on the hug side.
        # Without shore visible, neither the lateral term nor the tangent
        # estimator fires, so the margin stays small → tiny turns.  But
        # tiny margin in this regime means "no information" (search mode),
        # not "high confidence in current heading."  Commit at full
        # nominal so the bot can actively rotate to re-acquire shore.
        # Observed live in hug_debug_20260531_095814 t=24-27: bot
        # committed 13°-per-tick taps trying to recover while drifting
        # offshore, lost coast entirely.
        target_idx_check = 4 if self.side == "starboard" else 12
        bow_t_idx_check  = 2 if self.side == "starboard" else 14
        shore_visible_for_scaling = (
            nav.sectors[target_idx_check].land_fraction >= SHORE_VISIBLE_FRAC
            or nav.sectors[bow_t_idx_check].land_fraction >= SHORE_VISIBLE_FRAC
        )
        # §13.18 phase 4 follow-up (2026-06-01): both magnitude tweaks
        # below are tuned against the legacy VFH+ cost recipe.  Under
        # point_pursuit, the avoider's μ1·target + μ2·current cost
        # owns the magnitude through the §13.17 variable-duration
        # steering rule above.  Letting the legacy margin scaling /
        # search cap run anyway pinches turns by a factor computed
        # from a different cost formula — see audit
        # docs/heading_pca_yellow_anchor.md + 2026-06-01 conversation.
        _legacy_magnitude_rules = (self.driver_mode != "point_pursuit")
        if (_legacy_magnitude_rules and not escape_fired
                and 0 in costs and best_idx != 0
                and shore_visible_for_scaling):
            margin = max(0.0, costs[0] - costs[best_idx])
            margin_scale = min(
                1.0,
                max(MARGIN_MIN_SCALE, margin / MARGIN_SATURATION),
            )
            target_angle = target_angle * margin_scale
        elif (_legacy_magnitude_rules and not escape_fired
                and best_idx != 0
                and not shore_visible_for_scaling):
            # §13.14 — Search mode: shore not visible.  Cap turn
            # magnitude so the bot performs a controlled spiral
            # search rather than rotating chaotically.  Allows
            # perception time to find the wall between taps.
            # Observed live in hug_debug_20260531_121728 t=25-35:
            # full 45°/90° taps every tick swept the bot through
            # 360°+ without re-acquiring shore.
            if target_angle > 0:
                target_angle = min(target_angle, SEARCH_MAX_TURN_DEG)
            else:
                target_angle = max(target_angle, -SEARCH_MAX_TURN_DEG)
        # else: full nominal (escape mode, hold direction, shore-lost
        # search under legacy VFH+, or any tick under point_pursuit).

        cost_dump = " ".join(
            f"{i}={costs.get(i, float('nan')):.2f}" for i in CANDIDATE_SECTORS
        )
        regime = f"wd={self.wall_distance:.2f}{'*' if wall_active else ''}"
        if escape_fired:
            regime += " ESC"

        if abs(target_angle) < TURN_DEADBAND_DEG:
            self._finalize_record(
                commanded_deg=0.0, chosen_sector=best_idx,
                ideal_sector=ideal_idx, phase=self.phase,
                cost_dump=costs,
            )
            return HugTickResult(
                action="hold",
                phase=self.phase,
                note=(f"VFH+ best={best_idx} ideal={ideal_idx} {regime} "
                      f"on-course [{cost_dump}]"),
            )

        # Cap per-tick rotation so the loop stays responsive.
        capped = math.copysign(
            min(abs(target_angle), MAX_TURN_PER_TICK), target_angle,
        )

        # Collision-bounce guard: when the commanded turn rotates the
        # bow INTO close opp-shore, scale the rotation magnitude down.
        # Rotating 45° in place when bow-port has shore at dist 0.11
        # drives the ship into the shore (live t=24 of 2026-05-30_114024:
        # commanded -45° left into bP=0.92/0.11, ship bounced +59° right).
        # Scaling rotation by opp_bow.dist gives the ship room to creep
        # forward into more open water before completing the turn.
        opp_bow_idx = 14 if self.side == "starboard" else 2
        opp_bow_sec = nav.sectors[opp_bow_idx]
        peeling_toward_opp = (
            (self.side == "starboard" and capped < 0)
            or (self.side == "port"      and capped > 0)
        )
        if (peeling_toward_opp
                and opp_bow_sec.land_fraction >= 0.40
                and opp_bow_sec.nearest_dist is not None
                and opp_bow_sec.nearest_dist < OPP_BOW_SAFE_DIST):
            scale = max(0.2, opp_bow_sec.nearest_dist / OPP_BOW_SAFE_DIST)
            capped = capped * scale
        side_dir: Literal["left", "right"] = (
            "left" if capped < 0 else "right"
        )
        action = self._hold_for_angle(
            side=side_dir,
            angle_deg=abs(capped),
            current_heading=nav.ship_heading_deg,
        )
        self._finalize_record(
            commanded_deg=capped, chosen_sector=best_idx,
            ideal_sector=ideal_idx, phase=self.phase,
            cost_dump=costs,
        )
        return HugTickResult(
            action=action,
            phase=self.phase,
            note=(f"VFH+ best={best_idx} ideal={ideal_idx} {regime} "
                  f"angle={target_angle:+.0f}° [{cost_dump}]"),
        )


# ── Convenience loop driver ─────────────────────────────────────────────────

def run_hug_shore_loop(
    side: Side,
    max_ticks: int = 120,
    tick_interval_s: tuple[float, float] = (0.15, 0.40),
    full_perceive_every: int = 0,
    hud_every: int = 1,
    minimap_lost_threshold_px: int = 30,
    minimap_lost_consecutive_ticks: int = 3,
    debug_dir: Optional[Path] = None,
    endpoint_lat: Optional[float] = None,
    endpoint_lon: Optional[float] = None,
    goal_heading_deg: Optional[float] = None,
    driver_mode: Optional[str] = None,
    astern_expansion: bool = True,
    junction_picker: str = "none",
) -> HugShoreGoal:
    """Drive HugShoreGoal in a tight perception loop.

    Each tick: capture frame, run the mini-map nav view (~25ms),
    update BotObservation, ask the goal for one steering primitive,
    sleep a jittered ~0.15–0.40s.

    `full_perceive_every=N` fires the expensive perceive() cascade
    every N ticks for overlay safety.  Default 0 (disabled) — the
    mini-map nav view alone is enough for steering and is ~600× faster.

    `hud_every=N` runs the sea-HUD reads (`read_speed`, `read_latlon`)
    only every N ticks.  Default 1 — read every tick.

    Historically this defaulted to 5 because lat/lon went through
    OmniParser (~10s/read) and we amortised cost.  Both readers were
    later moved to direct EasyOCR on a small crop (~30 ms speed +
    ~65 ms lat/lon, ~95 ms combined), making per-tick reads cheap.
    The default-5 was leftover from the OmniParser era and was
    silently freezing position for 4 of every 5 ticks (voyage
    180018 t1-t5 stuck at (30.16, 30.43) while the HUD progressed
    through (30.17, 30.35), (30.15, 30.26), (30.13, 30.18),
    (30.10, 30.10) — the bot acted on the stale t1 reading the
    whole time).  Set N>1 only if a future profiler shows HUD reads
    dominating tick time.

    `minimap_lost_threshold_px` / `_consecutive_ticks` — when the
    mini-map's green-ship pixel count falls below the threshold for
    N consecutive ticks, log a warning and force a full perceive() +
    HUD probe on the next tick.  Catches "we sailed into a port and
    the sea HUD vanished" without paying OmniParser every tick.

    `debug_dir` (optional): when set, dump aggressive per-tick
    diagnostics — mini-map crop PNGs and a JSONL trace of every signal
    + decision.  Use for noise / steering investigations.
    """
    from capture.adb_capture import capture_screen
    from vision.minimap_navigation_view import read_navigation_view, _crop_minimap
    from vision.sea_hud import (
        _NO_PENDING,
        accept_or_defer as _accept_latlon_or_defer,
        read_latlon,
        read_speed,
    )
    from brain import observation as _obs
    from actions import sea_actions

    goal = HugShoreGoal(
        side=side, max_ticks=max_ticks,
        endpoint_lat=endpoint_lat,
        endpoint_lon=endpoint_lon,
        goal_heading_deg=goal_heading_deg,
        driver_mode=driver_mode,
        astern_expansion=astern_expansion,
        junction_picker=junction_picker,
    )
    if endpoint_lat is not None and endpoint_lon is not None:
        logger.info(
            f"[hug_shore] §13.16.5 destination LOS guidance: "
            f"target=({endpoint_lat:.2f}, {endpoint_lon:.2f})"
        )
    if goal_heading_deg is not None:
        logger.info(
            f"[hug_shore] §13.16.3 manual goal heading: {goal_heading_deg:.0f}°"
        )
    if sea_actions.is_ship_moving() is False:
        logger.info("[hug_shore] ship stopped — calling sail_start()")
        sea_actions.sail_start()

    jsonl_fp = None
    if debug_dir is not None:
        debug_dir.mkdir(parents=True, exist_ok=True)
        jsonl_fp = open(debug_dir / "trace.jsonl", "w")
        logger.info(f"[hug_shore] DEBUG trace → {debug_dir}/")

    prev_heading: Optional[float] = None
    prev_action: str = ""
    prev_commanded_deg: float = 0.0
    consecutive_rejects: int = 0
    # §13.28 — When heading is rejected, drop anchor and wait for the
    # next accepted reading instead of sailing blind under the last
    # commanded direction.  Blind sailing through rejected ticks caused
    # the t473-t480 stutter in uturn_dest8_20260602_205841 (4 in/out
    # cycles at the Y-tip).  Tracked here so we only emit one sail_stop
    # tap per rejection burst, and one sail_start tap on recovery.
    rejection_stop_active: bool = False

    # After this many consecutive heading rejections, force-accept the
    # next reading and re-anchor the baseline.  Rationale: persistent
    # rejection means the baseline is probably stale — most likely the
    # ship legitimately rotated far (collision bounce, large physical
    # turn) and the *true* heading is whatever the detector now says.
    # If we never re-anchor, the bot is permanently blind once the
    # baseline drifts (observed live 2026-05-29: 23 consecutive
    # rejections after a collision left the bot sailing straight N).
    HEADING_REJECT_RESET = 3
    consecutive_minimap_lost: int = 0
    last_known_speed_kt: Optional[float] = None
    last_known_latlon: Optional[tuple[float, float]] = None
    # Pending-jump state for the confirmation-required lat/lon
    # acceptance gate.  Threaded tick to tick alongside
    # `last_known_latlon`; see vision/sea_hud.accept_or_defer.
    latlon_pending = _NO_PENDING
    # Ring of recently accepted (lat, lon, tick) used by the velocity-
    # extrapolation plausibility check inside `accept_or_defer`.
    # Length 4 is plenty for linear extrapolation (only last 2 are
    # consulted today); the extra slack absorbs a single rejected
    # tick without breaking the velocity estimate.
    latlon_history: list = []

    while not goal.is_complete:
        t_tick_start = time.monotonic()
        try:
            frame = capture_screen()
        except Exception as e:
            logger.warning(f"[hug_shore] capture failed: {e}")
            time.sleep(random.uniform(*tick_interval_s))
            continue
        t_after_capture = time.monotonic()

        if full_perceive_every and goal.tick_count % full_perceive_every == 0:
            try:
                from brain.perceive import perceive
                perceive(frame)
            except Exception as e:
                logger.warning(f"[hug_shore] full perceive failed: {e}")

        try:
            nav = read_navigation_view(frame)
        except Exception as e:
            logger.warning(f"[hug_shore] nav read failed: {e}")
            time.sleep(random.uniform(*tick_interval_s))
            continue
        t_after_nav = time.monotonic()

        # Mini-map-lost watchdog: if the green-ship icon vanishes for
        # multiple consecutive ticks, we're probably not on the sea HUD
        # any more (sailed into a port, dialog popped, etc).  Force a
        # full perceive() + HUD probe on the NEXT tick.  We don't try
        # to handle the situation here; just flag it for follow-up.
        ship_green_px = 0
        try:
            from vision.minimap_navigation_view import _color_masks, _clean_ship_green, MINIMAP_CROP
            crop = frame.crop(MINIMAP_CROP)
            import numpy as np_local
            green_mask = _clean_ship_green(
                _color_masks(np_local.array(crop))["ship_green"]
            )
            ship_green_px = int(green_mask.sum())
        except Exception:
            pass
        if ship_green_px < minimap_lost_threshold_px:
            consecutive_minimap_lost += 1
            if consecutive_minimap_lost == minimap_lost_consecutive_ticks:
                logger.warning(
                    f"[hug_shore] mini-map ship icon lost for "
                    f"{consecutive_minimap_lost} ticks "
                    f"(green_px={ship_green_px} < {minimap_lost_threshold_px}) "
                    f"— forcing HUD probe next tick"
                )
        else:
            consecutive_minimap_lost = 0

        # Sea-HUD readings: speed (knots) + lat/lon (world position).
        # OmniParser is ~10 s, so gate to every `hud_every` ticks.
        # Between probes we reuse the last-known values — they change
        # slowly compared to steering.  An immediate probe fires when
        # the mini-map-lost watchdog tripped (we want to know if we're
        # still on the sea HUD).
        should_hud = (
            hud_every > 0
            and (goal.tick_count % hud_every == 0
                 or consecutive_minimap_lost >= minimap_lost_consecutive_ticks)
        )
        if should_hud:
            # Preserve cache on transient failure.  read_speed /
            # read_latlon return None when the OCR can't extract a
            # confident value from an otherwise-fine frame — at
            # hug_debug_20260602_111404 t336 read_latlon returned None
            # while "8.73,32.69" was clearly readable in the saved
            # frame.  Wiping the cache on a None return cost us the
            # last good position and degraded the goal to vfh_fallback
            # at a critical moment.  Only update when we actually got a
            # value.
            try:
                _speed_now = read_speed(frame)
                if _speed_now is not None:
                    last_known_speed_kt = _speed_now
            except Exception as e:
                logger.debug(f"[hug_shore] read_speed failed: {e}")
            try:
                # Pass the cached value as a tiebreaker — `read_latlon`
                # uses it to disambiguate OCR garbles like the diamond-
                # marker case (see project_latlon_marker_occlusion.md
                # and `vision/sea_hud._parse_with_prior`).
                _latlon_now = read_latlon(frame, prev_latlon=last_known_latlon)
                # Confirmation-required acceptance gate — defers
                # implausibly-large jumps until they're confirmed by
                # a second matching read.  Without this, a stuck-bad
                # OCR can poison the cache for many ticks (observed
                # at explore_port_20260603_221458 t211-215 where lon
                # jumped 30 → 61 → 30 across 5 ticks).
                _accepted, latlon_pending = _accept_latlon_or_defer(
                    candidate=_latlon_now,
                    prev=last_known_latlon,
                    pending=latlon_pending,
                    history=latlon_history,
                    current_tick=goal.tick_count + 1,
                )
                if _accepted is not None:
                    last_known_latlon = _accepted
                    # Append to history.  Keep the ring bounded to
                    # length 4 — only the last 2 entries are consulted
                    # by linear extrapolation today.
                    latlon_history.append(
                        (_accepted[0], _accepted[1], goal.tick_count + 1)
                    )
                    if len(latlon_history) > 4:
                        del latlon_history[0]
                elif _latlon_now is not None:
                    logger.debug(
                        f"[hug_shore] latlon jump deferred: "
                        f"candidate={_latlon_now} prev={last_known_latlon} "
                        f"pending={latlon_pending}"
                    )
            except Exception as e:
                logger.debug(f"[hug_shore] read_latlon failed: {e}")
        ship_speed_kt = last_known_speed_kt
        latlon = last_known_latlon

        # ── Heading sanity check: reject implausible deltas (180° flips) ──
        # The mini-map heading detector uses a PCA + pixel-asymmetry
        # heuristic to break bow/stern symmetry — see vision/
        # minimap_navigation_view.py:_ship_heading.  It occasionally
        # flips 180° when the green-hull pixel count tips the wrong way
        # (perception noise, lighthouse beam overlap, dawn lighting).
        # When that happens, all ship-relative sector readings are
        # mirrored — the policy then makes "rational" decisions on
        # mirrored data, producing observed L↔R oscillation.
        #
        # Rule: if Δheading exceeds what's physically possible given the
        # last commanded rotation (with slop), reject the new heading
        # and dead-reckon from prev + commanded.  Tracked per session;
        # diagnostic-only override of nav.ship_heading_deg.
        raw_heading = getattr(nav, "ship_heading_deg", None) if nav else None
        cur_heading = raw_heading
        heading_rejected = False
        # Detector confidence (0..1) when available — low-confidence
        # readings (PCA fallback) get a wider plausibility window so
        # we don't reject them just because they're noisier.
        heading_confidence = getattr(nav, "heading_confidence", None) if nav else None
        heading_strategy   = getattr(nav, "heading_strategy",   None) if nav else None
        if raw_heading is not None and prev_heading is not None:
            raw_delta = ((raw_heading - prev_heading + 540) % 360) - 180
            # §13.32 — Plausibility budget with confidence-aware widening.
            #
            # The old shape (1 + (1 − conf)·1.25) widened the budget for
            # NOISY detectors and collapsed it for high-confidence ones.
            # That had the side effect of systematically rejecting
            # high-conf sail_pair readings whenever a real big rotation
            # happened (collision bounce, uncalibrated rate, stale prior
            # baseline).  Live data from
            # `explore_port_20260603_143438` t121-t527: 13 high-conf
            # rejections all confirmed as correct readings by ground
            # truth.  The detector was right; the rule was wrong.
            #
            # New shape: trust HIGH confidence MORE, not less.
            #   conf ≤ 0.6   (low/noisy)  → relax = 1.0  (tight gate)
            #   conf ≥ 0.85  (high)       → relax = 5.0  (effectively trust)
            #   0.6 < conf < 0.85         → smooth ramp 1.0 → 5.0
            #
            # The high-conf bypass also gates `wrong_direction`: a real
            # collision bounce IS a direction reversal we should accept.
            if heading_confidence is None or heading_confidence < 0.6:
                conf_relax = 1.0
            elif heading_confidence >= 0.85:
                conf_relax = 5.0
            else:
                conf_relax = (
                    1.0 + (heading_confidence - 0.6) / 0.25 * 4.0
                )
            plausible_max = max(abs(prev_commanded_deg) + 30.0, 30.0) * conf_relax
            # Direction-sign plausibility: when prior tick had a
            # meaningful command and the new reading shows substantial
            # rotation in the OPPOSITE direction, treat as suspect.
            # Gated on conf < 0.85 (§13.32) — high-confidence detectors
            # have already proven themselves, and the most common cause
            # of a direction reversal at high conf is a real collision
            # bounce or hit-shore event.
            wrong_direction = (
                (heading_confidence is None
                 or heading_confidence < 0.85)
                and abs(prev_commanded_deg) >= 15.0
                and abs(raw_delta) > 30.0
                and (raw_delta * prev_commanded_deg) < 0
            )
            if abs(raw_delta) > plausible_max or wrong_direction:
                heading_rejected = True
                consecutive_rejects += 1
                # Re-anchor escape: persistent rejection probably means
                # the baseline is stale (e.g. a real large rotation
                # we missed — collision bounce, pan-out, etc).  After
                # HEADING_REJECT_RESET in a row, force-accept the
                # current reading so the bot can keep navigating.
                if consecutive_rejects >= HEADING_REJECT_RESET:
                    logger.warning(
                        f"[hug_shore] heading-reject re-anchor: "
                        f"{consecutive_rejects} consecutive rejects → "
                        f"force-accepting raw {raw_heading:.0f}° "
                        f"(baseline was {prev_heading:.0f}°)"
                    )
                    heading_rejected = False
                    consecutive_rejects = 0
                else:
                    logger.warning(
                        f"[hug_shore] t{goal.tick_count + 1} heading REJECTED "
                        f"(#{consecutive_rejects}, strategy={heading_strategy}, "
                        f"conf={heading_confidence}): "
                        f"raw {prev_heading:.0f}°→{raw_heading:.0f}° "
                        f"(Δ{raw_delta:+.0f}°) exceeds plausible ±{plausible_max:.0f}° "
                        f"(commanded {prev_commanded_deg:+.0f}°)"
                    )
        if not heading_rejected:
            consecutive_rejects = 0
        # When rejected, don't trust the sectors either — they were
        # computed using the flipped heading, so they describe the
        # wrong world bins.  Skip the policy and issue a plain hold.

        _obs.update(nav=nav, frame_id=str(id(frame)))

        # ── Diagnostics: heading delta vs previous tick ───────────────
        if cur_heading is not None and prev_heading is not None:
            heading_delta = ((cur_heading - prev_heading + 540) % 360) - 180
        else:
            heading_delta = None

        logger.info(f"[hug_shore] sense {_fmt_sense(nav, side)}")
        logger.info(
            f"[hug_shore] DIAG t={goal.tick_count + 1}  "
            f"Δhdg={'%+.1f°' % heading_delta if heading_delta is not None else '---'}  "
            f"commanded_last={prev_commanded_deg:+.0f}°  "
            f"speed={'%.1fkt' % ship_speed_kt if ship_speed_kt is not None else '---'}  "
            f"latlon={'%.2f,%.2f' % latlon if latlon is not None else '---'}  "
            f"all_secs={_fmt_sense_all(nav)}  "
            f"capture={int((t_after_capture - t_tick_start) * 1000)}ms  "
            f"nav={int((t_after_nav - t_after_capture) * 1000)}ms"
        )

        # HUD readings into the goal so TickRecord captures lat/lon/speed
        # for Phase A signal derivation (world-bearing, median speed).
        goal.set_hud(
            lat=latlon[0] if latlon else None,
            lon=latlon[1] if latlon else None,
            speed_kt=ship_speed_kt,
            raw_heading=raw_heading,
            rejected=heading_rejected,
        )

        # Village-overlap suppression of the rejection.  When the bot
        # is inside a village's icon/label overlay AND has a prior
        # §13.21 commit to fall back to, dead-reckon on that commit
        # instead of stopping.  See `_extract_shore_line` for the
        # mirror gate on the tangent re-derive.
        #
        # IMPORTANT precondition: require an established §13.21
        # commit (segment_memory.committed_tick set).  Without one,
        # the bot has no direction to dead-reckon on — falling back
        # to "no anchor + accept bad heading" creates the oscillation
        # seen in `explore_port_20260607_151743` t1-500 where the
        # gate fired at Cairo (port marker present) and the bot
        # circled lat 27-29 unable to bootstrap a commit.
        seg_mem = getattr(goal, "_segment_memory", None)
        has_prior_commit = (seg_mem is not None
                            and getattr(seg_mem, "committed_tick", None)
                            is not None)
        in_village_overlap = (
            bool(getattr(nav, "village_overlap", False))
            and has_prior_commit
        )
        if heading_rejected and in_village_overlap:
            logger.info(
                f"[hug_shore] t{goal.tick_count + 1} village overlap — "
                f"heading rejection suppressed, DEAD-RECKONING on committed "
                f"direction"
            )
        elif heading_rejected:
            # §13.28 — Drop anchor on the first rejected tick of a burst,
            # then sit anchored until perception recovers.  Prior
            # behaviour (`action="hold"`) emitted no command but left the
            # ship sailing under its last commanded direction — see the
            # t473-t480 stutter analysis in
            # `memory/project_first_autonomous_destination_reached.md`.
            if not rejection_stop_active:
                logger.warning(
                    f"[hug_shore] t{goal.tick_count + 1} STOPPING SHIP for "
                    f"heading rejection — will resume on next accepted reading"
                )
                try:
                    sea_actions.sail_stop()
                    rejection_stop_active = True
                except Exception as e:
                    logger.warning(
                        f"[hug_shore] sail_stop failed during rejection: {e}"
                    )
        # When in village overlap, fall through to the normal action
        # path below (use the prior cached heading via baseline), so
        # the bot continues sailing.
        if heading_rejected and in_village_overlap:
            heading_rejected = False    # un-flag for downstream code
            cur_heading = prev_heading  # use prior heading (stable)
            goal.tick_count += 1
            raw_str = (f"{raw_heading:.0f}°" if raw_heading is not None
                       else "no_reading")
            prev_str = (f"{prev_heading:.0f}°" if prev_heading is not None
                        else "no_baseline")
            result = HugTickResult(
                action="stopped_for_rejection",
                phase=goal.phase,
                note=(f"heading rejected raw={raw_str} vs baseline {prev_str} "
                      f"— anchored until perception recovers"),
            )
        else:
            # §13.28 — Resume sailing after the rejection burst clears.
            if rejection_stop_active:
                logger.info(
                    f"[hug_shore] t{goal.tick_count + 1} RESUMING SHIP — "
                    f"heading accepted ({raw_heading:.0f}°)"
                )
                try:
                    sea_actions.sail_start()
                except Exception as e:
                    logger.warning(
                        f"[hug_shore] sail_start failed after rejection: {e}"
                    )
                rejection_stop_active = False
            result = goal.tick()
        t_after_decision = time.monotonic()

        logger.info(
            f"[hug_shore] tick {goal.tick_count}/{goal.max_ticks}  "
            f"phase={result.phase.name}  action={result.action}  {result.note}"
        )

        # ── Optional per-tick artefact + JSONL trace ──────────────────
        commanded_deg = _commanded_angle_from_action(result.action)
        if jsonl_fp is not None:
            try:
                crop_path = debug_dir / f"tick_{goal.tick_count:04d}.png"
                _crop_minimap(frame).save(crop_path)
            except Exception as e:
                logger.warning(f"[hug_shore] crop save failed: {e}")
                crop_path = None
            record = {
                "tick": goal.tick_count,
                "wall_iso": datetime.now().isoformat(),
                "capture_ms": int((t_after_capture - t_tick_start) * 1000),
                "nav_read_ms": int((t_after_nav - t_after_capture) * 1000),
                "decision_ms": int((t_after_decision - t_after_nav) * 1000),
                "heading_deg": cur_heading,
                "heading_deg_raw": raw_heading,
                "heading_rejected": heading_rejected,
                "heading_delta_deg": heading_delta,
                "heading_strategy": heading_strategy,
                "heading_confidence": heading_confidence,
                "speed_kt": ship_speed_kt,
                "lat":  latlon[0] if latlon is not None else None,
                "lon":  latlon[1] if latlon is not None else None,
                "commanded_deg_last": prev_commanded_deg,
                "commanded_deg_this": commanded_deg,
                "prev_action": prev_action,
                "nav": _nav_record(nav),
                "phase": result.phase.name,
                "action": result.action,
                "note": result.note,
                "rate_dps": getattr(goal, "rate_dps", None),
                "wall_distance": getattr(goal, "wall_distance", None),
                "side": getattr(goal, "side", side),
                # Endpoint of the voyage — used by the viewer to draw
                # the M-line direction.  Per-tick rather than once-at-start
                # so each trace row is self-contained for replay tools.
                "endpoint_lat": goal.endpoint_lat,
                "endpoint_lon": goal.endpoint_lon,
                # CoverageTracker — observer-only.  Replaces the
                # deprecated m_line_verdict from the previous design.
                "coverage_verdict":  goal._coverage_verdict,
                "visited_cells":     (
                    goal._coverage_tracker.visited_count
                    if goal._coverage_tracker is not None else None
                ),
                "frontier_cells":    (
                    goal._coverage_tracker.frontier_count
                    if goal._coverage_tracker is not None else None
                ),
                # FrontierPicker override state.
                "picker_target":              goal._picker_target,
                "picker_commit_remaining":    goal._picker_commit_remaining,
                "picker_invocations":         goal._picker_invocations,
                "alt_costs": getattr(goal, "_last_alt_costs", None),
                "alt_best":  getattr(goal, "_last_alt_best", None),
                # Phase 1 Lyapunov logging (read-only, no behaviour effect).
                # See docs/shore_following_design.md §12.
                "lyapunov":  getattr(goal, "_last_lyap", None),
                "crop": crop_path.name if crop_path else None,
            }
            jsonl_fp.write(json.dumps(record) + "\n")
            jsonl_fp.flush()

        # Baseline maintenance:
        #
        # Accepted reading → snap prev_heading to the new reading.
        #
        # Rejected reading → DEAD-RECKON prev_heading forward by the
        # prior commanded rotation.  Without this, the baseline stays
        # frozen across the rejection window and any later reading is
        # measured against a stale frame.  Live t=32 of 2026-05-29_205124:
        # detector correctly returned 57° (real bow) but baseline was
        # 119° (stuck since t=30) → delta 62° > plausible 30° → second
        # rejection cascading from the first.  Dead-reckoning would have
        # advanced baseline from 119° + (-45°) = 74° → t=32 delta would
        # have been |57 − 74| = 17°, well under plausible, accepted.
        #
        # Also: do NOT zero prev_commanded_deg when we hold (was the
        # other half of the cascade — plausible_max collapsed from 75°
        # to 30° on the hold tick).  Preserve the magnitude of the most
        # recent NON-HOLD command so the plausibility budget stays sane
        # while we're holding through a rejection.
        if not heading_rejected:
            prev_heading = cur_heading
        else:
            if prev_heading is not None:
                prev_heading = (prev_heading + prev_commanded_deg) % 360
        prev_action = result.action
        # Preserve magnitude across holds: a hold has commanded_deg=0,
        # which would collapse the plausibility budget.  Only update
        # prev_commanded_deg when a real rotation was issued.
        if commanded_deg != 0.0:
            prev_commanded_deg = commanded_deg

        sleep_s = result.delay or random.uniform(*tick_interval_s)
        time.sleep(sleep_s)

    if jsonl_fp is not None:
        jsonl_fp.close()

    # Stop the ship before exiting.  When phase=COMPLETE was triggered by
    # destination arrival (§13.17 / POINT_PURSUIT_ACCEPTANCE_DEG), the bot
    # may still be moving — drop anchor to avoid drifting past target.
    # For max-ticks termination we also want a clean stop.
    if sea_actions.is_ship_moving() is True:
        logger.info(
            f"[hug_shore] voyage complete (phase={goal.phase.name}) — "
            "calling sail_stop()"
        )
        try:
            sea_actions.sail_stop()
        except Exception as e:
            logger.warning(f"[hug_shore] sail_stop failed: {e}")

    return goal


_HOLD_NOTE_RE = None


def _commanded_angle_from_action(action: str) -> float:
    """Parse "hold_left:NNNms(~AA°@RR°/s)" / "hold_right:..." to extract
    the commanded rotation in degrees (signed: + = right, - = left).
    Returns 0.0 for "hold", "wait", "stop", or unparseable strings."""
    global _HOLD_NOTE_RE
    if _HOLD_NOTE_RE is None:
        import re
        _HOLD_NOTE_RE = re.compile(r"^hold_(left|right):\d+ms\(~(\d+(?:\.\d+)?)°")
    if not action:
        return 0.0
    m = _HOLD_NOTE_RE.match(action)
    if not m:
        return 0.0
    sign = -1.0 if m.group(1) == "left" else +1.0
    return sign * float(m.group(2))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", choices=("port", "starboard"), required=True,
                    help="which side to keep the shore on")
    ap.add_argument("--max-ticks", type=int, default=120,
                    help="ticks before the goal auto-completes")
    ap.add_argument("--interval", type=float, nargs=2, default=(0.15, 0.40),
                    metavar=("LOW", "HIGH"),
                    help="random sleep range between ticks (seconds)")
    ap.add_argument("--full-perceive-every", type=int, default=0,
                    help="run full perceive() every N ticks (0 = never)")
    ap.add_argument("--hud-every", type=int, default=5,
                    help="OmniParser-backed HUD reads every N ticks "
                         "(0 = never; default 5)")
    ap.add_argument("--debug-dir", type=Path, default=None,
                    help="dir to dump per-tick mini-map crops + JSONL trace "
                         "(for noise/steering diagnostics)")
    ap.add_argument("--driver-mode", choices=HUG_SHORE_DRIVER_MODES,
                    default=None,
                    help="steering driver: vfh | lyapunov | lyapunov_safe | "
                         "point_pursuit (§13.17 Lyapunov + canonical VFH+).  "
                         "Default: env UWO_HUG_SHORE_DRIVER or 'vfh'")
    ap.add_argument("--endpoint-lat", type=float, default=None,
                    help="endpoint latitude (game coords) — the mission "
                         "target.  Used for arrival check and initial "
                         "hug-side bias; not used as a per-tick steering "
                         "target")
    ap.add_argument("--endpoint-lon", type=float, default=None,
                    help="endpoint longitude (game coords)")
    ap.add_argument("--goal-heading-deg", type=float, default=None,
                    help="explicit goal compass bearing (overrides LOS/inferred)")
    ap.add_argument("--junction-picker", type=str, default="none",
                    choices=("none", "yamauchi", "tremaux"),
                    help="Frontier picker backend.  'none' (default) "
                         "= no override; 'yamauchi' = nearest-frontier "
                         "scoring; 'tremaux' = Trémaux DFS on perceived "
                         "junctions with Yamauchi fallback (Phase 3).  "
                         "See docs/destination_generator_design.md")
    ap.add_argument("--astern-expansion", action="store_true",
                    help="enable §13.26 astern-sector expansion at wp_rel_bow>90° "
                         "(diagnostic only; off by default — see §13.26 docstring "
                         "for the bend-oscillation rationale)")
    args = ap.parse_args()

    goal = run_hug_shore_loop(
        side=args.side,
        max_ticks=args.max_ticks,
        tick_interval_s=tuple(args.interval),
        full_perceive_every=args.full_perceive_every,
        hud_every=args.hud_every,
        debug_dir=args.debug_dir,
        driver_mode=args.driver_mode,
        endpoint_lat=args.endpoint_lat,
        endpoint_lon=args.endpoint_lon,
        goal_heading_deg=args.goal_heading_deg,
        astern_expansion=args.astern_expansion,
        junction_picker=args.junction_picker,
    )
    logger.info(
        f"[hug_shore] DONE.  phase={goal.phase.name}  ticks={goal.tick_count}  "
        f"last_steer={goal.last_steer}"
    )
