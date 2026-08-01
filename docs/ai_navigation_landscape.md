# AI Navigation on a Mac mini — Survey + Recommended Stack

> Written 2026-06-16 during a session triaging two failures the V11
> shore picker hit at Nubia (Nile river): (1) PCA-based heading
> flipped 180° when the village sprite occluded the ship icon, and
> (2) the morphological-close fix for the water mask filled real
> village buildings as water, creating a "sail through the house"
> bug.  Both pointed at the same conclusion — hand-written
> perception rules can only carry the bot so far.  This doc surveys
> the AI-assisted navigation landscape, with extra explanations of
> the unfamiliar concepts, and lands on a concrete stack that fits
> a Mac mini's compute envelope.

---

## 1. The problem in one paragraph

The bot reads a 400×190 px minimap crop once a second.  It needs to
work out (a) which way the ship is currently pointing, (b) where
water vs land is, (c) which path along the river to commit to, and
(d) when something structural has happened (junction, dead-end,
lake, village overlay obscuring the ship) that requires a real
decision rather than a continuation of the current plan.  Today
every layer is hand-written: PCA on the green ship-sprite pixels
for (a), a V11 brightness-threshold mask for (b), a contour-walking
shore picker for (c), and breadcrumb loop-closure for (d).  The
hand-written stack is brittle in roughly the same way every time —
it works in clean cases and falls apart whenever the game paints
something new on top of the water (text labels, port markers, NPC
sprites, village buildings).

The question this doc answers: where would I plug AI into this
stack, what does the AI ecosystem actually offer that's relevant,
and what fits in a Mac mini's compute budget?

---

## 2. Concepts primer

You'll see these terms throughout the survey — here's what each
one actually means and why it matters for this bot.

### 2.1 Vision-language model (VLM)

A VLM is a single model that takes both an image and a text prompt
and emits text.  Internally it's almost always two things glued
together: a **vision encoder** (a CNN or ViT — vision transformer
— that converts the image into a small grid of embedding vectors)
and a **language model** that consumes those embeddings as if they
were extra tokens in the prompt.

The reason VLMs matter for this bot is that they let you write a
prompt like "Is the ship in open water or in a narrow channel?" or
"Where is the river going next — north, south, east, or west?" and
get a short text answer back, without writing the perception rule
yourself.  The tradeoff is cost: even a small VLM is ~1–10 sec per
call on a Mac mini and ~$0.001–$0.01 per call via API.  So VLMs
are great for **slow deliberation** (a few times a minute) and
catastrophic for **per-tick perception** (60× a minute).

Concretely in this project, **Moondream2** and **Qwen2-VL** are
both VLMs.  OmniParser is *not* a VLM — it's a UI-element
extractor that uses a vision-only model plus rule-based grouping.

### 2.2 "LLM writes code as skills"

Made famous by **Voyager** (Minecraft) and now baked into many
agentic systems: when the LLM hits a novel situation, instead of
giving it a single action to take, ask it to *write a small Python
function* that handles the situation.  Save that function to disk
keyed by some description of the situation.  Next time the bot
hits something similar, look up the saved function and run it
directly — no LLM call required.

The bot already has this pattern under a different name:
`learned_recoveries.json` is exactly Voyager-style skill curation,
the Claude consult path in `brain/recovery.py` is exactly the
skill-write path, and the fingerprint registry is the lookup
mechanism that avoids unnecessary LLM calls.  The Voyager work is
relevant because it's *the same architecture, more battle-tested*,
not because it's something to copy from scratch.

### 2.3 State estimation vs perception

Perception = "what does this single image show?"  State estimation
= "what is the true state of the world given a sequence of noisy
observations?"

PCA-on-ship-pixels is pure perception: every tick is independent,
and a 180° flip on one tick gives a 180° flip output.  A **Kalman
filter** is the canonical state estimator: it maintains a
probability distribution over the true state (here, heading +
angular velocity), updates the distribution forward through time
using a motion model (when the bot issues `hold_right` for 800 ms
the angular velocity should be roughly +120°/sec), and corrects
the distribution every time a new noisy observation comes in.

The key insight: when the per-frame measurement is sometimes
catastrophically wrong (180° flip on a village occlusion), the
filter can recognize it as a low-likelihood event under the motion
model and downweight it heavily.  The output is smoother *and* more
robust than any per-frame fix.

EKF (Extended Kalman Filter) and UKF (Unscented) are variants
that handle non-linear motion models.  For heading estimation a
plain 1-D KF over angle (or its sin/cos) plus an EKF for the angle
wraparound is enough.  This is 30-year-old robotics — it's not
"AI" in the modern sense, but it's the *correct* shape of the
solution, and any modern recipe (including ones built around small
CNNs) ends up adding a filter on top.

### 2.4 Quantization, model size, tokens/sec

When you see "Moondream2 (1.9B), ~10 tok/s, ~2GB at 4-bit," here's
what each piece means:

- **1.9B** = parameter count.  Each parameter is a number the model
  learned during training.  More parameters → more "knowledge" but
  more memory + more compute per inference.
- **4-bit** = quantization.  The model was trained with each
  parameter stored as a 16-bit float (2 bytes), and at inference
  we're storing each as a 4-bit integer (½ byte) plus a small
  rescaling constant.  This shrinks the model 4× with usually <5%
  quality loss.  4-bit is the standard for local Mac inference;
  8-bit is "safe but bigger"; 2-bit is experimental.
- **~2GB** is the on-disk + in-memory footprint after quantization.
- **~10 tok/s** is the *generation* rate.  A "token" is roughly
  3–4 characters of text.  So 10 tok/s means the model emits about
  30–40 characters per second.  Image-input VLMs spend extra time
  *processing the input image* — roughly 1–3 seconds before they
  start generating, regardless of output length.

For this bot the implication is: a VLM call costs 1–3 sec setup
plus 0.5–2 sec generation = call it ~3 sec end to end on a Mac
mini for a small VLM.  That's incompatible with 1 Hz steering,
fine for "every 10 ticks", trivial for "once a minute".

### 2.5 Segment Anything Model (SAM) and the distilled family

**SAM** (Meta, 2023) is a foundation model for image segmentation:
give it an image and either a point or a bounding box, and it
returns a high-quality mask for whatever object you indicated.  The
original SAM is ~600M parameters and far too slow for real-time
work on a Mac mini.

The distilled variants — **MobileSAM**, **EdgeSAM**, **FastSAM** —
replace SAM's heavy ViT-Huge encoder with a much smaller backbone
(ViT-Tiny or YOLOv8-seg) and recover most of the quality at 10–
100× the speed.  Reported numbers on M-series Macs: ~10–30 ms per
image at 1024×1024.  For a 400×190 minimap, this is overkill-fast.

For this bot the SAM family is interesting because:
- it handles UI-overlay vs real-land disambiguation *learned from
  examples*, not from a rule that says "fill holes ≤ N pixels";
- it can be fine-tuned on a few hundred labeled minimap crops to
  produce a water/land segmenter that's both accurate *and* aware
  of sprites/text as not-land overlays;
- the inference cost is well within the 1 Hz budget.

### 2.6 Mapless visual navigation

A class of robot-learning work where the robot doesn't have a
pre-built map of the world.  It learns directly from raw vision +
goal information to a steering command.  Berkeley's NoMaD / ViNT /
GNM lineage is the public-weights leader here: a small ViT or
ResNet encoder consumes the last few seconds of camera frames,
takes a goal as either an image or a graph subgoal, and emits a
velocity command (or a diffusion-policy distribution over commands).

For this bot the interesting bit is the *shape* of the model: small
encoder, recent-frames context, simple action head, no SLAM, no
map.  It would need minimap-domain pretraining (which doesn't
exist), so the relevant transfer is architectural, not the weights.

### 2.7 MLX, llama.cpp, CoreML — what runs models on Apple Silicon

These are the three viable runtimes for local model inference on
Mac mini.  Choose one based on what you're trying to ship:

- **MLX** is Apple's own framework, designed specifically for the
  unified-memory architecture of M-series chips.  Best raw
  performance per watt on Mac.  Has the `mlx-vlm` package which
  supports Qwen2-VL, LLaVA, Phi-Vision, Moondream, idefics out of
  the box.  Frequent updates.  This is the path of least resistance
  for everything new.
- **llama.cpp** is the cross-platform C++ runtime that defined the
  "run LLMs locally" wave.  Excellent for text-only work, lags MLX
  on VLM support (vision encoders ship later, conversion takes
  more steps).
- **CoreML** is Apple's traditional ML runtime, the one iOS apps
  use.  Fastest inference *if* you can convert your model to it,
  but the conversion path for VLMs is painful — you often have to
  surgically split the vision encoder, the projector, and the
  language model into separate CoreML packages and stitch them
  back together.  Worth the effort for a production iOS app, not
  worth it for a prototyping bot.

The recommendation for this bot is **MLX** for any local model
work, with **llama.cpp** as a fallback for text-only Claude-replacements.

### 2.8 Group-equivariant CNNs

A regular CNN, if you train it on photos of cats facing right, will
not necessarily recognize cats facing left — you have to augment
the training data with rotations and flips.  A **group-equivariant
CNN** is built from operations that are *guaranteed* to behave
consistently under a chosen group of transformations (rotations,
reflections).  If you train such a network on heading regression,
its 180° response is exactly the 180° response of its 0° input —
no flip ambiguity, no extra training data.

The library `e2cnn` (`github.com/QUVA-Lab/e2cnn`) provides these
building blocks.  For a 400×190 input this is overkill and a
regular CNN with sin/cos heads is enough, but it's worth knowing
the technique exists for harder cases (full ego-pose estimation,
top-down map alignment under unknown rotation).

### 2.9 Sin/cos output heads — why not just regress an angle

If you train a regression model to output an angle directly (say
in degrees), the loss function will punish "0° vs 360°" as if the
network were maximally wrong, when those are actually the same
heading.  Result: the network struggles to learn anything near
0/360.  The trick is to output `(cos θ, sin θ)` as a 2-D vector
instead.  Then `atan2(sin, cos)` recovers the angle, and the loss
on the 2-D vector is well-behaved at the wraparound.  This is
standard in robotics + orientation work and is the right shape for
the bot's heading-CNN replacement.

---

## 3. Open-source projects worth knowing

Curated to the ~10 most relevant to this bot.  Ordered roughly by
relevance.

### 3.1 Mobile-Agent-v2 (Alibaba X-PLUG)

`github.com/X-PLUG/MobileAgent`.  Reads phone screenshots, emits
taps.  Architecture: a *perception VLM* extracts what's on screen,
a *planner LLM* decides the next action, a *reflector VLM* checks
whether the action did what was expected.  Calls the perception
VLM roughly per action.  Originally built on GPT-4V + a separate
grounding model (GroundingDINO); now supports local Qwen2-VL.

**Why it matters:** this is the closest match to *this bot's I/O*
in the entire open-source ecosystem.  The three-VLM split is more
modular than the bot's current Claude-consult pattern and worth
borrowing if the bot ever moves to VLM-per-step UI navigation
(which is overkill today but might fit later).

**Direct deployment:** moderate effort.  You'd take their planner
+ reflector structure, swap in your own perception layer (the bot
has OmniParser + DialogModel + family classifier — strictly better
than their generic grounding).

### 3.2 AppAgent (Tencent)

`github.com/mnotgod96/AppAgent`.  Uses GPT-4V to *explore* an app,
write its own UI-element documentation as it goes, then operate
from the written docstring rather than calling the VLM every step.

**Why it matters:** this is *the same architecture as `explore_port`*
in your codebase.  Both walk through unknowns, ask a vision model
what each element does, write down what they learn.  AppAgent's
contribution is a clean separation between "exploration phase"
(VLM-heavy, expensive, one-time) and "operation phase" (cheap,
docs-driven, fast).  Worth a read of their paper for the
exploration-vs-operation framing even if you don't copy the code.

### 3.3 SeeClick / OS-Atlas

`github.com/njucckevin/SeeClick` and `github.com/OS-Copilot/OS-Atlas`.
Fine-tuned 7–10B VLMs specifically for "given a screenshot and an
instruction, return pixel coordinates to click."  Public weights.
Quantized to 4-bit they run in ~5GB at ~1–2 tok/s on M-series.

**Why it matters:** these are point-to-action grounding models —
exactly the thing OmniParser does for you today.  Not a reason to
rip out OmniParser, but if OmniParser ever fails on a novel UI,
SeeClick or OS-Atlas is the right escalation: a local VLM that
takes "tap the Set Sail button" + screenshot and returns pixel
coordinates.

### 3.4 GNM → ViNT → NoMaD (Berkeley / Sergey Levine group)

`github.com/robodhruv/visualnav-transformer`.  Mapless visual
navigation models, ~30M params, designed for Jetson-class robot
compute.  Take a short stack of recent RGB frames + a goal image
(or topological subgoal) and emit a velocity command.

**Why it matters:** the *input shape* (small image, recent context,
goal) and *output shape* (steering command) match this bot's
problem almost perfectly.  The runtime cost is well within the
Mac mini budget — likely 50–100 ms per inference.  What's missing
is minimap-domain pretraining; the weights they ship are trained
on first-person robot RGB, not top-down game minimaps.

**Direct deployment:** no public weights for your domain.  The
*architecture* is the borrowable bit: small ViT/ResNet encoder +
temporal context + diffusion-policy action head.  If you ever
collect 10–100 hours of human play, training a NoMaD-shaped model
on it is a reasonable mid-term project.

### 3.5 Voyager (NVIDIA / MineDojo)

`github.com/MineDojo/Voyager`.  GPT-4 plays Minecraft by writing
Python skills (Mineflayer bot commands) into a growing skill
library.  When the agent encounters a known situation, look up the
skill and run it; only when it hits a new situation does it call
GPT-4 to write a new skill.

**Why it matters:** as noted above, this is structurally identical
to `learned_recoveries` in this codebase.  The Voyager paper has a
clean exposition of *skill curation* — how to decide what's worth
saving, how to retrieve the right skill, how to evolve skills as
the agent's competence grows.  Worth a read for the framing.

### 3.6 Cradle (BAAI)

`github.com/BAAI-Agents/Cradle`.  General computer control via VLM.
Plays Red Dead Redemption 2, Stardew Valley, etc.  Architecture:
information gathering (VLM) → self-reflection (LLM) → task
inference (LLM) → skill curation (file system) → action planning
(LLM).  All five stages run per "decision cycle."

**Why it matters as a cautionary data point:** their reported
decision cycle is 5–15 seconds.  That's six VLM/LLM calls per
decision.  For a bot that needs to make a steering decision every
~1 second, pure-VLM-everywhere is not viable, and Cradle is the
clearest evidence of where that ceiling sits.  Read it to be sure
*your* hot path stays out of the VLM, not because you should copy
the architecture.

### 3.7 Pokémon Red speedrun RL (PWhiddy)

`github.com/PWhiddy/PokemonRedExperiments`.  PPO + a small CNN,
trained on a single workstation, learns to play Pokémon Red.  The
2024 follow-up (Anthropic's Claude-plays-Pokemon livestream) used
a VLM directly without RL.

**Why it matters:** evidence that *small-CNN RL* is workable for
sprite games on consumer hardware.  Not a direct recipe for this
bot (Pokémon's action space is much smaller than a continuous
steering bot), but a sanity check that you don't need a research
GPU cluster to do RL on a game.

### 3.8 MobileSAM / EdgeSAM / FastSAM

`github.com/ChaoningZhang/MobileSAM` and friends.  Distilled
versions of SAM, 5–25× smaller than the original.  Reported
latencies on M-series Macs: ~10–30 ms per image at 1024×1024.  At
400×190 this drops to single-digit ms.

**Why it matters:** this is the bottom-of-stack replacement for
the V11 brightness mask.  A few hundred labeled minimap crops + a
fine-tune of MobileSAM gives you a water/land segmenter that
*learned* what to do with UI overlays, NPC sprites, village
buildings, and shadows — rather than you writing 50 rules to
handle each case.

### 3.9 Florence-2 (Microsoft)

`huggingface.co/microsoft/Florence-2-large`.  A *task-conditioned*
vision model (not a chat VLM): you give it an image + a task token
(`<OD>` for detection, `<REGION_TO_SEGMENTATION>` for region-to-
mask, `<OCR>` for OCR, `<CAPTION>` for captioning) and it emits
the structured output for that task.  0.23B (base) or 0.77B (large)
parameters.  Reported latencies on M2: <100 ms per call.

**Why it matters:** Florence-2 is *probably the right shape* for
medium-frequency tactical questions on this bot — "is there a
village in this crop", "where is the river boundary", "what does
this label say".  Much cheaper than a chat VLM, much more capable
than a custom CNN trained from scratch.  Underrated in agent work
because it doesn't have a chat interface.

### 3.10 VINS-Mono / OpenVINS (state-estimation pattern)

Not a model — a *pattern*.  Visual-inertial odometry systems for
drones and ground robots.  They solve a problem mathematically
identical to "PCA-180° heading flips": noisy visual measurements
plus a smooth motion model.  Their answer is always the same: an
EKF over the state, where the visual measurement is a correction
with a covariance.

**Why it matters:** this is the right shape for the heading
problem, regardless of what you put in for the per-frame heading
reader.  Don't classify heading per frame.  Run a filter, treat
PCA (or a CNN, or whatever) as a noisy observation, and let the
filter own the smoothing + outlier-rejection.

---

## 4. Mac mini deployment envelope

Approximate numbers for an M2/M3 Mac mini with 16GB unified RAM,
running quantized models via MLX or llama.cpp.  Treat all numbers
as order-of-magnitude, not benchmarks — they shift with model
version, prompt size, and image resolution.

| Model | Params | Quant | Memory | Setup | Gen rate | When to use |
|---|---|---|---|---|---|---|
| Moondream2 | 1.9B | 4-bit | ~2 GB | 1–2 s | ~10–20 tok/s | Quick sanity checks; small visual queries |
| Qwen2-VL-2B | 2B | 4-bit | ~2 GB | 1–2 s | ~10–15 tok/s | Better grounding than Moondream |
| Qwen2-VL-7B | 7B | 4-bit | ~5 GB | 2–3 s | ~3–6 tok/s | Sweet spot for "small + useful" |
| MiniCPM-V 2.6 | 8B | 4-bit | ~5 GB | 2–3 s | ~3–5 tok/s | Strong OCR + grounding |
| LLaVA-Phi | 3B | 4-bit | ~2 GB | 1–2 s | ~5–8 tok/s | Decent general-purpose |
| Florence-2-large | 0.77B | 8-bit | <1 GB | <100 ms total | n/a (structured output) | Task-specific perception |
| MobileSAM | 10M | fp16 | <100 MB | n/a | <30 ms total | Segmentation |
| YOLOv8-seg-n | 3M | fp16 | <50 MB | n/a | <20 ms total | Segmentation, even faster |
| Custom heading CNN | ~100k | fp16 | <1 MB | n/a | <5 ms total | Per-tick heading |

**Implications for the bot:**

- A per-tick (1 Hz) call to anything in the VLM column is
  impossible: even the fastest VLM eats ~2 sec.
- A per-tick call to anything in the bottom three rows is trivial:
  segmentation + heading combined fit easily inside 100 ms with
  budget to spare.
- A "consult every 5–10 sec on stuck-detector fire" of Qwen2-VL-7B
  fits comfortably.  ~5 sec per call × ~5 calls per voyage = ~25
  sec of consult time per voyage, against ~5 minutes of voyage
  duration.  Totally fine.
- A "consult every minute" of Claude API fits regardless of local
  hardware.

---

## 5. Recommended stack

Five layers, top to bottom by latency.  Each layer answers a
different question, at a different cadence, with a different model.

### Layer 1 — Heading regression (per tick, <5 ms)

Tiny CNN, ~50k–500k parameters, sin/cos output head.  Input: the
400×190 minimap crop centered on the ship.  Output: heading angle
+ a scalar confidence.

**Training**: bootstrap labels from ticks where lat/lon delta is
reliable — these give you ground-truth heading from `atan2(Δlon ·
cos lat, Δlat)`.  You already have ~10k labeled ticks in session
logs.  No manual labeling needed.  Train with sin/cos MSE loss,
augment with rotations of the input + corresponding label rotation
(this is what makes the network rotation-aware).

**Post-processing**: a small Kalman filter over (heading, angular
velocity), with the commanded `hold_left`/`hold_right` deflections
feeding the motion model.  The CNN's confidence channel modulates
the measurement covariance — low-confidence frames (occluded by
village sprite) downweight automatically.

Replaces today's `estimate_ship_heading` (PCA + brightness).
Directly fixes the Nubia 180°-flip bug.

### Layer 2 — Water/land segmentation (per tick, <50 ms)

MobileSAM fine-tuned on minimap crops, or YOLOv8-seg trained from
scratch.  Input: 400×190 minimap crop.  Output: a boolean water
mask, same shape.

**Training**: 500–2000 hand-labeled crops.  The labeling effort is
real but one-time — you draw water/land boundaries on a sample of
ticks covering rivers, lakes, coast, and ports.  Include negative
examples explicitly: a tile of a village sprite that *is* on land
gets labeled as land even though the sprite is bright.

**What it fixes**: the V11 brightness-threshold's failure modes —
bright UI overlays getting classified as land, dark coastal water
getting classified as land, village buildings being morphologically
"filled" as water by your closing fix.  All of these go away
because the network *learns* the right answer per case.

Replaces `channel_mask_from_rgb`.

### Layer 3 — Symbolic shore-walk + commit direction (per tick, <20 ms)

Keep your existing shore-walk picker and breadcrumb infrastructure.
Add the **commit direction** concept discussed in the earlier
session: a persistent "desired heading" that survives bow rotations
and only changes at structural events (target acquired, sustained
no-shore, loop closure).

This layer is symbolic and stays symbolic.  It works at 1 Hz with
budget to spare, and AI doesn't make it obviously better.  The
commit-direction split is the *architectural* fix; the heading CNN
and the segmentation model are the *perception* fixes.

### Layer 4 — Mid-frequency tactical consult (every 5–10 s or on stuck, ~3 sec)

Qwen2-VL-7B in MLX, asked structured questions like:
- "Given this minimap, is the ship in a (channel | junction |
  dead_end | lake | open_sea)?"
- "Where is the river going to next — what compass bearing should
  the ship steer toward?"
- "Is there a village overlay obscuring the ship in this frame?"

Output gets parsed to a discrete decision and fed back into Layer
3 as a fresh commit-direction.

**Trigger conditions**: stuck-detector fires, breadcrumb loop
closure, repeated `no_shore` from the picker, or a periodic 10-sec
heartbeat to confirm the current commit is still right.

Replaces the brittle hand-written JunctionDetector + lake/dead-end
detection.

### Layer 5 — Slow strategic replanning (on goal failure or every ~minute, ~5 sec)

Claude API, Voyager-style.  Asked open-ended questions when
something has gone *structurally* wrong:
- "The bot has been stuck near (lat, lon) for 60 ticks.  Here's
  the minimap, the recent trace, and the goal.  Write a Python
  primitive to handle this."
- "The voyage from Cairo to (5.71, 32.12) was supposed to take 500
  ticks but we're at 800.  What went wrong, and what should the
  bot do?"

Output is either a new commit-direction, a new waypoint, or — in
the spirit of Voyager — a new Python skill saved to
`learned_recoveries.json` and registered for future use.

This is already mostly built — the Claude consult path in
`brain/recovery.py` and the learned-recovery persistence are the
substrate.  What's new is calling it from the navigation loop, not
just from the FSM recovery loop.

---

## 6. Anti-recommendations — what we explicitly are NOT doing

These are tempting and wrong for this bot's situation.  Worth
naming so they don't keep coming back.

### 6.1 End-to-end deep RL

"Train an agent end-to-end to sail the Nile with PPO."  Needs 10⁴–
10⁶ episodes (~years of bot-time at 1 Hz).  Produces a policy that
fails in incomprehensible ways.  This bot has ~10 episodes of
useful data total and needs every failure to be debuggable.  RL is
right when you have a fast simulator and unlimited compute; this
project has neither.

### 6.2 End-to-end imitation learning

"Record a human playing for 100 hours, train a policy to clone."
Needs the human data, which is expensive to collect, and produces
a policy that fails on situations the human didn't encounter.
Worth considering after the symbolic stack is solid and the
remaining failures are clearly *style* issues rather than logic
gaps.

### 6.3 Pure-VLM-per-tick

"Just ask Claude/Qwen what to do at each step."  Cradle proves
this hits a 5–15 sec/cycle ceiling on consumer hardware.  The bot's
1 Hz steering loop cannot afford this.  VLMs go in the mid-
frequency consult layer, not the hot path.

### 6.4 Full SLAM / ego-pose estimation

"Build a real-time map of the world from raw vision."  ORB-SLAM3
and friends are mature but heavy.  The bot has lat/lon HUD readings
that give you global pose for free.  The hard problem is *local*
navigation under perception noise, not building a global map.
SLAM is overkill.

### 6.5 Frontier model in the hot path

"Use Claude Opus / GPT-4o for every decision."  Per-call latency
(~3–10 sec) and cost (~$0.01/call) are both fine for occasional
deliberation but multiply badly at 1 Hz.  These models go in the
slow strategic replanner, not the tactical consult.

---

## 7. Glossary

Quick reference for the unfamiliar terms.

- **CNN** — Convolutional Neural Network.  The standard learnable
  image processor.  Cheap, fast, well-understood.
- **ViT** — Vision Transformer.  Replaced CNNs as the default
  vision backbone in many large models.  More flexible, often
  more accurate, generally slower at small sizes.
- **VLM** — Vision-Language Model.  Image-in, text-in, text-out.
- **LLM** — Language Model.  Text-in, text-out.
- **PPO** — Proximal Policy Optimization.  The standard RL
  algorithm for continuous control tasks.
- **RL** — Reinforcement Learning.  Learning from rewards.
- **IL** — Imitation Learning.  Learning from demonstrations.
- **SAM** — Segment Anything Model.  Meta's foundation model for
  segmentation.
- **EKF / UKF** — Extended / Unscented Kalman Filter.  Variants of
  the Kalman filter for non-linear motion models.
- **SO(2)** — the rotation group in 2D.  Equivariant networks built
  on it handle rotation cleanly.
- **MLX** — Apple's ML framework for M-series chips.
- **Quantization** — compressing model parameters to fewer bits
  per value at inference.
- **Token** — the unit a language model emits.  Roughly 3–4 chars.
- **tok/s** — tokens per second.  Standard throughput metric.
- **Skill curation** — the Voyager pattern of LLM-writes-code +
  filesystem-stores-it + retrieval-finds-it.

---

## 8. Suggested first prototypes

If you want to validate the stack incrementally, here's the
ordering that gives the most learning per hour of work:

1. **Heading CNN + Kalman filter** (~1–2 days).  Autolabel from
   lat/lon delta on existing session logs, train a tiny network,
   wire it into the picker.  Directly fixes the Nubia 180° bug
   and gives the bot a confidence channel PCA can't.
2. **Water/land segmenter** (~1 week including labeling).  Label
   500 minimap crops, fine-tune MobileSAM (or train YOLOv8-seg
   from scratch), swap in for `channel_mask_from_rgb`.  Fixes
   the entire class of UI-overlay bugs that the morphological
   close half-fixed.
3. **Commit direction in HugShoreGoal** (~1 day).  Pure refactor,
   no new models.  Decouples persistent intent from bow direction.
4. **Tactical VLM consult** (~3–5 days).  Wire Qwen2-VL-7B in MLX,
   write the prompt + parser, fire on stuck-detector.  Replaces
   JunctionDetector et al.
5. **Strategic replanner** (~2 days).  Extend the existing Claude
   consult to also write nav primitives.  This is the smallest
   step because the substrate already exists.

Total: ~3 weeks of focused work to a stack where every problematic
layer of today's pipeline has been replaced or upgraded.  None of
the steps depends on the next, so they can be reordered or done in
parallel.
