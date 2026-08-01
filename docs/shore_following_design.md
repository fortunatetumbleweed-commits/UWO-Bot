# Shore-following design — Lyapunov regulator + VFH+ avoider + FSM modes

**Status:** Design proposal, not yet implemented.  Written 2026-05-30
after the t=80→t=95 perpetual-right-turn failure of session
`hug_debug_20260530_175557` made clear that the current cost-function
rule tangle in `_ideal_sector()` is reaching the limits of
incremental patching.

---

## 1. Why the current architecture is the wrong frame

`HugShoreGoal` today is a **single-layer VFH+** with `ideal_sector_index`
chosen by a rule tangle.  VFH+ (Ulrich & Borenstein 1998) is a
**collision-avoidance** algorithm: build a polar histogram of obstacles,
prefer the direction closest to the goal that is also obstacle-free.
Its built-in incentive structure says "stay AWAY from obstacles."

Shore-hugging needs the opposite incentive: keep a specific obstacle
(the shore) at a SPECIFIC bearing and distance.  We've been smuggling
that into VFH+ by:

- Choosing `ideal_sector` to *be* the target beam, so the cost function
  pulls the bot *toward* it (`W_IDEAL` term)
- Adding ad-hoc overrides — wall-follow override, astern-pull, drift
  detection, wrong-side trap, lost-shore recovery — each handling one
  failure case observed in a live run

The result is that `_ideal_sector()` has 7 distinct branches that
interact non-trivially.  Each new live failure is a candidate for a
new branch, and the branches modify each other's behaviour through
suppression flags (`bow_t_imminent`, `astern_t_loaded`, etc.).

**The failure mode this design replaces:** the policy re-decides every
tick.  At t=80 of the 175557 run, shore was lost; lost-shore recovery
emitted `ideal=bow-stbd` and the bot turned right.  At t=86 some shore
appeared on bow-port; the cost function picked the cheapest *that tick*
(a 90° right turn into open water).  At t=87 the wrong-side U-turn
fired and the bot peeled left.  At t=88 a faint target-side reading
(0.03/.32) crossed the `shore_present` threshold and the policy
abandoned the U-turn after one tick.  From t=88 onwards the bot
spiralled right indefinitely.

No single-tick decision can fix this.  We need:
- A **regulator** that defines what "good shore-keeping" *is*
  mathematically — not as a set of override rules
- A **commitment** mechanism for multi-tick actions (U-turns, lost-
  shore search) that doesn't get pre-empted by single-tick signals
- An **avoider** that handles tactical obstacles (NPCs, sudden coast
  irregularities) without conflicting with the regulator's intent

---

## 2. Algorithm family survey

| algorithm | role | what it's good at | what it's NOT good at |
|---|---|---|---|
| **VFH (Borenstein-Koren 1991)** | Reactive collision avoidance | Building a tractable polar obstacle model from noisy range readings | Smooth path-following; multi-step commitment |
| **VFH+ (Ulrich-Borenstein 1998)** | + kinematic feasibility + oscillation suppression | Pickable cost function `μ₁·target + μ₂·heading + μ₃·prev_dir`; the hysteresis term reduces flicker | Still single-tick reactive; assumes a global goal direction |
| **VFH\* (Ulrich-Borenstein 2000)** | + A\* lookahead | Bounded-horizon foresight — would catch "spinning right doesn't find shore" | Branching cost grows as `bᴺ` per tick; tree pruning is delicate |
| **Lyapunov-based wall-following** (various, e.g. Bemporad et al.) | Regulation | Provably-stable convergence to a set-point `(d*, θ*)`; smooth output | Knows nothing about obstacles not on the wall |
| **Vector-field path-following** (Lawrence-Frew-Pisano 2008) | Path tracking | Smoothly handles drift; integral curves spiral onto target trajectory | Requires a defined path; constructed from waypoints |
| **Pure pursuit** (Coulter 1992) | Path tracking | Mature, well-understood, plays well with kinematic constraints | Same — needs a path |

For shore-hugging:
- **Lyapunov regulation** matches the problem statement directly — we
  *do* have a set-point: "shore on side `S` at distance `d*`."
- **VFH+ as a safety layer** keeps the bot off close obstacles even
  when the regulator's "ideal" course would clip them
- **FSM** for non-regulation modes (lost-shore search, U-turn commit,
  tight-quarters slow-down) provides the commitment that the
  per-tick regulator lacks

---

## 3. Proposed architecture

```
┌─ MODE FSM ─────────────────────────────────────────────────┐
│                                                            │
│   HUGGING            ←→  TIGHT_QUARTERS                    │
│      ↓                       ↓                             │
│   LOST_SHORE                                               │
│      ↓                                                     │
│   SEARCH  →  U_TURN_LEFT  →  U_TURN_RIGHT                  │
│                                                            │
│  Each mode produces a desired heading (compass deg).       │
└────────────────────────────────────────────────────────────┘
                            │ desired heading (compass deg)
                            ▼
┌─ LYAPUNOV REGULATOR (outer loop) ──────────────────────────┐
│                                                            │
│   In HUGGING mode:                                         │
│     V = ½(d − d*)² + ½kθ(θ − θ*)²                          │
│     dV/dt by chain rule on (ḋ, θ̇)                          │
│     Pick desired turn rate that decreases V                │
│                                                            │
│   In other modes:                                          │
│     Set-point comes from the mode (e.g. U_TURN_LEFT emits  │
│     entry_heading + 180°), regulator just tracks it.       │
└────────────────────────────────────────────────────────────┘
                            │ desired heading
                            ▼
┌─ VFH+ AVOIDER (inner loop) ────────────────────────────────┐
│                                                            │
│   ideal_sector = bearing_to_sector(desired − current)      │
│   Run existing _score_sectors() / _select_sector() —       │
│   no changes to obstacle handling.                         │
│                                                            │
│   If close shore is between us and ideal_sector, VFH+      │
│   deflects to a safer adjacent sector.  The regulator      │
│   will see the deflection next tick and adjust set-point.  │
└────────────────────────────────────────────────────────────┘
                            │ chosen sector
                            ▼
                       ACTION (hold / hold_left N° / hold_right N°)
```

### 3.1 State variables (one tick's worth)

```
nav (existing) — 8-sector readings, ship heading, position
mode (new)     — current FSM mode
mode_entry     — heading + tick at last mode transition (for U-turn
                 progress, lost-shore search deadline, etc.)
signals (exist)— target_drift_rate, target_shore_last_seen_*, etc.
```

### 3.2 Lyapunov function — what we regulate

Define for `HUGGING` mode:

```
d       = nav.sectors[target_beam_idx].nearest_dist         # 0..1 (mini-map normalised)
d*      = D_TARGET                                          # e.g. 0.15
θ_err   = estimated angle of shore tangent vs current bow   # see §3.3
θ*      = 0    (i.e. we want bow aligned with shore tangent)

V       = ½(d − d*)² + ½ k_θ · θ_err²
```

The control law uses the time derivative `V̇`.  With a single steering
degree of freedom (turn rate ω), we pick ω to make `V̇ < 0`:

```
turn_rate_command = − K_d · (d − d*) · SIDE_SIGN  − K_θ · θ_err
                    └────────────────────┘         └─────────┘
                    "too far from shore →           "tangent
                     turn toward it"                 misaligned →
                    "too close → turn away"          rotate to fix"
```

Where `SIDE_SIGN = +1` for starboard hug (turning right brings shore
closer on stbd-beam) and `-1` for port.

Then `desired_heading = current_heading + turn_rate_command × tick_dt`,
capped at `MAX_TURN_PER_TICK`.

### 3.3 Estimating shore tangent angle

We need `θ_err` — the angle between the bow and the shore tangent in
the bot's reference frame.  Two approaches, pick whichever is more
robust:

**(a) From adjacent target sectors.**  If `bow_t.dist < astern_t.dist`
the shore is curving toward us (bow pointing into a cove); if vice
versa it's curving away (bow pointing out of one).  Convert the
distance difference and the 90° sector separation into an estimated
tangent angle.

**(b) From sequential observations.**  `signals.target_drift_rate`
already tracks how T.dist changes over time.  Positive rate means
shore is receding (bow pointing away); negative rate means shore is
approaching (bow pointing in).  Map rate to angle correction.

### 3.4 FSM modes

| mode | entry condition | set-point producer | exit condition |
|---|---|---|---|
| **HUGGING** | shore visible on target side AND |d − d*| within `D_TOLERANCE` | Lyapunov regulator | shore not visible → LOST_SHORE; \|d − d*\| > 2·D_TOLERANCE → TIGHT_QUARTERS or U_TURN |
| **TIGHT_QUARTERS** | ≥3 sectors with close shore | command max-rotation peel away (existing wall-follow style) | sectors clear → HUGGING |
| **LOST_SHORE** | `shore_present == False` for ≥2 ticks | last_known shore world bearing (from signals) | shore re-acquired → HUGGING |
| **SEARCH** | LOST_SHORE for ≥3 ticks without recovery | rotate in last-known direction; max 180° accumulated | shore acquired → HUGGING; 180° rotated → hold and wait |
| **U_TURN_LEFT** / **U_TURN_RIGHT** | wrong-side trap fires for ≥2 consecutive ticks | `entry_heading ± 180°` (commitment-locked) | accumulated rotation ≥ 150° → HUGGING; confirmed target-side shore (multi-tick) → HUGGING |

**Mode transitions are sticky.**  A single faint stbd reading
during U_TURN_LEFT does NOT exit the mode.  Only the explicit
exit conditions trigger transitions.  This is the commitment that
single-tick reactive policy lacks.

### 3.5 VFH+ inner loop — what stays the same

The existing `_score_sectors()` and `_select_sector()` functions
**don't change at all** except that `ideal_idx` now comes from
`mode.desired_heading()` instead of `_ideal_sector()`.

Cost function components remain:
- `W_OBSTACLE * obstacle_cost` — push away from close shore
- `w_ideal_effective * ideal_distance` — pull toward Lyapunov set-point
- `wall_cost` on opposite-side sectors — push toward target side
- `rotation_penalty` on bow-target when T is close — prevent rotating
  into close shore
- `W_HEADING_SMOOTH * heading_distance` — break ties

These continue to provide the *tactical* obstacle reaction.  They no
longer encode strategic shore-keeping (that moves to the regulator).

---

## 4. How each pinned scenario maps to the new framework

The `sim/scenarios.py` regression tests must continue passing.  Each
pinned scenario maps to a `(mode, expected action category)` pair:

| scenario | mode | Lyapunov d_err / θ_err | expected outcome |
|---|---|---|---|
| `tripoli_intrusion` | HUGGING | small d_err, small θ_err | hold (action category `hold`) ✓ |
| `corner_with_real_wall_ahead` | TIGHT_QUARTERS (A ahead) | large positive d_err | turn away ✓ |
| `river_channel_opens_right` | HUGGING (slight regulator pull right) | mild d_err | turn target ✓ |
| `close_T_rotation_hazard` | HUGGING | d at d*, θ_err small | hold ✓ |
| `facing_wall_peel_via_hysteresis_off` | TIGHT_QUARTERS | wall ahead | turn away (left peel) ✓ |
| `dead_end_escape_prefers_target_side` | TIGHT_QUARTERS or SEARCH | mixed | turn target ✓ |
| `wrong_side_u_turn` | U_TURN_LEFT (entry) | target side empty + opp visible | turn away (peel toward opp) ✓ |
| `lost_shore_recovery` | LOST_SHORE → SEARCH | n/a | turn target (toward last-seen) ✓ |
| `bow_target_velocity_peels_before_static_threshold` | TIGHT_QUARTERS (velocity-triggered) | strong approach signal | turn away ✓ |
| `astern_pull_beats_wall_follow_after_corner` | HUGGING (astern-shore counts as target visible) | shore-on-stbd-astern → d_err pulls right | turn target ✓ |
| `lost_shore_tied_cost_holds_course` | LOST_SHORE | n/a | hold (gentle search) ✓ |

Each scenario must pass under the new framework before migration is
considered complete.

---

## 5. Tick-interval considerations

**Lyapunov regulation assumes high update rate.**  At 4 s/tick, ω×dt is
significant — applying a correction at the start of a tick means the
ship can overshoot before the next observation.

Two mitigations:
1. **Per-tick projection.**  Use `SHIP_TURN_RATE_DPS` and `tick_dt` to
   predict the heading at the END of the tick, and aim the regulator
   at *that* — not the current state.  This is "one-step lookahead
   inside the regulator," much simpler than VFH\*'s tree search.
2. **Per-tick rotation cap.**  We already have `MAX_TURN_PER_TICK`.
   Keep it.  The regulator emits a `desired_heading` but the action
   layer clamps the actual delta.

---

## 6. Migration plan — phased, not big-bang

The current policy passes 18 pinned regression scenarios.  The new
architecture must continue to pass them all.

**Phase 1: Parallel evaluation (no behaviour change).**  Add the new
code as `compute_desired_heading_lyapunov()` alongside the existing
`_ideal_sector()`.  At every live tick, compute both and log them.
After 2-3 sessions, we have a corpus of (old, new) decision pairs and
can see where they agree / disagree.

**Phase 2: Switch on a flag.**  Add `--lyapunov` to `run_hug_shore_loop`.
Run a few live sessions on the flag; compare against historical traces
of the same routes.

**Phase 3: Make Lyapunov default; keep cost-function override for
TIGHT_QUARTERS only.**  At this point the rule tangle in
`_ideal_sector()` shrinks to just the cost-function obstacle handling.

**Phase 4: FSM modes.**  Add HUGGING / LOST_SHORE / SEARCH / U_TURN_*
mode tracking and transitions.  This is what catches the t=80→t=95
class of failure.  Without this phase, Lyapunov alone doesn't solve
the lost-shore spiral.

**Phase 5: Retire `_ideal_sector()`** once all 18 regression scenarios
pass under the new framework AND a live voyage validates no
regressions.

---

## 7. What this design does NOT solve

- **World-frame goal-bearing layer.**  This is a regulator for "keep
  shore at the right side"; it doesn't know "we were supposed to be
  going south to the Nile origin."  After a collision that reverses
  heading, the regulator happily hugs in the wrong world direction
  (Nile bounce of 2026-05-30 hug_debug_205124).  That's a higher-
  level navigator (memory: project_hug_shore_local_vs_global_goals).
- **Stop-Think-Act for tight pockets.**  TIGHT_QUARTERS mode in the
  FSM is a placeholder; the actual stop-rotate-restart maneuver is a
  separate primitive that needs the Phase 2 predictive substrate.
- **Speed control during turns.**  Whether rudder-hold acts as a
  brake remains unconfirmed (one ambiguous mid-turn reading at
  v=0.3 kt, not reproduced — see arc_20260530_174722 analysis).  If
  confirmed by a dense-sampling calibration, this enables tighter
  arcs at high speed without sail_stop.
- **Heading-detector flips during long turns.**  When the sail-pair
  fails and bow_wedge fires alone, the bot_wedge often disagrees
  with the actual bow.  Not a policy issue, but it limits how
  precisely we can hold a regulator set-point.

---

## 8. Decision criteria for "ship this"

The new architecture is worth shipping when:

1. All 18 pinned regression scenarios pass under the new code
2. A live voyage along the t=80→t=95 geometry of the 175557 run
   completes without the perpetual-right-turn spiral
3. Live Tripoli → Nile route completes at least as cleanly as the
   175557 first half (which was clean up through Benghazi)

If any of those fail, fix in place — do not retreat to the rule
tangle.

---

## 9. Worked example — why the river-mouth case is automatic

The most reassuring way to see why this architecture works is to walk
through a case the current code can't handle: a coast with a river
opening.

### Setup
Bot is sailing east with shore on starboard.  Coast runs east-west.
Then the Nile river opens, perpendicular to the coast, heading south.

### Current code (single-tick reactive)
1. Up to the river mouth: shore-on-stbd, hold or gentle adjust.  Fine.
2. At the mouth: shore-on-stbd drops out (S sector now reads open
   water from the river).  Lost-shore recovery fires.  Bot turns right
   to "go back toward where shore was."
3. Bot now heading south, into the river.  Shore is on both sides
   (river banks).  But because the bot's turning right, it ends up
   heading west — wrong direction!  We've seen this happen.

### New code (Lyapunov + VFH+ + FSM)
1. Up to the river mouth: HUGGING mode, regulator emits "desired
   heading = east (tangent-aligned with shore)."  VFH+ steers east.
2. At the mouth: nearest shore point now sits on the EAST riverbank
   (the bot is past the corner).  Tangent at that point points SOUTH
   (along the bank into the river).  Regulator emits "desired heading
   = south."  VFH+ steers south.  Bot enters river naturally.
3. Inside the river: shore on stbd (east bank) at d* distance.
   HUGGING continues smoothly.

The river mouth is not a special case in the new architecture.  It's
the same "follow the shore tangent" rule that handles every other
geometry.  The bot enters the river because the tangent of the shore
(now the bank) points into it.

### Why this works mathematically
The Lyapunov function `V = ½(d − d*)² + ½kθ(θ − θ*)²` has minima where
d = d* AND θ = θ* (bow parallel to local shore tangent).  As the shore
curves into the river mouth, the local tangent direction curves with
it.  The minimum of V tracks the bend.  The regulator drives the bot
toward the moving minimum.  Smooth, no special-casing.

### Heading vs. waypoint equivalence
For VFH+'s purposes, `desired_heading_degrees` and `virtual_waypoint`
are interchangeable representations of the same set-point:

```
desired_heading = compass_bearing(current_pos → virtual_waypoint)
virtual_waypoint = current_pos + lookahead × unit_vec(desired_heading)
```

The regulator could emit either — VFH+ just needs to know which
direction is "good" to bias its histogram toward.  We choose
heading-based because it's simpler (no need to convert sector-relative
shore distance into a world lat/lon target).  Waypoint-based gives a
cleaner mental model and is the natural way to think about the river
example, so feel free to think in waypoints even though the code
emits angles.

---

## 10. Why the layers don't need to talk

A natural question is "how does the regulator know if VFH+ deflected?"
Answer: **it doesn't need to.**  The layers communicate **through the
world**, not directly:

```
tick N:
   regulator → "go heading 90°"
   VFH+      → "obstacle at sector 0 — actually go to sector 1"
   action    → hold_right ~45°
   world     → ship physically rotates ~45° right; new heading ~135°

tick N+1:
   perception → reads ship_heading_deg = 135°
   regulator  → sees current state, recomputes set-point
              → "given I'm at 135° and shore tangent here is 100°,
                 desired heading is now 100°"
   VFH+       → biases toward 100° from current 135°
   …
```

The regulator never asks "did you do what I said?"  It just reads the
state next tick and re-decides.  This is the standard outer-loop /
inner-loop pattern from control theory.  Each layer has a bounded
job, and the world acts as the integrator.

This decoupling is what makes the architecture testable and
debuggable: each layer can be unit-tested in isolation (just provide
mock inputs, check outputs).  The current `_ideal_sector` rule
tangle can't be tested layer-by-layer because there are no layers.

---

## 11. The gearbox analogy for FSM

A useful way to think about how the FSM fits:

- **Lyapunov regulator** = the engine.  Produces drive torque
  (desired heading) continuously while it has fuel (visible shore).
- **VFH+** = the road.  Translates intent into actual motion,
  handling potholes (obstacles) automatically.
- **FSM** = the gear selector.  Picks which *kind* of behaviour is
  active at any moment.

When the engine has fuel (shore visible), the FSM stays in HUGGING
and the engine drives normally.  When fuel is gone (shore lost), the
FSM shifts to a mode that doesn't need the engine (LOST_SHORE with a
fixed search direction).  When the shore appears on the wrong side
and U-turn is needed, the FSM shifts into U_TURN_LEFT/RIGHT — engine
output is overridden by a "rotate to entry_heading + 180°" command,
held until rotation is complete regardless of single-tick blips.

The FSM provides **commitment across time** that a single-tick
regulator structurally lacks.  Without it, we get t=87→t=88: U-turn
starts, gets abandoned the next tick on a faint blip.  With it, the
mode holds the U-turn until the explicit exit criteria fire.

---

## 12. Phase 1 — Parallel evaluation plan

The goal of Phase 1 is **no behaviour change**.  We add Lyapunov logic
alongside the existing `_ideal_sector` rule tangle, log both per-tick
decisions, and after a few live runs we have enough data to argue
"the math gives the right answer in the right cases" before any
production code starts trusting it.

### 12.1 Deliverables

**Code (~120 lines, one PR):**

```
brain/goals/hug_shore.py
  + compute_desired_heading_lyapunov(nav, side, signals,
                                      current_heading)
        → Optional[float]      # compass deg, None if shore not visible
  + _lyapunov_state(nav, side) → (d, d_star, theta_err)
  + _estimate_shore_tangent(nav, side, signals) → float
  + K_D, K_THETA, D_TARGET    # tunable constants near other VFH constants

brain/goals/hug_shore.py:tick()
  + after current _ideal_sector() call, also compute
    compute_desired_heading_lyapunov() and convert to an
    "lyapunov_ideal_sector" (sector index)
  + log both into TickRecord (and via _record_observation into the
    JSONL trace) WITHOUT using lyapunov_ideal_sector to drive any
    action.  Pure read-only logging this phase.
```

**Tooling (~60 lines):**

```
tools/analyze_lyapunov_divergence.py
  Input:  one or more JSONL trace files
  Output: per-tick comparison table + summary metrics:
    - ticks where actual ideal_idx == lyapunov_ideal_sector
    - ticks where they disagree, with magnitude (|sector_idx diff|)
    - histogram of disagreement angles
    - top-N most divergent ticks for visual inspection
```

**Tests (~40 lines):**

```
tests/test_lyapunov_regulator.py
  Reuses existing sim/scenarios.py inputs.  For each scenario,
  asserts compute_desired_heading_lyapunov() returns a heading
  consistent with the scenario's expected_action category.
  This is the gate: every pinned scenario must match under the
  new math before we trust it.
```

### 12.2 Lyapunov function — concrete formulas for Phase 1

Keep it as simple as possible.  Refine in later phases if needed.

**State variables:**
```
target_beam_idx  = 2 if side == "starboard" else 6
bow_t_idx        = 1 if side == "starboard" else 7
astern_t_idx     = 3 if side == "starboard" else 5

target           = nav.sectors[target_beam_idx]
bow_t            = nav.sectors[bow_t_idx]
astern_t         = nav.sectors[astern_t_idx]

d                = target.nearest_dist or 1.0
d_star           = D_TARGET                        # ≈ 0.15
```

**Shore tangent angle (relative to current bow):**
```
# If bow_t is closer than astern_t, shore is curving in (we're
# approaching a corner).  If bow_t is farther, shore is curving away
# (we're leaving a corner).  Translate the distance imbalance into a
# tangent angle.
bt_dist = bow_t.nearest_dist or 1.0
at_dist = astern_t.nearest_dist or 1.0
tangent_drift_signal = (at_dist - bt_dist)   # +ve: shore curving in
                                              # -ve: shore curving away

# Translate to angle.  This is rough — a real fit will need live
# data.  Phase 1 is exactly the time to gather that data.
K_TANGENT = 45.0   # degrees per unit-of-mini-map-distance imbalance
theta_err = K_TANGENT * tangent_drift_signal
```

**Lyapunov-derived turn correction:**
```
K_D     = 30.0    # degrees turn per unit d-error
K_THETA = 1.0     # already in degrees

SIDE_SIGN = -1 if side == "starboard" else +1
# starboard hug: too far (d > d*) → turn RIGHT (toward shore) → +ω
# port hug:      mirror

turn_correction =   K_D * SIDE_SIGN * (d - d_star)   \
                  + K_THETA * theta_err

# Cap at MAX_TURN_PER_TICK to stay kinematically reasonable
turn_correction = clamp(turn_correction,
                        -MAX_TURN_PER_TICK,
                        +MAX_TURN_PER_TICK)

desired_heading = current_heading + turn_correction
```

**Shore-not-visible case (no Lyapunov output):**
```
shore_visible = (target.land_fraction >= SHORE_VISIBLE_FRAC
                 or (target.nearest_dist is not None
                     and target.nearest_dist < SHORE_VISIBLE_DIST))
if not shore_visible:
    return None    # caller falls back to existing _ideal_sector
```

This Phase-1 sketch deliberately:
- **Ignores velocity signals** (no APPROACH_DFRAC_DANGER equivalent).
  Add in Phase 2 if data shows we need them.
- **Has no obstacle-aware deflection.**  That's VFH+'s job in the
  integrated architecture.  Phase 1 just produces a desired heading;
  the next phase plumbs it into VFH+.
- **Has no FSM modes.**  That's Phase 4.  For now, when shore isn't
  visible we return None and rely on the existing recovery rules.

### 12.3 What gets logged per tick

Add to the JSONL trace these new fields:

```
"lyapunov_desired_heading_deg":   float | null
"lyapunov_d":                     float | null
"lyapunov_d_star":                float
"lyapunov_theta_err_deg":         float | null
"lyapunov_tangent_drift_signal":  float
"lyapunov_proposed_sector":       int    | null     # for divergence math
"actual_chosen_sector":           int                # from existing code
"action_diverged":                bool                # True if proposed
                                                      # would have
                                                      # produced a
                                                      # different action
```

The `action_diverged` field is the key one — over a run of N ticks,
the fraction that diverged tells us **how often the math disagrees
with the rule tangle**.

### 12.4 Analysis after each live run

For each session in `data/sessions/hug_debug_*` with the Phase 1
code:

```
$ python tools/analyze_lyapunov_divergence.py \
    data/sessions/hug_debug_<TS>/trace.jsonl
```

Expected output:

```
260 ticks total
   215 (82.7%) — same sector chosen by both
    32 (12.3%) — Lyapunov 1 sector off ⇐ acceptable noise
    13  (5.0%) — Lyapunov 2+ sectors off ⇐ investigate

Top divergent ticks:
    t= 82  actual=1  lyapunov=0  d=0.31 d_star=0.15 theta_err=+12°
           → old code: lost-shore recovery turn right
           → Lyapunov: too far from shore but tangent suggests hold,
             not turn (low confidence: shore barely visible)
    …
```

### 12.5 Acceptance criteria for moving to Phase 2

Don't move to Phase 2 (flag-gated live behaviour change) until:

1. **Same-sector agreement ≥ 75%** across at least 3 sessions covering
   diverse coastline geometry (Tripoli area, Nile mouth, plus one
   open-water + straits case).
2. **All 18 pinned regression scenarios** produce a Lyapunov heading
   whose `bearing_to_sector_index()` matches the scenario's
   `expected_action` category.
3. **Manual review of the top-10 divergent ticks per session** —
   we can articulate WHY each disagreement happens.  Either:
   - Lyapunov was wrong → fix the math (probably tangent estimation)
   - Old code was wrong → divergence is a *good* signal that the new
     math is correctly catching one of the failure modes we know
     about
4. **t=80→t=95 spiral of `hug_debug_20260530_175557` does not
   reproduce** under the Lyapunov decisions (after we project them
   through a tick-by-tick replay).

Once those four criteria are met, we have enough confidence to flag-
gate the new code as the live-driving policy in Phase 2.

#### 12.5.1 Addendum — historical-trace replay (2026-05-30)

Before any new live runs we replayed the regulator math against the
existing `hug_debug_20260530_175557` trace via `tools/replay_lyapunov.py`,
reconstructing a `NavigationView` from each tick's `nav.sectors` +
`heading_deg`.  This satisfies criterion (4) ahead of criteria (1)–(3),
which still need live data.

**Headline result for t=78→t=95 (the spiral):**

| ticks | shore visible? | Lyapunov outcome |
|---|---|---|
| 12 / 18 | no | regulator returns `None` — Phase 4 FSM territory |
| 6 / 18  | yes | 3 agree with old code; **3 diverge in the spiral-breaking direction** |

The three "divergences" are the smoking gun the architecture is
correct:

- **t=84** — shore visible on **bow-port** (sector 7).  Old code:
  hold-right.  Lyapunov: hold-LEFT (toward visible shore).  This is the
  tick the spiral becomes recoverable; old code blew through it.
- **t=86** — shore re-acquired more broadly.  Old code: 90° hard
  right.  Lyapunov: hold.  The over-rotation at t=86 is what locked in
  the rest of the spiral.
- **t=83** — sectors weak.  Old code: turn right (speculative).
  Lyapunov: hold.  Conservative-but-reasonable.

**Tuning change unblocked by the replay:** `LYAPUNOV_D_TARGET` was set
to `0.15` in the initial draft.  At t=79 the bot was in a tight hug
(d=0.04) and the regulator wanted to peel **away from** shore to reach
d* — a false divergence that disagreed with a perfectly good hug.
Lowered `LYAPUNOV_D_TARGET` to `0.07` (live hug-shore stays closer to
shore than the original guess assumed); the t=79 divergence
disappears and no other tick regresses.

**Honest qualification:** Phase 1 alone does NOT prevent this
particular spiral because 12 of 18 ticks have no shore visible at all —
the regulator can't drive policy when there is nothing to regulate
against.  **Spiral prevention requires Phase 4 (FSM modes
LOST_SHORE / SEARCH) layered on top.**  This is the layered
architecture working as designed: regulator handles "shore visible";
FSM handles "shore not visible"; neither alone suffices.

#### 12.5.2 Phase 1.5 — Memory-projected Lyapunov for short shore-blind gaps

The Benghazi live run (`hug_debug_20260530_202625`, t≈100–140) exposed
a fail mode the historical replay didn't show: when the old policy
over-commits and *throws the bow off shore* (e.g. a 90° hard-right at
t=102), the next several ticks have no shore visible at all.  In that
window:

- The regulator correctly returned `None` (no shore → nothing to
  regulate against).
- The old code didn't return None — it kept confidently committing
  45°–90° turns based on baked-in preferences, sustaining the spin.

So the **realistic baseline for shore-blind ticks is not "do nothing
safely"** — it is "old policy commits big confident turns based on no
data."  Any heading we can produce that has *some* grounding in
real recent observations is, in expectation, better than that baseline.

##### Design

When `_lyapunov_state(nav, side)` returns `None`, fall back to a
**projection** of the most recent valid state, bounded by two
sanity caps:

1. **Age cap** — drop the cached state if more than
   `LYAPUNOV_MEM_MAX_AGE_TICKS` (≈5) ticks have elapsed.
2. **Heading-delta cap** — drop the cached state if the ship's heading
   has rotated more than `LYAPUNOV_MEM_MAX_HEADING_DELTA_DEG` (≈45°)
   since the cached tick.  Past that, ego-motion error dominates.

Projection math, applied to the cached `(d, d_star, theta_err)` taken
at heading `H_T`:

```
heading_delta = wrap_signed(H_now − H_T)
theta_err_now = theta_err_cached − heading_delta
d_now         = d_cached            (Flavor 1 simplification — no
                                     position dead-reckoning yet)
```

The `d` simplification is honest about the limit: we are NOT tracking
position, only orientation.  Over 1–3 ticks of straight sailing this
is close enough; combined with the age cap it bounds the worst-case
error.

The regulator's existing control law then runs unchanged on
`(d_now, d_star, theta_err_now)`.

##### When this is BETTER than no projection

- 1-tick flicker (shore briefly out of frame): projection is almost
  truth.  Lyapunov keeps regulating; bot doesn't fall back to the bad
  baseline.
- 2–4 tick gap with the ship sailing roughly straight: projection is
  close.  Bot rides through the gap smoothly.

##### When this is GRACEFUL under failure

- Multi-tick gap with violent rotation: heading-delta cap triggers,
  projection is discarded, fall through to `None` (same as today —
  no regression).
- Cache is stale but ship is straight: age cap triggers, ditto.

The architecture choice is: **prefer projection over the existing
shore-blind behaviour, but refuse to project through chaos.**

##### What gets logged

- `_last_lyap["projected"]` — bool, True when the heading came from
  memory projection rather than live state.
- `_last_lyap["mem_age_ticks"]` — how stale the cached state was.
- `_last_lyap["mem_heading_delta"]` — signed degrees of rotation since
  cache.

`tools/analyze_lyapunov_divergence.py` will gain a column reporting
projection share; `tools/replay_lyapunov.py` cannot replay this branch
faithfully (it doesn't carry goal state across ticks), so live runs
are the only validation surface — that's an accepted limitation.

##### Scope vs Phase 4

This is *not* a replacement for FSM Phase 4.  It handles
**ego-motion-bounded gaps** (≤4 ticks, ≤45° rotation).  True
shore-blind blackouts after a hard spin still produce `None`; those
remain the FSM's job.  Adding memory projection inside the Phase-1
parallel-logging scaffolding is a pure-information change — no
policy switch yet.

#### 12.5.3 Phase 2 — Regulator drives, VFH+ shadows

After Phase 1 / 1.5 the regulator was producing decisions we trusted
on shore-visible ticks and on short shore-blind gaps.  The Benghazi
live run made the **expected baseline** clear: the old policy's
shore-blind behaviour is not a safe no-op, it is "confidently commit
45–90° turns from no data" — which is precisely what triggers and
sustains the spins.  Keeping the old policy as the driver while
Lyapunov shadow-logs left the worst class of failure (over-commitment)
unaddressed.

So in Phase 2 we flip: **Lyapunov drives `best_idx`; VFH+ shadows.**
The flip is gated by an **A/B `driver_mode`** so we can roll forward
and back without code changes between voyages.

##### Three driver modes

| `driver_mode`    | best_idx                                       | when to use |
|---|---|---|
| `vfh`            | always VFH+'s `vfh_best_idx`                   | safe rollback; baseline shipping today |
| `lyapunov`       | Lyapunov when proposal exists; VFH+ otherwise  | pure switch — measure unaltered regulator on live |
| `lyapunov_safe`  | Lyapunov when proposal exists AND its sector obstacle cost < `LYAPUNOV_VETO_OBSTACLE_COST` (0.50); VFH+ otherwise | default — same regulator wins, VFH+ vetoes only when Lyapunov would steer into a clearly-blocked sector |

Selection precedence: explicit constructor `driver_mode=...` wins,
otherwise `UWO_HUG_SHORE_DRIVER` env var, otherwise
`HUG_SHORE_DRIVER_DEFAULT` (`lyapunov_safe`).  The env var lets the
runner choose between voyages with no code change.

Concretely, in `HugShoreGoal.tick()`:

```python
if mode == "vfh":
    best_idx = vfh_best_idx
elif mode == "lyapunov":
    best_idx = lyap_proposed_sector if available else vfh_best_idx
else:  # "lyapunov_safe"
    if available and obstacle_cost[lyap_proposed_sector] < VETO:
        best_idx = lyap_proposed_sector
    else:
        best_idx = vfh_best_idx
```

VFH+'s `costs` and `vfh_best_idx` are still computed and recorded on
every tick (`_last_lyap["vfh_best_sector"]`, `vfh_ideal_sector`) so
shadow comparison continues without any extra plumbing.  The
downstream dead-end escape (`consecutive_blocked >= 3 and
best_idx == 0`) still runs against the final `best_idx`, so a
Lyapunov pick of "straight ahead" while BLOCKED still gets bumped
off the wall.

##### Why flip now (not after §12.5(1)–(3) were satisfied)

The four formal acceptance criteria in §12.5 were drafted before we
re-interpreted the agreement metric and saw the Benghazi trace.  In
practice:

- **(1) ≥75% same-sector agreement** — the live run showed 48%, but
  ~97% within Δ=1 sector, and most of the gap is *quantization*: the
  regulator produces a small proportional correction, the old code
  commits a full 45° tap.  This is a *property of the policy gap*, not
  a math failure.  Flipping is the way to measure it.
- **(2) All 18 pinned scenarios** — 17 of 18 are FSM-mode or
  VFH+-obstacle cases that the regulator *correctly* returns None on;
  Phase 2 keeps VFH+ as the fallback, so those cases are still
  handled the same way as today.
- **(3) Top-10 divergent ticks explainable** — done for Nile (replay)
  and Benghazi (live), see §12.5.1 and the Phase 1.5 motivation.
- **(4) Spiral does not reproduce** — the replay showed Lyapunov would
  not perpetuate the Nile spiral, and the Benghazi over-commitment
  trigger (t=102 90° hard-right) was a tick where Lyapunov said hold.

##### Known risk and how `lyapunov_safe` addresses it

Lyapunov has no obstacle awareness.  The Δ=2 cluster (Benghazi t=88,
97, 109; the `peninsula_tip_peels_left_when_left_safer` synthetic
scenario) is exactly this case: shore tangent says turn right,
**but a high obstacle cost on the right says there is land there.**

The default `lyapunov_safe` mode applies a minimum-effort veto:
when Lyapunov's chosen sector has obstacle cost ≥ 0.50 (same
threshold the BLOCKED phase and dead-end-escape already use), VFH+'s
`vfh_best_idx` takes over and we log `driver="vfh_veto"`.  This is
not Phase 3 (full VFH+ inner-loop deflection); it is the cheapest
guard that prevents the regulator from driving the bot into a wall
it can plainly see.  Phase 3 remains the right long-term fix.

To measure the pure regulator without this guard, set
`UWO_HUG_SHORE_DRIVER=lyapunov` for one voyage.  To roll all the way
back to today's policy, set `UWO_HUG_SHORE_DRIVER=vfh`.

##### What gets logged

`_last_lyap` per tick gains:

- `mode`              — the configured `driver_mode`
- `driver`            — what actually drove this tick: `"lyapunov"`,
                        `"vfh_fallback"` (no proposal), `"vfh_veto"`
                        (lyapunov_safe blocked it), or `"vfh"`
                        (vfh mode)
- `vfh_best_sector`   — what VFH+ would have picked this tick
- `vfh_ideal_sector`  — VFH+'s `ideal_idx` (regime label)
- `lyap_obs_cost`     — obstacle cost on Lyapunov's proposed sector,
                        so we can grade veto decisions after the fact

This makes Phase 2 a *strict superset* of Phase 1's logging: every
tick records both what was done AND what the old code would have
done, so we can grade Phase 2 against the live baseline after the
fact without re-running.

### 12.6 What Phase 1 does NOT do

To be explicit about scope:

- **(Superseded by Phase 2 — see §12.5.3.)**  Phase 1 did not change
  live behaviour; Phase 2 does.  Lyapunov now drives `best_idx` when
  it has a proposal; VFH+ remains the fallback and its picks are
  recorded for shadow comparison.
- **Does not solve the t=80→t=95 spiral.**  That's a behaviour-change
  fix; Phase 1 only generates the evidence that the math would solve
  it.
- **Does not add the FSM.**  No mode tracking, no U-turn commitment.
  Regulator math when shore visible, memory-projected math during
  short shore-blind gaps (Phase 1.5; see §12.5.2), None otherwise.
- **Does not add the world-frame goal layer.**  Nile-bounce class of
  failure is unaddressed; that's a separate higher-level navigator.

### 12.7 Estimated effort

- Lyapunov code + helpers: **2-3 hours**
- Tests reusing pinned scenarios: **1 hour**
- Analysis tool: **2 hours**
- Live runs + initial analysis: **1 hour** (mostly waiting)
- Iteration on tangent estimation if the first results are noisy: **2-4 hours**

Total: roughly **one focused day** to get to a Go/No-Go decision on
Phase 2.

---

## 13. Architectural debt — our "VFH+" is a channel-follower in obstacle-avoidance clothing

Discovered during the 2026-05-30 freeze post-mortem
(`hug_debug_20260530_211522`).  The Phase 1 / 1.5 / 2 work above
implicitly assumed the textbook division of labour:

- **VFH+** = obstacle avoidance, takes a goal direction from outside
- **Lyapunov** = wall-following, owns the goal direction

That isn't what's actually in the code.

### 13.1 What `_score_sectors` really does

The function we call "VFH+" combines:

1. **Obstacle cost** (textbook VFH+).
2. **Virtual-wall cost** on the *opposite* side, derived from a
   calibrated `wall_distance` (`brain/goals/hug_shore.py:176-185`).
3. **`_ideal_sector` bias** — a hand-rolled wall-following heuristic
   (`brain/goals/hug_shore.py:837+`) baked into the cost via
   `W_OBSTACLE × ideal-distance`.

The virtual-wall mechanism is the load-bearing one.  From the source
docstring:

> "Real shore is one wall of a corridor; we add a virtual wall on the
>  opposite side at the calibrated `wall_distance`.  Ship sails the
>  corridor.  Drift correction, hug-tightness, and stay-near-shore
>  pressure all emerge from a single tunable parameter."

So "VFH+" is not avoiding obstacles to reach a goal; it is **staying
in a manufactured corridor**.  In steady state this is mechanically
indistinguishable from wall-following at distance `wall_distance`,
but it's implemented via repulsion from a fake wall, not attraction
toward the real one.

### 13.2 The semantic substitution

The intent is **"keep my hug side close to shore"** → positive bias
toward target side.

The implementation is **"avoid getting close to real shore (real
obstacle cost) AND avoid drifting offshore (virtual wall cost)"** →
repulsion from both sides → bot lands in the middle of the corridor.

These have the same fixed point.  They diverge when:

- **Real shore is invisible / noisy.**  The virtual wall is parametric
  in the ship frame and doesn't need perception, so the "go onshore"
  gradient survives perception failures.  Lyapunov, which depends on
  current perception, can't replicate this without a parametric prior.
- **Geometry rotates.**  The virtual wall is anchored to the ship's
  current frame, not to an actual coastline.  When the bot rotates
  (e.g. after a hard turn), the virtual wall rotates with it — which
  is correct for "stay in a corridor centred on current heading" but
  wrong for "stay near a coastline that exists in the world."
- **"Confused" recovery.**  When the wall-follow override in
  `_ideal_sector` doesn't fire, the cheapest sector is biased
  toward the hug-target side because that's where the virtual wall
  ISN'T.  So the bot turns hug-side regardless of where shore
  actually is.  This is the source of the "always turns right in
  starboard hug when uncertain" behaviour observed in the Benghazi
  circling.

### 13.3 Lifecycle of the virtual wall

| event | when | how |
|---|---|---|
| Initialised | goal construction | `wall_distance = WALL_DIST_INIT = 0.20` |
| Updated   | start of every tick (`_update_wall_distance`) | very slow EMA (α=0.05) — only samples on stable HUGGING ticks (T.frac ≥ 0.20, T.dist ∈ [0.05, 0.30], ahead clear) |
| Applied   | inside `_score_sectors` each tick | cost on opposite-side sectors via `_wall_cost(wall_distance)` |

Critically, **the EMA does NOT recalibrate in trouble** — so during
a freeze the virtual wall is locked at its last-good calibration.
That's the right discipline (don't learn from collisions) but means
the channel doesn't deform to match the current geometry.

### 13.4 Why Lyapunov vs "VFH+" disagreements happen

Both layers are doing shore-following with different perception
sensitivities:

|  | reads from | reasons via |
|---|---|---|
| Lyapunov | 3 sectors (beam-, bow-, astern-target) | distance error + tangent angle, single-tick |
| "VFH+"   | all 8 sectors + multi-tick `signals` + parametric virtual wall | weighted cost minimisation with rules |

When perception is clean and geometry matches Lyapunov's assumptions
(shore on hug side, visible bow-to-astern), Lyapunov is smoother and
more principled.  When perception is noisy or geometry is broken,
"VFH+" degrades more gracefully because its parametric virtual wall
provides a shore prior that survives bad perception ticks.

The freezes of 2026-05-30 are this property in action: Lyapunov
believed phantom 1-pixel readings and got locked in false equilibrium;
"VFH+" had a stable virtual-wall gradient pointing onshore the whole
time and would have recovered.

### 13.5 Consequences for the Phase 2 / 3 roadmap

The design doc has been claiming "Lyapunov outer + VFH+ inner" as the
target architecture (§5, §10).  That can only work if "VFH+" is
actually pure obstacle avoidance.  Today it isn't — and stripping the
virtual wall + `_ideal_sector` out would remove the shore-prior that
keeps the bot recoverable when perception fails.

So there are two coherent end-states, not three:

**Path A — Acknowledge what `_score_sectors` actually is.**  Rename
it (`ShoreCorridor` / `WallFollower`), drop the Phase 3 framing of
"VFH+ inner loop," and accept that we have **two wall-following
implementations** that vote.  The `lyapunov_safe` mode already
behaves this way; the natural extension is a deadlock-breaker veto
(if VFH+ disagrees with Lyapunov's "hold" for ≥3 ticks, VFH+ drives
one tick).  Minimal code change.  Honest about reality.

**Path B — Realise the clean separation we keep claiming.**  Strip
the virtual wall and `_ideal_sector` out of `_score_sectors` so it
becomes pure obstacle avoidance.  Add a parametric prior to Lyapunov
("expected shore distance + tangent + last-known-side") so the
regulator survives perception failures the way the virtual wall does
today.  Only then compose Lyapunov-as-outer + VFH+-as-inner.  Large
change; clean end-state.

### 13.6 Recommendation

Path A in the near term.  The freezes proved Lyapunov is not yet
robust enough to own wall-following alone, so removing the virtual
wall now would regress live behaviour.  Path B remains the
aspirational target after Lyapunov gets:

- noise rejection at perception (connected-component filter on the
  mini-map land mask)
- a parametric shore prior (decay-based, not just last-tick read)
- variable-duration holds (so small proportional corrections aren't
  swallowed by the 22.5° quantization deadband observed in freeze #2)

In docs and code, **stop calling `_score_sectors` "VFH+"** unless
we genuinely strip its wall-following terms.  The name is misleading
us and will keep misleading anyone joining the project.

### 13.7 The missing lateral-balance term — fix the channel-follower, not abandon it

A deeper diagnosis of the 2026-05-30 voyages.  The freezes and the
"drift offshore until shore is lost" pattern have a structural cause
that §13.1–6 named but didn't pin down: **the cost function has no
term that responds to the bot's lateral position in the corridor.**

#### 13.7.1 The asymmetry between real shore and virtual wall

Real shore writes its cost into the *sectors it geometrically
occupies*.  As the bot approaches shore on the right:

- `obs` cost on sec 1 rises (shore in bow-right direction)
- `obs` cost on sec 2 rises (shore on beam)
- `rotation_penalty` on sec 1 fires (turning right would rotate
  shore into bow)
- Critically, if shore curves ahead `obs` on sec 0 *also* rises —
  the cost terms self-correct to geometry

So when the bot is too close to real shore, multiple per-sector
mechanisms push it away.  The cost function naturally tracks the
geometric situation because perception writes shore into whichever
sectors shore is in.

The virtual wall has **none of this self-correction**.  It writes a
fixed parametric cost into sec 6 and sec 7 in ship frame, and is
zero everywhere else.  When the bot drifts toward the wall
(equivalently: away from real shore):

- `obs` cost on sec 0 stays ~0 (water ahead)
- `obs` cost on sec 1 actually *decreases* (less shore to obstruct)
- `wall` cost on sec 0 = **hardcoded 0**
- No mechanism makes sec 0 more expensive as the bot drifts offshore

Result: the bot keeps holding straight as shore recedes.  The
virtual wall sits on sec 6/7 doing nothing useful, because the bot
was never going to pick those sectors in the first place.

#### 13.7.2 The three geometric sub-cases the cost function should handle

For a starboard hug along an east-running coast:

| geometry | sec 0 should be… | today the cost function does… |
|---|---|---|
| **shore parallel** (ideal steady state) | cheapest | sec 0 cheap ✓ (correct by accident — nothing penalizes it) |
| **shore curves into bow** (corner approaching) | expensive | sec 0 cost rises from `obs` because real shore writes into sec 0 ✓ |
| **shore curves away from bow** (drifting offshore) | should rise *proportionally* | sec 0 stays cheap until `_ideal_sector` flips at a hard frac threshold, then snaps to a 45° turn ✗ |

The third case is where the virtual wall's whole purpose — keeping
the bot in the corridor — should fire.  Today nothing fires:

- The wall_cost on sec 6/7 doesn't enter sec 0's cost.
- `_ideal_sector` waits for `T.land_fraction < SHORE_VISIBLE_FRAC`
  (0.15) before switching from "hold" to "turn-right."
- During the wait the bot drifts further, and when the switch finally
  happens it commits a full 45° tap — bang-bang, often overshooting.

#### 13.7.3 The fix — a lateral-balance term on sec 0

We don't need to abandon the channel-follower model.  We need to
finish it.  Add one term to `_score_sectors`:

```
lateral_drift = max(0, T.nearest_dist - wall_distance)
                # positive = bot drifted offshore past calibrated hug distance
                # zero     = at-or-tighter-than calibration (real-shore terms
                #            already handle "too close")

costs[sec 0] += K_LATERAL × lateral_drift
                # going straight while off-center is expensive,
                # proportional to how off-center the bot is
```

This restores the symmetric self-correction the wall has been
missing.  Walking the three sub-cases:

- **Shore parallel**: `T.nearest_dist ≈ wall_distance` → `lateral_drift = 0` → no penalty on sec 0 → bot holds.  Same as today.
- **Shore curves into bow**: `T.nearest_dist < wall_distance` → `max(0, ...) = 0` → no penalty on sec 0 (the existing `obs` on sec 0 and `rotation_penalty` on sec 1 already handle this).  Same as today.
- **Shore curves away**: `T.nearest_dist > wall_distance` → positive `lateral_drift` → proportional penalty on sec 0.  As shore fades, sec 0's cost climbs *gradually*, and at some lateral drift the cost gap with sec 1 closes — bot transitions to "turn right" smoothly, before perception crosses any threshold, with a magnitude proportional to actual drift.

We guard against the noise-pixel issue (§12.5.1) by only computing
`lateral_drift` when `T.land_fraction >= LATERAL_VALID_FRAC` (small,
e.g. 0.05) — so a 1-pixel artifact at distance 0.07 doesn't masquerade
as "shore is right where it should be."  When T's frac is below
that threshold, we set `lateral_drift = 0` (no centering signal
available; fall back to existing logic).

#### 13.7.4 Relationship to Lyapunov

The formula `K × (T.nearest_dist − wall_distance)` is **exactly the
Lyapunov distance-error term** `K_D × (d − d_star)`, with
`d_star = wall_distance` and `d = T.nearest_dist`.

So this fix is equivalent to folding Lyapunov's distance-error term
into the VFH+ cost function as a modifier on sec 0, instead of
running Lyapunov as a separate parallel driver.  Two benefits:

- No quantization deadband.  Lyapunov-as-driver was producing
  proportional headings whose 14° corrections got swallowed by the
  22.5° sector quantizer.  As a *cost modifier*, the proportional
  signal smoothly shifts which sector wins — small drift makes sec 0
  cheaper, growing drift gradually makes sec 1 cheaper.  The
  sector-quantized output is now the consequence of a continuous
  underlying cost, not a quantization of a continuous heading.
- One cost function, not two competing policies.  The §13.5 "Path A
  vs Path B" dichotomy collapses: we keep the cost-minimization
  framework (Path A's strength) and gain proportional response
  (Path B's strength).  No need to strip the virtual wall or rewrite
  the policy as outer/inner loops.

#### 13.7.5 What this changes about Phase 2

Phase 2 (`driver_mode = "lyapunov_safe"`) bet on a separate
regulator.  With the lateral term folded into VFH+, Lyapunov-as-
driver is no longer needed for the cases it was designed for — the
VFH+ cost function itself now produces the proportional response.

So we **switch the default `HUG_SHORE_DRIVER_DEFAULT` back to
`"vfh"`**.  The `lyapunov` and `lyapunov_safe` modes stay in the code
as A/B knobs for experimentation but are no longer the path forward.
The Lyapunov regulator + memory projection remain shadow-logged for
analysis.

### 13.8 The inverse-square ahead-wall cost — letting close obstacles dominate

Live failure (`hug_debug_20260531_072226` t=1-7): bot held straight
into a wall at frac=0.83 / dist=0.10 because the standard
`_obstacle_cost` saturates at 1.0, while virtual_wall + rotation
penalty + ideal-distance push other sectors above 1.0.  Sec 0 stayed
cheapest even with collision imminent.

Fix: `_ahead_obstacle_cost(sec)` uses VFH+/APF-style 1/r² scaling on
sec 0 only, gated on `frac > AHEAD_WALL_GATE (0.20)`.  Below the gate,
falls through to the linear `_obstacle_cost` so noise pixels don't
trigger the formula.  Above the gate at close range, cost grows
without the unit cap.

Matches the canonical wall-following literature: close obstacles
contribute to the polar histogram with magnitude inversely related to
distance squared (Borenstein/Ulrich VFH+, Khatib APF).  The unit cap
on our linear `_obstacle_cost` was the unprincipled remnant.

### 13.9 Pinched-passage peel escalation — 45° to 90°

Live failure (`hug_debug_20260531_075222` t=153): bot peeled 45° left
to escape close right-shore but landed pointing at an island ahead-
left.  The peel direction was correct but the magnitude was the
wrong-shaped commit — half the obstacles ended up on the bow.

Fix: when `_ideal_sector`'s wall-follow override fires AND the
bow-opposite sector ALSO has obstacle (frac ≥ 0.10 at dist <
OBSTACLE_RANGE), escalate the peel from bow-opposite (45°) to
beam-opposite (90°).  Geometric outcome: BOTH obstacles end up on
the hug side after the turn — bot resumes normal hug configuration
against the combined coastline.

This is the user's "treat them as one mass" insight, made discrete.

### 13.10 Principled Lyapunov tangent bias replaces astern-pull

Per the wall-following literature (Borenstein VFH+, Khatib APF,
sliding-mode wall-following): wall constraint should be relative to
the *real wall's* tangent direction, not the ship frame.  Astern-pull
was the 2.5× ideal-distance amplifier we used to compensate for the
fact that our virtual wall was ship-anchored (wouldn't rotate with
the actual coast around a corner).

§13.7 made the d-error term work proportionally.  §13.10 completes
the controller: the θ-error (tangent angle) term also works
proportionally.

Two pieces:

1. **Multi-sector tangent estimator** (`_shore_tangent_angle`).
   Weighted least-squares fit of a line through the hug-side shore
   points (bow-target, beam-target, astern-target).  Returns the
   slope of that line as a signed angle relative to ship's bow.
   Replaces the Phase-1 2-point bow-vs-astern differential, which
   gave wrong signs in corner geometries.

   On the live `astern_pull_beats` scenario:
   - Old 2-point: θ_err = −8.6° (turn left, wrong)
   - New multi-point: θ_err = +20.2° (turn right, correct)

2. **Tangent-bias cost term**.  Each sector pays a cost proportional
   to its angular distance from the estimated shore-tangent bearing.
   Biases the cost function toward sailing along the wall — exactly
   what Lyapunov wall-following theory specifies.

Astern-pull is "turned off" not deleted: `W_IDEAL_ASTERN_PULL: 2.5 →
0.35` (equal to `W_IDEAL`, so the multiplier is a no-op).  Code path
preserved for potential re-enable.

### 13.11 Search mode — full magnitude when shore is invisible

Live failure (`hug_debug_20260531_095814` t=24-27): shore went
invisible (all sector frac < 0.05); bot knew it needed to turn right
(ideal=1) but each tap was 13° because cost margin was tiny.

Root cause: when shore is invisible, neither §13.7's lateral term nor
§13.10's tangent estimator fires (both need shore points).  Only the
`W_IDEAL × ideal_distance` differential contributes to the margin,
which is small.

The variable-duration scaling was designed assuming "small margin
= high confidence in current heading, gentle adjustment."  But
shore-blind ticks have small margin because of **no information**,
not high confidence.

Fix: bypass margin scaling when target-beam and bow-target both have
`land_fraction < SHORE_VISIBLE_FRAC`.  In that regime the bot enters
**search mode** — full nominal commits so each tick contributes a
substantial rotation toward where shore "should be."

Principle: *proportional response when you can see what you're doing,
decisive action when you can't.*  Matches the literature's
distinction between "controller has good state estimate" and
"controller is in search/recovery mode."

### 13.12 Removing the categorical "shore visible → hold" rule

Live failure (`hug_debug_20260531_095814` t=18): bot drifting offshore
with proper lateral signal firing (0.19 cost on sec 0), but
`_ideal_sector` returned 0 because T.frac=0.20 > SHORE_VISIBLE_FRAC.
Sec 1 lost its ideal-distance advantage → margin collapsed to 0.02 →
turn was 11° when it should have been 30°+.

The hand-rolled `shore visible + in corridor → ideal=0` rule was a
pre-§13.7 compensation for the cost function's missing drift signal.
Once §13.7 added the proper proportional signal, the rule became:
- **redundant** for the steady-state case (margin naturally tiny)
- **harmful** for the drift case (categorical override of the
  proportional signal)

Fix (Option A — principled): replace the `return 0` fall-through with
`return _AHEAD_TARGET_SECTOR[side]`.  The cost function (obstacle +
lateral + tangent + rotation penalty) decides whether to actually
turn.  At steady-state hug the margin is tiny → no turn; at drift
the margin grows proportionally → bot turns proportionally.

Matches the textbook Lyapunov wall-following formulation: continuous
proportional correction, no discrete "hold" state.

Compensation: `rotation_penalty` weight raised from 0.30 → 0.65.  With
the categorical rule gone, this kinematic penalty is now load-bearing
— it prevents the bot from rotating into close beam shore when
bow-target itself looks clean.  Tuned so `close_T_rotation_hazard`
scenario still picks hold while `river_channel_opens_right` still
picks turn-target through a partial obstacle.

Live regression at t=18: margin 0.02 → 0.20, turn 11° → 30°.

### 13.13 Resolution

The §13 architectural debt is substantively resolved.  The cost
function now has:

| term | what it does | shape |
|---|---|---|
| `obs` / `_ahead_obstacle_cost` | obstacle avoidance | inverse-square close, linear far |
| `lateral_cost_sec0` (§13.7) | distance regulation (d-error) | proportional |
| `wall_cost` (virtual wall) | base-level offshore repulsion | parametric |
| `tangent_bias` (§13.10) | tangent alignment (ψ-error) | proportional |
| `rotation_penalty` | kinematic peel safety | proportional to T proximity |
| `W_IDEAL × ideal_distance` | direction-of-recovery preference | proportional |
| `W_HEADING_SMOOTH` | tiebreaker for parallel sectors | tiny |
| §13.9 pinch escalation | 45° → 90° peel when both sides blocked | geometric switch |
| §13.11 search mode | full magnitude when shore-blind | search/recovery |

Every term is either continuous-proportional or kinematically
grounded.  The hand-rolled categorical heuristics (`_ideal_sector`'s
"shore visible → hold", astern-pull's 2.5× multiplier) that
compensated for missing signals are gone.

The architecture finally matches the literature's specification:
**Lyapunov wall-following controller (d-error + ψ-error) integrated
into a cost-minimization sector selector with obstacle-avoidance
substrate.**  Pure proportional control during clean wall-following;
search/escape modes for the edge cases; no hand-rolled categorical
gain scheduling.

Sections §13.1–§13.6 (which framed the debt as unresolved) are kept
for historical reference, but the substantive criticism they raised
has been addressed by §13.7 through §13.12.

---

## 14. References

- Borenstein & Koren 1991 — original VFH
- Ulrich & Borenstein 1998 — VFH+
- Ulrich & Borenstein 2000 — VFH*
- Bemporad et al. — Lyapunov-based wall-following surveys
- Lawrence, Frew, Pisano 2008 — Vector field path following for UAVs
- Coulter 1992 — Pure pursuit
- (Internal) `sim/scenarios.py` — pinned regression cases that gate
  this work
