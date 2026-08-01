# Hug-shore predictive navigation pipeline

## Background

The original `HugShoreGoal` is a stateless single-tick VFH+ policy. Each
tick reads the 8-sector mini-map, computes per-sector costs over the
forward 5 candidates, and picks the lowest. State exists only in the
form of EMA-smoothed values (`rate_dps`, `wall_distance`) and a single
`last_chosen_sector` for VFH+ hysteresis.

Live runs over 2026-05-28 → 2026-05-29 surfaced failure modes that the
stateless policy can't resolve from a single tick alone:

| Failure mode | Why stateless fails |
|---|---|
| Open-water "donut" — bot spins right indefinitely in offshore mode | No memory; ideal=bow-target is a guess based on the hug-side, not on where shore was. |
| L→R→L oscillation after collision / overshoot | Each tick re-decides; no commitment to a direction. |
| Rotation-into-blind-spot collision (t78 of 2026-05-29_133115) | Bot turned right into shore that wasn't yet in radar view but was rotating into the bow. No predictive horizon. |
| Late wrong-side detection after overshoot (t114→t122 of 2026-05-29_145608) | Astern-opposite shore not visible to single-tick check; full-sector layout fixes the detection but doesn't address the 50% rotation overshoot itself. |
| Sailing-past-corner failure | Threshold-dependent astern-pull; needs to know "shore was on target side N ticks ago." |

These all share a structural cause: **the policy has incomplete
information** because it only sees the current tick. Fixes to date have
been patches to specific failure modes; a structural answer is to
introduce *memory* and *prediction* as first-class concepts.

## What we're building

A five-layer pipeline, each layer independently shippable, that
incrementally moves the bot from reactive single-tick decisions to
predictive multi-tick planning:

| Layer | What it gives | Builds on |
|---|---|---|
| **A. Multi-tick state memory** | Recent sector readings, headings, lat/lon, speed, actions | Just storage + derived signals |
| **B. Velocity & rotation rate from world frame** | "I'm moving at 5 m/s heading 90°, rotating at 60°/s" | A |
| **C. World-frame land map** | "Land was at ⟨sector S, dist 0.30⟩ at tick T-3; at current velocity it should now be at ⟨sector 0, dist 0.10⟩" | A + B |
| **D. Predictive collision horizon** | "If I keep this course, I'll be at land in 1.8 ticks" | C |
| **E. Blind taps** | Execute pre-computed corrections without waiting for the next perception cycle | D + calibrated rotation |

Each layer is testable in isolation. The system never goes fully
open-loop — when a perception update arrives, it overrides the
prediction; the prediction only fills the gap between perceptions.

## Phase A — multi-tick memory (concrete spec)

This phase adds **temporal state** to the goal. It is pure memory + a
small set of derived signals; the existing policy decisions are
unchanged except where they consume the new signals.

### Data structure

```python
@dataclass
class TickRecord:
    tick:            int
    wall_time:       float
    heading:         Optional[float]   # accepted heading (post-sanity check)
    raw_heading:     Optional[float]   # what the wedge returned
    rejected:        bool              # was the heading rejected
    lat:             Optional[float]
    lon:             Optional[float]
    speed_kt:        Optional[float]
    sectors:         tuple[SectorReading, ...]  # all 8
    commanded_deg:   float             # last tick's commanded angle
    actual_delta:    Optional[float]   # measured rotation that resulted
    phase:           HugPhase
    chosen_sector:   Optional[int]
    ideal_sector:    Optional[int]
    cost_dump:       dict[int, float]
```

Keep a `deque[TickRecord]` of size 10 on the goal. Each tick after
the policy runs, append the current observation.

### Derived signals (read off the deque)

These are pure functions of the history; the goal computes them once
per tick and exposes them as properties or a snapshot struct:

| Signal | How it's computed | Used by |
|---|---|---|
| `target_shore_last_seen_at_tick` | Most recent tick in deque where target-side sectors (1, 2, 3) had `frac >= SHORE_VISIBLE_FRAC` | "shore-just-lost" branch in `_ideal_sector` |
| `target_shore_last_seen_sector` | Which of (1, 2, 3) was loaded at that tick | Disambiguates whether shore drifted aft or off |
| `opposite_shore_last_seen_at_tick` | Symmetric on opp-side sectors (5, 6, 7) | Wrong-side detection refinement |
| `target_shore_trend` | Slope of target-side `max(frac)` over last 3 ticks | "Approaching" vs "receding" |
| `recent_rotation_direction` | Sign of cumulative heading-delta over last 3 ticks: `+1` right, `-1` left, `0` mixed | Same-direction commitment |
| `consecutive_same_direction_turns` | Run length of consecutive same-sign commanded turns | Cap right-turn search in offshore mode |
| `world_motion_bearing` | `atan2(Δlon, Δlat)` accumulated over rolling 5-tick window | Cross-check vs wedge heading; basis for Phase B |
| `mean_speed` | Median of last 5 readable `speed_kt` (outlier-resistant) | Phase B calibration; scaling thresholds |

### Policy integrations enabled by Phase A

Three concrete uses of the memory in `_ideal_sector`, each addressing
a known failure mode:

1. **Shore-just-lost recovery**

   ```python
   if not layout.shore_present:
       if state.target_shore_last_seen_at_tick is not None and \
          (current_tick - state.target_shore_last_seen_at_tick) <= 3:
           # Shore was just here, turn toward where it was
           return _AHEAD_TARGET_SECTOR[side]
   ```

   Fixes the open-water donut pattern: when shore vanished off the
   target side, turn target-ward to re-find it. When shore was
   *never* seen recently, default to sailing straight (Fix B from
   sim discussion).

2. **Same-direction commitment**

   ```python
   if state.consecutive_same_direction_turns >= 3 \
      and layout.phase == OFFSHORE \
      and not layout.shore_present:
       return 0  # force hold; the search pattern isn't working
   ```

   Fixes the rotation-into-blind-spot pattern at t78: bot was
   committing R+R+R rotations without verifying. Force a hold after
   3 consecutive turns without finding shore.

3. **Post-collision recovery commit**

   ```python
   # Detect collision: large rotation with small command
   if state.recent_rotation_direction != 0 \
      and abs(state.actual_rotation_last_n_ticks(3)) > 120 \
      and abs(state.commanded_in_last_n_ticks(3)) < 60:
       # Collision-induced rotation.  Force U-turn toward target side.
       return _AHEAD_TARGET_SECTOR[side]
   ```

   Fixes the post-collision recovery: when the bounce sends the ship
   far from its target heading, commit to bringing shore back to the
   target side rather than picking direction tick-by-tick.

### Risk and mitigations

- **Memory contamination from bad readings.** If a flipped heading or
  contaminated sector reading is recorded into history, it
  contaminates future decisions. Mitigation: only record headings
  that were accepted by the sanity check; for sectors, record what
  was observed (we still have it for analysis even if it's bad).
- **Threshold sensitivity.** Derived signals (e.g. "shore last
  seen") have implicit thresholds. Keep them aligned with the
  existing constants (`SHORE_VISIBLE_FRAC`, `WRONG_SIDE_TINY_FRAC`).
- **State-vs-policy coupling.** Care must be taken so that the
  Phase A state doesn't introduce a feedback loop with the policy's
  own decisions. The state should be derivable from observations
  alone, not from policy outputs (with a few intentional exceptions
  like rotation-direction commitment, which is explicitly a
  policy-state interaction).

## Phase B — velocity & rotation rate from world frame

### Inputs

Rolling 5-tick window of `(lat, lon, heading, commanded_deg,
actual_delta, wall_time)`.

### Outputs

- `world_velocity = (Δlat_per_sec, Δlon_per_sec)` — accumulated motion
  over the window divided by the window's wall-time span. Robust
  against single-tick noise.
- `world_bearing = atan2(Δlon, Δlat)` — direction of motion. Should
  match `heading` modulo wind/current.
- `rotation_rate_dps` — recomputed from `heading` deltas over the
  window. Replaces the per-tick EMA `goal.rate_dps`.
- `heading_bias = world_bearing - mean_heading` — systematic offset
  in the wedge detector. If consistently non-zero, subtract from
  reported heading before passing to the policy.

### Calibration thresholds

- **Window minimum**: require at least 5 ticks of constant-heading
  sailing to trust the world-bearing measurement. Tracked via
  `consecutive_same_direction_turns == 0` and `commanded_deg == 0`
  over the window.
- **Distance minimum**: require Δposition magnitude above the lat/lon
  quantization noise floor (`> 0.02°` per accumulated window).
- **Rate update**: only update `rotation_rate_dps` from explicit
  turn-commands; ignore drift between turns.

### What it fixes

The 50%+ overshoot at t114 of the 2026-05-29_145608 run. Commanded
−90°, actual −141° — rate was undercalibrated by ~33%. With
world-frame measurement, the calibration converges within a few clean
turns and the per-tick EMA stops chasing wedge noise.

## Phase C — world-frame land map

### Per-tick projection

For each tick T's observation:
1. Take each sector reading with `nearest_dist != None`.
2. Convert to ship-relative position: `bearing_rel = sector_idx ×
   45° + heading_world`.
3. Project to world-frame position: `land_world = ship_pos +
   (cos(bearing_rel) × dist_meters, sin(bearing_rel) × dist_meters)`
   where `dist_meters = nearest_dist × VIEW_RADIUS_METERS`.

### Aggregation

- Maintain a list of recent observations (last 10 ticks), each
  storing `(world_position, observed_tick, sector_seen_in)`.
- On a new tick:
  - For each existing observation, compute its expected
    ship-relative position now (given ship motion since the
    observation tick).
  - Match new observations to existing world-frame entries within
    a tolerance ball — these are the same land point seen again.
  - Update positions weighted by confidence (newer + closer = more
    confident).
  - Add unmatched new observations as fresh entries.
  - Drop entries older than 10 ticks.

### What it gives

A persistent ship-relative knowledge of recently-observed land that
extends beyond the radar view. Astern shore that drifted out of the
radar is still remembered for several ticks. The policy can query
"what's the closest land in my forward arc, even if it's outside
current view?" — answering it from the map, not from the current
radar.

## Phase D — predictive collision horizon

### Per-tick computation

For the current heading + speed:
1. Project ship's position over next N=5 ticks: `ship_future[T] =
   ship_pos + heading × speed × (T × tick_period)`.
2. For each land entry in the world-frame map, compute the closest
   approach distance to the projected trajectory.
3. Find the minimum closest-approach distance and which tick it
   occurs at.

### Output signals

- `time_to_nearest_land` (in ticks)
- `predicted_collision_arc` (which sectors will be loaded)
- `clearance_distance` (how close the predicted path comes)

### Policy use

- If `time_to_nearest_land <= 3` AND `clearance_distance <
  SAFE_CLEARANCE`, override `ideal_sector` to peel away from
  predicted contact.
- If `time_to_nearest_land <= 1` (about to hit), trigger Phase E
  blind tap.

### What it fixes

The t78-style collision where shore was outside radar view, then
appeared in front of the bow after rotation. With the persistent
map, the bot can see "land is 0.4 radar-radii away in the forward
arc; at current speed I'll be there in 1.5 ticks" and react before
the radar shows it.

## Phase E — blind taps

### When to fire

Only when:
1. Phase D predicts `time_to_nearest_land < perception_cycle_ticks`
2. Calibration is fresh (recent turn commands matched expectations
   within tolerance)
3. We're not in a phase where the user has explicitly disabled
   predictive control (debug, manual override, etc.)

### How

- Compute required deflection: enough rotation to clear the predicted
  collision with a safety margin.
- Compute hold-duration: `required_deg / rotation_rate_dps × 1000`.
- Execute via `actions.sea_actions.hold_left` / `hold_right`
  directly, not through the cost function.
- Record the blind tap in the tick record with a flag so the next
  perception cycle can reconcile.

### Post-tap reconciliation

When the next perception update arrives:
1. Compare actual heading delta to expected (predicted).
2. If off by > tolerance, mark calibration as stale (forces a
   re-calibration window before next blind tap allowed).
3. Compare actual nearest-land to predicted; if very different,
   the land map is stale → forget far-history entries.

### Safety: when blind taps are disabled

- After any rejected heading reading (recent perception noise)
- After any collision-bounce detected in Phase A
- When `rotation_rate_dps` has been updated within last 2 ticks
  by more than 30% (rate is unstable)

## Phased delivery plan

Each phase is independently shippable, with its own regression tests
and a clear before/after live-run gate.

1. **Phase A (memory + 3 policy integrations)** — ~150 lines + ~30 lines tests.
   Live test: open-water donut prevention, post-collision recovery,
   shore-just-lost handling.

2. **Phase B (velocity + rotation calibration)** — ~80 lines on top of A.
   Live test: overshoot reduction on commanded turns.

3. **Phase C (world-frame land map)** — ~120 lines on top of A + B.
   Live test: rotation-into-blind-spot prevention.

4. **Phase D (predictive horizon)** — ~80 lines on top of C.
   Live test: early-warning peel-away on collision-course.

5. **Phase E (blind taps)** — ~100 lines on top of D.
   Live test: safe operation at fast-ship speeds where perception
   cycle is too slow for purely reactive avoidance.

## Open questions

- **Land-map decay rate.** How fast should we forget old observations?
  Trade-off: too fast = lose useful info, too slow = stale data from
  game-state changes (NPC movement, weather, dawn/dusk).

- **Calibration freshness window.** How "recent" must a calibration
  be for blind taps to be safe? Probably 5-10 ticks, but depends on
  voyage variability.

- **Fast-ship per-tick travel.** At 27 kt the ship covers ~0.013
  world-units per tick. At what tick period does perception become
  too slow for pure reactivity? Phase D should answer this empirically.

- **Interaction with the 26 existing regression scenarios.** Phase A's
  `TickRecord` deque starts empty; the derived signals should all
  fall back gracefully when history is short, so existing scenarios
  (single-tick test cases) continue to pass identically.

## Status

- **2026-05-29**: design captured (this doc), Phase A implementation
  starting.
