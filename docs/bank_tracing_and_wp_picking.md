# Bank tracing & waypoint picking — current rules (2026-07-25)

Authoritative reference for what the tactical layer does per tick.
Reflects code state after commits `04a907d` (shift-gate rewrite),
`1c014a1` (tactical cross-check), and the P16/P17 tracker fixes.

Everything below lives in:

- `brain/ai_nav/layers/tactical.py` — `LookaheadTactical` class
- `tools/bank_tracer.py` — `trace_left_bank` function

The bot has ONE authoritative tactical destination at any time,
represented as `(_current_dest_lat_lon, _current_dest_px_offset)`.
Every tick, `maybe_consult` updates it in place; the planner (L3)
consumes it via `state.tactical_dest_latlon`.

---

## 1. Per-tick data flow — `LookaheadTactical.maybe_consult`

```
                  ┌──────────────────────────────┐
                  │ 0. Shift-integrate stored    │
                  │    pixel offset by frame_shift│
                  └──────────────────────────────┘
                                │
              no dest?          │          have dest
                ┌───────────────┴────────────────┐
                ▼                                ▼
       ┌────────────────┐          ┌────────────────────────┐
       │ 3. Fresh pick  │          │ 1. SEARCH tracker      │
       │  (bank tracer) │          │   _track_search_dest   │
       └────────────────┘          └────────────────────────┘
                ▲                       │ fail    │ success
                │                       ▼         ▼
                │            ┌───────────┐  ┌──────────────────┐
                │            │ 3. Fresh  │  │ 2. Corner-split  │
                │            │    pick   │  │    check          │
                │            └───────────┘  └──────────────────┘
                │                                │ split-in-corner?
                │                    ┌───────────┴─────────────┐
                │                    │yes                     no│
                │                    ▼                          │
                └──────────── ┌───────────┐                     │
                              │ 3. Fresh  │                     │
                              │    pick   │                     │
                              └───────────┘                     ▼
                                                        (hold dest)
```

Then:

- Compute `commit_direction` = compass bearing from ship → dest.
- Expose `state.tactical_dest_latlon`, `state.tactical_walked_path`,
  `state.tactical_dest_px_offset`.

---

## 2. Step 0 — Shift-integration

```python
if _current_dest_px_offset is not None and frame_shift_px is not None:
    dy, dx, conf = frame_shift_px
    if conf >= SHIFT_CONF_MIN:               # 0.15
        _current_dest_px_offset += (dy, dx)
    _current_dest = ship_ll + offset_to_ll(_current_dest_px_offset)
```

**Purpose**: keep the dest world-locked as the mini-map scrolls.
Without this the dest lat/lon would jitter with HUD OCR noise.

**Threshold**: `SHIFT_CONF_MIN = 0.15`. Note this is LOWER than the
pipeline-side gate (0.20, plus speed-consistency check).  A shift can
be trusted enough here to move the anchor forward, but not trusted
enough at the pipeline level to update `shift_motion_bearing_deg`.

The dest lat/lon is always recomputed from `(ship + offset)` on the
same tick — never carried across HUD OCR jumps.

---

## 3. Step 1 — SEARCH tracker (`_track_search_dest`)

**Purpose**: cheap continuation of an existing dest across ticks
without re-running the bank tracer (~2 ms vs ~15 ms).  Applies to
`frame_edge` dests — the picker's default result.

### Algorithm

1. **Anchor** = ship centre + shift-integrated offset.
2. **Classify anchor's nearest edge**: top / bot / left / right by min
   axis-parallel distance.
3. **Candidate radius** =
   `max(TRACKER_SEARCH_RADIUS_PX=80, 3 × expected_shift_px)`.
   Scales with recent motion so fast-ship ticks still find candidates.
4. **Gather candidates** on `(same_edge, *ADJACENT[same_edge])` —
   never the OPPOSITE edge (a world-fixed exit can't jump across the
   frame in one tick).  Adjacency:
   - `top` ↔ `left`, `right`
   - `bot` ↔ `left`, `right`
   - `left` ↔ `top`, `bot`
   - `right` ↔ `top`, `bot`
5. **Per-edge midpoint** via `_find_run_midpoint_on_edge`: scan the
   edge for contiguous water runs of length ≥ `TRACKER_MIN_EDGE_RUN_PX
   = 6`; pick the run whose midpoint has minimum TRUE 2D Euclidean
   distance to (anchor_row, anchor_col).  Gate on `hypot(dy, dx) ≤
   radius`.  **(2026-07-25 P16 fix: was axis-only distance.)**
6. **Reachability filter**: candidate must be in the ship's water CC
   (from `scipy.ndimage.label` on the water mask).  Rejects candidates
   in split-off water pools.
7. **Pick minimum-distance candidate** across all edges.  Log at DEBUG
   if the chosen edge differs from the anchor's edge.
8. **On success**: update `_current_dest_px_offset` to the picked
   midpoint's offset from ship; recompute `_current_dest` in lat/lon;
   append current dest to `_current_walked_path_latlon`.
9. **On failure** (no candidate within radius on any allowed edge):
   return False → caller runs Step 3 (fresh pick).

### Design intent

- Ship orientation, commit direction, mission goal are **irrelevant**
  to the tracker's decision.  Those pollute the answer with stale
  directional bias (e.g. post-bounce N-spin at t13).
- The tracker asks only: *"where did the world-fixed water exit go
  this tick, given where it was last tick + how much the frame moved?"*

---

## 4. Step 2 — Corner-split detection (`_corner_split_since_last_tick`)

**Purpose**: force a fresh pick when the bank continuation at the
dest has topologically changed — specifically when a corner region
went from CONNECTED (water spans both adjacent edges through the
corner) to SPLIT (a land divider emerged).

### Algorithm

1. **Compute connected-corner set** for current mask:
   `_connected_corners(mask)` — for each of the 4 corners (TL/TR/BL/BR),
   check within a 20×20 pixel corner region whether water pixels
   touching the two adjacent edges are in the same connected component.
   Returns `{"TL", "TR", "BL", "BR"} ∩ (currently connected)`.
2. **Diff**: `split = _prev_connected_corners - new_connected`.
3. **Update `_prev_connected_corners`** for next tick.
4. **Determine which corner (if any) the dest is IN**: dest position
   within the 20×20 corner regions.
5. **Fire fresh pick iff `dest_corner ∈ split`.**

### 2026-07-25 stricter rule (P17)

The original rule (2026-07-25 morning) fired whenever the dest was on
ANY edge touched by ANY split corner.  That was too aggressive:
session `ai_nav_2026-07-25T15-54-02` t401 had a BL corner split while
the dest was on the BOT edge at col 264 — ~245 px from the BL corner
region.  The bank continuation at col 264 was completely unchanged;
forcing a fresh pick flipped the dest to the top edge and stalled the
voyage for 300+ ticks.

**Semantic principle**: the dest must actually be IN the affected
corner's region for the split to be relevant to it.

### Known trade-off (P12)

The stricter rule loses the "happy accident" that made P12 correct
(`ai_nav_2026-07-25T11-01-09` t448, TL corner split, dest at top-edge
col 87).  Under the stricter rule, P12-class corner situations will
be handled by the tracker eventually failing as the top-edge run
scrolls off frame — not by proactive corner-split re-pick.  If P12
scenarios cause visible navigation issues in live runs, add a separate
periodic bank-tracer sanity check.

---

## 5. Step 3 — Fresh pick (`_pick_new_dest`)

Runs when Step 1 fails OR Step 2 forces re-pick OR there's no dest yet.

### Algorithm

1. **Restrict to ship's CC**:
   ```python
   labeled = cc_label(water_mask)
   reachable_mask = (labeled == labeled[ship_row, ship_col])
   ```
   Ensures the trace can't step onto disconnected water pools.

2. **Determine approach direction** — the ONLY signal `_pick_new_dest`
   uses to score bank continuations.  Priority chain, most reliable
   first:

   | # | Signal | Notes |
   |---|--------|-------|
   | 1 | `shift_motion_bearing_deg` | Frame-shift-based ship motion.  Vision-independent of HUD OCR + heading CNN. |
   | 2 | `motion_bearing_deg` | Lat/lon Δ over recent ticks.  Slower but self-refreshing. |
   | 3 | `heading.bearing_deg` | CNN ship-bow orientation. |
   | 4 | `_last_commit_deg` | Continuity from last tick's tactical dest direction. |
   | 5 | `self.goal_bearing_deg` | CLI `--commit-bearing` fallback. |
   | 6 | `180°` | Absolute last resort. |

3. **Shift-vs-motion cross-check safety net** (2026-07-25): if both
   shift and motion bearings exist and disagree by more than 90°,
   treat shift as stale and use motion instead.  Prevents wedged
   `shift_motion_bearing_deg` (P14/P15) from steering the picker.

4. **Trace the port-side bank forward**:
   ```python
   polyline = trace_left_bank(
       reachable_mask, (ship_row, ship_col), approach_bearing,
       hug_side=self.hug_side,   # "port" or "starboard"
       max_length_px=240,
       offset_px=10,
   )
   ```
   Returns a list of `(y, x)` pixels along the bank, or `None` on
   failure.

5. **Convert polyline to lat/lon** using `PX_PER_DEG = 100`.

6. **Return** `(polyline_end_latlon, reason="frame_edge")` — the far
   end of the polyline becomes the dest.  The full polyline is stored
   as `_last_walked_path_latlon` and exposed to `HugPathPlanner` for
   its "walk backward, pick farthest ship-visible point" reflex.

---

## 6. Bank tracing (`trace_left_bank`)

Literal wall-follower on the mask contour.

### Algorithm

1. **Outer contour** via `cv2.findContours(mask, RETR_EXTERNAL,
   CHAIN_APPROX_NONE)`.  Returns points in CCW order along the water
   CC's outer boundary (interior on the left of each tangent).

2. **Project ship onto contour** — `_project_ship`:
   - Compute the ship's PORT direction from `approach_bearing`
     (rotate 90° CCW on compass).  For starboard hug, negate.
   - Score each contour point:
     `score = perp_dist + penalty_if_wrong_side + max(0, -port_dist)`
     - `perp_dist` — magnitude perpendicular to port direction.
     - `penalty_if_wrong_side = 1000.0` if the point lies on the
       starboard side of the ship (large so port-side always wins
       when available).
     - `max(0, -port_dist)` — tie-break toward larger port projection.
   - Pick lowest-score index.

3. **Trial-walk both directions** (P10 fix):
   Walk ±1 along the contour for 30 steps.  Stop early if a frame-edge
   pixel is hit (so we don't pollute the direction estimate by walking
   along the boundary).  Compute net displacement for each direction;
   pick whichever bearing is closest to `approach_bearing`.

4. **Walk the contour**, emitting inward-offset points:
   - Local tangent = (contour[i+step] − contour[i−step]).
   - Normal into water = 90° rotation of tangent; sample mask 3 px
     inward to verify, flip if not water.
   - Offset point = contour[i] + `offset_px` × normal (default 10 px).
   - Clamp inside frame.  If offset lands on land (thin channel),
     keep contour point itself.
   - Append to polyline.  Break if loop returns to start.
   - **Stop at frame edge** once `walked > 15 px` (excludes projection
     near a boundary).  Bank naturally exits the frame at an opening
     — that's the tactical destination.

5. **Truncate at max Euclidean distance from ship** (P11 fix):
   When the bank curves 180° around a dead-end pocket, the trace goes
   IN, curls around the terminal, and comes back OUT past the ship's
   vicinity.  Truncating at `argmax(dist_to_ship)` ensures the polyline
   goes AWAY from ship monotonically in Euclidean terms — HugPathPlanner's
   "farthest ship-visible point" reflex then works correctly.

### Configuration

| Param | Value | Meaning |
|-------|-------|---------|
| `max_length_px` | 240 | Cap on total polyline walked-length |
| `offset_px` | 10 | Inward offset from actual bank |
| `TRIAL_STEPS` | 30 | Steps used to disambiguate walk direction |
| `FRAME_MARGIN_PX` | 2 | Distance from frame edge that counts as "at edge" |

---

## 7. Approach-bearing pipeline signals (upstream)

The picker's approach-direction quality depends on the pipeline
producing a fresh, correct `shift_motion_bearing_deg`.  The pipeline
computes it via a per-tick + cascade fallback (2026-07-25 rewrite,
commit `04a907d`):

### Per-tick shift gate (`pipeline.py`)

```
expected_mag = speed_kt × K_PX_PER_KT     # K = 1.35 (calibrated Nile)
accept if:
    conf ≥ CONF_FLOOR (0.20)
    AND MAG_FLOOR (0.3) × expected ≤ mag ≤ MAG_CEIL (3.0) × expected
    AND speed_kt ≥ MIN_SPEED (1.0)
```

**Rationale**: ship speed is an independent channel (HUD OCR) that
directly predicts pixel-displacement magnitude.  Combining a
confidence floor + speed-consistency band catches:
- **Weather/hue transitions**: legitimate motion, transiently low conf
  → speed check confirms → accepted at conf ≥ 0.20.
- **Registration failures**: tiny shifts at high speed → magnitude
  check rejects even if conf is OK.
- **Lock-in regressions**: replaced the old avg-relative gate
  (`mag ≤ 3 × avg_mag`) which learned small avg during slow sections
  and rejected all subsequent legitimate large shifts forever.

### Cascade fallback

On ≥2 consecutive rejections AND speed ≥ 1 kt, correlate current
mask against N-2, N-4, N-8 (from an 8-deep mask ring buffer).  Bigger
baseline = better phase-correlate SNR through weather transitions.
Cascade result divided by K for per-tick-equivalent storage.

### Weighted-average bearing

```python
wy = sum(-dy * conf * mag  for (dy,dx,conf) in shift_history)
wx = sum(-dx * conf * mag  for (dy,dx,conf) in shift_history)
if hypot(wy, wx) > 3.0:
    shift_motion_bearing_deg = degrees(atan2(wx, -wy)) % 360
```

Weighted by `conf × mag` so low-quality or tiny shifts don't dominate.

---

## 8. Regression cases and what they teach

See `docs/tactical_regression_gallery/index.html` for the visual
gallery.  Highlights that shaped current rules:

| Case | Diagnosis | Fix |
|------|-----------|-----|
| P4 t674 | SEARCH tracker held stale reason after reclassification | Fresh-pick on tracker failure, no reclassification cascade |
| P9 t395 | Body-frame LEFT/STRAIGHT/RIGHT bucketing failed at complex junctions | Retired bucketing; adopted literal bank tracing |
| P10 t139 | Single-tangent walk-direction pick was fragile at concavities | Trial-walk both directions, score net displacement |
| P11 t391 | Dead-end curl-back put wp near ship | Truncate polyline at max Euclidean distance from ship |
| P12 t445 | Tracker held top-edge dest when bank continuation flipped to left | Corner-split detection (later refined — see P17) |
| P13 t573 | Hairpin dest jump | Corner-split + true-2D distance combined resolve most instances |
| P14 t212 | `shift_motion_bearing_deg` stale at wrong direction | Pipeline speed-consistency gate + cross-check safety net |
| P15 t373 | Same as P14 (weather transition) | Same |
| P16 t206 | Tracker admitted 286-px away midpoint via axis-only distance | True 2D distance for both scoring AND gating |
| P17 t401 | Corner-split fired on far corner during post-bounce | Corner-split gate now requires dest IN corner region |

---

## 9. Known weaknesses to watch

- **Approach direction during/immediately after bounces**: momentary
  motion reflects post-bounce drift, not the ship's intent.  P17
  root-cause 2.  Deferred fix: filter approach-bearing computation
  to exclude ticks with speed drops ≥ 70%.
- **Fresh-pick has no memory of prev dest**: on any fresh pick, the
  trial-walk direction is chosen purely by approach direction, with
  no preference to stay close to the previous dest.  Amplifies
  approach-direction errors into full 180° flips.
- **Shift-integration vs pipeline gate mismatch**: tactical shift
  integration uses `SHIFT_CONF_MIN = 0.15`, pipeline uses `≥ 0.20 +
  speed check`.  A shift may be trusted enough here to move the anchor
  but not enough to update `shift_motion_bearing_deg`.  Usually
  harmless but a source of drift on marginal ticks.
- **`_pick_new_dest` throws away prev dest info**: a slight nudge
  might have sufficed; instead we re-derive from scratch every time.
