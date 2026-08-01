# Exploration navigation — perception, mapping, planning

The river/shore exploration stack follows the standard robotics
three-layer split.  Each layer has a clear job, talks to the next via
a well-defined contract, and ignores everything that isn't its job.

```
┌──────────────────────────────────────────────────────────────┐
│  PERCEPTION  — per-tick, stateless                           │
│  • Minimap → 16 sectors (water_fraction, nearest_dist)       │
│  • Minimap → water mask → medial-axis skeleton                │
│  • Per-frame topology classification (CHANNEL / JUNCTION /   │
│    DEAD_END / LAKE)                                          │
│                                                              │
│  "What does the current scene look like?"                    │
└──────────────────────────────────────────────────────────────┘
                          │
                          ▼  (per-tick TopologyKind label, sectors)
┌──────────────────────────────────────────────────────────────┐
│  MAPPING  — stateful, world-frame                            │
│  • JunctionGraph keyed by HUD lat/lon cell                   │
│  • Trémaux directional edge marks (forward / reverse)        │
│  • Trajectory log for reversal detection                     │
│  • Place graph IS the cross-tick memory                      │
│                                                              │
│  "Where have I been?  How are the places connected?"         │
└──────────────────────────────────────────────────────────────┘
                          │
                          ▼  (graph state, current node, edges)
┌──────────────────────────────────────────────────────────────┐
│  PLANNING  — decision per tick                               │
│  • TremauxPicker: DFS over the place graph                   │
│  • Hug-shore goal: local waypoint generation                 │
│  • VFH+: collision-avoidance commit                          │
│                                                              │
│  "What should I do next tick?"                               │
└──────────────────────────────────────────────────────────────┘
```

## The split that matters

The most important architectural principle: **perception is stateless,
mapping owns memory, planning consumes both.**

This is the *eye vs brain* analogy.  Your eye doesn't remember what it
saw 1 second ago — your brain does.  Same here: every tick produces a
fresh local classification from the current minimap; the *fact* that
we sailed through the same junction twice is recorded in the lat/lon-
keyed graph, not in the perception primitive.

Two ticks observing the same Y from different bow angles produce
different pixel-space skeletons but map to the same place-graph node
because the lat/lon cell is the same.  The skeleton answers "what
does this scene look like?"; the graph answers "where have I been?".

## Layer 1 — Perception

**Inputs:** minimap crop (181×384 px); HUD lat/lon OCR.

**Outputs:** per-tick `SectorSet` + per-tick `TopologyKind`.

**Files:**
- `vision/minimap_navigation_view.py` — 16-sector binarisation.
- `vision/water_skeleton.py` — medial-axis skeleton extraction
  (Zhang-Suen 1984) with branch-point and endpoint detection.
- `brain/goals/junction_detector.py` — arc-count classifier with
  Brunskill-2007 persistence filter (N=3 in production); will gain
  skeleton-classifier fusion in the next integration commit.

**Key properties:**
- **Stateless across ticks.**  Same input → same output.  The detector
  holds a small N-tick hysteresis buffer for the persistence filter,
  but that's an in-frame stability mechanism, not cross-tick memory.
- **Per-tick classification only.**  No prediction of upcoming geometry;
  no awareness of past geometry.  Anticipation is structurally
  impossible from a fixed-radius sensor (cited research dispatch
  2026-06-06).
- **Independent skeleton per tick.**  Skeletons from consecutive ticks
  are not stitched; the lat/lon graph in Layer 2 carries the cross-tick
  connection.

## Layer 2 — Mapping

**Inputs:** per-tick TopologyKind + HUD lat/lon + bow heading.

**Outputs:** evolving topological graph (nodes = junction cells,
edges = traversed paths with directional Trémaux marks).

**Files:**
- `brain/goals/junction_graph.py` — nodes, edges, trajectory log.
- `brain/goals/coverage_tracker.py` — visited-cell grid + STUCK verdict.

**Key properties:**
- **World-frame keying.**  Place nodes are addressed by coarsened
  HUD lat/lon cells (~0.1° / cell).  Two ticks at the same cell hit
  the same node regardless of pixel-space minimap differences.
- **Directional edge marks (Dudek 1991).**  Each traversed edge stores
  the direction it was walked; re-traversal in the reverse direction
  is detectable for backtrack reasoning.
- **Current limitation: tree, not graph.**  Single root, parent
  pointers, DFS stack.  Cycles (islands) are pinned as a future task
  in `project_river_islands_create_cycles.md` for a multigraph
  migration.

## Layer 3 — Planning

**Inputs:** current node, edge state, current sectors, current wp.

**Outputs:** per-tick action — `hold_left` / `hold_right` / `hold` /
`sail_stop` / `sail_start`.

**Files:**
- `brain/goals/frontier_picker.py::TremauxPicker` — DFS state machine
  over the place graph.
- `brain/goals/hug_shore.py` — local waypoint generation, VFH+ avoider,
  destination-anchored tangent (§13.21).

**Key properties:**
- **Trémaux DFS** for branched exploration with reversal detection.
- **Bug2 commitment** (§13.21) anchors the local tangent direction
  across noisy single-tick perception.
- **VFH+** is the collision-avoidance commit on top of the wp generator.

## Why this split

- **Debuggability.**  Each layer has its own trace.jsonl fields;
  divergence between layers is localisable.
- **Replay determinism.**  Perception is a pure function of the frame;
  mapping is a pure function of the perception trace + HUD; planning
  is a pure function of the graph state + sectors.  The replay
  harness (`tools/capture_voyage.py`) hangs off this directly.
- **Independent evolution.**  Adding the medial-axis skeleton in
  Layer 1 didn't require touching Layer 2 or 3.  Adding the multigraph
  migration in Layer 2 won't require re-deriving sector logic.

## What's deferred to which layer

| Future task | Layer | Memory ref |
|---|---|---|
| Skeleton + arc-count fusion | 1 | this doc |
| Width-ratio + heading-continuity scoring | 1+3 | research synthesis 2026-06-06 |
| Multigraph migration (cycles / islands) | 2 | `project_river_islands_create_cycles.md` |
| Kunz heading-integration island-done test | 2 | `project_future_island_done_kunz_2008.md` |
| Lake-traversal policy (perimeter-follow + cell-frontier exit-find) | 3 | `project_nile_ends_in_a_lake.md` |
| RiverExploreMission vs ShoreExploreMission | above 3 | `project_river_vs_shore_exploration_distinction.md` |

## Open-loop replay validates the split

The capture-voyage / replay-diff framework (`tools/capture_voyage.py`,
upcoming replay harness) only works because perception is a pure
function of the frame.  Replay re-runs each layer on captured frames:

1. Re-perceive each tick from `tick_NNNN.png`.
2. Re-map: feed perception into a fresh `JunctionGraph`.
3. Re-plan: ask `TremauxPicker.pick()` for the action.
4. Diff against the captured (human action + bot intent + ground-truth
   verdict label).

If any layer became stateful in a way that depends on physical
sailing (e.g., perception caching across ticks based on game time),
replay would diverge from live runs.  Treat that as a design red
flag if it shows up.
