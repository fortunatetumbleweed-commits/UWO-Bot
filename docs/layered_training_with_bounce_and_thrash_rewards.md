# Layered Training: Bounce-Reward (Reactive) + Thrash-Reward (Strategic)

**Status:** Design draft, 2026-06-30.  Many decisions still open.

## 0. Why now

Two observations turn into the substrate for self-supervised training:

1. **`speed_kt` is a reliable, independent collision detector.**  A drop of
   ≥70-90% tick-to-tick is a bounce; hard steering (even max-aggressive
   96°/sec commanded rotation) doesn't drop speed.  Free, dense, untainted
   by the heading channel.  Memory: `project_speed_is_bounce_detector.md`.
2. **The Y-tip thrash failure mode is not a perception or steering bug.**
   The bot reaches the junction fine, picks a branch, bounces in the
   dead-end, returns, picks the same branch again.  This is a *memory /
   strategic-decision* gap, not a reactive one.  No amount of bounce-
   reward will fix it — it needs a different layer with a different
   reward signal.

Together: we have a clean reactive supervisor (bounce → bad reactive
behavior) and a clear failure mode at a different timescale that needs
its own supervisor.

## 1. The layered framing

Real production navigation systems (Waymo, Boston Dynamics, AlphaZero,
MuZero) all decompose by timescale.  We already have the layer skeleton
in `brain/ai_nav/layers/`.  Mapping each layer to its supervisor:

| Layer | Timescale | Decides | Failure mode it fixes | Supervisor (this doc) |
|---|---|---|---|---|
| **Reactive** (`tactical.py`) | 0.5–3 s | Should I turn this tick?  How hard? | Bounces, near-misses | **Bounce-reward** (§2) |
| **Local planner** (`planner.py`) | 3–30 s | Centerline waypoint, hug-shore commit | Channel-following, smoothness | Progress + smoothness aux (§3) |
| **Strategic** (`strategic.py` / `mission.py`) | 30 s – minutes | Which Y-branch?  Should I back out?  Where to go next? | Y-tip thrash, junction memory, dead-end recovery | **Thrash-reward + junction memory** (§4) |

**Crucial principle:** each layer gets a supervisor matched to its
timescale.  Don't try to teach the reactive model to remember which
Y-branch it tried — the signal would be too delayed and too sparse to
shape the policy.  Don't try to teach the strategic model to dodge
shorelines — bounce events are too noisy to inform 30-second decisions.

## 2. Reactive layer — bounce-reward training

**Status:** well-understood, ready to implement once §2.4 is decided.

### 2.1 Signal definition

For each session trace, compute per-tick bounce-credit by propagating
the bounce event backward K ticks with γ-discount:

```
bounce_t   = 1 if speed[t+1] / max(speed[t], 1.0) < 0.2 else 0
reward_t   = -Σ_{k=0..K} γ^k * bounce_{t+k}
```

Default: K=8 ticks (~24 s of lookahead), γ=0.7.  Both are
hyperparameters to sweep once we have the dataset built.

### 2.2 Filtering noise events

Not every bounce is a collision the bot caused.  Filters to apply:

- **Topology gate:** count bounces in `channel` / `junction` /
  `dead_end` only.  Pure open-water `topology=channel` with low
  `water_frac` neighbours is noise (likely scripted event or NPC
  encounter).  We already have `water_frac` and `topology` in the
  trace.
- **Speed-only triggers:** speed=0 with no heading discontinuity
  AND no shore visible in the bow-arc → likely game event, not the
  bot.  This filter is more complex; v1 can skip it and use the
  topology gate alone.

### 2.3 Model + training

- Add an auxiliary head to the existing v2 BC model
  (`brain/ai_nav/learned/model.py`): `bounce_likelihood ∈ [0, 1]`.
- Loss: BCE against the discounted bounce-credit, weighted ~0.3
  relative to the action-prediction CE loss.
- **Inference-time safety filter:** at decision time, query the head.
  If `bounce_likelihood > τ` (default τ=0.6) for the proposed action,
  fall back to a "safe" action (HOLD or last-known-safe steering).

This gives us:
- A model that *predicts* bounces several ticks ahead
- A drop-in safety filter that vetos high-risk actions without
  needing to retrain end-to-end

### 2.4 Sibling signal — frantic-steering penalty

**Layer:** **reactive** (same layer as bounce-reward — both are
self-supervised signals derivable from telemetry alone, both inform
the per-tick action policy).

**The signal:** when the bot's commanded rotation **reverses sign
several times in quick succession** (e.g. −96°, +96°, −96°, +96°…),
something upstream is wrong — usually a bad heading read causing the
planner to keep "correcting" past the goal.  Frantic flipping is the
*symptom*; bounces are the *outcome* that often follows.  Catching
the symptom gives us an earlier negative signal than waiting for a
bounce.

**Detection (no labels needed):**
```
flips_t = number of sign reversals in commanded_deg over last W ticks
mag_t   = mean |commanded_deg| over last W ticks
frantic_t = 1 if (flips_t ≥ FLIP_THRESHOLD  AND  mag_t ≥ MAG_THRESHOLD)
```
Defaults to validate: W=5, FLIP_THRESHOLD=3, MAG_THRESHOLD=30°.
Frantic-flip events are then propagated backward γ-discounted just
like bounces (§2.1).

**Why both signals together are better than either alone:**

| Signal | What it catches | When it fires |
|---|---|---|
| Bounce penalty | Actual shore collisions | At impact + retroactive K ticks |
| Frantic-flip penalty | Perception/decision instability before impact | While the planner is thrashing |

The frantic-flip signal catches the t1-t10 rough-start case from
voyage 9 — the bot commanded −96°/+96°/−96°/+96° while digging out
of the 180°-flipped heading.  A reactive model with this penalty
would learn "if I'm about to issue another big counter-rotation, my
upstream signal is probably wrong — back off, hold, let perception
resettle" instead of doubling down.

**Important caveat — this is a symptom signal, not a root-cause
fix.**  The actual root cause of voyage 9's rough start was a
180°-flipped heading at t1 (template won, PCA was correct).  Fixing
that is a higher-layer arbitration decision (planner-level detector
cross-check at cold-start, deferred).  The frantic-flip penalty
trains the reactive layer to be more graceful when the upstream
signal is degraded — *defense in depth*.

**Implementation footprint:** ~30 LOC in the v3 dataset builder
(§2.1).  Same γ-discounted backward propagation, same loss head
architecture, just a different trigger.

### 2.5 Open questions for §2

- **What's the safe-fallback action?**  Reverse-direction hold?
  HOLD + commit-bearing override?  Last action that didn't bounce?
- **Calibration of τ:** depends on bounce base rate.  Will need a
  held-out validation set to pick.
- **Curriculum?** Train first on clean shore-following sessions
  (Cairo→Khartoum stretch) before adding Y-tip / lake sessions where
  bounces are dense?

## 3. Local-planner layer — progress + smoothness aux

**Status:** smaller scope, secondary priority.

### 3.1 Signal definition

The local planner picks the centerline waypoint and emits the
`commit_bearing`.  Its job is "follow the channel without thrashing
the rudder."  Reward components:

- **Progress reward:** `+α * (lat_progress_toward_goal per tick)`.
  Goal bearing comes from §4; for v1 just use Cartesian distance to
  a fixed destination.
- **Smoothness penalty:** `-β * |Δheading_command|^2` — discourage
  whipping the rudder back and forth at decision-time.
- **Bounce penalty (shared with §2):** the planner also sees the
  bounce signal but with longer K (~20 ticks) since planner decisions
  have longer downstream effect.

### 3.2 Open question

- Is this layer worth training at all yet?  The current centerline +
  wp_inertia logic is classical and works.  Maybe defer until the
  reactive and strategic layers are both in shape.

## 4. Strategic layer — thrash-reward + junction memory

**Status:** open.  This is where the user's question lives.  Many
sub-decisions to resolve.

### 4.1 The Y-tip thrash, restated

```
       (south Sudan)
              |
       junction J  ← bot enters here
            /  \
           /    \
        right    left  ← branches
         |        |
       dead     (Lake)
       end
```

Failure mode observed in voyage 9 (session
`ai_nav_2026-06-30T07-59-21`, ticks 382–450):

1. Bot reaches J, picks **right** (heading-bias makes it look closer
   to goal).
2. Sails right ~30 ticks, bounces in dead-end (bounce #1).
3. Returns to J (~30 more ticks).
4. Picks **right again** because nothing tells it "you tried this
   already."
5. Repeat.  10 bounces in this region.

### 4.2 Two flavours of strategic supervisor

There are two paths, and we should probably do **both**:

**(a) Classical Trémaux-style edge memory** (cheap, well-understood):
- At each junction, record (junction_id, branch_taken, outcome).
- Outcome ∈ {progressed, dead_end, unknown}.
- On revisit: prefer branches with `outcome=unknown` > `progressed` > `dead_end`.
- This is the **memory** layer.  Doesn't need RL — just bookkeeping.
- Requires: stable junction identity (graph node), branch identity
  (edge), outcome detector ("did we make progress past this junction
  before returning?").

**(b) Learned strategic policy with thrash-reward** (the user's
intuition):
- A small model that picks branch given (junction features, goal
  bearing, history).
- Reward signal — this is the design question.

### 4.3 Thrash-reward signal — candidates

The user's intuition: punish "keeps getting in and out of the Y-tip."
Several ways to formalize.  All operate at strategic-decision timescale
(per junction-visit, not per tick).

**Option A — Revisit-without-progress penalty**

```
At junction J, bot picks branch B.
Wait until either:
  (i)  bot makes lat-progress of ≥δ past J in goal direction → reward = +1
  (ii) bot returns to J without (i) → reward = -1
  (iii) timeout (>200 ticks) → reward = -0.5
```

Simple, sparse but clear, no manual labeling.  This is the cleanest
formulation and probably the right v1.

**Option B — Cumulative revisit penalty**

```
penalty(J) = -k * (visits[J] - 1)^2
```

Punishes revisits quadratically.  Encourages avoiding J entirely
once it's been visited.  Risk: discourages legitimate revisits
(passing through a junction twice on a long voyage).

**Option C — Branch-conditional revisit penalty** (combines A and Trémaux)

```
Penalty for re-entering the same (J, B) combination after (J, B)
previously had outcome=dead_end.
```

This is just "RL learns to follow Trémaux's rule."  Probably
redundant with (a) — if we have the memory, we don't need to learn
the rule.

**Recommendation:** start with Option A.  It's the minimal sufficient
signal.

### 4.4 Strategic action space

At a junction, the actions are: `pick_branch_i` for i ∈ branches at
this junction.  Plus maybe `back_out` (return to previous waypoint
without committing).

**Open question:** what's the *state* the strategic model sees?
Candidates:
- Goal bearing (delta from current heading to destination)
- For each branch: bearing, water_fraction, has-target-marker
- Visit history at this junction (from (a) above)
- Recent bounce density (signal that we're in a thrash region)

This is where the layered framework gets concrete: the strategic model
shouldn't see raw frames; it should see *features computed by the
lower layers*.

### 4.5 Open questions for §4

- **Junction identification.**  Is junction J the same J across
  visits?  Need a stable hash — probably (lat, lon) bucketed to 0.1°
  + branch-bearing fingerprint.  Memory `project_river_islands_create_cycles.md`
  is relevant: multi-edge handling.
- **Outcome detection.**  How do we know we "made progress"?
  Threshold on lat delta toward goal?  Threshold on traversal time?
- **Lake / open-water case.**  At a lake the "junction" topology
  fires but there are no discrete branches.  Strategic layer needs to
  recognize "I'm in lake, the right action is frontier exploration,
  not branch-pick."  Probably orthogonal to thrash-reward.
- **Reward attribution across nested decisions.**  If at J1 you pick
  branch B1, sail past J2 where you pick B2, then bounce — does B1
  get credit or just B2?  Standard RL credit assignment, but worth
  thinking about.

## 5. Perception layer — supervised ship-heading CNN

**Status:** new proposal, 2026-06-30.  Sits **below** §2–§4.  Ships
first because it improves every layer above it.

### 5.1 What it replaces and what it doesn't

The current heading pipeline is template-matching + a self-healing
stack:

```
template_match  →  antipode_tiebreaker  →  physics_reject  →  motion_lock_break  →  noise_reject
```

Each stage exists to paper over a failure mode of the one before:

| Failure mode | Cause | Currently handled by |
|---|---|---|
| 0° vs 180° ambiguity | Template-match scores symmetric ship body identically at 0° and 180°; only the bow distinguishes them | `antipode_tiebreaker` |
| Single-tick heading spike | Template match latches onto wrong angle for one frame | `physics_reject` |
| Steady-state antipode lock | Wrong-side pick is locally consistent but globally 180° off | `motion_lock_break` |
| Sub-physics-threshold noise | Small wrong jumps that don't trip physics_reject but degrade steering | `noise_reject` |
| Ship icon fully occluded | Text / NPC sprite / village label on top of the icon | (no graceful handling — bot is blind) |

A small CNN trained on rotated + occlusion-augmented crops natively
solves **the first failure mode**: rotation-symmetric ambiguity goes
away because the model learns to weight the asymmetric bow region.
**It does not replace the rest.**

The rest still handle a different failure mode — *the bot can't see
the ship at all* — and they use **orthogonal signals** (commanded
rotation, lat/lon motion bearing) that remain valid when vision is
blocked.  So when the CNN reports low confidence (heavy occlusion),
the pipeline still gracefully degrades to motion-bearing + prior
+ physics gates.

**The post-CNN pipeline is therefore:**

```
ship_icon_CNN  →  (if conf < τ_perc) motion_bearing_substitute  →  physics_reject  →  motion_lock_break
                                                                   (antipode_tiebreaker REMOVED)
                                                                   (noise_reject likely removed; CNN provides smoother estimates)
```

### 5.2 Model + I/O

- **Input:** ~50×50 crop centered on the ship-icon location in the
  minimap.  Smaller than MNIST.
- **Output:** `(sin θ, cos θ)` for heading, decoded via `atan2`.
  Plus a scalar `confidence ∈ [0, 1]`.  Two regression heads on the
  same trunk.
- **Architecture:** 3-4 conv layers, ~10-50K params.  Runs on CPU
  in <5ms.  Fits trivially alongside the rest of the perception
  budget.
- **Loss:** `1 - cos(θ_pred - θ_true)` for the angle (a proper
  angular distance loss that wraps at 360°) + BCE on confidence
  trained against a synthetic occlusion-fraction label.

### 5.3 Data strategy

The MNIST analogy holds: we need fewer real labels than people
assume, because the variation is synthesizable.

**Real labels (target ~1-2K crops):**
- Mine ship-icon crops from existing session traces (50+ sessions
  available).
- Filter to high-confidence template-match picks, then **post-process
  through the full current self-healing stack** to get our best
  available "ground truth" heading.  These are weak labels — the CNN
  just has to match or beat them on aggregate.
- Skew the mining toward known hard cases: HUD-pirate-occlusion ticks
  (`memory/project_hud_pirate_sprite_occlusion.md`), village-overlap
  ticks, the t553-554 antipode-flip ticks.

**Synthetic augmentation (target 10× real):**
- Take a clean ship-icon crop, rotate by an arbitrary angle in
  software → exact angle label.
- Composite over minimap-background patches sampled from real frames
  (varied land/water/junction backgrounds).
- Overlay occlusion patches drawn from the *actual occlusion sources
  we observe in real frames*: HUD lat/lon digits, NPC sprites, village
  labels, the "Safe Waters" notice ribbon.  We know exactly what
  occludes the ship — we don't need generic ImageNet augmentation.
- Vary occlusion fraction across `{0%, 10%, 30%, 60%, 90%}` — the 60%
  and 90% buckets train the confidence head to back off.

**Total:** ~10-20K examples.  One afternoon of data prep, one evening
of training on CPU.

### 5.4 Open questions for §5

- **Bow asymmetry strength.**  Is the ship icon's bow distinctive
  enough at 50×50 resolution for the CNN to actually disambiguate 0°
  vs 180°?  Spot-check before committing.  If not, the antipode
  tiebreaker stays.
- **Confidence calibration.**  The threshold τ_perc that decides
  "fall back to motion bearing" needs validation against real
  occluded frames — sim the failure mode.
- **Re-using the template-match output as features?**  Could
  concatenate the template-match top-K scores as auxiliary inputs to
  the CNN.  Probably unnecessary — leave for later if needed.

### 5.5 Why this ships before §2-§4

- It replaces the antipode-disambiguation logic (~150 LOC) with a
  cleaner upstream signal.
- It improves the **labels** used to train the bounce-reward model
  in §2 (better heading → better state representation).
- It improves the **state features** the strategic model sees in §4
  (cleaner heading → better junction-decision input).
- It's plain supervised learning, no RL — lowest project risk of
  anything in this doc.
- Fail-safe: if it doesn't beat the current stack, we ignore the
  CNN output and keep the existing pipeline.  No regression risk.

## 6. What ties the layers together (and what doesn't)

**Shared:** bounce events.  Both §2 and §4 use them.  Reactive layer
uses them as immediate-tick penalty; strategic layer uses them as part
of the "outcome=dead_end" signal.

**Not shared:** action spaces.  Reactive picks {HOLD, L/R × short/med/long}.
Strategic picks {branch_A, branch_B, back_out}.  Different vocabularies,
different timescales, different models.

**Coupling discipline:** the strategic layer's output is a **commit
bearing** (or "use waypoint X").  It does NOT issue rudder commands.
The reactive layer's output is rudder commands.  It does NOT decide
"which branch."  Each layer's output is the next layer's input feature
or constraint.  This is what makes them independently trainable.

## 7. Phased rollout proposal

| Phase | What ships | Risk | Validation |
|---|---|---|---|
| **0** | This doc reviewed + key decisions pinned | — | User sign-off |
| **A** | Ship-icon CNN (§5): data mining + synthetic augmentation + tiny CNN trained | low | Held-out frames including HUD-pirate + village-overlap ticks; CNN matches/beats current self-healing stack |
| **B** | CNN wired into pipeline as primary heading source; antipode_tiebreaker removed; physics_reject + motion_lock_break stay as confidence-gated fallbacks | medium | Live voyage 10; compare bounce count + perception-event counts vs voyage 9 |
| **1** | v3 dataset builder: bounce-credit per tick + topology filter (uses CNN-improved heading labels) | low | Inspect on Cairo→Khartoum (clean) and Y-tip (noisy) sessions |
| **2** | BC model gets `bounce_likelihood` aux head, retrained on v3 | low | Held-out session, check head predicts known bounces in advance |
| **3** | Inference-time safety filter wired into `tactical.py` | medium | Live run; compare bounce count vs voyage 9 (11 bounces baseline) |
| **4** | Junction graph + Trémaux memory in `mission.py` (classical, no ML) | medium | Y-tip session: branch-pick switches after first dead-end |
| **5** | Learned strategic head with Option-A thrash-reward, offline trained from session traces | high | A/B vs classical Trémaux on a held-out junction-rich session |
| **6** | Local-planner aux training (§3) — only if §2 and §4 don't close the loop | low priority | — |

**Phases A and B** are the new perception work — they ship first
because everything downstream benefits from a cleaner heading
signal.  Phases 1-3 are bounce-reward (reactive layer).  Phase 4 is
Trémaux memory (classical, no ML).  Phase 5 is the real strategic-RL
experiment and depends on Phases 4 being in place.

Phases 1-3 are bounce-reward only and don't touch the strategic layer
— easy wins.  Phase 4 is the Trémaux memory and is mostly a
software-engineering task, not a training task.  Phase 5 is the real
RL experiment and depends on Phases 1-4 being in place.

## 8. Key open questions, consolidated

1. **§5.4 bow asymmetry** — is the ship icon distinctive enough at
   50×50 for the CNN to disambiguate 0° vs 180°?  Spot-check before
   committing; keep antipode_tiebreaker if not.
2. **§5.4 CNN confidence threshold τ_perc** — calibrated on real
   occluded frames, decides when to fall back to motion bearing.
3. **§2.4 safe-fallback action** when bounce_likelihood > τ.
4. **§4.5 junction identity** — bucketed lat/lon + branch fingerprint?
5. **§4.3 thrash-reward formulation** — confirm Option A.
6. **§4.4 strategic-layer state representation** — features from
   lower layers, not raw frames.
7. **§4.5 lake / open-water handling** — out of scope for this doc?
8. **Order of phases 4 and 5** — should Trémaux memory ship before
   we even attempt the learned strategic head?  (Recommendation: yes.)

## 9. References

- Memory: `project_speed_is_bounce_detector.md` — the substrate.
- Memory: `project_first_zero_bounce_voyage_2026-06-29.md` — voyage 9
  arc, where the Y-tip thrash was observed.
- Memory: `project_river_islands_create_cycles.md` — multi-edge
  junction graph needs.
- Memory: `project_future_island_done_kunz_2008.md` — deferred
  strategic-layer work that intersects this design.
- Doc: `docs/exploration_navigation_layers.md` — perception /
  mapping / planning split (the layered principle this doc extends).
- Doc: `docs/learned_navigation_controller_v2_reframe.md` — v2 BC
  reframe this builds on.
