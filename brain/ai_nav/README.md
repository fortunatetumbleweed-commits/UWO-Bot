# brain/ai_nav — five-layer navigation stack

Branch: `ai_nav`.  Scaffold for the architecture recommended in
`docs/ai_navigation_landscape.md` §5.  See `docs/ai_nav_architecture.md`
for the design rationale + extension recipes.

## Layout

```
brain/ai_nav/
├── vision_input.py     VisionFrame + sources (ADB / file / static)
├── state.py            NavState, Heading, CommitDirection
├── pipeline.py         AiNavPipeline + PipelineConfig + HeadingKalman
└── layers/
    ├── heading.py       L1: HeadingLayer  (PCAHeading | CNNHeading | VLMHeading)
    ├── segmentation.py  L2: SegmentationLayer (V11Segmenter | Learned | VLM)
    ├── planner.py       L3: ShoreHugPlanner with commit_direction
    ├── tactical.py      L4: TacticalLayer (NoOp | QwenVLTactical)
    └── strategic.py     L5: StrategicLayer (NoOp | ClaudeStrategic)
```

## Pipeline at a glance

```python
from brain.ai_nav import AiNavPipeline, AdbVisionSource, NavState

pipe = AiNavPipeline(source=AdbVisionSource())     # all defaults
state = NavState()
for tick in range(500):
    state = pipe.tick(state)
    if state.planner_output and state.planner_output.command:
        hold(state.planner_output.command, state.planner_output.hold_ms)
```

The defaults (`PCAHeading + V11Segmenter + ShoreHugPlanner + NoOp +
NoOp`) reproduce the current behaviour of the legacy shore picker,
*except* the planner's port/starboard side and walk direction are now
decoupled — port-side comes from heading (L1), walk direction comes
from `commit_direction` (persistent state).

## Swap a layer

```python
from brain.ai_nav import PipelineConfig
from brain.ai_nav.layers.heading import CNNHeading
from brain.ai_nav.layers.tactical import QwenVLTactical

cfg = PipelineConfig(
    heading=CNNHeading(weights_path="..."),
    tactical=QwenVLTactical(model_path="..."),
)
pipe = AiNavPipeline(source=AdbVisionSource(), config=cfg)
```

No other code changes — the pipeline doesn't know which impl is plugged
in.

## Vision input swap (minimap → full screen)

The `VisionFrame` exposes both `minimap()` and `full_screen()`.  A
layer's choice of accessor determines which region it reads.
To swap a layer from minimap-based to full-screen-based:

1. write a new impl (e.g. `VLMSegmenter`) that calls
   `frame.full_screen()` instead of `frame.minimap()`;
2. pass it in via `PipelineConfig`.

No pipeline rewrite, no other-layer changes.  This is the
flexibility hook the user asked for during the 2026-06-21 scaffold
session.

## What works today vs what's stubbed

| Layer | Default | Status |
|---|---|---|
| L1 heading | `PCAHeading` (wraps legacy PCA reader) | works |
| L1 heading | `CNNHeading`, `VLMHeading` | stub — raises |
| L2 segmentation | `V11Segmenter` (wraps V11 mask) | works |
| L2 segmentation | `LearnedSegmenter`, `VLMSegmenter` | stub — raises |
| L3 planner | `ShoreHugPlanner` (commit_direction + shore-walk) | works |
| L4 tactical | `NoOpTactical` | works (no-op) |
| L4 tactical | `MoondreamTactical` | works — Moondream via Ollama |
| L4 tactical | `QwenVLTactical` | stub — raises |
| L4 tactical | `ClaudeTactical` | stub — raises |
| L5 strategic | `NoOpStrategic` | works (no-op) |
| L5 strategic | `ClaudeStrategic` | stub — raises |
| Heading smoother | `HeadingKalman` | works (off by default; pass in PipelineConfig) |

So the pipeline runs end-to-end today with default impls and produces
steering commands.  Replacements drop in one at a time.

## Tests

```
pytest tests/test_ai_nav_pipeline_smoke.py
```

Smoke test runs three ticks against a saved session frame and asserts
the pipeline produces a valid output without crashing.

## Runner — `tools/run_ai_nav_live.py`

```bash
# Live run against the phone:
python -m tools.run_ai_nav_live --max-ticks 500 --side port \
    --start-lat 30.17 --start-lon 30.54

# Dry-run replay against a saved session (no ADB, no tap firing):
python -m tools.run_ai_nav_live \
    --source file:data/sessions/live_centerline_2026-06-17T11-53-04 \
    --action noop --no-sail-start --max-ticks 50 --out-dir /tmp/replay
```

Output is a session dir in `data/sessions/ai_nav_<ts>/` with:
- `tick_NNNN.png` — raw minimap crops (one per tick)
- `trace.jsonl` — per-tick NavState snapshots in tick_viewer-
  compatible schema (so `tools/tick_viewer.py` and
  `tools/diagnose_shore_mask.py` work without modification)

The runner exposes `--source` (`adb` | `file:<path>`) and `--action`
(`adb` | `noop`) so the same pipeline drives live, replay, or
dry-run workflows.

## Next steps (per `docs/ai_navigation_landscape.md` §8)

- [x] Phase 0 — Scaffold
- [x] Phase 1 — `tools/run_ai_nav_live.py` runner
- [x] Phase 7 (out-of-order) — `MoondreamTactical` L4 impl via Ollama.
  Wire-in with `--tactical moondream`.
- [ ] Phase 2 — A/B compare runner output vs `run_centerline_live`
- [ ] Phase 3 — Heading CNN training script (autolabel from lat/lon delta)
- [ ] Phase 4 — Swap `PCAHeading` → `CNNHeading`; enable `HeadingKalman`
- [ ] Phase 5 — Label 500–2000 minimap crops for water/land
- [ ] Phase 6 — Swap `V11Segmenter` → `LearnedSegmenter`
- [ ] Phase 7 — Wire MLX Qwen2-VL-7B + `QwenVLTactical`
- [ ] Phase 8 — Wire `ClaudeStrategic` into recovery pathway
- [ ] Phase 9 — Retire the legacy shore picker in `tools/`
