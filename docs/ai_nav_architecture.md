# ai_nav — Five-Layer Navigation Architecture

> Branch: `ai_nav`.  Companion to `docs/ai_navigation_landscape.md`
> (the survey that selected this stack).  This doc describes the
> code organisation under `brain/ai_nav/`, the load-bearing
> interfaces, and how to extend each layer.

---

## 1. Why this exists

The legacy navigation stack (PCA heading + V11 brightness mask +
shore-walk picker) has two structural problems that surfaced
repeatedly in live runs through 2026-05 to 2026-06:

1. **Perception fragility** — bright UI overlays read as land,
   bow occlusion flips PCA 180°, dark coastal water reads as land.
2. **No state across ticks** — the picker recomputes from scratch
   every frame; a single bad reading commits the bot to the wrong
   shore.

The 2026-06-21 design decision: stop tuning the rule-based stack,
start replacing it layer by layer with the architecture in
`docs/ai_navigation_landscape.md` §5.  This branch is the
scaffold.

## 2. The five layers

| # | Layer | Cadence | Budget | Default impl | Future impl |
|---|---|---|---|---|---|
| 1 | Heading | per tick | < 5 ms | `PCAHeading` (legacy wrap) | `CNNHeading`, `VLMHeading` |
| 2 | Segmentation | per tick | < 50 ms | `V11Segmenter` (legacy wrap) | `LearnedSegmenter`, `VLMSegmenter` |
| 3 | Planner | per tick | < 20 ms | `ShoreHugPlanner` (commit-aware) | swap algorithm here |
| 4 | Tactical | every 5–10 s | ~3 s | `NoOpTactical` | `QwenVLTactical` |
| 5 | Strategic | every ~60 s | ~5 s | `NoOpStrategic` | `ClaudeStrategic` |

Layers L1 and L2 are **perception**.  Layer L3 is **deterministic
planning**.  Layers L4 and L5 are **deliberation**.  L4 and L5
own their own cadence gates — the pipeline calls them every tick
and they choose whether to actually fire.

---

## 3. The two load-bearing abstractions

### 3.1 `VisionFrame` — the input swap point

```python
@dataclass
class VisionFrame:
    raw: Image.Image
    tick: int
    wall_ts: float

    def full_screen(self) -> Image.Image: ...
    def minimap(self) -> Image.Image: ...
    def ship_crop(self, radius=64) -> Image.Image: ...
    def sea_hud_latlon(self) -> Image.Image: ...
```

The frame carries the *full* phone screenshot and exposes named
regions through accessors.  Layers ask for what they want.

**Why this matters.**  Today perception is minimap-only — heading
CNN, water segmenter, shore-walk all read the 400×190 minimap
crop.  The eventual goal stated 2026-06-21 is to swap minimap-
based perception for **full-screen VLM perception** that reads
the rendered 3D world directly.  With this abstraction, the swap
is:

```python
# minimap-based segmenter (today)
class V11Segmenter:
    def segment(self, frame):
        rgb = np.asarray(frame.minimap())   # <-- minimap region
        ...

# full-screen VLM segmenter (future)
class VLMSegmenter:
    def segment(self, frame):
        rgb = np.asarray(frame.full_screen())  # <-- full screen
        ...
```

`PipelineConfig(segmentation=VLMSegmenter(...))` and you're done.
No changes to the pipeline, no changes to other layers, no
changes to call sites.  Same shape for L1 and L4.

### 3.2 `NavState` — the working state passed through the stack

```python
@dataclass
class NavState:
    tick: int
    heading: Optional[Heading]
    commit_direction: Optional[CommitDirection]
    water_mask: Optional[np.ndarray]
    planner_output: Optional[PlannerOutput]
    tactical: Optional[TacticalDecision]
    strategic: Optional[StrategicDirective]
    lat, lon, breadcrumbs, cumulative_km, ...
```

Each layer reads NavState, writes the fields it owns, returns it.
Field ownership is strict:

- `heading` ← L1
- `water_mask` ← L2
- `planner_output` ← L3
- `commit_direction`, `tactical_dest_latlon`, `tactical_walked_path`
  ← L4 (LookaheadTactical writes these; L3 reads them)
- `tactical` ← L4
- `strategic` ← L5

`commit_direction` is the key state-across-ticks addition.  The
legacy picker used per-tick heading for both port/starboard side
AND walk direction; this branch splits them so a bow rotation
(collision, hard turn) doesn't reverse the walk direction.  See
§5 of `docs/ai_navigation_landscape.md`.

**Ownership note (2026-07-22):** with `LookaheadTactical` +
`HugPathPlanner`, L4 runs BEFORE L3 within the tick (tactical picks
the dest, then reflex-planner picks a waypoint along the walked
path).  L4 owns `commit_direction`, `tactical_dest_latlon`, and
`tactical_walked_path` in this stack.  L4 also runs every tick
here — the "cadence-gated L4" note in §6 below only applies to the
older `MoondreamTactical` / consult-style layers, not to
`LookaheadTactical`.  L4 SEARCH-mode continuity uses the tracker
described in `docs/tactical_search_tracker.md`, not a per-tick
skeleton walker call.

---

## 4. Adding a new layer impl

### Recipe — replace L1 with a CNN

1. Train (offline) — see `docs/ai_navigation_landscape.md` §8.1
2. Implement:

```python
# brain/ai_nav/layers/heading.py
class CNNHeading:
    name = "cnn"
    latency_budget_ms = 5.0

    def __init__(self, weights_path):
        self.model = load_cnn(weights_path)

    def estimate(self, frame, prior):
        crop = np.asarray(frame.ship_crop(radius=64))
        sin_cos = self.model(crop)
        bearing = (math.degrees(math.atan2(sin_cos[1], sin_cos[0])) + 360) % 360
        conf = self.model.confidence(crop)
        return Heading(bearing, conf, source="cnn")
```

3. Wire:

```python
cfg = PipelineConfig(heading=CNNHeading("weights.pt"))
pipe = AiNavPipeline(source=..., config=cfg)
```

No other code touches.

### Recipe — replace L2 perception with full-screen VLM

```python
class VLMSegmenter:
    name = "vlm_full_screen"
    latency_budget_ms = 500.0

    def segment(self, frame):
        img = frame.full_screen()        # <-- key swap
        result = self.vlm.query(img, "<REGION_TO_SEGMENTATION> water")
        return parse_to_mask(result)
```

Then `PipelineConfig(segmentation=VLMSegmenter(...))`.

The pipeline doesn't know L2 went from 50 ms to 500 ms — only the
layer's `latency_budget_ms` declaration changes (used by future
schedulers, not by the pipeline itself).

### Recipe — add a new layer

Don't.  The five-layer split is deliberate: any new component
fits into one of the five buckets by cadence (per-tick / mid /
slow).  A new "L6" is almost always a sign you're conflating two
concerns.  If you genuinely need it, extend `PipelineConfig` and
add another call in `AiNavPipeline.tick`.

---

## 5. The pipeline

```python
class AiNavPipeline:
    def tick(self, prior_state: NavState) -> NavState:
        frame = self.source.capture(prior_state.tick + 1)
        state = prior_state
        state.tick = frame.tick

        # L1 — heading (with optional Kalman smoothing)
        raw = self.config.heading.estimate(frame, state.heading)
        state.heading = (self.config.heading_smoother.update(raw)
                         if self.config.heading_smoother else raw)

        # L2 — segmentation
        state.water_mask = self.config.segmentation.segment(frame)

        # L3 — planner (owns commit_direction)
        state = self.config.planner.plan(frame, state)

        # L4 — tactical (cadence-gated)
        state = self.config.tactical.maybe_consult(frame, state)

        # L5 — strategic (cadence-gated)
        state = self.config.strategic.maybe_replan(frame, state)

        return state
```

That's the entire orchestrator.  All algorithm choices live in
the layer impls.

---

## 6. Cadence philosophy

- **Per-tick layers (L1, L2, L3)** run every frame.  Total budget
  ~75 ms leaves comfortable headroom inside the 1 Hz tick.
- **L4 (tactical)** uses a heartbeat + trigger gate.  Heartbeat
  ≥ 5 s; triggers: sustained `no_shore`, sustained low heading
  confidence, future "stuck detector."  When it fires it can take
  ~3 s — within budget because it's not per-tick.
- **L5 (strategic)** uses a slower heartbeat (60 s) + failure
  triggers.  When it fires it can take ~5 s and may call the
  Claude API.  Used for open-ended re-planning, not steering.

**Anti-pattern to resist**: calling L4 or L5 per tick.  See
Cradle's 5–15 s decision cycle in `docs/ai_navigation_landscape.md`
§3.6 — that's the ceiling for "VLM per decision," and the bot
cannot afford it at 1 Hz.

---

## 7. Heading smoothing

`HeadingKalman` is a placeholder 1-D Kalman over heading angle
(sin/cos-wrap-safe).  Treats `Heading.confidence` as inverse
measurement variance: low conf → prior dominates → smooth.
Off by default — pass into `PipelineConfig(heading_smoother=...)`
to enable.

Replace with an EKF when we plumb commanded turn rate as the
motion model.  See `docs/ai_navigation_landscape.md` §2.3 + §3.10.

---

## 8. Migration plan from the legacy stack

Phase rollout, each step independently shippable:

| Phase | Change | Effort | Risk |
|---|---|---|---|
| 0 | Scaffold (this commit) | done | none |
| 1 | Wire `AiNavPipeline` into a new runner alongside legacy | 1 day | low |
| 2 | A/B compare runner output vs `run_centerline_live` on saved sessions | 1 day | low |
| 3 | Add heading-CNN training script (autolabel from lat/lon delta) | 2 days | medium |
| 4 | Swap PCAHeading → CNNHeading + enable HeadingKalman | 1 day | medium |
| 5 | Label 500–2000 minimap crops for water/land | 1 week | medium |
| 6 | Swap V11Segmenter → LearnedSegmenter | 2 days | medium |
| 7 | Wire MLX Qwen2-VL-7B + QwenVLTactical | 3 days | high |
| 8 | Wire ClaudeStrategic into recovery pathway | 2 days | medium |
| 9 | Retire the legacy shore picker in `tools/` | 1 day | low |

Each phase keeps the legacy stack runnable so we can fall back.

---

## 9. What this scaffold deliberately does NOT do

- **No end-to-end RL.**  See `docs/ai_navigation_landscape.md` §6.1.
- **No pure-VLM-per-tick.**  See §6.3.
- **No SLAM.**  See §6.4.
- **No frontier-model in the hot path.**  See §6.5.
- **No FSM integration yet.**  The pipeline is currently
  independent of the bot's FSM / agent / planner.  Integration
  comes in Phase 1 of §8 — a runner that wraps the pipeline +
  ADB action layer.

---

## 10. Glossary

- **commit_direction** — persistent desired heading; survives bow
  rotations from collisions; updated only by L4/L5 or on hard
  structural events.
- **port-side** — bot's left (heading − 90°); the side the
  shore-hug planner tries to keep land on.
- **swap point** — the VisionFrame accessor a layer calls; the
  hook that lets minimap-based perception be replaced by
  full-screen VLM perception without pipeline changes.
- **layer** — one of L1–L5; replaceable via `PipelineConfig`.
- **cadence gate** — internal trigger logic inside L4/L5 that
  decides whether to actually fire when the pipeline ticks them.
