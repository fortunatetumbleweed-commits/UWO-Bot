# Lookahead Tactical Layer — Design

> ⚠️ **SUBSTRATE OBSOLETE (2026-07-24)** — the "walk the water
> skeleton to find a lookahead dest" approach described in this
> document is being retired.  The skeleton-endpoint substrate
> (`skimage.morphology.skeletonize` + spur pruning + `edge`/
> `dead_end`/`turning_point` anchor taxonomy) produces spurious
> anchors from sprite fragments, closing-bridge artifacts, and
> boundary-erosion truncation of wide open regions — none of
> which have a clean tip-based fix.
>
> The replacement is the **edge-anchored centerline extractor**
> in `docs/edge_anchored_centerline_design.md` (prototype in
> `tools/edge_centerline_prototype.py`), which finds frame-edge
> openings directly and computes centerlines between them via
> Dijkstra over `1/(DT+1)`.  Read that document first when
> touching tactical anchor code.
>
> This document is retained for historical context and for the
> parts still applicable to the SEARCH-mode continuity tracker.
>
> ⚠️ **Previously PARTIALLY SUPERSEDED (2026-07-22)** — see
> `docs/tactical_search_tracker.md` for the SEARCH-mode continuity
> tracker.  Sections describing the per-tick walker call and EMA
> smoothing on `commit_direction` are OUT OF DATE.  SEARCH mode
> no longer re-runs the walker every tick; `commit_direction` is
> the direct `ship→current_dest` bearing (no EMA); dest continuity
> is enforced by a hard `MAX_DEST_JUMP_KM=30` guard.

Design for a tactical layer that computes `commit_direction` from
terrain **ahead** of the ship, not from the ship's own past heading.
Modelled after how a human helmsman navigates a river: anticipates
bends and forks before reaching them, only reacts to bounces as a
rare safety mechanism.

Written 2026-07-20 after a series of live voyages exposed limits of
the current design (static `--commit-bearing 180°` + hug-left picker
that ignores commit at junctions).

## Problem

Today's stack has two structural issues:

1. **`commit_direction` is static** — set once from `--commit-bearing`,
   never updated during the voyage.  Doesn't reflect river bends
   (which turn the "correct" direction 90° or more), forks (where a
   specific branch is the right choice), or dead-ends (where the ship
   should reverse).

2. **The picker ignores `commit` at junctions** — `pick_waypoint`
   only aligns with commit for the first edge choice; every junction
   after that uses hug-left blindly.  Result: the ship can pick an
   NNE spur even when commit is south.  Observed at t1 of the
   2026-07-17 live voyage.

## Core rule

**Committed direction = derived from the water topology visible
ahead of the ship, updated each tick, smoothed across ticks for
continuity.**

Concretely, each tick:

1. Extract skeleton from the current tick's water mask (already
   computed by L2).
2. Find the ship's projected edge on the skeleton.
3. Walk forward along that edge, applying **hug-side rule** at every
   junction (left for `port`, right for `starboard`).
4. Continue walking until `LOOKAHEAD_PX = 50` px consumed OR a leaf
   node is reached.
5. `raw_commit = bearing from ship to walk endpoint`.
6. Smooth via EMA: `commit = angular_ema(prior, raw, alpha=0.15)`
   with `alpha` reduced to 0.02 when `|raw − prior| > 30°`
   (noise rejection).
7. Write `commit_direction` into `NavState`.

The reflex layer (`HugShorePlanner`) uses `commit_direction` as its
steering goal, choosing local waypoints that keep the ship pointed
roughly in that direction while respecting the immediate hug-side
shore.

## How each case is handled

### Straight sailing

Skeleton in the wedge is a straight line.  Walk endpoint 50 px south.
`commit = 180°` for many ticks.  EMA stable.  Ship sails straight.

### Bend

Skeleton curves.  Walk follows curve for 50 px, so endpoint is
partway around the bend.  Bearing to endpoint is between the
pre-bend and post-bend directions.

As ship progresses, endpoint moves further into the bend, bearing
tracks the bend's curve.  Ship starts turning EARLY (before the
bend is at the bow), turn matches the river's curve.

No overshoot, no last-minute correction.  Anticipatory.

### Fork

Walk reaches a junction node within `LOOKAHEAD_PX`.  Hug-side rule
picks a branch (leftmost from arrival, for `port`).  Walk continues
into that branch until 50 px consumed or leaf found.

Committed direction now points into the chosen branch.  Ship starts
committing to the branch BEFORE reaching the fork.  When ship
arrives at the fork, no decision is needed — it's already pointed
in.

### Dead-end (leaf ahead)

Walk terminates at a leaf node (skeleton degree-1 node) within
`LOOKAHEAD_PX`.

- **Leaf distance > `SAFE_APPROACH_DISTANCE` (25 px)**: `raw_commit`
  = bearing to leaf.  Ship continues approaching normally.  Nothing
  special.
- **Leaf distance ≤ 25 px**: **flip** — `raw_commit = reverse of
  bearing to leaf`.  Ship pre-emptively turns around.

The ship starts reversing while still 25 px from the tip — enough
space to complete the U-turn before physically hitting the leaf.

When the ship reverses out of the branch, hug-side rule naturally
brings it back to the parent junction along the OTHER bank (because
"port" is now the opposite side after the U-turn).  At the junction,
hug-side picks the other branch that hasn't been explored.

**No JunctionGraph memory required** — the physical geometry of
"walk along shore, come back on the other side" does the exploration
for us.  This is the classic wall-follower theorem: on a connected
water shape with a shore, hug-side visits every branch and returns
to origin.

### Bounce (safety net)

With the pre-emptive dead-end reversal, most bounces are
eliminated.  Rare remaining causes:
- Sharp bend where turn rate can't keep up
- Sudden mask corruption
- Village-aura chrome pushing ship briefly onto "land"

Handling: if `speed drops ≥ 70%` between consecutive ticks, enter
`bounce_recovery`:

- **Freeze `commit_direction`** at its current smoothed value for
  `FREEZE_TICKS = 15` ticks.  Lookahead is disabled during freeze
  (mask is briefly unreliable right after a bounce).
- During those 15 ticks, the reflex layer sees a stable target and
  steers to re-align the ship's heading.
- After 15 ticks: resume live lookahead updates.

### Two-strike escalation (rare)

If a second bounce fires within the freeze window AND the ship's
position hasn't moved meaningfully (net displacement < 5 px):

- Ship is genuinely stuck at a narrow tip.
- Force `smoothed_commit = reverse of frozen_commit`.
- Extend freeze another 15 ticks.

The ship is now committed to going the OPPOSITE direction from
what it was doing before the bounces started.  Usually clears the
stuck spot.

### Open water (no skeleton)

Skeleton extraction returns no nodes near ship.  Common in Lake
Victoria or the Mediterranean.

- Fall back to prior smoothed commit direction (last-known-good).
- No update this tick.  Wait for skeleton to reappear as ship
  approaches a shore.
- If prior commit is also `None` (voyage start in open water): use
  CLI-supplied `--commit-bearing` as the default.

## Continuity via temporal smoothing

Per-frame lookahead can drift on a bad frame — one tick's spurious
skeleton branch produces a bad `raw_commit`.  The EMA smooths this
out over ~10 ticks.

Two-tier rule:

| `|raw − prior|` | interpretation | `alpha` |
|---|---|---:|
| < 30° | normal update | 0.15 |
| ≥ 30° | probable bad frame | 0.02 |

Real terrain changes (bends, forks) propagate through the smoothing
in 3–10 ticks — fast enough that the ship's steering keeps up.
Noise gets almost entirely rejected.

## Pseudocode

```python
class LookaheadTactical:
    LOOKAHEAD_PX          = 50
    SAFE_APPROACH_DIST_PX = 25
    FREEZE_TICKS          = 15
    HEADING_JUMP_DEG      = 30
    ALPHA_NORMAL          = 0.15
    ALPHA_SUSPECT         = 0.02
    BOUNCE_SPEED_DROP     = 0.30   # curr/prev ratio

    def update(self, state):
        # 1. Bounce detection
        bounce = (self._prev_speed and state.speed_kt and
                  state.speed_kt <= self._prev_speed * self.BOUNCE_SPEED_DROP
                  and self._prev_speed >= 3.0)

        if bounce and self._freeze_ticks == 0:
            # Two-strike check
            if (self._last_bounce_tick is not None
                    and state.tick - self._last_bounce_tick < self.FREEZE_TICKS
                    and self._near_last_bounce_pos(state)):
                self._smoothed = (self._smoothed + 180) % 360     # escalate
            self._pre_bounce_commit = self._smoothed
            self._freeze_ticks = self.FREEZE_TICKS
            self._last_bounce_tick = state.tick
            self._last_bounce_pos = (state.lat, state.lon)

        # 2. Compute commit for this tick
        if self._freeze_ticks > 0:
            state.commit_direction = self._pre_bounce_commit
            self._freeze_ticks -= 1
        else:
            raw = self._compute_lookahead(state)
            if raw is not None:
                delta = self._ang_diff(raw, self._prev_raw or raw)
                alpha = (self.ALPHA_SUSPECT if delta > self.HEADING_JUMP_DEG
                         else self.ALPHA_NORMAL)
                self._smoothed = self._ang_ema(self._smoothed, raw, alpha)
                self._prev_raw = raw
            state.commit_direction = self._smoothed

        # 3. Update history
        self._prev_speed = state.speed_kt
        return state

    def _compute_lookahead(self, state):
        skeleton = extract_skeleton(state.water_mask)
        edge = find_ship_edge(skeleton, state.ship_px)
        if edge is None:
            return None
        endpoint, leaf_distance = walk_forward(
            skeleton, edge, state.ship_px,
            hug_side=self.hug_side,
            lookahead=self.LOOKAHEAD_PX,
        )
        if endpoint is None:
            return None
        bearing = bearing_from(state.ship_px, endpoint)
        if leaf_distance is not None and leaf_distance <= self.SAFE_APPROACH_DIST_PX:
            bearing = (bearing + 180) % 360     # pre-emptive reversal
        return bearing
```

## Interaction with reflex layer

`LookaheadTactical` writes `state.commit_direction`; the reflex
layer (`HugShorePlanner` or `CenterlinePlanner`) reads it and
picks a **local** waypoint that steers the ship in that direction.
The tactical layer never emits waypoints directly — separation of
concerns.

If reflex's own hug-side preference clashes with commit (rare),
commit wins.

## Tuning constants

Tuned via sim iteration on the stitched Nile canvas.  Live behavior
may need adjustment for perception noise levels.

| Constant | Default | Notes |
|---|---:|---|
| `LOOKAHEAD_PX` | 50 | ~5-10 ticks of sailing at cruise |
| `SAFE_APPROACH_DIST_PX` | 25 | Enough to complete a U-turn at cruise |
| `FREEZE_TICKS` | 15 | ~15-45 s live; enough to clear a bounce spot |
| `HEADING_JUMP_DEG` | 30 | Above this, treat as bad-frame noise |
| `ALPHA_NORMAL` | 0.15 | ~7-tick smoothing horizon |
| `ALPHA_SUSPECT` | 0.02 | ~50-tick smoothing horizon for bad frames |
| `BOUNCE_SPEED_DROP` | 0.30 | curr/prev ratio; 30% = 70% drop |

## Design rationale

Why this over alternatives:

- **Static commit_direction (today)**: fails at bends and forks
  because the "correct" direction changes with terrain.
- **Past-heading EMA**: lag → ship overshoots bends; corrupted by
  bounces (rotates heading randomly).
- **Full JunctionGraph memory**: requires reliable lat/lon (broke
  tonight); coarse cell granularity collapses real forks and
  dead-ends into single junctions.
- **Lookahead + hug-side + pre-emptive reversal**: uses only current
  water mask + ship's local terrain visibility.  No memory
  required.  Bounces don't corrupt because commit is visual, not
  motion-based.  Bends and forks anticipated.  Dead-ends handled
  by geometric reversal, not by graph state.

## Related work in this repo

- Current mission: `brain/ai_nav/mission.py::RiverExploreMission` —
  static default_bearing + JunctionGraph.
- Current planner: `brain/ai_nav/layers/planner.py::CenterlinePlanner`
  + `tools/centerline_waypoint_prototype.py::pick_waypoint` — uses
  hug-side blindly at junctions, ignoring commit direction.
- ValidatedPlanner wrapper + radial-scan fallback:
  `brain/ai_nav/layers/planner_validator.py` — will remain as a
  safety net.
- Harness architecture: `docs/agentic_navigation_harness.md` —
  Tactical is the middle tier this doc details.
- Bounce detection pattern:
  `memory/project_speed_is_bounce_detector.md`.
- Wall-follower discussion this design distills from:
  chat 2026-07-20.

## Next steps

1. Implement `LookaheadTactical` in `brain/ai_nav/layers/tactical.py`.
2. Slim `CenterlinePlanner` to a commit-following variant, OR write
   a fresh `HugShorePlanner` reflex layer.
3. Wire via runner: `--tactical lookahead`.
4. Sim regression on the stitched Nile canvas: expect zero bounces,
   pre-emptive fork commitment, clean dead-end reversal.
5. Live voyage validation.

Deferred (bigger scope):

- Route knowledge — for later, when the ship needs to travel between
  specific ports rather than just "hug shore forever."
- Leg-based Mission with return-home: pairs cleanly with the
  lookahead — mission provides "outbound goal" and "return goal";
  tactical picks the direction each phase.
