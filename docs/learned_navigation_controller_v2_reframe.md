# Learned Navigation Controller — v2 Reframe

Supersedes the goal-conditioned framing in
`docs/learned_navigation_controller_plan.md`.  Written 2026-06-28
after the t245–t283 antipode-flip post-mortem on the
`ai_nav_2026-06-24T20-49-36` session.

## TL;DR

The v1 plan treated navigation as **goal-conditioned steering**
(`desired_deg` as an input, BC on human action labels).  That framing
breaks because:

1. The same image at the same spot has **two opposite correct
   actions** depending on whether the ship is outbound or inbound.
2. The `desired_deg` source (planner) is brittle at lakes, Y-tips,
   and other places where a global goal is undefined.
3. A single-frame model can't distinguish "going north on this river"
   from "going south on this river" — direction is purely temporal.

The v2 framing is **goal-free local hugging**:

- Policy = "stay close to shore, don't bounce."
- No `desired_deg` input.  Either direction along a shore is correct.
- Bounce is the main training signal (dense negative reward).
- Temporal context (frame stack or motion-in-aux) lets the model
  notice things like "the minimap has been drifting the wrong way
  for 5 ticks."
- Topology context (`is_channel | is_junction | is_dead_end`) lets
  the model condition behavior at events.

## Why the old framing fails — the antipode case

In session `ai_nav_2026-06-24T20-49-36` ticks 246–283 (38 ticks),
the heading detector locked into the wrong 180° antipode.  Real
heading was ~10–30° (NNE); readout was ~190–210° (SSW).  The
planner kept commanding right-holds because it thought heading was
"slightly west of due south" and goal was 180°.  The ship sailed
166 km north of where it started — the entire opposite of intent.

What a human would have noticed: **the minimap was drifting up the
screen over many ticks.**  Not "shore on wrong side" — both sides
have shore in a river.  Not "compass reading wrong" — human doesn't
read a compass.  Just *the ship has been moving the wrong way on
the radar for a while.*  That's a temporal signal that a one-frame
model literally cannot see.

## Reframed training sample

```
┌─────────────────────────────────────────────────────────────────────┐
│ ONE TRAINING SAMPLE                                                 │
├─────────────────────────────────────────────────────────────────────┤
│ image    : minimap crop      shape (3, 192, 384) float32 normalized │
│             (or (3·K, 192, 384) if frame-stack)                     │
│                                                                     │
│ aux      : 7 floats          shape (7,)         float32             │
│             [0] heading_sin     ─┐ optional; image carries this     │
│             [1] heading_cos     ─┘                                  │
│             [2] dlat_5tick      ─┐ smoothed motion (physics-filter) │
│             [3] dlon_5tick      ─┘  — immune to antipode bug        │
│             [4] is_channel      ─┐                                  │
│             [5] is_junction     ─┤ topology one-hot                 │
│             [6] is_dead_end     ─┘                                  │
│                                                                     │
│ label    : action_idx        int8  ∈ {HOLD, L_SHORT, …, R_LONG}     │
│                                                                     │
│ reward   : float32                                                  │
│             −1.0  on bounce                                         │
│             +0.01 per healthy tick                                  │
│             +small from labels.jsonl verdict/state if present       │
│                                                                     │
│ valid    : bool       True if lat/lon reading is clean              │
└─────────────────────────────────────────────────────────────────────┘
```

**What's gone from v1:** `desired_sin`, `desired_cos`, `delta_norm`
— the entire goal channel.

**What's new:** motion aux, topology aux, bounce reward, `valid`
mask.

## The bounce detector — separate, rule-based, upstream

Bounce detection is the **reward function**, not part of the
policy.  Five reasons it must be separate:

1. A reward signal cannot be the same network it trains (the model
   would minimize bounce-output to get free reward).
2. Bounce has clean physical signatures — speed drop, sudden
   heading jump, ship-on-land, lat/lon stall.  Rules outperform
   models for this.
3. The reward signal sits upstream of every training run forever
   — its reliability dominates everything.
4. Reusable across labeling tool, recovery primitive, evaluation
   metric.
5. Separation of concerns — same reason heading detection isn't
   fused into the policy.

Implementation lives next to `detect_bounce_or_hard_turn` in
`tools/build_rl_dataset.py`.  Inputs: trace of last N ticks (speed,
lat, lon, heading) + current minimap.  Outputs: `bounce_now: bool`,
`bounce_imminent: bool`, `bounce_severity: float`.

## Bounce recovery — visual snapshot, not heading replay

The simple "remember last heading, turn back to it" approach
inherits every perception bug.  In the antipode-flip case, the
remembered heading would be the *flipped* one, so recovery would
turn the wrong way.

Three options, in increasing order of fidelity to human behavior:

| Option | Mechanism | Pros | Cons |
|---|---|---|---|
| A. Heading snapshot | save `last_healthy_heading`, turn to it | trivial | inherits antipode/OCR bugs |
| **B. Visual snapshot** | save minimap from N ticks ago; compute rotation by comparing ship icons in saved vs current | robust to detector drift; uses existing template-match | needs frame buffer + the icon detector |
| C. Image-goal-conditioned policy | model takes (current, reference) and steers until they match; reference=current during normal sailing, reference=pre-bounce during recovery | unified, no FSM state, most human-like | doubles input bandwidth; needs paired training data |

**Decision: build B for now, plan for C.**  Both treat "previous
orientation" as a *visual reference*, not a number — so a later
migration to C is incremental.

## Temporal context — frame stack vs motion-in-aux

The antipode failure is unsolvable by a single-frame model.
Direction-of-travel is only visible in motion between frames.

Two ways to add it, additive:

**1. Motion-in-aux (cheaper — start here).**  Add `dlat_5tick`,
`dlon_5tick` to aux, computed from physics-filtered lat/lon over
the last 5 ticks.  Five-line change, immune to heading detector
bugs because it's derived from a different perception source.

**2. Frame stack (escalate if (1) isn't enough).**  Input becomes
`(3·K, 192, 384)` with K∈{2,4} frames at strides 1, 3, 5, 10 ticks
back.  The CNN sees motion directly via first-layer convolutions.
Same architecture, ~K× the first-layer FLOPs.  Standard Atari-DQN
trick.

Don't go to LSTM/transformer first — too much new training surface
area to debug before exhausting the simpler options.

## Lat/lon quality filter

The motion-in-aux signal depends on lat/lon being accurate.  HUD
OCR has two failure modes:

- **Single-tick spike:** OCR misread on one frame, snaps back next
  tick.  Detected via local slope agreement (median + MAD over a
  10-tick window) or curvature spike (`|lat[i+1] − 2·lat[i] +
  lat[i−1]|` exceeds a threshold).
- **Extended bad span:** HUD occluded for K+ consecutive ticks
  (pirate sprite, village overlay covering digits).  Detected via
  dead-reckoned vs OCR position mismatch.

**Filter design — physics-relative, not magnitude:**

Ship cruise speed varies by class (slow trader → fast frigate).
Absolute thresholds (0.2°/tick) don't generalize.  Instead, compare
each tick's slope to the local median.  Auto-scales per ship.

**Use of the mask:**

- Add `latlon_quality` column to npz at build time.
- Single-tick spikes: drop the tick from training (DataLoader
  filter).
- Extended bad spans: drop the span + a 3-tick margin on either
  side (boundaries are noisy as OCR transitions).
- Episode timing stays intact (`terminal` flags don't shift) — the
  trainer just skips bad ticks via its sampler.

## Two-dimensional labeling (state + action verdicts)

Shipped 2026-06-28 in `tools/tick_viewer.py` and
`tools/build_rl_dataset.py`.

A single "this tick was wrong" label conflates two dimensions:

- **Action verdict** (`ideal`|`acceptable`|`wrong`) — was the
  chosen action correct given the state?
- **State verdict** (`good`|`bad`) — was the ship in a desirable
  state at all?

Example from t178–t180 in the same antipode session:
- t178: state=good, action=wrong (blew a good position).
- t179: state=bad, action=wrong (made bad situation worse).
- t180: state=bad, action=ideal (correct escape).

**Viewer keybindings:** `1/2/3/0` = action verdict, `[/]/\` =
state verdict.

**Reward shaping:**
- `r_verdict`: `+0.5 | +0.1 | −1.0` per action verdict.
- `r_state`: `+0.2 | −0.5` per state verdict.
- **Hindsight propagation:** a bad state at tick T charges
  γ-discounted penalty (γ=0.9, window=10) to ticks T−1…T−N within
  the same episode.  Teaches the model that actions leading *into*
  a bad state inherit blame, not just the action inside it.

## Junctions and branches

At a junction in a river, two opposite actions can both be
"correct."  How does the model decide?

**Option A — Pure-learned: train on consistent labels.**  If
junction ticks are consistently labeled "always left," the model
learns "junction-shape + this approach direction → hold_left."
Works if:
- Approach direction is in input (motion aux or ship icon in image).
- Labels are consistent (the discipline trap — humans drift).
- Coverage exists (one Nile junction memorizes one junction; need
  ~10+ junction variants for "junction-ness" to generalize).
- `is_junction=1` in aux helps the model know which regime it's in.

**Option B — Hybrid: rule-override at junction.**  Topology layer
fires `take_left_exit()` primitive when `is_junction=1`.  Model
handles only continuous channel hugging.  Simpler model, harder
infrastructure (one more module).

**Decision: build pure-learned first** with a strict labeling
rule ("always left at branches").  Use it as the baseline.  Add
rule-override layer only if specific junction types defeat the
model.  Don't preemptively build the hybrid — it might just work.

## Hierarchy of inputs vs. existing planner

The learned controller is the **tactical** layer (how to steer
between waypoints).  The **strategic / mission** layer above it
still:

- Decides when the voyage is complete (arrival at destination,
  Y-tip recognition, lake explored).
- For waypoint-based missions, provides the next waypoint.  This
  no longer feeds into the model — it just decides "are we still
  going, or are we done."
- Reverses the mission at terminals (Y-tip → start return leg).

The brittle parts of `commit_direction` calculation (lakes, Y-tips,
defaults like `river_explore_default`) become **mission-authoring
concerns**, not run-time inference concerns.  Author the waypoint
chain explicitly; the model doesn't need it.

## Architecture diagram

```
┌──────────────────────────────────────────────────────────────────┐
│ Mission / Strategic (rule-based, mission-specific)               │
│   - waypoint chain, arrival recognition, mission reversal        │
│   - does NOT feed desired_deg to the controller                  │
└──────────────────────────────────────────────────────────────────┘
                                │
                  emits "voyage ongoing" / "voyage done"
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ Topology + perception layer (rule-based, fast)                   │
│   - JunctionDetector → CHANNEL | JUNCTION | DEAD_END             │
│   - Lat/lon quality filter (curvature, MAD, occlusion gates)     │
│   - Bounce detector (speed drop, heading jump, ship-on-land)     │
│   - HeadingDetector (existing; bug-prone but used only as aux)   │
└──────────────────────────────────────────────────────────────────┘
                                │
                  aux = [hdg, dlat, dlon, topology_onehot]
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ Learned controller (tiny CNN + MLP head)                         │
│   - input:  minimap image + aux (7 floats)                       │
│   - output: action_idx (HOLD | L_SHORT/MED/LONG | R_SHORT/...)   │
│   - trained: BC + reward (bounce-driven), with verdict shaping   │
│              and hindsight propagation                           │
└──────────────────────────────────────────────────────────────────┘
                                │
                                ▼
                          ADB tap / hold
```

## Implementation status

| Component | Status | Location |
|---|---|---|
| Two-dim labeling (action+state verdicts) | shipped 2026-06-28 | `tools/tick_viewer.py`, `tools/build_rl_dataset.py` |
| Verdict + state reward components | shipped 2026-06-28 | `tools/build_rl_dataset.py` |
| Hindsight propagation (γ=0.9, window=10) | shipped 2026-06-28 | `tools/build_rl_dataset.py` |
| Tiny CNN + BC trainer | shipped (v1) | `brain/ai_nav/learned/model.py`, `tools/train_bc.py` |
| LearnedController wire-in | shipped (v1) | `brain/ai_nav/learned/controller.py` |
| **Drop desired_* from aux** | not started | `tools/build_rl_dataset.py`, `brain/ai_nav/learned/model.py` |
| **Add motion-in-aux (dlat/dlon)** | not started | `tools/build_rl_dataset.py` |
| **Add topology-in-aux (one-hot)** | not started | `tools/build_rl_dataset.py` |
| **Bounce reward as primary signal** | partial — bounce detected but not weighted as primary | `tools/build_rl_dataset.py::compute_reward` |
| **Lat/lon quality filter + DataLoader mask** | not started | `tools/build_rl_dataset.py`, `tools/train_bc.py` |
| **Frame-stack option** | deferred until motion-in-aux insufficient | — |
| **Visual snapshot bounce recovery** | not started | new module under `brain/ai_nav/` |
| **Pure-learned junction handling** | not started — needs ≥10 junction variants for generalization | data collection task |

## Open questions

1. Keep `heading_sin/cos` in aux, or drop entirely?  Image carries
   ship-icon orientation; aux is redundant but cheap.  Default:
   keep, drop later if ablation shows no value.

2. Junction labeling discipline — "always left" hard rule, or
   per-river preference?  Affects how the bot explores a real
   river system with branches that dead-end.

3. Bounce detector — pure rule today; when (if ever) does it
   warrant a learned classifier?  Probably never for binary
   "bounced y/n," maybe for `bounce_imminent` lookahead.

4. Frame-stack stride — 1, 3, 5, 10 ticks back?  Or uniform 1, 2,
   3, 4?  Standard DQN uses uniform; non-uniform gives wider
   temporal receptive field.  Empirical question.

5. For multi-river generalization (Nile → Amazon → others), is one
   global model sufficient, or one model per river class?  Same
   conditioning argument as junctions — one model with river
   class as aux probably wins on data efficiency.

## Related docs

- `docs/learned_navigation_controller_plan.md` — v1 design,
  superseded by this doc but useful for the BC/IQL choice
  rationale and the original phased rollout.
- `docs/exploration_navigation_layers.md` — perception / mapping /
  planning split; load-bearing for the topology layer.
- `docs/navigation_models_status.md` — mini-map + shoreline
  classifier corpus and training notes.
- `docs/sea_capture_and_steering.md` — sea capture loop +
  real-time plan; the substrate this controller sits on top of.

## Related memory entries

- `project_ship_sonar_punch_holes_in_water_mask.md` — perception
  bug that originally motivated the move to learned segmentation.
- `project_translucent_overlay_corrupts_perception.md` — transient
  perception corruption on overlay frames.
- `project_v11_sim_matches_reference_baseline.md` — sim vs
  production divergence study; concrete failure mode for the
  perception → policy chain.
- `feedback_prefer_learned_segmentation_over_color_rules.md` —
  user's explicit "pivot to learned" decision after the V8–V13
  iteration.
- `feedback_sea_steering_rudder_deflection.md` — calibrated hold
  semantics (~120°/sec) used by the action decoder.
- `feedback_hug_shore_design_lessons.md` — closed-loop steering
  lessons from `HugShoreGoal`; the predecessor of this controller.
