# Lyapunov Point-Pursuit Navigation — Design Doc

**Status:** Proposed, not yet implemented.
**Replaces:** §13.16 Bug2 wall-ahead commit stack (§13.16.1–.5) in
`brain/goals/hug_shore.py`.
**Related memory:**
- `project_goal_management_architecture.md`
- `project_bug2_known_limitations.md`

## 1. Problem statement

The current steering stack accumulated five layered heuristics
(Trémaux fixed direction → centroid alignment → per-tick re-evaluation
→ VFH+ μ₁ goal-direction override → history-derived sequencer) to
work around the absence of a clear goal-point representation. Each
layer was a patch for the previous one's failure mode:

- §13.16: fixed Trémaux flips badly on the wrong side of a wall.
- §13.16.1: centroid alignment improved direction picking but had no
  goal awareness.
- §13.16.2: re-evaluation tracked the bot's rotation but oscillated.
- §13.16.3: goal-direction term was added as a hard cost override
  (`-1e6`), which removed obstacle veto power — **caused a real
  collision-avoidance regression** observed at
  `hug_debug_20260531_175331`.
- §13.16.4 / .5: sequencer + LOS destination tried to provide an
  external anchor but still composed with the override above.

Outcome from live runs: bot oscillates in narrow channels, ignores
close obstacles when goal direction points through them, and
auto-locks onto wrong directions after wall bounces.

The architectural mistake was treating "where do I want to go" as a
discrete *direction* and patching with rules. The canonical form
represents it as a *waypoint* and reduces all the ad-hoc rules to
two pieces: a waypoint generator + a Lyapunov-stable controller +
a canonical VFH+ candidate masker. This doc specifies that.

## 2. Target architecture

```
Inputs: side ∈ {starboard, port, None}
        destination ∈ {(lat, lon), None}
        current pos (lat, lon, ψ)
        minimap perception (8-sector readings)
        │
        ▼
Waypoint Generator                              (NEW — small)
  Mode dispatch on (side, destination) presence:
    • side ✓, dest ✗     → shore-tangent target at d_star, ahead along tangent
    • side ✓, dest ✓     → shore-tangent target biased toward destination LOS
    • side ✗, dest ✓     → LOS target to destination, clipped to navigable water
    • side ✗, dest ✗     → refuse launch
  Hug-mode reuses existing `_lyapunov_state` math
  Output: waypoint (lat, lon) — single intermediate waypoint
        │
        ▼
Lyapunov Point-Pursuit Controller                 (~20 lines new)
  V = ||target − current||² + (ψ − ψ_d)²
  ψ_d = bearing(current_pos, waypoint)
  Output: desired_heading_deg
        │
        ▼
VFH+ Candidate Masking (canonical)                (mostly existing)
  Mask out sectors with imminent collision (§13.15 swept-volume predicts crash,
    OR nearest_dist < SAFETY)
  Among unmasked candidates, pick the sector whose bearing is closest to ψ_d
        │
        ▼
Action Executor                                    (unchanged)
```

What disappears: all of §13.16.* — Bug2 wall-ahead commit, Trémaux
default, centroid alignment, re-evaluation hysteresis, goal-direction
force-win override, history-inferred sequencer.

What stays:
- §13.15 swept-volume lookahead (provides the candidate-mask signal)
- Sector perception layer (mini-map → 8 sectors)
- Existing `_lyapunov_state` math (reused inside the goal-point
  generator for hug mode)
- LOS bearing math (used by the destination-mode branch and inside
  the point-pursuit controller)

## 3. Waypoint Generator — modes

### 3.1 Hug mode (side set, destination not set)

The bot's intent is "stay at d_star from the visible shore, parallel
to it." Existing `_lyapunov_state(nav, side)` returns `(d, d_star,
θ_err)` — the tangent direction is implicit in θ_err. The target
point is the projection of "where I should be one tick from now if
I were perfectly tracking":

```
tangent_direction = ψ + θ_err          (correct toward shore tangent)
lateral_correction = K · (d - d_star)  (correct toward d_star)
waypoint = current_pos +
  L · unit_vector(tangent_direction) +
  lateral_correction · unit_vector_perpendicular_toward_shore
```

`L` is a forward step (~one tick of cruise distance). This is the
classical wall-following-as-point-pursuit reduction.

### 3.2 Destination mode (destination set, side optional)

```
los_bearing = bearing(current_pos, destination)
projected_point = current_pos + L · unit_vector(los_bearing)
waypoint = clip_to_navigable_water(projected_point, minimap)
```

`clip_to_navigable_water` finds the farthest point along the LOS ray
that is inside the minimap's observed water region. If the ray hits
shore, the target is set to the nearest navigable water point along
the line. This bounds the lookahead to what's actually reachable.

### 3.3 Combined mode (both set)

The shore-tangent target (3.1) and the destination LOS (3.2) are
combined by weighted average, weight depending on how close the bot
is to its hug distance. When `d ≈ d_star` and `θ_err ≈ 0` (bot is
hugging well), heavy bias toward destination LOS — the destination
pulls the bot's path along the shore. When the bot has drifted from
the hug line, heavier bias toward shore-tangent — re-engage shore
first, then resume destination pursuit.

This is the Lekkas-Fossen pattern: cross-track error governs how
strongly the bot prioritises path tracking vs goal pursuit.

### 3.4 No-input mode (neither set)

Refuse launch. Forces operator clarity — every voyage must declare
either "hug this side" or "head to this destination" or both.

### 3.5 Per-tick target dynamics (rolling horizon)

The target is **recomputed every tick** by the Generator — it's not a
stored value advanced by a separate "switch to next waypoint" rule.
This is the canonical *rolling-horizon* / *virtual-leader* pattern
(MPC, pure-pursuit, Lekkas-Fossen ILOS).

Each tick:

```
1. Read minimap → 8-sector perception
2. Read HUD → current_pos (lat, lon, ψ)
3. target_t = Generator.compute(side, destination, current_pos_t, perception_t)
4. ψ_d_t   = bearing(current_pos_t, target_t)
5. VFH+ mask → pick sector closest to ψ_d_t
6. Execute action
7. Bot moves; next tick starts from new current_pos
```

Two sources of target motion between ticks:

- **(a) Bot's own motion.** Hug mode: `target = current_pos + L · unit(tangent)`,
  so target moves ~vdt forward each tick (matches bot velocity). Destination
  mode: same — target moves ~vdt toward destination. Smooth, monotonic.
- **(b) Perception change.** Shore tangent rotates as the bot's view shifts;
  new water visible past a landmass releases the destination-mode clip ray
  forward; near shore newly visible pulls the destination-mode clip back.
  These can be discontinuous — a single tick can shift the target by up to
  the disc radius.

**Stability implication.** A static-target Lyapunov function has
dV/dt < 0 monotonically. With a moving target, V can momentarily
increase when source (b) jumps the target forward. This is fine — the
bot catches up over 2-5 ticks and overall progress continues. The
classical bound (Lekkas-Fossen) is target velocity ≤ bot velocity,
which holds for source (a) by construction and is occasionally
violated by source (b) for one tick. **No special handling needed for
v1.** If live runs show pathology, the canonical remedy is target
rate-limiting (clip per-tick target motion to ≤ v_bot · dt) — ~5
lines, added later if needed.

**Arrival behaviour (destination mode).** When
`||current_pos − destination|| < ACCEPTANCE_RADIUS`, the destination
is reached: phase = COMPLETE, voyage returns to caller (matches
existing `HugShoreGoal` termination). Hug mode has no arrival — the
bot follows shore until `max_ticks` or operator interrupt.

## 4. The Lyapunov Controller — point stabilization

For waypoint `P_T` and bot pose `(x, y, ψ)`:

```
e_pos = P_T − (x, y)              (position error vector)
ψ_d   = atan2(e_pos.y, e_pos.x)   (desired heading: bearing to target)
ψ_err = wrap(ψ_d − ψ)              (heading error, signed [−π, π])

V = ||e_pos||² + ψ_err²
```

Control law in discrete 8-sector form:
```
desired_heading_deg = ψ_d          (the controller's output)
```

In our discrete sector setting, "minimise ψ_err" reduces to "pick
the sector whose bearing is closest to ψ_d." That's it. No internal
state, no hysteresis, no wall-ahead branch.

**Stability claim.** With ideal forward velocity and obstacle-free
space, dV/dt < 0 — bot reaches `P_T` with correct alignment. Proof
follows Brockett 1983 / Aicardi-Casalino-Bicchi 1995. Not reproduced.

The waypoint `P_T` updating each tick (from §3) is itself a
Lyapunov-stable process when the waypoint generator is itself
provably-decreasing — true in hug mode (existing wall-following
proof) and in destination mode (LOS reduces ||current − destination||
when followed).

## 5. VFH+ Candidate Masking (canonical exclusion, not cost-blending)

Canonical VFH+ (Ulrich & Borenstein 1998) does NOT trade off goal
vs obstacle in cost. Instead:

- Per-direction obstacle density forms a polar histogram
- Apply dual-threshold (Schmitt hysteresis): directions above
  `τ_high` are blocked, below `τ_low` are free, in-between retain
  previous state
- Only free directions are candidates
- Among free candidates, score by goal-alignment / smoothness
- Pick min-cost candidate

Mapping to our 8-sector setting:

```
for each candidate sector i:
  if collision_imminent(i):           # §13.15 swept-volume predicts crash
    mark blocked
  elif nearest_dist[i] < SAFETY:      # too close, raw veto
    mark blocked
  else:
    mark free

if any sector is free:
  pick free sector minimising angular_diff(bearing[i], ψ_d)
else:
  fall back to escape behaviour (see §6)
```

**No K_GOAL vs K_OBSTACLE tuning surface** — goal direction and
obstacle avoidance compose via exclusion, not weighted sum. This
restores the collision-avoidance property that the §13.16 `−1e6`
override broke, without introducing a new trade-off parameter.

## 6. Failure mode — local minima

Pure point-pursuit + obstacle masking can stall at U-shaped
obstacles (peninsula traps, dead-end coves). Two-phase remedy:

- **Phase A (this design)**: ship without the trap remedy. Most UWO
  voyages (river transit, open-water travel) don't encounter U-traps.
  If a trap is hit, the bot stops making goal-progress, the operator
  notices, and either repositions or sets a different destination.

- **Phase B (deferred)**: add Bug-style wall-following escape per
  TangentBug (Kamon-Rivlin-Rimon 1998) — when BLOCKED for N ticks
  AND `||current − P_T||` hasn't decreased, switch into wall-
  following mode until LOS to `P_T` is restored. The existing
  `_lyapunov_state` wall-follower already does this; only need to
  add the switching logic.

Phase B is a known follow-up, not blocking Phase A.

## 7. What gets deleted

In `brain/goals/hug_shore.py`:

- §13.16: `_wall_ahead_commit` state, entry/release logic in `tick()`
- §13.16.1: `_obstacle_centroid_relative_bearing`,
  `_bug2_commit_sector`, `CENTROID_ALIGNED_TOLERANCE_DEG`
- §13.16.2: re-evaluation block in `tick()`
- §13.16.3: `GOAL_HEADING_HYSTERESIS_DEG`, the goal-direction branch
  inside `_bug2_commit_sector`
- §13.16.4: `_infer_goal_heading_from_history`,
  `_update_inferred_goal_heading`, `_inferred_goal_heading_deg`,
  related constants
- §13.16.5: keep `_los_bearing_deg()` (consumed by the destination-
  mode branch of the waypoint generator); keep `destination_lat/lon`
  parameters; remove `_effective_goal_heading_deg()` priority chain

Net deletion: ~400 lines from `hug_shore.py`.

Added: ~80 lines (waypoint generator with 4 mode branches +
Lyapunov controller + updated candidate-mask logic).

## 8. Tests — what changes

Existing tests to delete or rewrite:
- `test_hug_shore_bug2_wall_ahead.py` — entire test file (Bug2 gone)
- `test_hug_shore_bug2_centroid.py` — entire test file
- `test_hug_shore_goal_heading.py` — rewrite as point-pursuit tests
- `test_hug_shore_goal_inference.py` — delete (sequencer gone)
- `test_hug_shore_los_destination.py` — keep, adapt

The two **known-limitation skips** in `test_hug_shore_regression.py`
(`river_channel_opens_right`, `astern_pull_beats_wall_follow_after_corner`)
should un-skip and pass naturally — narrow channels get
"shore-tangent target ahead along the channel + side sectors masked
out by close-shore safety," astern-pull gets "tangent target
slightly ahead-right after the corner."

New tests:
- `test_goal_point_generator.py` — for each of the 4 modes (§3),
  given inputs, assert the produced waypoint is in the expected
  region of state space
- `test_lyapunov_point_pursuit.py` — bot at various positions and
  headings relative to a target; assert the picked sector minimises
  `angular_diff(bearing, ψ_d)` among unmasked candidates
- `test_candidate_masking.py` — a sector with imminent collision
  must be masked out even if it best aligns with `ψ_d`; goal
  direction never overrides safety

## 9. Open design decisions

1. **SAFETY threshold value.** The cutoff for "nearest_dist too
   close, mask out." Current §13.15 uses `LOOKAHEAD_SAFETY_DEPTH ≈
   0.10` (10% of mini-map radius). Reuse this, or pick a separate
   sector-level threshold? Recommend reusing — single concept of
   "imminent" rather than two.

2. **Combined-mode weighting (§3.3).** The blend between shore-
   tangent target and destination LOS — explicit functional form
   (e.g., `weight = sigmoid(|d − d_star|)`) or just take a weighted
   average with fixed 0.5/0.5? Recommend starting with fixed 0.5/0.5
   and tuning after a live run if needed.

3. **clip_to_navigable_water implementation (§3.2).** What "water"
   means perceptually — the disc-shaped navigable mask we already
   compute, or per-sector reachability? Recommend disc-mask + ray
   trace: project LOS ray, find farthest point on ray inside the
   `water` mask we already build in
   `vision/minimap_navigation_view.py`.

4. **Lookahead step `L` (§3.1, §3.2).** Distance from current
   position to the projected target. Too small → controller
   over-reactive; too large → controller ignores recent
   perception. Recommend `L = 0.5 × max_r` (half the mini-map
   radius) so the target sits comfortably in observable space.

5. **What happens when minimap is unreadable.** Goal-point generator
   needs perception. If a tick has no minimap data, recommend
   "hold last waypoint for one tick" — short-term graceful
   degradation. After N missing-perception ticks, refuse to steer
   (`wait` action) until perception recovers.

6. **Existing `_lyapunov_state` reuse exactly as-is, or refactor.**
   Current `_lyapunov_state` returns `(d, d_star, θ_err)`. Hug-mode
   waypoint generator needs to project these to a waypoint —
   could be a thin wrapper, or inline the math. Recommend thin
   wrapper to keep responsibilities separated.

## 10. Migration plan

If approved:

1. Implement waypoint generator + Lyapunov point-pursuit + updated
   candidate-mask, gated behind a `driver_mode="point_pursuit"` flag
   — new behaviour, no regression risk to existing mode.
2. Add the three new test files.
3. Live-validate on the Cairo departure case with
   `side="port"`, no destination → pure hug mode, verify bot tracks
   river going north along east bank tangent.
4. Live-validate with `side="port"`, `destination=(31.2, 29.9)`
   → combined mode, verify bot heads toward Alexandria while still
   respecting hug-line.
5. Tune `L`, `SAFETY` thresholds based on live behaviour.
6. Switch `HUG_SHORE_DRIVER_DEFAULT` to `"point_pursuit"`.
7. Delete §13.16.* code and tests.
8. Un-skip the two known-limitation regression scenarios — they
   should pass naturally.

Stages 1–5 are reversible. Stages 6–8 are the commit point.

## 11. Sources

- Lekkas & Fossen 2014 — "Line-of-Sight Guidance for Path Following
  of Marine Vehicles" (cross-track-error blending pattern used in
  §3.3 combined mode)
- Brockett 1983 — "Asymptotic stability and feedback stabilization"
  (point-stabilization Lyapunov framework)
- Aicardi, Casalino, Bicchi 1995 — "Closed loop steering of unicycle-
  like vehicles via Lyapunov techniques" (point-pursuit specifically)
- Ulrich & Borenstein 1998 — VFH+ canonical candidate-masking via
  dual-threshold polar histogram (§5)
- Yamauchi 1997 — Frontier-based exploration (the
  `clip_to_navigable_water` pattern is a single-step frontier walk)
- Kamon, Rivlin & Rimon 1998 — TangentBug (point-pursuit ↔ wall-
  following composition for Phase B local-minimum escape)
