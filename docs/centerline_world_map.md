# Centerline-based World Map — perception, mapping, navigation

A SLAM-style world-map design built on the M6f centerline-extraction
prototype (`tools/centerline_extraction_prototype.py`) and the
waypoint picker (`tools/centerline_waypoint_prototype.py`).

This doc captures architectural decisions made during the
2026-06-12 design discussion that the M6f prototype motivated.  It
covers:

1. Why the per-tick centerline tree is not enough — the U-turn failure
   mode in closed-loop sim.
2. The world-map data structure that lets the bot commit to a course.
3. How the same extractor handles river / coast / lake / open-water
   modes by varying only the "navigable corridor" mask.
4. The merge rules that distinguish world-stable features (junctions,
   dead-ends) from observer-relative ones (frame-edge anchors).

Implementation status: **Piece A in flight** — see
`sim/centerline_world_map.py` and `tools/sim_centerline_waypoint.py`.

---

## The problem the world map solves

M6f extracts a centerline tree from the current minimap frame.  At
each tick the tree is fresh.  The waypoint picker walks the tree
from the ship's projection toward a 50-px-ahead destination using a
hug-side rule at junctions.

Closed-loop sim on the canonical Nile reference (a3a073d):

- Tracked the reference cleanly from lat 30 down to ~lat 18.
- Then **U-turned** and ended back at lat 28, 3040 km from the goal.
- Mean cross-track deviation 41 km, max 95 km — i.e. it stayed
  *on* the river the whole time, but reversed direction.

The U-turn happens because the picker has no persistent state.  Each
tick it re-derives "which end of the current edge is forward" purely
from the instantaneous heading.  At a sharp bend the rate-limited
heading drifts perpendicular to the channel; both ends look
near-equally-aligned; the picker flips the chosen end; the
rate-limiter slowly turns the ship around.

The fix is memory: the bot has to know *I am traveling from A to B
along edge E* and only re-evaluate that commitment when something
explicit happens (junction, dead-end, recovery).

## Architectural insight — one extractor, mode-specific corridor mask

A natural objection to a centerline-tree world-map: the tree
representation seems tied to rivers.  Open-water coast hugging looks
like a polyline, not a branching tree.

But hugging a coast IS building a tree — one that mostly doesn't
fork.  The forks it DOES have (river mouths, bay entrances,
peninsulas joining a strait) are exactly the decision points where
the bot should choose.  The unification falls out by varying *only*
what counts as the "navigable corridor" before running M6f:

| Mode | Navigable corridor = | What M6f finds |
|---|---|---|
| **River channel** | water bounded by banks (V11 mask) | medial axis = channel centerline (tree) |
| **Coast hugging** | water ∩ (within K pixels of land) | medial axis = centerline of a coastal strip (degree-2 chain) |
| **Lake/bay traversal** | water bounded by enclosed land | medial axis of the basin (tree or cycle) |
| **Open ocean** | (no land in view) → corridor undefined | (no graph; fall back to pure goal navigation) |

The coastal strip is a one-liner with
`scipy.ndimage.binary_dilation`: dilate the land mask by K pixels
and intersect with the water mask.  The K-pixel offshore distance
is the tuneable "sail this far from shore" parameter.

Consequences:

- **Same extractor everywhere.**  No mode-dispatch class hierarchy.
  M6f's outputs always have the same shape (`Tree` of polylines).
- **Forks happen where you'd want them.**  Coast meeting a river
  mouth → real fork in the corridor mask → real fork in the tree →
  real decision point.  Peninsula tip → either a dead-end (if your
  strip encloses it) or a smooth bend (if it doesn't).
- **Cross-mode transitions are detectable from tree shape.**  Long
  chain with no forks = coast or open river run.  Branching = approach-
  ing a junction.  Closed cycle = going around an island.  The picker
  doesn't need a mode label to behave correctly.
- **Hug-side rule is identical in every mode.**  "Hug left" = keep
  the bank/coast on your port side.  Same math, river or coast.

Caveats:

1. **K (coastal-strip width) matters.**  Too narrow → bot scrapes
   shore in chop; too wide → bot wanders far offshore.  Hardcode for
   the prototype (~20 px ≈ 5–10 km); make it speed/weather-dependent
   later.
2. **Open ocean genuinely needs a fallback.**  When the corridor
   mask is empty, M6f produces no tree.  That's the signal to switch
   to pure goal-bearing navigation.  No mode classifier needed — the
   trigger is "extractor produced nothing."
3. **Islands break the tree.**  Coast wrapping an island is a cycle,
   not a tree.  Same issue rivers have with mid-river islands; see
   `project_river_islands_create_cycles`.  Deferred fix:
   multi-edge graph (not strict tree) in the WorldMap.

## The WorldMap data structure

The WorldMap is a generic planar graph in world coordinates (lat,
lon).  Mode-agnostic at the data layer — what differs per mode is
just node `kind` labels and the upstream corridor mask.

```python
@dataclass
class WorldNode:
    id: str
    kind: str          # "junction" | "dead_end" | "headland" | "river_mouth" | …
    lat: float
    lon: float
    mode: str          # "river" | "coast" | "lake" | "open"
    n_observations: int

@dataclass
class WorldEdge:
    a: str             # WorldNode id
    b: str             # WorldNode id
    polyline: list[tuple[float, float]]   # lat, lon trail walked
    mode: str
    explored: bool     # have we walked the whole edge end-to-end?

@dataclass
class WorldMap:
    nodes: dict[str, WorldNode]
    edges: list[WorldEdge]
    bot_state: BotState                   # "on edge E, headed toward node X"
    mode_history: list[tuple[int, str]]   # (tick, mode)
```

## Which local-tree nodes are world-stable

Not all M6f node kinds represent world-fixed locations.

| Local node kind | World-stable? | Why |
|---|---|---|
| **Junction** | ✅ Yes | A river fork at lat 25.3 stays at lat 25.3 |
| **Dead-end** | ✅ Yes | A lake tip is a fixed world feature |
| **Frame-edge anchor** | ❌ No | "The channel continues off-screen" — slides as the bot moves |

Only junctions and dead-ends are candidates for cross-tick
consolidation.  Frame-edge anchors are *transient observations* of
"there's more channel that way."  They're useful for steering the bot
toward unexplored territory but never become world-map nodes.

## Pixel → world coordinate transform

At each tick, the bot's `sim_lat, sim_lon` plus its pixel position
`ship_xy` define a linear map from minimap pixels (y, x) to world
(lat, lon):

```
world_lat = sim_lat  - (py - ship_y) / px_per_deg_lat
world_lon = sim_lon  + (px - ship_x) / px_per_deg_lon / cos(sim_lat)
```

`px_per_deg_lat` and `px_per_deg_lon` are fixed properties of the
minimap zoom level.  Need to be **calibrated once** empirically:
e.g. take two consecutive reference ticks with known
(lat, lon) delta, run cross-correlation between the frames, recover
the pixel shift.

**Calibration matters more than absolute accuracy:** the merge
threshold (~3 km) tolerates a few % scale error.  Within-region
consistency matters more than absolute correctness — the WorldMap
is self-consistent even if its lat/lon is off by 0.1°.

## Data association — when is "this junction" the same as "that junction"?

Standard nearest-neighbour-within-threshold:

```
For each newly-observed stable node n_new at (lat, lon):
  find nearest existing global node n_old with
      dist(n_new, n_old) < MERGE_THRESHOLD_KM (= ~3)
  if found:    update n_old's position via EWMA, increment n_observations
  else:        add n_new as a new global node
```

Edges follow:

- Bot is "on edge E from A to B."  Each tick, confirms current edge by
  matching the local tree's edge-at-ship-projection to E.
- When the bot's world position passes B's lat/lon → arrival at B.
  Trigger junction logic: enumerate B's neighbours, apply hug rule,
  commit to next edge.
- New edges/nodes emerge when the local tree shows topology beyond
  what's in the global map.

## The bot's commitment under this design

```
bot_state = ("on edge E from A toward B", arc_length_along_E)

each tick:
  1. extract local tree from current frame
  2. transform stable local nodes → world coords → merge into WorldMap
  3. localize bot on the world graph by polyline cross-track
  4. picker decisions:
     - if bot still on E and not near B → keep heading toward B
       (waypoint = next polyline point along E toward B)
     - if bot near B → arrival at junction →
         * lookup B's neighbours in WorldMap
         * apply hug-side rule among neighbours
         * commit: bot_state = ("on edge B→<next>", 0)
     - if local observation contradicts E → recovery
       (left the channel, or perception failure)
```

The U-turn issue vanishes because **the commitment lives in the
WorldMap, not in the per-tick tree**.  Local perception drifts
won't flip it — only crossing into a new world node does.

## Implementation in two pieces

**Piece A** — build the WorldMap during sim *without* using it for
navigation.  Output: a final WorldMap rendered over the trajectory
plot.  Lets us validate merge thresholds and node counts before
trusting the map for decisions.  Files: `sim/centerline_world_map.py`,
update `tools/sim_centerline_waypoint.py`.

**Piece B** — wire the WorldMap into the picker.  Bot now walks the
global graph instead of the per-tick tree.  Files: extends Piece A.

Open questions to revisit during Piece A:

- Is `MERGE_THRESHOLD_KM = 3` too tight (fragmenting the same
  junction) or too loose (fusing nearby distinct features)?
- Does the bot's dead-reckoned `sim_lat, sim_lon` drift enough over
  800 ticks to require loop-closure correction?
- How many distinct junctions does the Nile descent produce, and
  do they correspond to recognisable bends?

## References

- M6f extraction: `tools/centerline_extraction_prototype.py`
- Waypoint picker: `tools/centerline_waypoint_prototype.py`
- Closed-loop sim: `tools/sim_centerline_waypoint.py`
- Per-tick replay: `tools/replay_centerline_waypoint.py`
- Test corpus: `data/test_frames/channel_perception/`
- Ground-truth paths: `data/test_frames/desired_path/`
- Reference voyage: `data/sessions/reference_nile_full_20260606_141824`
- Related design docs:
  - `docs/exploration_navigation_layers.md` (perception/mapping/planning split)
  - `docs/navigation_models_status.md` (production navigation status)
- Related memory entries:
  - `project_skeleton_steering_migration` (production skeleton-tangent migration)
  - `project_river_islands_create_cycles` (multi-edge graph for islands)
  - `project_goal_management_architecture` (three-layer hybrid)
