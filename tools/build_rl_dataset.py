"""Phase 1 of the learned navigation controller plan.

Converts saved session directories (`data/sessions/*/trace.jsonl`
+ `tick_NNNN.png`) into an RL-ready offline dataset.

Output format (NPZ) — v2 schema (see
`docs/learned_navigation_controller_v2_reframe.md`):

  Per-tick image + aux + label:
    - frame_paths       str[N]   Relative paths to tick_NNNN.png files
    - heading_sin       f32[N]   sin(heading_deg)
    - heading_cos       f32[N]   cos(heading_deg)
    - dlat_5tick        f32[N]   Cleaned lat[i] - cleaned lat[i-5] (per episode)
    - dlon_5tick        f32[N]   Cleaned lon[i] - cleaned lon[i-5] (per episode)
    - is_channel        f32[N]   Topology one-hot (3 floats sum to ≤1)
    - is_junction       f32[N]
    - is_dead_end       f32[N]
    - action_idx        i8 [N]   0=HOLD, 1=L_SHORT, 2=L_MED, 3=L_LONG,
                                   4=R_SHORT, 5=R_MED, 6=R_LONG

  Quality / metadata for training-time filtering:
    - heading_quality   str[N]   "clean"|"uncertain"|"miss"
                                   uncertain = antipode tiebreaker fired
    - latlon_quality    str[N]   "clean"|"spike"|"extended_bad"|"missing"
    - roi               str[N]   "channel"|"cairo_outflow"|"nubia_village"|
                                   "the_bend"|"y_tip"|"barri_village"|"lake_end"
    - valid             bool[N]  Tiered filter: channel ticks need both
                                   quality flags clean; rare-ROI ticks
                                   survive uncertain readings.
    - speed_kt          f32[N]   NaN when not in trace
    - lat / lon         f32[N]
    - hug_side          i8 [N]   0 = port
    - topology          str[N]   Raw topology label (for diagnostics)
    - heading_source    str[N]   Raw source string (for diagnostics)

  Reward channels (combined into `reward`):
    - reward            f32[N]
    - r_smoothness, r_progress, r_collision, r_stuck, r_per_tick   f32[N]
    - r_verdict         f32[N]   Action verdict (ideal/acceptable/wrong)
    - r_state           f32[N]   State verdict (good/bad) + hindsight
                                   propagation (γ=0.9, window=10)

  Human labels (sidecar `labels.jsonl` per session):
    - verdict           str[N]   "ideal"|"acceptable"|"wrong"|""
    - state_verdict     str[N]   "good"|"bad"|""
    - corrected         bool[N]
    - original_action_idx i8[N]

  Episode bookkeeping + telemetry:
    - terminal          bool[N]
    - episode_id        i32[N]
    - is_bounce, is_hard_turn  bool[N]

DROPPED in v2 vs v1: `desired_sin`, `desired_cos`, `delta_norm` — the
controller is no longer goal-conditioned (see reframe doc §reframed
training sample).

Usage
─────
  python -m tools.build_rl_dataset \\
      --sessions data/sessions \\
      --out data/rl/dataset_v1.npz \\
      --glob 'ai_nav_*' 'hug_debug_*' 'reference_nile_*'
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── Action space ─────────────────────────────────────────────────────

HOLD       = 0
LEFT_SHORT = 1; LEFT_MED  = 2; LEFT_LONG  = 3
RIGHT_SHORT= 4; RIGHT_MED = 5; RIGHT_LONG = 6

ACTION_NAMES = {
    HOLD: "HOLD",
    LEFT_SHORT: "L_SHORT", LEFT_MED: "L_MED", LEFT_LONG: "L_LONG",
    RIGHT_SHORT: "R_SHORT", RIGHT_MED: "R_MED", RIGHT_LONG: "R_LONG",
}


def action_to_idx(cmd: str | None, hold_ms: int | None) -> int:
    """Map (planner command, hold duration) → discrete action index."""
    if not cmd or cmd == "noop" or not hold_ms:
        return HOLD
    base = LEFT_SHORT if cmd == "hold_left" else RIGHT_SHORT
    if hold_ms < 300:
        return base                    # SHORT
    elif hold_ms < 600:
        return base + 1                # MEDIUM
    else:
        return base + 2                # LONG


# ── Reward shape (see docs/learned_navigation_controller_plan.md §reward) ─

@dataclass
class RewardComponents:
    smoothness: float = 0.0
    progress: float = 0.0
    collision: float = 0.0
    stuck: float = 0.0
    per_tick: float = -0.001
    verdict: float = 0.0      # bonus/penalty from human action label
    state: float = 0.0        # bonus/penalty from human state label

    def total(self) -> float:
        return (self.smoothness + self.progress + self.collision
                + self.stuck + self.per_tick + self.verdict + self.state)


# Verdict → reward magnitude.  Tunable but starts simple.  Used in
# both BC weighting and any IQL-style reward training.
VERDICT_REWARD = {
    "ideal":      +0.5,
    "acceptable": +0.1,
    "wrong":      -1.0,
}

# State verdict — independent dimension from action verdict.  The bot
# can take a *correct* action from a *bad* state (e.g. escape steering
# while facing a bank).  We tag the bad STATE so the model learns to
# avoid the trajectories that lead into it, not just the action taken
# inside it.  Magnitudes are smaller than the action-verdict bonus so
# state shaping nudges rather than dominates.
STATE_VERDICT_REWARD = {
    "good": +0.2,
    "bad":  -0.5,
}

# Hindsight credit propagation for bad states: the bad reward at tick T
# is also charged (with γ-decay) to ticks T-1..T-N inside the same
# episode, so the model learns that the action choices that *led* to
# the bad state were also undesirable.  Set to 0 to disable.
HINDSIGHT_BAD_STATE_WINDOW = 10
HINDSIGHT_BAD_STATE_GAMMA  = 0.9


def compute_reward(
    prev_action: int, action: int,
    speed: float | None, prev_speed: float | None,
    dlat: float, dlon: float,
    is_stuck: bool,
) -> RewardComponents:
    r = RewardComponents()

    # Smoothness — penalize action thrashing
    r.smoothness = -0.01 * abs(action - prev_action)

    # Progress — reward any motion on the map.  Goal-free (v2): there
    # is no `desired_deg` to align with, so any displacement counts.
    # `dlat`/`dlon` are per-MOTION_WINDOW degrees (~5 ticks); a typical
    # cruising tick at 12 kt is ~0.05° lat over 5 ticks ⇒ motion≈0.05
    # ⇒ progress≈0.1 (the historic cap).
    motion = math.hypot(dlat, dlon)
    r.progress = min(0.1, motion * 2.0)

    # Collision — speed drop > 30% with prev speed having been "cruising"
    if (speed is not None and prev_speed is not None
            and prev_speed > 5.0):  # was cruising
        drop = (prev_speed - speed) / prev_speed
        if drop > 0.30:
            r.collision = -1.0

    # Stuck — sustained low speed (caller flags `is_stuck`)
    if is_stuck:
        r.stuck = -0.5

    return r


# ── Bounce / hard-turn detection (Phase 7 telemetry) ──────────────────

def detect_bounce_or_hard_turn(
    prev_delta: float, curr_delta: float,
    prev_speed: float | None, curr_speed: float | None,
    max_speed_seen: float,
) -> tuple[bool, bool]:
    delta_jump = abs(curr_delta - prev_delta)
    speed_drop = 0.0
    if prev_speed is not None and curr_speed is not None and prev_speed > 0:
        speed_drop = (prev_speed - curr_speed) / prev_speed

    is_bounce = (
        delta_jump > 30
        and speed_drop > 0.4
        and (prev_speed or 0.0) > 0.5 * max_speed_seen
    )
    is_hard_turn = (
        abs(curr_delta) > 60
        and abs(curr_delta) < abs(prev_delta) + 5
    )
    return is_bounce, is_hard_turn


# ── Quality + ROI helpers (v2) ────────────────────────────────────────

ROIS_PATH = ROOT / "data" / "reference" / "training_rois.json"
MOTION_WINDOW = 5           # ticks to look back for dlat/dlon
SPIKE_K_MAD = 4.0           # MAD multiples that defines a "spike"
SPIKE_LOCAL_WINDOW = 5      # ticks each side for median+MAD
EXTENDED_BAD_RUN = 3        # ≥ this many consecutive non-clean ⇒ extended_bad
EXTENDED_BAD_MARGIN = 3     # extra ticks each side flagged as extended_bad

# Heading source → quality.  `template_match` (and the legacy `pca_legacy`)
# are clean.  Anything tagged `antipode_*` means the matcher's antipodal
# tiebreaker fired — readings can be silently inverted (the t246–t283
# Nile flip).  `*_miss` means no ship found at all.
TRUSTED_HEADING_SOURCES = {"template_match", "pca_legacy"}

def heading_quality_from_source(src: str) -> str:
    if not src:
        return "miss"
    if src in TRUSTED_HEADING_SOURCES:
        return "clean"
    if "miss" in src:
        return "miss"
    return "uncertain"


def topology_onehot(topo: str | None) -> tuple[float, float, float]:
    """Returns (is_channel, is_junction, is_dead_end).  Unknown ⇒ all zero."""
    t = (topo or "").lower()
    return (
        1.0 if t == "channel"  else 0.0,
        1.0 if t == "junction" else 0.0,
        1.0 if t == "dead_end" else 0.0,
    )


def load_rois(path: Path = ROIS_PATH) -> list[tuple]:
    """Returns list of (name, lat_min, lat_max, lon_min, lon_max, area)
    sorted by area ascending so the smaller-area ROI wins at lookup."""
    if not path.exists():
        print(f"  WARN: {path} missing — all ticks will be roi='channel'")
        return []
    raw = json.loads(path.read_text())
    rois = []
    for name, box in raw.items():
        if name.startswith("_"):
            continue
        lat_min, lat_max = box["lat"]
        lon_min, lon_max = box["lon"]
        area = (lat_max - lat_min) * (lon_max - lon_min)
        rois.append((name, lat_min, lat_max, lon_min, lon_max, area))
    rois.sort(key=lambda x: x[5])
    return rois


def lookup_roi(lat: float | None, lon: float | None, rois: list) -> str:
    if lat is None or lon is None:
        return "channel"
    for name, lat_min, lat_max, lon_min, lon_max, _ in rois:
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            return name
    return "channel"


def smooth_motion_aux(
    lats: list[float | None],
    lons: list[float | None],
) -> tuple[list[float], list[float], list[str]]:
    """Per-episode motion + lat/lon quality.

    Steps:
      1. Mark ticks with no lat/lon as `missing`.
      2. For each remaining tick, compare against the median of its
         ±SPIKE_LOCAL_WINDOW neighbors using MAD scaling.  Beyond
         k=SPIKE_K_MAD MAD ⇒ `spike`; replace value with local median.
      3. Promote runs of EXTENDED_BAD_RUN+ consecutive non-clean ticks
         (plus EXTENDED_BAD_MARGIN ticks on each side) to `extended_bad`.
      4. Compute dlat_K, dlon_K from the cleaned series.
    """
    n = len(lats)
    quality = ["missing" if v is None else "unknown" for v in lats]
    clean_lat = list(lats)
    clean_lon = list(lons)

    # Pass 1: spike detection on lat AND lon
    for i in range(n):
        if quality[i] != "unknown":
            continue
        win = [j for j in range(max(0, i - SPIKE_LOCAL_WINDOW),
                                min(n, i + SPIKE_LOCAL_WINDOW + 1))
               if j != i and quality[j] == "unknown"]
        if len(win) < 3:
            quality[i] = "clean"
            continue
        win_lats = sorted(lats[j] for j in win)
        win_lons = sorted(lons[j] for j in win)
        med_lat = win_lats[len(win_lats) // 2]
        med_lon = win_lons[len(win_lons) // 2]
        mad_lat = sorted(abs(v - med_lat) for v in win_lats)[len(win_lats) // 2]
        mad_lon = sorted(abs(v - med_lon) for v in win_lons)[len(win_lons) // 2]
        # Floor MAD so a perfectly steady stretch doesn't reject every
        # tick at the first millidegree of motion.
        mad_lat = max(mad_lat, 0.01)
        mad_lon = max(mad_lon, 0.01)
        if (abs(lats[i] - med_lat) > SPIKE_K_MAD * mad_lat or
                abs(lons[i] - med_lon) > SPIKE_K_MAD * mad_lon):
            quality[i] = "spike"
            clean_lat[i] = med_lat
            clean_lon[i] = med_lon
        else:
            quality[i] = "clean"

    # Pass 2: extended_bad runs.  Any run of ≥ EXTENDED_BAD_RUN
    # spike/missing ticks promotes the whole run + margin to
    # extended_bad so the trainer skips both the run and its
    # boundary ticks (perception transitions are noisy).
    i = 0
    while i < n:
        if quality[i] in ("spike", "missing"):
            run_start = i
            while i < n and quality[i] in ("spike", "missing"):
                i += 1
            run_len = i - run_start
            if run_len >= EXTENDED_BAD_RUN:
                lo = max(0, run_start - EXTENDED_BAD_MARGIN)
                hi = min(n, i + EXTENDED_BAD_MARGIN)
                for j in range(lo, hi):
                    if quality[j] != "clean":
                        quality[j] = "extended_bad"
        else:
            i += 1

    # Pass 3: compute dlat, dlon from cleaned series.  Zero for early
    # ticks where no prior reference exists.
    dlat = [0.0] * n
    dlon = [0.0] * n
    for i in range(n):
        ref = i - MOTION_WINDOW
        if ref < 0 or clean_lat[i] is None or clean_lat[ref] is None:
            continue
        dlat[i] = float(clean_lat[i] - clean_lat[ref])
        dlon[i] = float(clean_lon[i] - clean_lon[ref])
    return dlat, dlon, quality


# ── Heading self-healing via motion cross-check ───────────────────────
#
# The template_match heading detector has a 180° symmetry; when its
# antipodal tiebreaker fires (source contains "antipode_*"), the
# *mechanism* was triggered but the picked direction is usually still
# correct.  Empirical proof in
# `data/sessions/ai_nav_2026-06-24T20-49-36`: of 624 ticks tagged
# "uncertain" by the source-string rule, only ~50 actually showed a
# >90° disagreement with lat/lon-derived motion bearing.
#
# This cross-check promotes heading_quality from "uncertain" → "clean"
# when motion confirms the heading, and demotes it to "flipped" when
# the disagreement is >90° (the antipode-lock signature seen at
# t246-t283 and t759).

CROSS_CHECK_AGREE_DEG  = 30.0   # within this ⇒ motion confirms heading
CROSS_CHECK_FLIP_DEG   = 90.0   # beyond this ⇒ heading is antipode-locked
CROSS_CHECK_MOTION_MIN = 0.02   # min |motion| in degrees-per-window
                                # to bother computing the bearing
# Upper bound on plausible motion per 5-tick window.  Empirically,
# the Cairo→Lake Victoria voyage cruised at ~0.3-0.5°/window.  Anything
# above ~1.0 is OCR garbage that survived the spike filter (happens
# when bad readings persist long enough for the local median to drift).
# Without this cap, the t853-t856 case (mot_mag = 4-6) produced
# nonsense motion bearings and false-flipped a perfectly good heading.
CROSS_CHECK_MOTION_MAX = 1.0


def cross_check_heading(
    heading_deg: float,
    dlat: float,
    dlon: float,
    latlon_quality_clean: bool,
) -> str | None:
    """Compare heading against lat/lon-derived motion bearing.

    Returns one of:
      None       — no decision (motion outside trusted range, or lat/lon
                   not clean).  Caller keeps the existing quality flag.
      "clean"    — within CROSS_CHECK_AGREE_DEG: heading confirmed
      "uncertain" — borderline disagreement (between agree and flip)
      "flipped"  — >CROSS_CHECK_FLIP_DEG: antipode-locked, drop the reading
    """
    if not latlon_quality_clean:
        return None  # don't validate heading with bad lat/lon
    motion = math.hypot(dlat, dlon)
    if motion < CROSS_CHECK_MOTION_MIN or motion > CROSS_CHECK_MOTION_MAX:
        return None  # too slow or implausibly large (likely OCR garbage)
    motion_brg = math.degrees(math.atan2(dlon, dlat)) % 360.0
    diff = abs(((heading_deg - motion_brg + 540.0) % 360.0) - 180.0)
    if diff < CROSS_CHECK_AGREE_DEG:
        return "clean"
    if diff > CROSS_CHECK_FLIP_DEG:
        return "flipped"
    return "uncertain"


# Rare ROIs where we keep uncertain ticks (perception-stress examples
# we can't afford to throw away).  Channel ticks get the strict filter.
RARE_ROIS = {"nubia_village", "barri_village", "the_bend",
             "y_tip", "lake_end", "cairo_outflow"}


def tier_valid(roi: str, hq: str, lq: str) -> bool:
    """Tiered validity rule.

    - extended_bad lat/lon or missing/flipped heading => always drop.
    - rare ROIs: keep even when uncertain (training-data scarcity wins).
    - channel ROI: require both heading_quality and latlon_quality clean.
    """
    if lq in ("extended_bad", "missing"):
        return False
    if hq in ("miss", "flipped"):
        return False
    if roi in RARE_ROIS:
        return True
    return hq == "clean" and lq == "clean"


# ── Session loading ───────────────────────────────────────────────────

@dataclass
class TickRecord:
    frame_path: str
    heading_deg: float
    commit_deg: float
    action_idx: int             # corrected if a correction exists
    original_action_idx: int    # what was actually fired live
    speed_kt: float | None
    lat: float | None
    lon: float | None
    verdict: str | None         # "ideal" | "acceptable" | "wrong" | None
    state_verdict: str | None   # "good" | "bad" | None
    corrected: bool             # True when label overrode the action
    topology: str               # "channel" | "junction" | "dead_end" | ""
    heading_source: str         # raw source string for quality derivation


# Map ACTION_NAMES back to indices for label parsing.
_NAME_TO_IDX = {v: k for k, v in ACTION_NAMES.items()}


def load_session(session_dir: Path) -> list[TickRecord]:
    trace_path = session_dir / "trace.jsonl"
    if not trace_path.exists():
        return []

    # Optional sidecar with viewer annotations.  Schema per line:
    #   {"tick": N, "verdict": "ideal|acceptable|wrong",
    #    "corrected_action": "HOLD|L_SHORT|...", "wall_iso": "..."}
    labels: dict[int, dict] = {}
    labels_path = session_dir / "labels.jsonl"
    if labels_path.exists():
        for line in labels_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                labels[int(d["tick"])] = d
            except (json.JSONDecodeError, KeyError, ValueError):
                pass

    recs = []
    for line in trace_path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        tick = r.get("tick")
        hdg = r.get("heading_deg")
        cmt = r.get("commit_deg")
        if tick is None or hdg is None or cmt is None:
            continue
        fp = session_dir / f"tick_{tick:04d}.png"
        if not fp.exists():
            continue
        original_action = action_to_idx(r.get("action"), r.get("hold_ms"))
        action = original_action
        verdict = None
        state_verdict = None
        corrected = False
        lbl = labels.get(tick)
        if lbl:
            verdict = lbl.get("verdict")
            state_verdict = lbl.get("state_verdict")
            corrected_name = lbl.get("corrected_action")
            if corrected_name and corrected_name in _NAME_TO_IDX:
                action = _NAME_TO_IDX[corrected_name]
                corrected = True
        try:
            rel = fp.resolve().relative_to(ROOT)
        except ValueError:
            rel = fp
        recs.append(TickRecord(
            frame_path=str(rel),
            heading_deg=float(hdg),
            commit_deg=float(cmt),
            action_idx=action,
            original_action_idx=original_action,
            speed_kt=(float(r["speed_kt"])
                     if r.get("speed_kt") is not None else None),
            lat=(float(r["lat"]) if r.get("lat") is not None else None),
            lon=(float(r["lon"]) if r.get("lon") is not None else None),
            verdict=verdict,
            state_verdict=state_verdict,
            corrected=corrected,
            topology=str(r.get("topology") or ""),
            heading_source=str(r.get("heading_source") or ""),
        ))
    return recs


# ── Build ─────────────────────────────────────────────────────────────

def signed_delta(target_deg: float, current_deg: float) -> float:
    """Signed shortest-arc delta from current to target, in (-180, 180]."""
    return (target_deg - current_deg + 540.0) % 360.0 - 180.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=Path, default=ROOT / "data" / "sessions")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--glob", nargs="+",
                   default=["ai_nav_*", "hug_debug_*",
                           "reference_nile_*"])
    ap.add_argument("--stuck-ticks", type=int, default=5,
                   help="Consecutive low-speed ticks to flag as stuck.")
    ap.add_argument("--stuck-speed-frac", type=float, default=0.2,
                   help="Speed below this × max_seen counts as low-speed.")
    args = ap.parse_args()

    session_dirs = sorted(
        d for pat in args.glob for d in args.sessions.glob(pat)
        if d.is_dir() and (d / "trace.jsonl").exists()
    )
    print(f"Found {len(session_dirs)} candidate sessions")

    all_records: list[TickRecord] = []
    episode_ids: list[int] = []
    # Track which session dir each episode_id corresponds to, so we can
    # drop per-session quality sidecars later for the review tool.
    sess_dir_by_ep: dict[int, Path] = {}
    for sess_idx, sess_dir in enumerate(session_dirs):
        sess_dir_by_ep[sess_idx] = sess_dir
        recs = load_session(sess_dir)
        if not recs:
            print(f"  skip {sess_dir.name}: no usable ticks")
            continue
        all_records.extend(recs)
        episode_ids.extend([sess_idx] * len(recs))
        print(f"  {sess_dir.name}: {len(recs)} ticks")

    N = len(all_records)
    if N == 0:
        raise SystemExit("no records loaded")
    print(f"\nTotal: {N} ticks from {len(set(episode_ids))} sessions")

    # Per-session max speed (for stuck detection scaling).
    sess_max_speeds = {}
    for ep, r in zip(episode_ids, all_records):
        if r.speed_kt is not None:
            sess_max_speeds[ep] = max(sess_max_speeds.get(ep, 0.0), r.speed_kt)

    # Load ROI definitions once.
    rois = load_rois()
    print(f"Loaded {len(rois)} ROIs from {ROIS_PATH.name}")

    # Build arrays.
    frame_paths = np.array([r.frame_path for r in all_records], dtype=object)
    heading = np.array([r.heading_deg for r in all_records], dtype=np.float32)
    speed = np.array(
        [r.speed_kt if r.speed_kt is not None else np.nan
         for r in all_records], dtype=np.float32,
    )
    lat = np.array(
        [r.lat if r.lat is not None else np.nan for r in all_records],
        dtype=np.float32,
    )
    lon = np.array(
        [r.lon if r.lon is not None else np.nan for r in all_records],
        dtype=np.float32,
    )
    action_idx = np.array([r.action_idx for r in all_records], dtype=np.int8)
    ep_ids = np.array(episode_ids, dtype=np.int32)
    hug_side = np.zeros(N, dtype=np.int8)   # all "port" for now

    heading_sin = np.sin(np.deg2rad(heading))
    heading_cos = np.cos(np.deg2rad(heading))

    # ── v2: motion aux + per-tick quality (computed PER EPISODE) ───────
    # lat/lon time-series filtering and dlat/dlon computation must not
    # cross episode boundaries.  Walk the records episode by episode.
    dlat_5tick = np.zeros(N, dtype=np.float32)
    dlon_5tick = np.zeros(N, dtype=np.float32)
    latlon_quality = np.empty(N, dtype=object)
    heading_quality = np.empty(N, dtype=object)
    roi_arr        = np.empty(N, dtype=object)
    topology_arr   = np.empty(N, dtype=object)
    heading_source_arr = np.empty(N, dtype=object)
    is_channel  = np.zeros(N, dtype=np.float32)
    is_junction = np.zeros(N, dtype=np.float32)
    is_dead_end = np.zeros(N, dtype=np.float32)
    valid       = np.zeros(N, dtype=bool)

    # Group indices by episode while preserving order.
    ep_start_indices = {}
    for i, ep in enumerate(episode_ids):
        ep_start_indices.setdefault(ep, []).append(i)

    for ep, indices in ep_start_indices.items():
        ep_lats = [all_records[i].lat for i in indices]
        ep_lons = [all_records[i].lon for i in indices]
        dlat, dlon, qual = smooth_motion_aux(ep_lats, ep_lons)
        for k, i in enumerate(indices):
            dlat_5tick[i] = dlat[k]
            dlon_5tick[i] = dlon[k]
            latlon_quality[i] = qual[k]

    # Per-tick scalars that don't need episode windows.
    for i, rec in enumerate(all_records):
        heading_quality[i]    = heading_quality_from_source(rec.heading_source)
        heading_source_arr[i] = rec.heading_source
        topology_arr[i]       = rec.topology
        c, j, d               = topology_onehot(rec.topology)
        is_channel[i]  = c
        is_junction[i] = j
        is_dead_end[i] = d
        roi_arr[i]     = lookup_roi(rec.lat, rec.lon, rois)

    # Lat/lon implausible-motion demotion.  The single-tick MAD spike
    # filter inside smooth_motion_aux misses multi-tick OCR errors
    # (when 4+ consecutive ticks share the same wrong drift, the local
    # median tracks the garbage).  The motion magnitude is a different
    # signal: |dlat/dlon over 5 ticks| exceeding CROSS_CHECK_MOTION_MAX
    # is physically impossible.  Demote those ticks to extended_bad so
    # tier_valid drops them (and the trainer never sees their bogus
    # dlat/dlon aux values).  Example caught: t853-t856 had motion_mag
    # of 4-6° while typical cruise is 0.3-0.5°.
    n_demoted_latlon = 0
    for i in range(N):
        if latlon_quality[i] != "clean":
            continue
        if math.hypot(float(dlat_5tick[i]), float(dlon_5tick[i])) > CROSS_CHECK_MOTION_MAX:
            latlon_quality[i] = "extended_bad"
            n_demoted_latlon += 1
    if n_demoted_latlon:
        print(f"Lat/lon implausible-motion demote: {n_demoted_latlon} "
              f"clean→extended_bad (|motion|>{CROSS_CHECK_MOTION_MAX}°/window)")

    # Heading self-healing pass — two stages.
    #
    # Stage 1: pass-by-pass cross-check.  Each tick that was "uncertain"
    # by the source-string rule gets a per-tick verdict from
    # cross_check_heading: clean | uncertain | flipped.
    #
    # Stage 2: require a minimum run length to confirm a "flipped"
    # verdict.  Empirical reason (Cairo→Lake voyage):
    #   - Real antipode locks last many ticks (the matcher's tiebreaker
    #     re-picks the wrong side every frame).  Examples: t247-t261
    #     (15), t729-t765 (37).
    #   - "Ship-side bias" — only half the ship sprite was visible, so
    #     the heading is offset ~90-115° but the matcher self-corrects
    #     next tick.  Singletons.
    # Angular-diff alone can't separate these (both hit ~100°).
    # Requiring ≥ MIN_FLIPPED_RUN consecutive flipped ticks (Stage 2)
    # does the separation cleanly.
    MIN_FLIPPED_RUN = 2

    # Stage 1: per-tick provisional verdicts (clean/uncertain/flipped).
    provisional = list(heading_quality)
    for i, rec in enumerate(all_records):
        if heading_quality[i] != "uncertain":
            continue
        verdict = cross_check_heading(
            rec.heading_deg,
            float(dlat_5tick[i]),
            float(dlon_5tick[i]),
            latlon_quality_clean=(latlon_quality[i] == "clean"),
        )
        if verdict is not None:
            provisional[i] = verdict

    # Stage 2: demote isolated "flipped" verdicts back to "uncertain"
    # if they don't form a run of ≥ MIN_FLIPPED_RUN within the same
    # episode.  Promotions to "clean" don't need run-length filtering.
    n_promoted = sum(1 for q in provisional if q == "clean") - \
                 sum(1 for q in heading_quality if q == "clean")
    n_demoted_singletons = 0
    n_demoted_confirmed = 0
    for i in range(N):
        if provisional[i] != "flipped":
            heading_quality[i] = provisional[i]
            continue
        # Count consecutive flipped neighbors in the same episode.
        run = 1
        j = i - 1
        while (j >= 0 and provisional[j] == "flipped"
               and ep_ids[j] == ep_ids[i]):
            run += 1; j -= 1
        j = i + 1
        while (j < N and provisional[j] == "flipped"
               and ep_ids[j] == ep_ids[i]):
            run += 1; j += 1
        if run >= MIN_FLIPPED_RUN:
            heading_quality[i] = "flipped"
            n_demoted_confirmed += 1
        else:
            heading_quality[i] = "uncertain"
            n_demoted_singletons += 1

    print(f"Heading cross-check: promoted {n_promoted} uncertain→clean, "
          f"confirmed {n_demoted_confirmed} flipped (run≥{MIN_FLIPPED_RUN}), "
          f"kept {n_demoted_singletons} singleton-flip as uncertain")

    # Now compute the valid mask with the healed heading_quality.
    for i in range(N):
        valid[i] = tier_valid(roi_arr[i], heading_quality[i], latlon_quality[i])

    # Compute rewards + per-tick flags (depend on prev tick within episode).
    r_smooth = np.zeros(N, dtype=np.float32)
    r_progress = np.zeros(N, dtype=np.float32)
    r_collision = np.zeros(N, dtype=np.float32)
    r_stuck = np.zeros(N, dtype=np.float32)
    r_per_tick = np.full(N, -0.001, dtype=np.float32)
    r_verdict = np.zeros(N, dtype=np.float32)
    r_state = np.zeros(N, dtype=np.float32)
    is_bounce = np.zeros(N, dtype=bool)
    is_hard_turn = np.zeros(N, dtype=bool)
    terminal = np.zeros(N, dtype=bool)
    verdict_arr = np.array(
        [r.verdict if r.verdict else "" for r in all_records], dtype=object,
    )
    state_verdict_arr = np.array(
        [r.state_verdict if r.state_verdict else "" for r in all_records],
        dtype=object,
    )
    corrected_arr = np.array(
        [r.corrected for r in all_records], dtype=bool,
    )
    original_action_idx = np.array(
        [r.original_action_idx for r in all_records], dtype=np.int8,
    )

    low_speed_run = 0
    prev_speed_local = None
    for i in range(N):
        # Detect episode boundary; reset.
        new_ep = (i == 0 or ep_ids[i] != ep_ids[i - 1])
        if new_ep:
            low_speed_run = 0
            prev_speed_local = None
            prev_a = action_idx[i]
        else:
            prev_a = action_idx[i - 1]

        curr_sp = speed[i] if not np.isnan(speed[i]) else None
        prev_sp = prev_speed_local if not new_ep else None

        ep_max = sess_max_speeds.get(int(ep_ids[i]), 0.0) or 1.0
        if curr_sp is not None and curr_sp < args.stuck_speed_frac * ep_max:
            low_speed_run += 1
        else:
            low_speed_run = 0
        is_stuck = low_speed_run >= args.stuck_ticks

        rc = compute_reward(
            prev_action=int(prev_a),
            action=int(action_idx[i]),
            speed=curr_sp, prev_speed=prev_sp,
            dlat=float(dlat_5tick[i]),
            dlon=float(dlon_5tick[i]),
            is_stuck=is_stuck,
        )
        r_smooth[i] = rc.smoothness
        r_progress[i] = rc.progress
        r_collision[i] = rc.collision
        r_stuck[i] = rc.stuck
        r_verdict[i] = VERDICT_REWARD.get(all_records[i].verdict, 0.0)
        r_state[i] = STATE_VERDICT_REWARD.get(
            all_records[i].state_verdict, 0.0
        )

        # Bounce telemetry — use motion magnitude as a proxy for heading
        # delta (heading_deg can be antipode-flipped; lat/lon-derived
        # motion is independent).  Threshold scaled so a fast cruise
        # → ~0.05 motion, a stall → ~0; bounce ~ big drop in motion.
        if i > 0 and ep_ids[i - 1] == ep_ids[i]:
            prev_motion = math.hypot(dlat_5tick[i - 1], dlon_5tick[i - 1])
        else:
            prev_motion = 0.0
        curr_motion = math.hypot(dlat_5tick[i], dlon_5tick[i])
        bounce, hard = detect_bounce_or_hard_turn(
            prev_motion * 1000.0, curr_motion * 1000.0,
            prev_sp, curr_sp, ep_max,
        )
        is_bounce[i] = bounce
        is_hard_turn[i] = hard

        prev_speed_local = curr_sp

        # Mark terminal on the last tick of each episode.
        if i + 1 == N or ep_ids[i + 1] != ep_ids[i]:
            terminal[i] = True

    # Hindsight propagation: a "bad state" verdict at tick T also
    # discounts ticks T-1..T-N inside the same episode, so the actions
    # that *led* to the bad state inherit a fraction of the penalty.
    # Only bad states get hindsight credit — "good" states are already
    # local (we don't want to retroactively praise random preceding
    # actions just because the ship ended up in good shape).
    if HINDSIGHT_BAD_STATE_WINDOW > 0:
        bad_penalty = STATE_VERDICT_REWARD.get("bad", 0.0)
        for i in range(N):
            if all_records[i].state_verdict != "bad":
                continue
            for k in range(1, HINDSIGHT_BAD_STATE_WINDOW + 1):
                j = i - k
                if j < 0 or ep_ids[j] != ep_ids[i]:
                    break
                r_state[j] += bad_penalty * (HINDSIGHT_BAD_STATE_GAMMA ** k)

    reward = (
        r_smooth + r_progress + r_collision + r_stuck
        + r_per_tick + r_verdict + r_state
    ).astype(np.float32)

    # Save (v2 schema — see docstring at top of file).
    args.out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        args.out,
        frame_paths=frame_paths,
        # v2 aux inputs (7 floats):
        heading_sin=heading_sin.astype(np.float32),
        heading_cos=heading_cos.astype(np.float32),
        dlat_5tick=dlat_5tick,
        dlon_5tick=dlon_5tick,
        is_channel=is_channel,
        is_junction=is_junction,
        is_dead_end=is_dead_end,
        # Position / speed / housekeeping:
        speed_kt=speed,
        hug_side=hug_side,
        lat=lat, lon=lon,
        # Action + reward:
        action_idx=action_idx,
        reward=reward,
        r_smoothness=r_smooth,
        r_progress=r_progress,
        r_collision=r_collision,
        r_stuck=r_stuck,
        r_per_tick=r_per_tick,
        r_verdict=r_verdict,
        r_state=r_state,
        # Quality / ROI / valid mask (drives DataLoader filtering):
        heading_quality=heading_quality,
        latlon_quality=latlon_quality,
        roi=roi_arr,
        valid=valid,
        # Diagnostic fields (raw values, for review tool):
        topology=topology_arr,
        heading_source=heading_source_arr,
        # Human labels:
        verdict=verdict_arr,
        state_verdict=state_verdict_arr,
        corrected=corrected_arr,
        original_action_idx=original_action_idx,
        # Episode bookkeeping + telemetry:
        terminal=terminal,
        episode_id=ep_ids,
        is_bounce=is_bounce,
        is_hard_turn=is_hard_turn,
    )

    # Per-session quality sidecars — one JSONL per session, one row per
    # tick.  The review-mode tick_viewer reads these locally to filter
    # the flagged ticks and to show quality flags in the info panel.
    # Schema:
    #   {"tick": N, "roi": str, "heading_quality": str,
    #    "latlon_quality": str, "valid": bool,
    #    "heading_source": str, "topology": str}
    n_sidecars = 0
    for ep, sd in sess_dir_by_ep.items():
        mask = (ep_ids == ep)
        if not mask.any():
            continue
        sidecar = sd / "quality.jsonl"
        with sidecar.open("w") as fp:
            for i in np.where(mask)[0]:
                tick = int(Path(str(frame_paths[i])).stem.split("_")[-1])
                # Motion bearing from cleaned dlat/dlon — same value the
                # cross-check uses internally.  Surfaced so the viewer
                # can show it and the user can adopt it as a corrected
                # heading.  Null when motion is too small/large to trust.
                dlat = float(dlat_5tick[i]); dlon = float(dlon_5tick[i])
                mot = math.hypot(dlat, dlon)
                if CROSS_CHECK_MOTION_MIN <= mot <= CROSS_CHECK_MOTION_MAX:
                    mot_brg = math.degrees(math.atan2(dlon, dlat)) % 360.0
                else:
                    mot_brg = None
                fp.write(json.dumps({
                    "tick": tick,
                    "roi": str(roi_arr[i]),
                    "heading_quality": str(heading_quality[i]),
                    "latlon_quality": str(latlon_quality[i]),
                    "valid": bool(valid[i]),
                    "heading_source": str(heading_source_arr[i]),
                    "topology": str(topology_arr[i]),
                    "heading_deg": float(heading[i]),
                    "motion_bearing_deg": mot_brg,
                    "motion_mag": mot,
                }) + "\n")
        n_sidecars += 1
    print(f"Wrote {n_sidecars} per-session quality.jsonl sidecars")

    # Stats summary.
    print()
    print(f"→ {args.out}")
    print(f"\nDataset stats:")
    print(f"  total tuples: {N}")
    print(f"  episodes: {ep_ids.max() + 1}")
    print(f"  ticks with speed_kt: "
          f"{int((~np.isnan(speed)).sum())} ({100 * (~np.isnan(speed)).mean():.0f}%)")
    print(f"  ticks with lat/lon: "
          f"{int((~np.isnan(lat)).sum())} ({100 * (~np.isnan(lat)).mean():.0f}%)")
    print(f"\nAction distribution:")
    for k, v in Counter(action_idx.tolist()).most_common():
        print(f"  {ACTION_NAMES[int(k)]:>8}: {v:>6} ({100*v/N:>5.1f}%)")
    print(f"\nReward stats (mean ± std):")
    for name, arr in [("smoothness", r_smooth), ("progress", r_progress),
                     ("collision", r_collision), ("stuck", r_stuck),
                     ("per_tick", r_per_tick),
                     ("verdict", r_verdict), ("state", r_state),
                     ("TOTAL", reward)]:
        print(f"  {name:>11}: {arr.mean():+.4f} ± {arr.std():.4f}  "
              f"(range {arr.min():+.2f} … {arr.max():+.2f})")
    print(f"\nTelemetry flags:")
    print(f"  is_bounce:    {int(is_bounce.sum()):>5} ticks "
          f"({100*is_bounce.mean():.2f}%)")
    print(f"  is_hard_turn: {int(is_hard_turn.sum()):>5} ticks "
          f"({100*is_hard_turn.mean():.2f}%)")
    print(f"  terminal:     {int(terminal.sum()):>5} ticks")

    # v2 — ROI / quality / valid breakdowns.
    print(f"\nROI distribution (tick counts):")
    for name, n_roi in Counter(roi_arr.tolist()).most_common():
        pct = 100 * n_roi / N
        print(f"  {name:<18}: {n_roi:>5} ({pct:>5.1f}%)")
    print(f"\nLat/lon quality:")
    for q, n_q in Counter(latlon_quality.tolist()).most_common():
        print(f"  {q:<14}: {n_q:>5} ({100*n_q/N:>5.1f}%)")
    print(f"\nHeading quality:")
    for q, n_q in Counter(heading_quality.tolist()).most_common():
        print(f"  {q:<14}: {n_q:>5} ({100*n_q/N:>5.1f}%)")
    print(f"\nValid mask (tiered filter — channel strict, rare ROIs lax):")
    print(f"  valid     : {int(valid.sum()):>5} ({100*valid.mean():.1f}%) "
          f"— trainer uses these")
    print(f"  rejected  : {int((~valid).sum()):>5} ({100*(~valid).mean():.1f}%)")
    # Per-ROI valid breakdown — confirms rare ROIs survived uncertain readings.
    print(f"\nValid breakdown by ROI:")
    for roi_name in sorted(set(roi_arr.tolist())):
        mask = (roi_arr == roi_name)
        n_total = int(mask.sum())
        n_valid = int(valid[mask].sum())
        kept = 100 * n_valid / n_total if n_total else 0
        print(f"  {roi_name:<18}: {n_valid:>5}/{n_total:<5} kept ({kept:>5.1f}%)")
    print(f"\nHuman labels (from labels.jsonl sidecars):")
    label_counts = Counter(v for v in verdict_arr if v)
    state_label_counts = Counter(v for v in state_verdict_arr if v)
    any_labels = bool(label_counts) or bool(state_label_counts)
    if any_labels:
        print(f"  action verdicts:")
        for v, n in label_counts.most_common():
            print(f"    {v:>11}: {n:>5} ticks")
        print(f"  state verdicts:")
        for v, n in state_label_counts.most_common():
            print(f"    {v:>11}: {n:>5} ticks")
        print(f"  corrected:   {int(corrected_arr.sum()):>5} ticks "
              f"(action overridden by human)")
        if HINDSIGHT_BAD_STATE_WINDOW > 0:
            n_bad = state_label_counts.get("bad", 0)
            print(f"  hindsight: bad-state penalty propagated {n_bad}× "
                  f"over {HINDSIGHT_BAD_STATE_WINDOW} prev ticks "
                  f"(γ={HINDSIGHT_BAD_STATE_GAMMA})")
    else:
        print(f"  (no labels.jsonl files found — run tools/tick_viewer.py "
              f"to annotate)")


if __name__ == "__main__":
    main()
