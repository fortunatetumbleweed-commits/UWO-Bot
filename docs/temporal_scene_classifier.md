# Temporal Scene Classifier — two-stage perception with LLM aggregation

Status: design captured 2026-05-15. Calibration pending.

## Why this exists

`vision/scene_model.py` reduces a frame to a deterministic `SceneModel`
populated by region detectors. That works once we know which **family**
of screen we're looking at (overworld vs chromed UI vs transition).
Slice 1–6 built the detectors and the family classifier; the empirical
audit in `/tmp/big_icon_audit_fast.py` revealed two gaps that the
SceneModel alone cannot close:

1. **The big-icon signal is unreliable on its own.**
   OmniParser detects the lighthouse / ship icon in only ~38% of
   labelled `port_overworld` and `sea` frames; 4% of the time the
   "largest icon" is actually the shield (a button-typed element that
   the disambiguator then routes to `sea`); 32% have no zone element
   at all. Identifying the scene family from one detector is fragile.

2. **Transition states are inherently temporal.**
   `port_loading` (Info Card), `port_arrival_overlay` (City Overlay)
   and the docking animation are three phases of the *same* event
   ("arriving at a port"). The bot's polling rate doesn't match the
   game's animation rate — frames may repeat (the Info Card lingers
   for several seconds) or be missed entirely (the docking animation
   only flashes for ~1 second). No single-frame classifier can paper
   over that mismatch; the **sequence** of frames is what carries the
   meaning.

The fix is to add a thin Stage-1 classifier that asks **what FAMILY of
scene is this** using a small VLM (Moondream) that answers binary
questions, then keep a short time-series of those answers and let a
text LLM aggregate them into a single state estimate per tick.

This doc captures the design and the calibration plan. The code lives
in `vision/scene_model.py` (Stage 2, deterministic) and in
`tools/moondream_temporal_calibration.py` (calibration harness).

## Two-stage perception

```
Frame  ─► Stage 1 (Moondream binary cascade)
            │
            ├── Q1: "Is this a 3D rendered game world?"      → yes/no
            ├── Q2: "Is this a menu / UI screen with chrome?" → yes/no
            ├── Q3: "Is this a loading or transition screen?" → yes/no
            └── Q4: "Is this a popup or dialog overlay?"     → yes/no
            │
            ▼
         family ∈ {overworld, chromed, transition, overlay, unknown}
            │
            ▼
       Stage 2 (SceneModel, deterministic)
            │
            ├── top_left, bottom_chrome, left_menu, action_buttons,
            │   composite_buttons, panels — narrowed by family
            │
            ▼
         SceneModel  (port_overworld | sea | building | sub_menu | …)
```

### Why a binary cascade, not a single multi-class question

- Each binary question is short enough that Moondream answers fast and
  consistently. Multi-class prompts ("which of these five screens is
  this?") hallucinate.
- A no-confidence answer on Q1 falls through to Q2/Q3/Q4 — the cascade
  degrades gracefully to "unknown family".
- Stage 2 detectors are only run on the branch their family supports,
  so the answer narrows the search rather than racing against it.

### Why Moondream, not Qwen or Claude

| | Moondream-2B | Qwen2-VL-7B | Claude Vision |
|---|---|---|---|
| Latency (local) | ~0.5s | ~3s | ~2s + network |
| Cost | free | free | API charge |
| Binary Q&A accuracy | high | high | high |
| Free-form reasoning | weak | strong | strong |

Stage 1 only needs **binary classification with low latency**, which
Moondream nails. Stage 2 is structural code, not a model. Free-form
reasoning is reserved for Stage 3 (novel scene → Claude → KB).

## Temporal Q&A buffer

Every tick the bot stores the Stage-1 answers in a ring buffer:

```
buffer[i] = (timestamp, q1, q2, q3, q4, sceneModel.scene_type)
```

The buffer is short — last N frames (default N=8, covering ~20–40 s of
real time depending on poll rate). The LLM aggregator reads the buffer
and infers the **most likely current state**, with confidence.

This handles two failure modes that single-frame classification can't:

- **Duplicate frames** — the bot polls faster than the screen changes,
  so the same answers repeat. Aggregator output: "stayed in state X".
- **Missing intermediates** — the bot polls slower than a fast
  animation (the docking flash). Aggregator output: "transitioned X→Z;
  Y was probably skipped".

## FSM transition graph as LLM context

The LLM aggregator is given the FSM transition graph
(`memory/knowledge/fsm/states.json`) as part of its system prompt. The
graph tells it which transitions are physically possible. For example:

- From `sea` the legal exits are `world_map`, `port_loading` (arriving
  at a port), or `sailing_idle` (HUD-off cinematic).
- From `port_loading` the only progression is
  `port_loading → port_arrival_overlay → port_overworld`.
- `world_map → building` is impossible — there is no direct edge.

With that prior, a buffer like

```
sea, sea, port_loading, ???, port_overworld
```

is unambiguously interpreted as "the ??? frame was the
docking-animation phase of `port_arrival_overlay` that we missed", not
"unknown jump". The LLM doesn't have to *guess* what could happen — it
follows the graph.

## Polling-rate vs transition-speed mismatch

The bot's perception loop is not synchronised to the game's animation
clock. Production sessions show three regimes:

| Regime | Poll cadence | What it sees |
|---|---|---|
| **Dense** | ~0.5 s | Every transient state captured, often duplicated |
| **Sparse** | ~5 s | Fast transients skipped; stable states intact |
| **Production-like** | mixed (action-driven) | Some scenes polled twice in 1 s while a tap settles; others not polled for 10+ s while sailing |

The calibration script exercises all three. The expectation is that
the aggregator's accuracy degrades smoothly as cadence sparsens — and
that the FSM graph context recovers most of the missed intermediates.

## Calibration plan

Goal: measure how well the Stage-1 cascade + LLM aggregator infer the
ground-truth `screen_type` across the labelled corpus.

### Frame inventory (`data/labels.jsonl`)

```
sub_menu:                187
dialog_transaction:       75
dialog_event:             64
building_interior:        60
world_map:                57
dialog_gameplay:          51
loading:                  49
port_overworld:           45
sea:                      45
dialog_overlay:           41
port_loading:             33
port_map:                 31
result_screen:            26
main_menu:                26
port_arrival_overlay:     25
building_npc_overlay:             24
dialog_reward:            23
sailing_idle:             21
dialog_system:            21
dialog_game_notice:       16
other:                    16
announcement:             13
dialog_shop:               9
dialog_android:            2
total:                   960
```

25 distinct sessions; the two longest (`2026-04-15_16-05-29` with 333
frames and `2026-04-15_21-54-26` with 190 frames) contain enough
contiguous sequences to exercise temporal aggregation.

### Three subsampling variants

1. **dense** — every labelled frame in session order. Upper-bound for
   what the aggregator can possibly know.
2. **sparse** — every Nth frame (default N=5). Stresses missed-frame
   inference.
3. **production-like** — variable spacing: 0.5–1 s while frames belong
   to a "fast" category (transitions), 3–8 s while belonging to a
   "slow" category (stable navigation states). Stresses the realistic
   case where the bot polls hard during action and idles otherwise.

### Per-sequence harness

```
for session in sessions_with_temporal_data:
    frames = order_by_capture_time(session.labelled_frames)
    for variant in (dense, sparse, production_like):
        sequence = subsample(frames, variant)
        for f in sequence:
            answers = moondream_cascade(f.image)
            buffer.append((f.t, answers, scene_model(f).scene_type))
            inferred = llm_aggregator(buffer, fsm_graph)
            compare(inferred, f.label)
```

Scoring:

- Per-frame accuracy (inferred state vs label).
- Per-transition accuracy (did the aggregator place the boundary
  within ±1 frame of the ground-truth boundary?).
- Recovery rate (when a frame is dropped by sparse sampling, does the
  aggregator still emit the right state via FSM-graph inference?).

### Acceptance bars (pre-slice-7)

- Dense variant: ≥95% per-frame accuracy on stable states,
  ≥80% on transition states.
- Production-like variant: ≥85% per-frame accuracy overall.
- Aggregator must never invent a state that isn't a successor of the
  previous one in the FSM graph (zero illegal-transition emissions).

If any bar isn't met, the calibration output points at the dominant
failure mode (Moondream binary noise vs LLM hallucination vs missing
FSM edges) and slice 7 targets that gap specifically.

## Open questions

- **Sailing cinematic** is currently labelled `sailing_idle` (21
  frames). Detection is deferred — user keeps that mode off. The
  doc / code keeps it as a placeholder family member but the
  calibration doesn't score it yet.
- **Sub-states inside `port_arrival`** (loading card → docking flash →
  overlay) need their own labels in the FSM graph before the
  aggregator can pin them precisely. The graph today has `sea →
  port_overworld` as a single edge with `loading` as a generic
  transient. Slice 7 may need to split that edge.
- **LLM choice for aggregator** — Qwen-7B local vs Claude Haiku.
  Calibration will run both and pick on cost / accuracy tradeoff.

## See also

- `docs/scene_model_design.md` — Stage 2 deterministic perception.
- `vision/scene_model.py` — SceneModel dataclass + `detect_scene()`.
- `memory/knowledge/fsm/states.json` — FSM transition graph.
- `tools/moondream_temporal_calibration.py` — calibration harness (this slice).
