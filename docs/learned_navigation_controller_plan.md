# Learned Navigation Controller — Plan

> Status: design proposal as of 2026-06-24.  Not started.
> Captures the conversation that followed the `cairo_to_equator`
> milestone, where the user proposed replacing the 5-layer
> classical-rule stack with a single learned controller.

## Why this might be the right pivot

The current 5-layer stack reached the equator (`tag cairo_to_equator`)
on entirely classical algorithms:

| Layer | Implementation |
|---|---|
| L1 Heading | Rotational template matching + antipodal + physics gates |
| L2 Segmentation | V11 brightness threshold + fill_holes + largest CC |
| L3 Planner | HybridPlanner (centerline tree + shore-hug fallback) |
| L4 Tactical | NoOp (Moondream available but slow) |
| L5 Mission | RiverExploreMission (JunctionGraph + Trémaux) |
| L5 Strategic | LoopDetectorStrategic (bounding-box trajectory check) |

The classical stack works but every new failure mode (bend
circling, hairpin U-turn, lake↔Y oscillation, frame-corner anchor
artefacts) requires a code fix.  A learned controller would
*absorb* these cases into the policy by training on examples.

Side benefit the user surfaced: a learned model with `AdaptiveAvgPool`
is **resolution-invariant**.  Game UI updates that shift the
minimap location/size break the current hardcoded `MINIMAP_CROP`
and `LATLON_CROP`; a CNN that takes the full screen learns to
attend wherever the minimap is.  **Eliminates an entire class of
maintenance work** caused by game-vendor updates.

## What gets replaced vs what stays

```
Today:                              Future:
─────────────                       ─────────────
L1 heading       (template)
L2 segment       (V11)         →    L1 LearnedController
L3 planner       (centerline)        (CNN encoder + MLP)
Mission          (junction graph)
L5 Strategic     (loop detect)
                                    L5 GoalLayer (simple)
                                     - target lat/lon
                                     - arrival detection
                                     - "stop / go to port / return"
L4 Tactical      (Moondream)   →    L4 Tactical (kept as fallback
                                     when controller is uncertain)
```

The controller eats L1+L2+L3+Mission+Strategic because it learns
"at junctions, do what produced good rewards in the past."  The
goal layer stays tiny and rules-based — it's just "where do I want
to end up + when am I there."

## Options

### Training-paradigm options

| Option | What it is | Pro | Con |
|---|---|---|---|
| **A — BC only** | Supervised learning on (image, action) pairs from human/bot voyages | Simplest, fast to train, deterministic | Ceiling = best trajectory in training data; can't exceed |
| **B — Offline RL only** | Train on captured trajectories with rewards, no live updates after | Better than BC (learns to avoid bad actions), no live game time needed | No adaptation to new regions/scenarios; ceiling still bounded by dataset coverage |
| **C — BC/Offline pretrain → Online fine-tune** ⭐ | Pretrain on captures, then continue learning during live runs | Bootstraps from existing data, improves over time, fits user's "initial training then learn" intent | More complex pipeline; risk of online catastrophic forgetting |
| **D — Online RL from scratch** | Bot starts random, learns purely from live game | No prep work | ~115 days of live game time at 1 tick/sec for 10M steps; infeasible |

**Recommended: C** — matches user's stated intent ("initial training, then run live/sim to learn").

### Algorithm options (assuming Option C)

| Algorithm | Type | Pro | Con |
|---|---|---|---|
| **IQL** (Implicit Q-Learning) ⭐ | Off-policy, action-value | Designed for offline→online; smooth transition; no policy collapse | Newer (2021), fewer reference impls |
| CQL | Off-policy, action-value | Well-studied | Constrained optimization adds complexity |
| AWAC | Off-policy, actor-critic | Decent offline→online | Hyperparameter-sensitive |
| DQN family (Double, Prioritized, n-step) | Off-policy, action-value | Bulletproof, lots of reference code | Worse offline behavior; needs careful pretrain |
| PPO | On-policy, policy-gradient | Stable, widely used | Poor offline pretrain; wastes live data |
| Dreamer V3 | World-model based | Most sample-efficient | Compute-heavy, doesn't fit Mac MPS well |
| Decision Transformer | Sequence model, offline | Excellent offline pretrain | Awkward for continual online learning |

**Recommended: IQL** for both phases.

### Action-space options

| Option | Example | Pro | Con |
|---|---|---|---|
| **Discrete, 7 actions** ⭐ | `{hold, L_short, L_med, L_long, R_short, R_med, R_long}` | Easier to train; matches today's planner outputs | Slightly less expressive |
| Discrete, 3 actions | `{left, hold, right}` (fixed magnitude) | Simplest | Loses fine control |
| Continuous, 2-D | `(turn_delta ∈ [-1, +1], hold_ms ∈ [0, 800])` | Most expressive | Harder to train stably |

**Recommended: discrete 7-action** to start; promote to continuous later if needed.

### Observation-space options

| Option | Pro | Con |
|---|---|---|
| **Full screen 384×192 downscaled** ⭐ | Captures sea HUD, lat/lon, dialog overlays; resolution-invariant | Bigger model input |
| Minimap crop only | Smaller input, faster | Brittle to game UI updates (the very thing we're trying to fix) |
| Multi-resolution (full screen + minimap zoom) | Best of both | Architectural complexity |

**Recommended: full screen downscaled.**  Eliminates `MINIMAP_CROP`
and `LATLON_CROP` brittleness.

### Auxiliary inputs (beyond raw image)

Concatenated into the dense layer:
- **speed** (scalar from HUD OCR) — needed for collision detection in reward
- **commit_direction / desired_heading** (sin/cos = 2D) — where the bot wants to point
- **heading** (sin/cos = 2D) — current bow direction
- **delta_normalized** (scalar) — signed (desired − heading) / 180°, explicit error signal
- *Optional*: short history (last 4 frames) for smoother behavior

**Note: hug_side is NOT in the model input.**  See "Hug side via
mirror-and-flip" below.

### Hug side via mirror-and-flip (port-only training)

Navigation is geometrically symmetric (water/land/ship behave the
same on either side), so we train the model on **port-hug data
only** and handle starboard hugging via a horizontal mirror at
inference time:

```python
# At inference, if hug_side == "starboard":
image       = horizontal_flip(image)
heading_deg = (360 - heading_deg) % 360    # E ↔ W swap
desired_deg = (360 - desired_deg) % 360
delta       = -delta

action = model(...mirrored inputs...)

# un-mirror the action: left ↔ right
if action == LEFT_*:  action = RIGHT_*
elif action == RIGHT_*: action = LEFT_*
# hold stays hold
```

**`hug_side` stays as a NavState field** (`state.hug_side ∈
{"port", "starboard"}`).  For Phase 1 onwards, it's hard-coded to
`"port"` so the mirror-and-flip switch is dormant.  Keeping the
field reserved leaves room for future situations where the goal
layer needs to flip hug side mid-voyage (e.g., island in middle
of river, mission-driven re-routing).

**Why mirror-and-flip rather than hug_side as model input**:
- Half the training data needed (port-only)
- Symmetry enforced by architecture, not learned
- Existing voyage captures are all port-hug — no need to deliberately
  collect starboard data
- Model can't accidentally pool features and ignore the side signal

**What could break mirroring (worth flagging, all minor in UWO)**:
- HUD text reads backwards — model isn't reading text, ignored
- Compass / wind indicators get flipped — likely symmetric in practice
- Ship icon orientation — consistent with mirrored heading_deg
- Asymmetric environmental rules (right-hand shipping lanes etc.) —
  unlikely in this game; if eval shows starboard-only failures, we'd
  fall back to including hug_side as a model input

## Reward design

```python
r_smoothness = -0.01 * abs(action_idx - prev_action_idx)
r_collision  = -1.0 if speed_drop_pct > 30 else 0
r_progress   = +0.1 * cos(heading - commit_direction)
r_stuck      = -0.5 if speed < 0.2 * max_speed for 5 consecutive ticks
r_per_tick   = -0.001

reward = r_smoothness + r_collision + r_progress + r_stuck + r_per_tick
```

Collision detection via speed drop is the key insight — game
already encodes collision feedback in the speed channel.

## Architecture

```
Input: full_screen (384×192×3)
   ↓
CNN encoder: 4 conv blocks @ 32/64/128/128 ch, ReLU, MaxPool
   ↓
AdaptiveAvgPool2d((4, 4))    ← resolution invariance
   ↓
Flatten → 256-d
   ↓
Concat: + speed (1) + commit_sin/cos (2) + heading_sin/cos (2) = 261-d
   ↓
MLP: 261 → 256 → 128 → 64
   ↓
   ├─── Action head:  64 → 7 logits
   └─── Value head:   64 → 1 scalar
```

Model size ~500k-1M params.  Inference 5-15 ms/tick on Mac MPS.
Trainable on CPU as fallback.

## Phased plan

| Phase | Duration | Output | Risk |
|---|---|---|---|
| **0. Prerequisites** | 0.5 day | `read_speed()` wired into NavState pipeline (HUD OCR for speed; coordinates exist in `vision/sea_hud.py::SPEED_CROP`) | Low — same pattern as lat/lon |
| **1. Dataset builder** | 1 day | Script converts session dirs → `(s, a, r, s')` tuples, stored as compact NPZ; supports all existing voyages | Low — purely data wrangling |
| **2. Trainer + IQL impl** | 2-3 days | `LearnedController` class + training script; trains on offline data | Med — IQL impl + hyperparameter tuning |
| **3. Pipeline wire-in** | 1 day | Runner flag `--planner learned` swaps controller in for L1+L2+L3 | Low |
| **4. First live evaluation** | 1 hour | Cairo→south voyage, compare to `cairo_to_equator` baseline | Med — first contact with reality |
| **5. Online fine-tuning loop** | 2-3 days | Live runs collect data, periodic IQL updates from replay buffer | Med — stability under continual learning |
| **6. Iteration & reward shaping** | open-ended | Refine rewards based on observed failure modes | Open-ended by definition |
| **7. Bounce-aware behavior validation** | 1 day | Verify model handles collisions via unified policy, NOT special-case strategic | Low — purely diagnostic |

Total: ~1.5-2 weeks to first live test of the learned controller;
then continual iteration.

## Resolution-invariance benefit, concretely

Today's stack has hardcoded constants that break with game updates:
- `MINIMAP_CROP = (1985, 205, 2385, 395)` — shifted once recently
- `LATLON_CROP = (2255, 360, 2395, 400)` — shifted today
- `SPEED_CROP = (1810, 225, 1900, 275)`

When game vendor updates UI, every constant needs re-calibration.

A CNN with `AdaptiveAvgPool` accepts any input size and learns to
attend to whatever's salient.  If the game moves the minimap 50 px
right tomorrow, the model still works — the relevant features
(water/land patterns, ship shape, text positions) all moved
together, and the convolution is translation-equivariant.  The
only constants we keep are the screen resolution itself.

This alone is a strong argument for the pivot, independent of any
performance gains.

## Tradeoffs / risks

### Pro
- Single model absorbs L1+L2+L3+Mission+Strategic complexity
- Resolution-invariant (huge maintenance win)
- Improves with more data; can transfer between regions
- Eliminates per-failure-mode rule writing
- Reward shape encodes "what good navigation looks like" once, model figures out the rest

### Con
- Loses interpretability — when the bot makes a weird decision, "the model said so" is the only answer.  Debugging requires probing activations or analyzing replay
- Mac MPS is fine for inference but training may be slow vs. a CUDA GPU
- Online learning adds operational complexity (replay buffer, checkpoints, rollback strategy)
- Game changes that affect ship handling = new environment; model needs re-training
- Doesn't generalize to non-navigation tasks (trade, combat) — those stay rule-based

### Risk mitigations
- **Catastrophic forgetting in online phase** → IQL is conservative; also keep offline data in replay buffer
- **Reward hacking** → start with simple rewards, watch for degenerate behaviors
- **Sim-to-real gap** (if we use sim training later) → train mostly online from live data; sim is supplementary
- **Game updates breaking model** → tag + snapshot dataset before any update; have rollback to current classical stack

## When to abandon and revert

If after Phase 4 (first live evaluation) the learned controller:
- Performs WORSE than `cairo_to_equator` baseline after 1 week of
  fine-tuning, AND
- Fails on scenarios the classical stack handles cleanly

…then revert to the classical stack and revisit the ML approach
with what we learned.  The classical stack stays in tree as the
fallback `--planner hybrid` option.

## Phase 7: bounce-aware behavior (no special-case strategic)

Once trained, the model needs to handle collisions ("bounces") that
push the bow off course.  The chosen design **does not add a
BackoffStrategic override**; instead, recovery emerges from the
unified policy.

### Insight: heading vs desired_heading distinguishes turn from bounce

There's always a `heading` and a `desired_heading`.  In normal
operation:
- Small delta → mild steering
- Hard turn at a bend → large delta, **converging** (gap closing)
- **Bounce → delta was small, then suddenly large** (gap opening
  discontinuously) accompanied by speed drop

The right action in all three is the same: align heading with
desired.  Model trained on (image, heading, desired, delta, speed)
gets this for free.

### "Land everywhere" case

If a bounce points the ship deep into the bank, the model sees mostly
land.  Hug-side bias (port-only training + mirror-flip for starboard)
gives the model an unambiguous answer: "turn so the port bank ends
up on my left" → find the water region in frame, turn toward it
consistent with hug rule.

### Detection (for telemetry + training-data labeling, not runtime)

Bounce vs hard-turn signature:

```python
delta_jump = abs(curr_delta - prev_delta)
speed_drop = (prev_speed - curr_speed) / max(prev_speed, 1.0)

is_bounce = (
    delta_jump > 30           # gap suddenly widened
    and speed_drop > 0.4       # speed also dropped
    and prev_speed > 0.5 * max_speed_seen  # was cruising
)

is_hard_turn = (
    abs(curr_delta) > 60
    and abs(curr_delta) < abs(prev_delta) + 5  # gap closing, not jumping
)
```

These flags get written into the dataset for analysis + training
weighting.  They are NOT used to modify runtime behavior.

### Fallback if Phase 7 evaluation shows persistent collision failures

If the model still grinds into banks after training (e.g., 5%+
collision rate per voyage), add a strategic-layer override as a
backstop:
- `BackoffStrategic` rotates commit toward `last_cruising_heading`
  for K ticks on collision
- Model still does the actual steering, just with a biased commit
  input

But try the simpler path first.  Most collision-recovery behavior
should emerge from training on the right inputs.

## Open questions to revisit before starting

1. **Mac MPS vs CUDA**: training speed on Mac MPS may be 5-10x
   slower than a cheap CUDA GPU.  Worth evaluating before
   committing.
2. **Replay buffer storage**: 1M frames at 384×192 raw ≈ 200 GB; at
   compressed JPEG ≈ 30 GB.  Affordable.  Decide between in-RAM,
   on-disk lazy, or hybrid.
3. **Frame stride**: do we need every tick frame in the dataset, or
   every 5th?  Reduces dataset size 5x at small cost.
4. **Negative-example weighting**: collision frames are rare; do we
   upsample them in training?

## References
- Kostrikov, Nair, Levine 2021 — "Offline Reinforcement Learning with
  Implicit Q-Learning" (the IQL paper)
- Mnih et al. 2015 — DQN (still the baseline for discrete-action visual RL)
- Janner et al. 2021 — Decision Transformer (alternative offline approach)
- `docs/learned_segmenter_plan.md` — companion L2 plan; if we go with
  the full learned controller, the segmenter plan is subsumed
- Memory: `cairo_to_equator` tag for the regression baseline

## Recommendation

**Start with Phase 0 (speed reading)** — useful regardless of which
direction we ultimately pick.  Then commit to Phases 1–4 as a
~1 week investment to evaluate whether the learned controller
approach is viable.  If Phase 4 evaluation looks promising, continue
with Phases 5–6.  If not, revert and keep iterating on the
classical stack.

The classical stack reaching the equator on its first attempt is
evidence the navigation problem is *not* impossible to solve
algorithmically; the question is whether the ongoing maintenance
cost of rule-stacking exceeds the upfront cost of ML training.
With game-vendor UI shifts happening multiple times this month
alone, the resolution-invariance benefit may justify the pivot
on its own.
