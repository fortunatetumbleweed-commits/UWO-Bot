# UWO Bot — Architecture Overview

Last updated 2026-05-22.  Snapshot of the runtime as it stands after
the village-arrival landing.  Refresh after YOLO Phase 4a integrates.

## High-level layer diagram

```
                            ┌─────────────────────────────────────┐
                            │  Entry points                       │
                            │    run.py            (chat REPL)    │
                            │    run_task.py       (YAML task)    │
                            │    brain/agent.py    (long-running) │
                            └────────────┬────────────────────────┘
                                         │ command
                                         ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │  Goal / Plan layer                                                   │
   │                                                                      │
   │    brain/goals/sail_to.py    SailToGoal — FSM-driven voyage          │
   │    brain/states/*.py         per-state handlers (docked, navigating, │
   │                              entering_building, exploring_port, …)   │
   │    brain/plan_loop.py        achieve_goal() — perceive → execute     │
   │                              → light_check / heavy_check → replan    │
   │    brain/plan.py             plan datatype + step categoriser        │
   │    brain/planner.py          high-level planner (Layer 3b)           │
   │    brain/recovery.py         recover_to_port_overworld / FSM BFS     │
   │    actions/task_runner.py    YAML-step iterator (sail / sell / buy)  │
   │    routines/runner.py        named-routine dispatch                  │
   └────────────┬─────────────────────────────────────────────────────────┘
                │ perceive()             ↑ tap / swipe
                ▼                        │
   ┌──────────────────────────────────┐  │  ┌──────────────────────────────┐
   │  Perception (brain/perceive.py)  │  │  │  Action layer                │
   │                                  │  │  │                              │
   │  Three passes in order:          │  │  │  actions/sail_actions.py     │
   │    1. Interruptors / obstructions│  │  │     building-list navigation │
   │    2. Active atomic flow         │  │  │     port-map navigation      │
   │    3. Navigation state           │  │  │     world-map navigation     │
   │       (chrome → fingerprint →    │  │  │     departure / arrival      │
   │        OCR → Moondream gate →    │  │  │  actions/market_actions.py   │
   │        Qwen → Claude fallback)   │  │  │     buy / sell / negotiate   │
   │                                  │  │  │  actions/explore_actions.py  │
   │  Emits PerceiveResult:           │  │  │     enumerate buildings      │
   │    { state, port, flow, detail,  │  │  │  actions/world_map_nav.py    │
   │      confidence }                │  │  │     pan / find / tap port    │
   │  Caches: per-frame OCR / Omni    │  │  │  actions/water_tap.py        │
   │  Updates: BotObservation         │  │  │     water-tap lat/lon read   │
   │           (brain/observation.py) │  │  │  actions/latlon_localize.py  │
   │                                  │  │  │     affine + water heuristic │
   │                                  │  │  │  actions/adb_actions.py      │
   │                                  │  │  │     tap/swipe/press_back     │
   └────────────┬─────────────────────┘  │  └──────────┬───────────────────┘
                │ frame                  │             │ shell input swipe
                ▼                        │             ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │  Vision (vision/*)                                                   │
   │                                                                      │
   │    capture/adb_capture.py     ADB screencap → PIL                    │
   │                                                                      │
   │    Structural detectors (deterministic):                             │
   │      chrome_detector.py     hamburger / home / back / right panel    │
   │      chrome_via_omniparser.py  same, via OmniParser elements         │
   │      ocr.py                 EasyOCR — port name, building list,      │
   │                             quantity fields, lat/lon                 │
   │      omniparser.py          Florence-2 — all interactive elements    │
   │      element_postprocess.py merge / dedup OmniParser elements        │
   │      screen_perception.py   region-based screen parse                │
   │      text_correction.py     fuzzy-match against port/village KB      │
   │      world_map_parser.py    parse visible ports on world map         │
   │      template_matcher.py    OpenCV template match                    │
   │                                                                      │
   │    Family/state classifiers:                                         │
   │      screen_classifier.py   fingerprint-based ScreenType lookup      │
   │      state_fingerprints*.py registered fingerprints + data           │
   │      scene_model.py         SceneModel (Stage-2, deterministic)      │
   │      scene_layouts.py       layout KB (memory/scene_layouts/)        │
   │                                                                      │
   │    Typed overlay detectors:                                          │
   │      obstruction_classifier.py  structural popup/dialog detection    │
   │      DialogModel + BuildingNpcOverlay + StoryNPCConversation         │
   │                                                                      │
   │    Local LLM:                                                        │
   │      local_vision.py        Moondream — yes/no on hand-crafted Qs    │
   │      moondream_cache.py     TTL + event cache for Moondream          │
   │      qwen_perception.py     Qwen2.5-1.5B-Instruct (mlx) — semantic   │
   │                             scene understanding when ambiguous       │
   │                                                                      │
   │    API fallback:                                                     │
   │      claude_vision.py       Claude Vision — first-visit scene labels │
   │      obstruction_consult.py Claude — dismissal for novel obstructions│
   │                                                                      │
   │    [planned] yolo_uwo.py    YOLO11n — family + navigation classes    │
   │                             (see docs/yolo_navigation_classifier.md) │
   └──────────────────────────────────────────────────────────────────────┘
```

## Decision loop (zoomed in)

```
   ┌────────────────────────────────────────────┐
   │ Goal.tick()                                │
   │                                            │
   │   state = perceive()           ◄─ ground   │
   │                                  truth      │
   │   if state matches arrival     ───► DONE   │
   │                                            │
   │   if state.in_flow                         │
   │       advance_active_flow()                │
   │   else                                     │
   │       dispatch by state.state              │
   │         building       → _handle_building  │
   │         port_overworld → _handle_overworld │
   │         sea            → _handle_sea       │
   │         world_map      → _handle_world_map │
   │         village        → ARRIVED if match  │
   │         loading        → wait              │
   │         unknown        → _handle_unknown   │
   │                                            │
   │   return TickResult(action, phase, delay)  │
   └────────────────────────────────────────────┘
```

The phase/state distinction is deliberate.  **State** comes from
perceive() (ground truth from vision).  **Phase** is the goal's
intent.  The dispatch table is keyed on state, not phase — when state
contradicts the phase (e.g. expected SAILING but seeing
port_overworld), the bot trusts ground truth and the goal re-routes.

## Perception cascade (zoomed in)

```
   frame ───► chrome_detector                 ◄── ~100 ms
                │
                ▼
            fingerprint_lookup                 ◄── ~5 ms (cached)
                │ specific?
        ┌───────┴───────┐
        ▼               ▼
       YES            NO
        │               │
        │               ▼
        │           ocr.read_port_name + read_building_menu  ◄── ~500 ms
        │               │
        │               ▼
        │           text_correction (fuzzy match port/village/waters)
        │               │ recognised?
        │       ┌───────┴───────┐
        │       ▼               ▼
        │      YES             NO
        │       │               │
        │       │               ▼
        │       │           moondream_cache.ask           ◄── ~500 ms gated
        │       │               │ confidence high?
        │       │       ┌───────┴───────┐
        │       │       ▼               ▼
        │       │      YES             NO
        │       │       │               │
        │       │       │               ▼
        │       │       │           qwen_perceive          ◄── 5-8 s
        │       │       │               │ confidence high?
        │       │       │       ┌───────┴───────┐
        │       │       │       ▼               ▼
        │       │       │      YES             NO
        │       │       │       │               │
        │       │       │       │               ▼
        │       │       │       │           claude_vision  ◄── ~5 s API
        │       │       │       │               │
        └───────┴───────┴───────┴───────────────┘
                                │
                                ▼
                       PerceiveResult
```

Each tier exists because the prior tier didn't have enough information
to commit.  YOLO Phase 4a slots in at the *very top*: a 50 ms call
that, when confident, short-circuits the entire cascade.

## Knowledge base layout

```
memory/knowledge/
├── fsm/
│   ├── states.json              navigation states + transitions
│   ├── flows.json               atomic flows (market_buy, harbour_depart, …)
│   ├── interruptors.json        popup signatures + dismissal methods
│   └── learned_recoveries.json  Claude-recommended dismissals (cached)
│
├── ports/<slug>.json            which buildings exist in each port
├── buildings/<port>__<bld>.json visit history + actions per building
├── building_types/<type>.json   cross-port building behaviour
├── markets/<port>__market.json  per-visit price snapshots
│
├── scenes/<type>__<title>.json  full SceneInventory from Claude
├── scene_layouts/<screen>.md    layout KB feeding Qwen
├── state/<state>.json           per-state element / action registry
├── plans/<predicate>/*.json     plan presets per predicate
├── flows/<flow>/*.json          per-flow assets (screenshots, steps)
│
├── obstruction_analyses/<hash>__<goal>.json
│                                cached Claude consult results
├── learned_fingerprints/*.json  fingerprints learned at runtime
│
├── world_map/
│   ├── port_coordinates.json     224 ports — game (x,y)
│   ├── village_coordinates.json  69 villages — game (x,y)
│   ├── calibration.json          scale persisted between runs
│   ├── latlon_to_catalogue.json  affine (lat,lon) ↔ catalogue (x,y)
│   └── latlon_samples.json       calibration samples
│
├── control/qwen_chrome_glossary.md  prompt context for Qwen
├── strategy/*.json                  bot-level strategy knobs
└── trade_log/*.json                 per-session trade results
```

Brain modules read this KB via thin wrappers:

- `brain/kb.py` — `control()` and `strategy()` namespaces
- `brain/fsm_registry.py` — FSM graph singleton (states, flows, interruptors)
- `brain/predicate.py` — predicate evaluation against `plans/`
- `actions/sail_actions.py` — direct reads for `ports/`, `buildings/`,
  `world_map/`

## Cross-cutting structures

### `brain/observation.py` — `BotObservation`

Singleton with the most recent classified state.  Decays after
~30 s, so stale state isn't acted on.  Populated by `perceive()`,
consumed by the goal layer to avoid re-classifying mid-tick.

### `brain/goal_context.py` — goal stack

Lets the obstruction-consult layer know which goal is active when an
unfamiliar popup appears, so Claude can decide if the popup is
goal-related.

### `brain/fsm_registry.py`

Loads `memory/knowledge/fsm/*.json` once, exposes:
- `detect_state(frame)`  → which navigation state
- `path_to(target_state)` → BFS through the state graph
- `flows_for(state)` → which atomic flows can be active here
- `add_state()`, `add_transition()` for runtime learning

### `actions/world_map_nav.py` — `WorldMapNavigator`

Catalogue-based world-map navigation.  Same class powers both
port and village navigation (the village navigator is a factory
that swaps the catalogue).  Carries scale across iterations,
persists calibration across sessions, dead-reckons when no port
labels are visible, falls back to water-tap localization via
`actions/latlon_localize.py` when no anchor is available.

### Data flywheel

When a local detector is uncertain, Claude labels the example and
`training/collector.py` saves it under `data/training/<category>/`.
Periodically the local detector is retrained on the accumulated
labels.  Categories today: `market_tile`, `scene_type`,
`obstructions`.  YOLO becomes a fourth category.

## What feeds what (one-line summary)

```
ADB screencap ───► capture/adb_capture
                       │
                       ▼ PIL.Image
              vision/* (cascade above)
                       │
                       ▼ PerceiveResult
              brain/perceive
                       │
                       ▼ state/port/flow/detail
              brain/observation (singleton snapshot)
                       │
                       ▼
              brain/goals/* + brain/states/*
                       │
                       ▼ action intent (tap/swipe/exit)
              actions/* (sail / market / explore / world_map_nav)
                       │
                       ▼ x, y
              actions/adb_actions
                       │
                       ▼ shell input
                  ADB device
```

Knowledge KB sits orthogonal — read by every layer, written by the
perception cascade (cached scenes), the planner (learned recoveries),
and the action layer (port records, market snapshots).

## Where YOLO will plug in (planned)

```
   vision/yolo_uwo.py                                      ◄── ~50 ms
        │ classify_family() + detect_navigation()
        ▼
   FamilyVerdict + list[NavigationDetection]
        │
        ▼
   brain/perceive._classify_nav_state
        │ if YOLO confidence ≥ 0.7
        │     return mapping[YOLO family]    # short-circuit
        │ else
        │     fall through to existing cascade
```

Phase 4a (family classifier) replaces most of the chrome → fingerprint
→ OCR → Moondream chain.  Phase 4b (navigation classifier) adds new
information the current pipeline doesn't have (proximity events: Quick
Supply prompts, nameplates, landmarks, enemy/player fleets).

See `docs/yolo_navigation_classifier.md` for full design,
`docs/yolo_phase4b_labeling.md` for the labelling plan,
`docs/perf_analysis_2026-05-22_sail_to_berber.md` for the
expected wall-clock impact.

## What this doc deliberately doesn't cover

- Per-flow detail (market negotiation, harbour-departure step sequences) — those live in their own KB files.
- The labeler / supervisor web UI architecture — see `supervisor/`.
- Combat / auction / guild perception — not yet implemented.
- The data flywheel internals — see `training/collector.py`.
- Tests — see `tests/` (organised by feature, not module).

## File-count summary

| Layer | Files | LOC (top contributors) |
|---|---:|---|
| Vision | 25 | omniparser 634, qwen_perception 662, scene_model 764, claude_vision 803 |
| Brain | ~30 | perceive 2841, agent 1548, recovery 1130, plan 708, plan_loop 658 |
| Actions | ~12 | sail_actions 4185, market_actions 1978, world_map_nav 998 |
| Capture | 1 | adb_capture |
| Tools | ~20 | tree_simulator 811, seed_layout_from_frame 657 |
| Tests | ~50 | feature-organised |
| Memory KB | hundreds of JSON | continuously growing |
| **Total** | ~150 .py | ~45 000 LOC |
