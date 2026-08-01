# Pixel-continuity navigation design

**Status:** design draft, not yet implemented (2026-07-23).
**Supersedes** the world-coord (`state.tactical_dest_latlon`) continuity
approach for the tactical + reflex loop.
**Companion** to `docs/tactical_search_tracker.md` and
`docs/lookahead_tactical_design.md`.

---

## 1. Why

Current design stores tactical dest as `(lat, lon)` and reprojects
to pixels each tick.  This entangles the navigation hot path with
HUD OCR, which produces cascading failures when sprites occlude the
lat/lon text (Nubia village, pirate icons, etc.).  The
`accept_or_defer` gate mitigates one-shot garbles but a plausible-
looking garbage read still corrupts prev, wrecking the dest.

Session `ai_nav_2026-07-22T22-35-04` t357-t365 (fork mispick) also
showed the fundamental identity problem: the SEARCH tracker's
"nearest edge midpoint" smoothly drifted from the correct
NW-branch exit to the wrong N-branch exit as the fork emerged,
because the tracker had no persistent identity for "which world
point we're heading toward."

The fix: **navigate in pixel space.  Store waypoints as
(row, col).  Measure ship motion by registering consecutive frames'
water masks.  Shift world-anchored waypoints by the measured
frame delta each tick.**  Re-derive frontier-anchored waypoints
from the current mask, but constrain them by a first-order motion
prior to prevent spurious jumps.

## 2. Two identity types

Every waypoint has one of two identity semantics:

- **Feature-anchored** — "the same world feature we picked at tick N."
  A turning-point pivot, a leaf terminus, a narrow choke.  Identity
  = a specific place in the world.  Update rule: **shift by measured
  frame delta each tick.**

- **Frontier-anchored** — "the current best exit / next step."
  A SEARCH-mode edge midpoint, a reflex waypoint one step ahead.
  Identity changes with the frame.  Update rule: **re-derive from
  current mask each tick, constrained by a motion-prior tolerance
  vs the shifted-prev value.**

Nothing is anchored to `(lat, lon)` in the tactical or reflex loop.

## 3. Frame-shift measurement

Each tick the pipeline computes `state.frame_shift_px = (dy, dx, conf)`:

```
water_mask   = state.water_mask
shore_edge   = water_mask & ~erode(water_mask)     # ~1 px shore ring
shore_edge   &= valid_mask                          # exclude sprites
(dy, dx), conf = cv2.phaseCorrelate(
    prev_shore.astype(np.float32),
    curr_shore.astype(np.float32),
    hann_window,
)
```

- **Input**: shore-edge pixels only, sprite-masked.  Shore edges
  carry translation signal without the aperture-problem-inducing
  bulk of homogeneous water interior.  Bench on
  `ai_nav_2026-07-22T22-35-04` t108-t115 (Nubia): shore-edge fixes
  the aperture failure that IoU-on-full-mask stuck at (0, 0).
- **Algorithm**: `cv2.phaseCorrelate` — ~1.2 ms per call, 76×
  faster than brute-force IoU (91 ms) and better in Nubia due to
  Hanning windowing built in.
- **Confidence**: cv2's response peak height.  Typical values
  0.4-0.9 on good frames; below ~0.3 indicates registration
  failure (occluding UI overlay, camera change, etc.).
- **Sign convention**: (dy, dx) such that a world point at
  `prev_frame[r, c]` appears at `curr_frame[r + dy, c + dx]`
  (opposite of ship-motion direction).

## 4. Motion prior on re-derived waypoints

For frontier-anchored waypoints, the re-derivation is constrained:

```
predicted_wp = prev_wp + frame_shift
tolerance    = 1.5 × max(1, |frame_shift|)
if deliberate_change:
    accept(re_derived_wp)
elif distance(re_derived_wp, predicted_wp) <= tolerance:
    accept(re_derived_wp)          # small correction, on track
else:
    accept(predicted_wp)            # clamp; log anomaly
    anomaly_counter[kind] += 1
```

**"Deliberate change"** signals per waypoint kind:
- **SEARCH tactical**: fork emerged (edge-run count changed);
  walker fired with hug-side; reclassified to turning_point.
- **Turning-point tactical**: ship arrived, walker picked next leg.
- **Reflex**: tactical dest itself changed.
- **Walked path**: walker re-picked the whole path.

Deliberate changes lift the tolerance.  Everything else is a small
correction and should stay within `1.5 × |frame_shift|` of the
predicted position.

## 5. LookaheadTactical rewrite

### State

```
_current_dest_px: Optional[tuple[int, int]]      # (row, col)
_current_dest_reason: str
_last_walked_path_px: list[tuple[int, int]]      # pixel polyline
```

Removes: `_current_dest`, `_prev_ship_ll` (lat/lon), all `PX_PER_DEG`
math inside the tactical layer.

### Per-tick flow

```python
def maybe_consult(self, frame, state):
    dy, dx, conf = state.frame_shift_px

    # 1. Shift feature-anchored state
    if conf >= _SHIFT_CONF_MIN and self._current_dest_px is not None:
        if self._current_dest_reason in _TURNING_POINT_REASONS:
            self._current_dest_px = (
                self._current_dest_px[0] + dy,
                self._current_dest_px[1] + dx,
            )
        self._last_walked_path_px = [
            (r + dy, c + dx) for r, c in self._last_walked_path_px
        ]

    # 2. State machine
    if self._current_dest_px is None:
        self._current_dest_px, reason = self._walker_pick_px(state)
        self._current_dest_reason = reason
    elif self._current_dest_reason in _TURNING_POINT_REASONS:
        if self._at_turning_point_px(state):
            new_px, reason = self._walker_pick_px(state)
            if new_px is not None:
                self._current_dest_px = new_px
                self._current_dest_reason = reason
    else:  # SEARCH
        re_derived = self._nearest_edge_run_midpoint_px(state)
        deliberate = self._fork_emerged(state)   # edge-run count changed
        if re_derived is None:
            # Reclassify as turning_point; ship approaches pivot
            self._current_dest_reason = "turning_point"
        else:
            self._current_dest_px = self._apply_motion_prior(
                re_derived, deliberate, (dy, dx),
            )

    # 3. Commit direction = bearing from ship center to dest_px
    state.commit_direction = bearing_from_px_offset(self._current_dest_px)

    # 4. Publish for reflex
    state.tactical_dest_px = self._current_dest_px
    state.tactical_walked_path_px = self._last_walked_path_px
```

### Walker changes

`_pick_new_dest` already runs in pixel space internally.  Change:
return `dest_px` directly (no lat/lon conversion).  `_last_walked_path`
becomes `list[(row, col)]`.

### Arrival check

`_at_turning_point_px` — pure pixel distance from frame center
(ship position) to `_current_dest_px`.  Threshold unchanged
(`TURNING_APPROACH_PX = 40`).

### Fork-emergence trigger

`_fork_emerged`: `len(current_frame_edge_runs) > len(prev_frame_edge_runs)`.
This is the "deliberate change" signal for SEARCH mode.  When
triggered, walker fires with hug-side; new dest is a fork branch
pick, not a smoothed-continuation midpoint.

## 6. HugPathPlanner (reflex) rewrite

### State

Add `_last_wp_px: Optional[tuple[int, int]]`.

### Per-tick flow

```python
def plan(self, frame, state):
    dy, dx, conf = state.frame_shift_px
    prev_wp = self._last_wp_px
    if prev_wp is not None and conf >= _SHIFT_CONF_MIN:
        predicted_wp = (prev_wp[0] + dy, prev_wp[1] + dx)
    else:
        predicted_wp = None

    re_derived = self._pick_from_path_px(state)   # existing algorithm
    deliberate = (state.tactical_dest_px != self._prev_tactical_dest_px)
    wp = self._apply_motion_prior(
        re_derived, deliberate, (dy, dx), predicted_wp,
    )
    self._last_wp_px = wp
    self._prev_tactical_dest_px = state.tactical_dest_px
    # ... existing rudder command emission ...
```

## 7. NavState schema changes

Add:
- `frame_shift_px: tuple[int, int, float]` — (dy, dx, confidence)
- `tactical_dest_px: Optional[tuple[int, int]]`
- `tactical_walked_path_px: Optional[list[tuple[int, int]]]`

Deprecated (kept as debug-only, derived on serialize when
`state.lat/lon` is fresh):
- `tactical_dest_latlon`
- `tactical_walked_path`

## 8. What still uses lat/lon

- **Mission-layer arrival** (PointToPointMission `am I near Cairo?`)
- **`visited_dead_ends` memory** — cross-many-tick persistence;
  motion-integration would accumulate too much drift over 100+ ticks
- **Motion-bearing cross-check** for heading validator
- **Trace / debug output** — human-readable position

The tactical + reflex hot path no longer touches lat/lon.

## 9. Failure modes and handling

| Failure | Symptom | Recovery |
|---|---|---|
| Registration confidence low (< 0.3) | UI overlay, camera change, popup | Skip shift; hold turning-point dest at prev pixel (drift 1 tick), SEARCH re-derives (already tolerant) |
| Motion prior clamp fires often | Perception noise (sprite flicker, mask jitter) | Log anomaly; if persistent, walker re-picks |
| Ship on land in mask (no water CC at center) | Beached, or mask hole from sprite | Existing fallback: use full mask; walker's single-anchor path fires |
| Turning-point dest scrolls off frame (row/col outside [0, H)/[0, W)) | Ship overshot the pivot or was way off | Treat as arrival; walker picks next |

## 10. Testing plan

- Add unit tests: `state.frame_shift_px` set correctly; tactical dest
  shifts as expected; motion prior clamps + logs anomaly.
- Replay `ai_nav_2026-07-22T22-35-04` t357-t365 (fork mispick):
  turning-point-shifted dest should stay on the west branch, not
  drift to east.
- Replay Nubia t108-t115: dest tracking should stay stable through
  village text overlay.
- Full voyage regression: Cairo round-trip on saved reference.

## 11. Future direction — multi-frame ship-center comparison for heading

The same image-registration principle that gives us `frame_shift_px`
for the WORLD content can be extended to a second registration on the
SHIP-CENTER region.  The ship sprite sits at frame centre and rotates
with the vessel; comparing the center crop across ticks gives:

- **Direct heading Δ** — angular difference between consecutive center
  crops (rotational registration, e.g. log-polar transform + phase
  correlation, or ORB feature matching restricted to the ship sprite).
- **Occlusion robustness** — today's heading pipeline (CNN + template
  match) is single-frame: one occluded frame → one bad heading read.
  Multi-frame comparison lets an occluded tick borrow information from
  its N-1 predecessors (majority vote on the delta, or a small
  temporal filter over the last 4-5 rotations).
- **Cross-check** against the CNN heading — big disagreement =
  perception anomaly, feed into the physics-reject filter alongside
  the motion-bearing signal.

Concrete sketch:

```
center_crop_t   = frame_t[ship_bbox]         # e.g. 40x40 around center
center_crop_t-1 = frame_{t-1}[ship_bbox]
delta_deg, conf = register_rotation(center_crop_t-1, center_crop_t)
heading_t = heading_t-1 + delta_deg          # if conf high
```

For occlusion:
- Skip low-conf pairs; interpolate heading via 3-4 tick moving window
- Combine with heading CNN via a weighted average (CNN weight goes
  down when its confidence drops, up when frame-delta agrees)

Deferred to a follow-up milestone.  The current tactical + reflex
refactor above stays scoped to WORLD-content registration for
translation.  Ship-center rotational registration is a natural next
extension that would improve heading reliability the same way this
design improves waypoint continuity.

## 12. Future direction — RL-based reflex steering

The reflex planner today is rule-based: pick the farthest reachable
point on the tactical walked-path (Bresenham + water-mask straight-
line check), emit a rudder hold.  Its failure mode is well-known:
the straight-line check passes at wp-selection time, but the ship's
actual bow arc under the commanded hold curves into a shore
protrusion the line-check missed → bank collision + speed drop.
This is a *local control under uncertainty* problem — exactly what
RL policies are good at.

**Proposed split** (tactical stays algorithmic, reflex becomes RL):

- **LookaheadTactical stays as-is** — global topology decision
  (SEARCH edge exit, turning-point pivot, hug-side branch choice).
  Deterministic, testable, interpretable.  Don't RL this.
- **Reflex → learned policy.**  Maps ship's local state + tactical
  goal → discrete rudder action.

### State representation

Recommend structured features over raw pixels:

- Water mask (H×W bool, from V11Segmenter) — cheaper + already computed
- `tactical_dest_px_offset` — goal in ship-relative pixels
- `frame_shift_px` — ship's measured motion signal
- Last N actions (short temporal context)
- Ship heading confidence (perception quality)

Raw-frame policy (RGB CNN) is more flexible but sample-inefficient
given we've already invested in perception.  Feed engineered
features to the policy; let it learn *steering*, not *perception*.

### Action space

Discrete matches the existing rudder vocabulary:

```
{hold_left  200 / 500 / 1000 ms,
 no-op,
 hold_right 200 / 500 / 1000 ms}
```

7 actions.  Same set the reflex planner uses today, so IL / RL
transitions are apples-to-apples.

### Reward shape

- **Progress**: Δ(distance ship→tactical_wp) per tick, positive
  when shrinking.  Computed from `tactical_dest_px_offset` +
  `frame_shift_px`.
- **Bounce penalty**: speed drop ≥ 70% tick-to-tick → large
  negative.  Already-tracked signal.
- **Time / step penalty**: small negative to discourage stalling.
- **Safety margin (optional)**: negative gradient as bow-forward
  cone approaches the shoreline inside `SAFE_APPROACH_DIST_PX`.

### Phased transition

Aligned with the same measurement-first philosophy as the pixel-
continuity rollout:

- **Phase A — Data collection.**  Reflex steers as today; every
  tick log `(state, action, reward)` to a replay buffer.  Zero
  behavioral change.  Uses existing session traces.
- **Phase B — Imitation-learning warm start.**  Train policy to
  mimic reflex on the buffer.  Model reaches "reflex parity"
  before any RL objective; no exploration risk.
- **Phase C — Offline RL fine-tune.**  Conservative offline RL
  (IQL / CQL) on the same buffer.  Model learns to *outperform*
  reflex on the cases where reflex fails (bank collisions).
- **Phase D — Advisor mode.**  RL suggests; reflex still commits.
  Log agreements + disagreements + outcomes.  Trust threshold
  gates when RL is allowed to override.
- **Phase E — RL commits, reflex as safety net.**  Policy steers;
  reflex catches only cases where the RL action would violate a
  hard safety envelope (e.g. rudder magnitude bounds, imminent
  land-in-bow arc).
- **Phase F — RL fully commits.**  Reflex removed.  Safety
  envelope stays.

### What this design gives us free from the current work

- `frame_shift_px` quantifies "did the ship actually move" per
  tick — critical for reward attribution.
- Sprite-masked water masks are ready-to-use structured features.
- `tactical_dest_px_offset` gives the policy its goal in ship-
  relative pixels — no coord conversion.
- Bounces already detected via speed-drop rule.
- The stable tactical state machine means the RL isn't asked to
  solve global navigation, only local execution.

### Not scope for this doc

- Model architecture (CNN sizes, encoder choices)
- Training infrastructure
- Simulator loop for offline eval
- Convergence criteria

All that lives in a separate RL design doc when Phase A data
collection produces enough replay data to characterise the state
distribution.

## 13. Roll-out

Phased:
1. Ship `state.frame_shift_px` + registration only (no consumers).
   Log to trace; validate offline on saved sessions.
2. Migrate turning-point continuity to shift-integration.  Keep
   SEARCH on lat/lon reproject for comparison.
3. Migrate SEARCH to pixel re-derivation with motion prior.
4. Migrate reflex + walked_path.
5. Remove `tactical_dest_latlon` from decision path (keep as debug
   derived field).

Each phase is independently reversible.
