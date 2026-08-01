# Destination Generator — Design Note

## Why this doc exists

The hug-shore steering pipeline has been carrying three overlapping
patches (§13.21, §13.24, §13.33) that all wrestle with one question:
"what is the per-tick steering target?"  Each was added to fix a
symptom of the same underlying confusion — we conflated the **mission
endpoint** (a fixed lat/lon hundreds of nautical miles away) with the
**controller destination** (the per-tick point Lyapunov should
converge to).  The literature distinguishes these sharply; our code
did not.

This doc establishes the vocabulary, the layer responsibilities, and
the design of a new **destination-generator** module that produces the
per-tick destination from honest inputs.  No code changes here — the
goal is a single artifact future work can compare against.

## Vocabulary

| Term                | Meaning                                                                       |
| ------------------- | ----------------------------------------------------------------------------- |
| **endpoint**        | Final mission target (e.g. `(5.71, 32.12)` for the upper-Nile voyage).  Used by the arrival check and the initial hugging-side bias.  Never enters the per-tick steering math. |
| **destination**     | The per-tick point Lyapunov converges toward.  Sourced from the destination-generator. |
| **shore tangent**   | A direction (bearing) extracted from Phase A's LSQ fit of the visible shore segment. |
| **lookahead point** | A point projected along the shore tangent at a fixed distance ahead of the bot. |
| **waypoint**        | Generic term for "the controller's near-field target."  In our new layering, `waypoint` is a synonym for `destination` at the controller interface. |

Endpoint and destination are **not the same thing**.  Endpoint is
configured per voyage and rarely changes; destination is recomputed
every tick from local perception.

## Layering and data flow

```
                                        config: endpoint, hug_side
                                        │
                                        ▼
shore tangent ──┐
mini-map        ├── destination-generator ──→ destination point (lat,lon)
last-known      ┤                              │
phase-A state   ┘                              ▼
                                        Lyapunov controller
                                               │
                                               ▼
                                       desired_heading_deg
                                               │
                                               ▼
                                          VFH+ avoider + sector occupancy
                                               │
                                               ▼
                                       chosen sector → rudder hold
```

Each layer has one job and a small interface:

- **Destination-generator (new)** — picks the controller's per-tick
  target from local perception + voyage configuration.  Encodes the
  hugging policy.
- **Lyapunov controller (§13.17)** — state-to-heading converter.
  Given destination + current `(pos, heading)`, emits the bearing
  whose pursuit minimises the Lyapunov function `V`.  Outputs one
  number: `desired_heading_deg`.
- **VFH+ avoider** — safety filter.  Given `(sectors, ship_heading,
  desired_heading, candidate_set, config)`, picks the lowest-cost free
  sector and returns it.  Never sees the destination point or the
  endpoint.

The downstream controllers are **policy-agnostic** — they convert and
filter without reasoning about the path.

## Mapping current code to new vocabulary

| Current name                 | New role                                                                    |
| ---------------------------- | --------------------------------------------------------------------------- |
| YAML `destination`           | Renamed to `endpoint`.  Used by arrival check and initial hugging bias.     |
| `pp_waypoint_bearing`        | The destination's bearing — output of the destination-generator.            |
| `pp_waypoint_hug`            | An internal input to the destination-generator.  Not a separate output.     |
| `pp_waypoint_dest_los`       | Removed.  Its role (a fallback when shore tangent is stale) is filled inside the destination-generator by "hold last-known shore direction." |
| `pp_combined_dest_weight`    | Removed.  No blending — destination-generator produces one point.           |
| §13.21 destination-anchored tangent | Concept preserved: the shore tangent is anchored toward the direction of progress along the boundary.  "Direction of progress" is derived from `endpoint`, not from a per-tick destination. |
| §13.24 dest-LOS-on-HOLD fallback | Hook preserved, behavior replaced.  When Phase A's LSQ fit goes stale, the destination-generator holds the last-known shore direction (TangentBug boundary-following continuation), not the bearing to the endpoint. |
| §13.33 adaptive blend        | Removed.  With one canonical destination per tick, no two-source blend exists. |
| §13.27 stuck-detector / UTurnRecovery | Unchanged.  Operates on drift + forward-arc clearance and is independent of the destination concept. |

## Destination Generator

### Interface

**Inputs (per tick):**

- `pos: (lat, lon)` — current bot position.
- `heading_deg: float` — current bow heading.
- `shore_tangent: TangentResult | None` — Phase A's output.  Carries
  the bearing of the visible shore segment, the LSQ residual, and a
  freshness/staleness verdict (`segment_action`).
- `last_known_destination: (lat, lon) | None` — the destination
  emitted on the previous tick.  Held for fallback continuity.
- `endpoint: (lat, lon)` — mission target (from voyage config).
- `hug_side: Literal["left","right"]` — port-side or starboard-side
  hug (from voyage config).

**Output:**

- `destination: (lat, lon)` — the point Lyapunov should converge to
  this tick.

The generator is a **pure function** of these inputs.  No hidden
state beyond `last_known_destination`, which is passed in by the
caller.

### Algorithm

The generator implements **shore-projected lookahead** —
Option A in the design discussion.  This matches the canonical
reactive-navigation pattern (pure pursuit on a sensed shore,
TangentBug boundary-following) and avoids any far-field projection of
the endpoint into the per-tick steering math.

Algorithm:

1. **If shore tangent is fresh** (`shore_tangent.segment_action == 'act'`):
   - Compute `lookahead_point` by projecting `pos` along
     `shore_tangent.direction` by `L_d` nautical miles, biased toward
     `hug_side`.
   - The bias is a small lateral offset (perpendicular to the
     tangent, toward the shore) so the controller settles at a fixed
     stand-off distance from the bank rather than directly on it.
   - **Return** `lookahead_point`.

2. **If shore tangent is stale** (`segment_action == 'hold_dest_los'`
   or perception missing):
   - **Return** `last_known_destination` if present.  This is the
     TangentBug boundary-following continuation: keep heading the
     direction the shore was going when last seen.
   - If `last_known_destination` is `None` (first tick, no fresh
     read), fall back to a synthetic lookahead point straight ahead
     of the bow (`pos + L_d * unit(heading_deg)`).  This is the only
     case where the endpoint does not enter at all — start of voyage,
     no perception yet, just go forward.

3. **Arrival check** (handled by the caller, not the generator):
   - If `dist(pos, endpoint) < arrival_radius`, the goal layer above
     marks the voyage complete.  The generator does not see this
     state change.

### Principle: why this algorithm

The principle is:

> The controller's destination is **always a point on the navigable
> water within sensor range**, projected from what perception
> currently sees, NOT from any far-field projection of the endpoint.

Three things follow from this principle:

- **The destination point is always reachable in a straight line from
  the bot's current position.**  Lyapunov-on-pursuit is provably
  convergent for any straight-line reachable target.  The "wp points
  at land" failure mode (the t394/t395 collision cascade) is
  impossible by construction.
- **Lyapunov and VFH+ stop fighting.**  The CLF (Lyapunov) and CBF
  (avoider) conflict in CLF-CBF literature appears when the CLF asks
  the system to chase a point the CBF must veto.  A shore-projected
  lookahead is always in the safe set, so the CBF never has to
  override.
- **The hugging behavior emerges from one rule, not from a blend.**
  The destination-generator is the single home for the hugging
  policy.  There are no `target_weight` vs `clearance_weight` knobs
  fighting over what "hug" means at the avoider layer.

The endpoint enters in two places only:

- At voyage initialization, to bias `hug_side` so the bot starts
  going *toward* the endpoint along the shore, not away from it.
- In the arrival check, owned by the goal layer above the generator.

This is the same separation TangentBug makes between
boundary-following (perception-driven, no goal in the inner loop) and
goal-direct (only when goal is locally visible).

### Tunables

- `L_d` (lookahead distance) — start at ~5 ship-lengths.  Smaller
  values give tighter tracking with more oscillation; larger values
  smooth the trajectory but cut corners.  Match the canonical pure-
  pursuit range (2-5× vehicle length).
- `lateral_offset` (stand-off from shore) — start at ~1 ship-length.
  Equals "how close the bot prefers to be to the bank" along the
  hug.  Below this, the avoider's clearance penalty starts
  contributing; above it, the bot drifts toward open water.
- `arrival_radius` — owned by the goal layer, not the generator.
  Existing value `1.0°` continues to work.

## What this is not

- Not a global path planner.  No A* on a river centerline, no
  pre-computed waypoint chain.  The destination is the next near-
  field carrot, period.
- Not a navigation function (Rimon-Koditschek).  The avoider remains
  the safety filter; the generator does not encode obstacles into the
  destination calculation.
- Not endpoint-aware in the per-tick loop.  The endpoint informs
  initial hugging side and the arrival check; nothing else.

## Phase 2 update — M-line superseded by Coverage + Frontier Picker

The M-line monitor described in the next section was shipped as
observer-only and ran on one full voyage (`explore_port_20260604_104620`,
630 ticks).  The empirical result was decisive: **5 M-line crossings
in 630 ticks, all before t251; 152 Y-tip ticks produced zero
verdicts.**  The bend detour and Y oscillation — the exact failure
modes the monitor was supposed to catch — produced no `REGRESSING_AT_HIT`.

The cause is structural, not a tuning issue.  Bug2's M-line is a
straight line from start to endpoint; for our Cairo → upper Nile
voyage, that line passes through Sudan.  Most of it is on dry land;
the bot can never physically reach it.  No M-line crossings → no
verdict changes → silent monitor.

This is the canonical failure mode of Bug-family algorithms on
river/coastal geometries.  Bug2 was designed for open space with
discrete blocking obstacles; we have the geometric inverse — a
thin water channel bounded by land on both sides.

### Replacement: Coverage Tracker + Frontier Picker

Two new modules supersede the M-line monitor:

- **CoverageTracker** (`brain/goals/coverage_tracker.py`) — 2D grid
  of visited cells.  Records per-tick position into a cell.  Emits
  `MAKING_PROGRESS` when the visited set grows, `STUCK` when it
  hasn't grown in N ticks.  Works in both endpoint and exploration
  modes (no endpoint needed — STUCK is a coverage criterion, not a
  goal-distance criterion).

- **FrontierPicker** (`brain/goals/frontier_picker.py`) — decision
  module invoked when `STUCK` fires.  Identifies *frontiers* (cells
  adjacent to visited cells but unvisited) and picks one as the
  override target.  Pluggable backend:
    - `YamauchiPicker` (Yamauchi 1997): nearest frontier, optionally
      goal-biased.  Pure heuristic, no model.
    - `AIPicker` (future): consults Claude or a learned model.
    - `HybridPicker` (future): Yamauchi when confidence is high,
      AI for ambiguous junctions.

The frontier-picker target overrides the destination-generator's
shore-projected lookahead for a commit window (N ticks), breaking
the oscillation before the generator can fight back.  After the
commit window OR new cells entered, normal generation resumes.

### Empirical justification

| Failure mode | M-line caught? | Coverage caught? |
| ------------ | -------------- | ---------------- |
| Bend detour (Run 1, lat 18 → 20 → 18) | no — same side of M-line throughout | yes — cells revisited |
| Y tip oscillation (152 ticks, ~10 cells) | no — never crossed M-line | yes — STUCK fires almost immediately |
| Far-endpoint progress (5 crossings) | yes (5 MAKING_PROGRESS verdicts) | yes (also tracked) |

Coverage Tracker is strictly more general.  The cases M-line catches,
Coverage also catches.  The cases M-line misses (bend, Y), Coverage
catches.  M-line monitor is therefore demoted to a documented
reference; the wire-in is removed.

### Why "FrontierPicker" specifically

In the Yamauchi 1997 frontier-based exploration paper, a *frontier*
is the boundary between known-traversable space and unknown space.
The picker's output is a target direction toward such a frontier.
This name covers:

- **Yamauchi** (heuristic): pick nearest frontier.
- **AI** (future): still picks a frontier — just uses a learned
  policy to score frontiers instead of distance.
- **Hybrid**: same shape, different policy.

The role is identical across backends — the *name* describes the
role, not the algorithm.

### Updated module layout

```
brain/goals/
├── coverage_tracker.py      ← NEW: substrate (visited cells, frontiers, STUCK verdict)
├── frontier_picker.py       ← NEW: decision module (Yamauchi / AI / hybrid backends)
├── m_line_monitor.py        ← DEPRECATED: kept as reference; not wired in
├── destination_generator.py ← unchanged
└── uturn_recovery.py        ← unchanged
```

### Updated control flow

```
shore tangent ──┐                                        ┌─→ destination
mini-map        ├── destination-generator ────────────→  │   (per-tick)
last-known      ┤                                        │
endpoint        ┘                                        │
                                                         ▼
                                                  [Override?]  ←─── FrontierPicker
                                                         │           (fires when
                                                         │            STUCK)
                                                         ▼
                                                  Lyapunov controller
                                                         │
                                                         ▼
                                                  desired_heading_deg
                                                         │
                                                         ▼
                                                  VFH+ avoider
                                                         │
                                                         ▼
                                                  chosen sector
```

The CoverageTracker runs in parallel (records every tick).  When it
emits `STUCK`, the goal layer invokes the configured FrontierPicker
to compute an override target; the override holds for a commit
window and then the normal destination flow resumes.

### Phase 2 plan of work

| Step | Output | Risk |
| ---- | ------ | ---- |
| 1. `CoverageTracker` module + tests | Pure dataclass; visited grid + STUCK verdict | Low — pure logic |
| 2. CoverageTracker observer wire-in | Trace gains `coverage_verdict`, `visited_count`, `frontier_count` per tick.  No behavior change. | Low |
| 3. `FrontierPicker` protocol + `YamauchiPicker` + tests | Pure module; takes coverage state, returns target | Low |
| 4. FrontierPicker wire-in behind flag (default off) | When STUCK fires, picker emits override; HugShoreGoal honors it for N ticks | **Medium** — behavior change |
| 5. Voyage validation; flip default to on | Run a Cairo→endpoint voyage with picker on; confirm Y is resolved | — |
| 6. (Future) `AIPicker` consulting Claude | Same module shape, different backend | Low |
| 7. (Future) Train local model on (situation → claude's choice) trace data | Distill Claude into a cheap deterministic model | — |

────────────────────────────────────────────────────────────────────

## Phase 3 plan — STUCK criterion fix + junction memory

After empirical validation of Phase 2 (CoverageTracker + Yamauchi
FrontierPicker on trace `explore_port_20260604_104620`), two
structural issues surfaced:

### 3a. The STUCK criterion is wrong

The "no new cells in N ticks" rule was empirically tested against
the Y-tip ticks t537-593.  Result: **STUCK never fires during
legitimate Y-tip exploration** at the default 30-tick window
(the bot keeps entering new cells every 5 ticks while descending
into the branch).  Tightening to a 10-tick window catches the
trouble at t587, but is conceptually still wrong — it conflates
"slow productive exploration" with "stuck oscillation."

The canonical replacement is **net displacement over time window**,
exactly as in Nav2's `ProgressChecker`:

```
STUCK iff |pos_now − pos_N_ticks_ago| < MIN_NET_DISPLACEMENT
```

This correctly distinguishes:

| Behavior | Cell entry rate | Net displacement |
| -------- | --------------- | ---------------- |
| Productive descent into a branch | medium (new cells every ~5 ticks) | **high** (~1.5° in 30 ticks) ✓ |
| Backtracking through known area | low | **medium-high** (~0.9°) ✓ |
| Genuine oscillation at a junction | low | **low** (<0.3°) — STUCK ✓ |

Verified against the trace:

- t537–585 (legitimate Y descent): silent. ✓
- t286–290, t311–315, t381–385 (bend struggles): STUCK fires. ✓
- t586–680 (Y-tip oscillation, multiple bursts): STUCK fires. ✓
- t771–775 (post-Y oscillation): STUCK fires. ✓
- 0 false positives during legitimate exploration windows.

The cell-tracking + frontier-identification machinery stays — only
the STUCK predicate changes.

Defaults from the trace analysis:
- `displacement_window_ticks = 30` (matches CoverageTracker's existing window)
- `min_displacement_deg = 0.3` (~3 nm; cleanly separates the
  three behavior classes)

### 3b. Perception-driven topology + Trémaux DFS (primary path)

Pure Yamauchi handles the simple Y case correctly (verified at
t593: bot at (8.89, 32.83), picker bearing 210° vs controller's
wrong 132°).  But this is **implicit topological reasoning** via
the frontier set — Yamauchi gets the right answer for the wrong
reason.  It can fail when:

- The bot returns to a fork after a long branch exploration; if
  STUCK fires only after re-entering the wrong branch, the bot has
  to commit ~30 ticks of wrong-direction motion before recovery.
- A long branch contains stray frontiers (islands, sub-channels);
  Yamauchi may pick a frontier inside the explored branch.
- Multi-fork rivers (Mississippi delta, Amazon basin) with 3+
  branches at one junction — nearest-frontier can pick the wrong
  unvisited branch.

The deeper observation: Yamauchi is a workaround for *not knowing
the topology*.  When the topology can be observed directly from
perception, a much older and structurally cleaner algorithm
applies.

**Trémaux's algorithm (1882) — the canonical answer.**  Predates
Bug2 by over a century.  Provably complete for any finite branching
environment.  Used in every maze-solving competition since the 19th
century.  In modern terms:

```
JunctionGraph: Dict[junction_id, JunctionState]
JunctionState  = (exits, explored, came_from)
JunctionStack  = List[junction_id]   # path back through unfinished junctions

def at_junction(junction, perceived_exits, came_from):
    if junction is new:
        store(junction, exits=perceived_exits, came_from=came_from)
        push(JunctionStack, junction)
        pick = leftmost_unexplored_exit(junction)   # hug-left rule
        mark_explored(junction, pick)
        take(pick)
    else:
        # Returning — we hit a dead-end on the previous edge.
        unexplored = exits − explored − {came_from}
        if unexplored:
            pick = leftmost_unexplored_exit(junction)
            mark_explored(junction, pick)
            take(pick)
        else:
            # All exits tried.  Backtrack via came_from.
            take(came_from)

def at_dead_end():
    # backtrack to last junction with unexplored exits, by popping
    # the stack and following came_from edges.
```

Properties:
- O(junctions) memory — tiny, even for the Mississippi delta.
- Visits each edge at most twice (once forward, once back if dead-end).
- Terminates when the stack is empty.
- Branch choice is **deterministic** (hug-left), not heuristic.

Yamauchi becomes unnecessary as the primary navigation strategy at
junctions.  All three of its failure modes above are eliminated:

| Failure mode | Trémaux behavior |
| ------------ | ---------------- |
| Wrong-branch re-commit after backtrack | Junction memory remembers which exits are explored; never re-commits. |
| Stray frontiers inside explored branch | Topology comes from observation, not from cell-set inference; strays are not in the graph. |
| Multi-fork junction | Native — `exits` is a list, supports any cardinality. |

References:
- Trémaux (1882, via Lucas) — original maze-solving algorithm.
- Pledge (1975) — O(1)-memory variant for restricted topologies.
- Kuipers (1978, 2000) — Spatial Semantic Hierarchy formalization.
- Choset (1996) — Sensor-Based Motion Planning, GVD-based junctions.
- Thrun (1998) — Hybrid metric-topological maps.
- MOOS-IvP (MIT) — marine-domain waypoint chains, closest analog.

### 3c. Junction detection from mini-map perception

Trémaux requires that the bot can *recognize* "I'm at a junction"
and *enumerate the exits*.  This is the critical dependency — and
where Yamauchi gracefully degrades when perception is uncertain.

For our setup, the existing 16-sector mini-map perception already
exposes the signals needed:

| Junction cue | Signal in current perception |
| ------------ | ---------------------------- |
| Multiple water channels diverging | ≥ 2 distinct sectors with `land_fraction < threshold`, separated by sectors with high `land_fraction` |
| Land between channels | High `land_fraction` in intermediate sectors |
| Channel "opens up" | Wide arc of low `land_fraction` |

Detection algorithm:
```
def detect_junction(nav_sectors, hug_side) -> Optional[JunctionDescriptor]:
    # Group consecutive sectors with land_fraction < threshold into
    # 'water arcs'.  Each connected water arc with bearing distinct
    # from the bot's incoming direction (and from hug-side shore)
    # is a candidate exit.
    water_arcs = group_low_land_sectors(nav_sectors)
    exits = [arc for arc in water_arcs if is_distinct_exit(arc)]
    if len(exits) >= 2:
        return JunctionDescriptor(
            exits=[arc.midpoint_bearing for arc in exits],
        )
    return None
```

Failure modes for junction detection:
- Wide channel mistaken for junction (single arc, but wide).
- Junction sectors partially occluded by mini-map text overlay.
- Bot too far from junction to see all branches.

These are perception failures, not algorithm failures.  When they
happen, Yamauchi takes over.

### 3d. Trémaux-only at confirmed junctions

The structural pattern:

```
FrontierPicker (Trémaux backend) is called every tick STUCK fires:
  1. detect_junction(nav) — does perception confirm a junction?
  2. if yes: consult JunctionGraph; apply Trémaux logic;
     return the chosen exit's target point.
  3. if no: return None (no override).
```

**No Yamauchi fallback.**  Earlier iterations of this design did
fall back to Yamauchi at non-junctions, on the theory that "STUCK
without a junction" was still an exploration opportunity.

The voyage `explore_port_20260604_141023` showed this was wrong.
At the bend (not a junction, but STUCK fires due to §13.21 tangent
flip), Yamauchi picks the nearest unvisited frontier toward the
endpoint — a *south-pointing* target — and commits the bot to it
for 30 ticks.  But the bot's bow is heading NW; VFH+'s forward-arc
picker can't physically turn ~155° to chase a southward target.
The picker target stays unreachable; the bot drifts north under
inertia for the full commit window; STUCK fires again; the picker
re-locks south for another 30 ticks; the bot tracks **+3.7° north**
over 80 ticks while chasing south.  Net effect: a configuration
that *passed* the bend in voyage `104620` (no picker) now *fails*
it under this picker (Trémaux + Yamauchi fallback).

The lesson: STUCK at a non-junction is a perception/decision
instability, not a topological choice.  The destination-generator
will self-correct on the next confident perception read (typically
within a few ticks).  Locking the bot to an unreachable target for
30 ticks *worsens* the situation rather than recovering it.

Yamauchi's role retracts to: a separate, *non-default* backend
chosen when there is no topological data and the user explicitly
wants frontier-greedy exploration (e.g. `junction_picker:
yamauchi`).  Inside `TremauxPicker`, Yamauchi is no longer invoked.

When the bot is genuinely stuck-in-place AND there's no junction,
neither Trémaux nor Yamauchi is the right primitive — the right
primitive is **wrong-direction recovery (§4b)** which executes a
180° turn rather than committing to an unreachable target.

The wire-in is the same as Phase 2's: STUCK from CoverageTracker
(net-displacement) triggers the picker.  The picker now returns
either a Trémaux target (at confirmed junctions) or None (no
override) — never a Yamauchi target.

### 3e. Connection to AI

Two natural AI integration points:

1. **Junction classifier from perception**: train a small model to
   produce `JunctionDescriptor` from a mini-map crop.  Better than
   the heuristic sector-grouping detector; handles edge cases
   (occlusion, ambiguous wide channels) by learning from labelled
   examples.  Training data: voyage traces with Claude-labelled
   "this frame is/isn't a junction; if junction, exits are at …".

2. **Junction policy**: at known multi-fork junctions, instead of
   hug-left, learn which exit to prefer given local geometry.
   Training data: voyage outcomes — "we picked exit A; outcome was
   X."  Distills branch-preference patterns across voyages and
   across rivers.

Either AI variant slots into the existing FrontierPicker protocol
without disturbing the rest of the stack.

A reasonable progression:
1. Pure Yamauchi (Phase 2 — shipped).
2. Net-displacement STUCK criterion (Phase 3a — small fix).
3. Trémaux DFS with perception-based junction detection
   (Phase 3b/c/d — replaces Yamauchi as the primary strategy at
   confirmed junctions; Yamauchi fallback when perception is
   uncertain).
4. AI-augmented junction classifier and/or policy
   (Phase 4 — slots into 3 as drop-in replacements for the
   heuristic detector / hug-left rule).

### 3f. Phase 3 work plan

| Step | Output | Risk |
| ---- | ------ | ---- |
| 3a.1 | Update `CoverageTracker` STUCK predicate to net-displacement.  Keep cell tracking + frontiers unchanged. | Low — predicate-only change. |
| 3a.2 | Update tests for new STUCK semantics; rerun trace simulation. | Low. |
| 3a.3 | Re-verify Phase 2 wire-in works with the new predicate; no other changes needed. | Low. |
| 3b.1 | New `brain/goals/junction_detector.py` — heuristic mini-map sector-grouping classifier.  Output: `JunctionDescriptor` (exit bearings) or `None`.  Tunables: `land_fraction_threshold`, `min_arc_width_sectors`. | Low — pure logic over existing sectors. |
| 3b.2 | Tests for junction detection on synthetic Y/T/multi-fork sector data. | Low. |
| 3c.1 | New `brain/goals/junction_graph.py` — `JunctionGraph` (id → exits/explored/came_from), `JunctionStack` (backtrack list), Trémaux algorithm. | Low — pure data. |
| 3c.2 | Tests for Trémaux: single Y, multi-fork, dead-end backtrack, repeated traversal. | Low. |
| 3d.1 | New `TremauxPicker` backend in `frontier_picker.py`.  Same protocol as `YamauchiPicker`.  Internally: if `junction_detector` produces a `JunctionDescriptor`, apply Trémaux; otherwise fall through to Yamauchi. | Medium — composition of two backends. |
| 3d.2 | Tests for hybrid behavior: at confirmed junction, picks Trémaux exit; at ambiguous geometry, falls back to Yamauchi. | Low. |
| 3d.3 | Wire `TremauxPicker` behind a YAML flag (`junction_picker: tremaux`).  Default stays `none` while we validate. | Low — same plumbing as Phase 2 step 4. |
| 3d.4 | Voyage validation: re-run Cairo→endpoint with `tremaux` picker.  Y-tip should be resolved on the first revisit without oscillation. | — |

3a is independent and strictly improving; ship and validate it
alone.  3b and 3c are pure-logic modules with no behavior effect
until 3d wires them in.

### 3e. What stays the same

- `destination_generator` (Phase 1): unchanged.
- `Lyapunov` controller: unchanged.
- `VFH+ avoider`: unchanged.
- `m_line_monitor`: still deprecated.
- The picker's *output* shape: still a (lat, lon) target with a
  commit window.

────────────────────────────────────────────────────────────────────

## Phase 4 plan — Avoider tuning + wrong-direction recovery

Phase 3 (Trémaux + CoverageTracker) solves *which way to go at
junctions*.  Voyage `explore_port_20260604_141023` (run with
`junction_picker: tremaux`) surfaced two failure modes that are
*orthogonal* to junction logic and need their own fixes.  Both are
classical pathologies of vanilla VFH+ documented in the reactive-
navigation literature; both have canonical solutions.

### 4a. VFH+ avoider tuning — Control Barrier Function clearance

**Empirical evidence** (t142-t148, t185-t189, voyage
`explore_port_20260604_141023`):

The bot heads directly into the south bank.  At t142 the forward
sector (sec 0) has `land_fraction=0.41, dist=0.12`; by t145 it's
`0.65, dist=0.066` — basically at the safety boundary.  Through all
four ticks the picker reports `best=0 on-course` and commands no
turn.  At t146 the heading detector flips wildly (148° → 283°)
because the bot is physically bouncing off the bank.

The picker math:

| Sector | `target_weight·|sec − wp|` | Inertia | Clearance penalty | Total |
| ------ | -------------------------- | ------- | ----------------- | ----- |
| sec 0 (forward, hitting land) | 5 × 7° = 35  | 0  | ~140 (capped at 180) | ~175 |
| sec 2 (right, clear water)    | 5 × 38° = 190 | 90 | 0                   | ~280 |

Sec 0 wins because target alignment dominates even when imminent
collision is detected.

**Root cause:** the clearance penalty is *linear with a 180 ceiling*.
At the safety boundary it tops out at 180; the target penalty
(`target_weight × angle_diff`) easily exceeds this when wp is just
a few degrees off bow.  Net: clearance can't outvote target
alignment when wp points at imminent land.

**Canonical fix:** **Control Barrier Function (CBF) clearance**
(Ames 2014).  Replace the linear-capped penalty with a hyperbolic
barrier:

```
penalty(d) = k / (d − safety_dist)    for d > safety_dist
           = +∞                       for d ≤ safety_dist
```

Or quadratic:

```
penalty(d) = k · ((safety_dist + margin − d) / margin)²
```

Both go to infinity (or arbitrarily large) at the safety boundary.
The picker can no longer rationalise an imminent collision via
target attraction — there is no finite target reward that beats an
infinite clearance penalty.

This is the canonical pattern in modern CLF-CBF QP controllers
(Quadratic Program solvers used by Waymo, Cruise, Spot's locomotion
stack).  The CBF guarantees the safe set is *forward-invariant* —
once safe, always safe.

**Pragmatic variants if CBF is too heavy:**

1. **Adaptive target_weight**: when `forward_clearance <
   critical_dist`, set `target_weight = 0` for one tick.  Clearance
   penalty alone picks the safest direction.
2. **Panic stop**: if `min(forward_clearance) < critical_dist`,
   override the picker → command `sail_stop`, drift, re-perceive,
   then re-engage VFH+.  Cheap, robust, and matches how human
   coastal pilots actually navigate.

Either gets us out of the imminent-collision trap without a full
CBF rewrite.

### 4b. Wrong-direction recovery — bearing-aware stuck detector

**Empirical evidence** (t283-t385, same voyage):

After the bot rounds the bend at lat 18, the destination-generator
re-anchors the wp toward south (toward the endpoint at lat 5.7).
The bot at t283 is heading 76° (NE); the wp is at 232° (SW) —
`rel = +156°`, wp nearly dead astern.

From t301 onward the bot tracks steadily NORTH while wp consistently
points SOUTH:

| Tick | Position    | Heading | wp bearing | rel    |
| ---- | ----------- | ------- | ---------- | ------ |
| t320 | (18.60, 30.91) | 4°    | 158°       | +154°  |
| t340 | (20.17, 30.61) | 14°   | 170°       | +156°  |
| t360 | (21.22, 31.10) | 35°   | 195°       | +159°  |
| t380 | (22.03, 31.74) | 12°   | 210°       | −163°  |

Net displacement over 80 ticks: **+3.7° latitude (north)** — the
exact opposite of where the bot needs to go.

**Why every existing recovery missed it:**

| Primitive | Trigger | Why it didn't fire |
| --------- | ------- | ------------------ |
| §13.27 UTurnRecovery | position drift < 0.15° AND forward clearance < 0.22 over 30 ticks | Bot moving freely (3.7° drift) with clear water ahead |
| §13.26 astern-expansion | wp > 90° from bow | Disabled (caused Bug2 oscillation at bends — see § 3) |
| VFH+ forward-arc picker | always within ±90° of bow | No forward-arc sector aligns with wp 180° behind; inertia keeps bow on course |
| CoverageTracker STUCK (Phase 3a) | net displacement < 0.3° / 30 ticks | Net displacement 3.7° — far above threshold |

The bot is freely sailing in the wrong direction with **no signal
in the entire stack** that fires.

**Canonical fix:** **bearing-aware stuck detection**.  Trigger
UTurnRecovery when the destination-relative bearing has been beyond
a threshold for N consecutive ticks, *independent* of angular drift
or forward clearance.

```
WrongDirectionDetector:
    threshold_deg = 135
    window_ticks  = 20
    state: deque of recent (tick, abs(rel_bearing))

    def record(tick, heading, wp_bearing):
        rel = abs(((wp_bearing − heading + 180) % 360) − 180)
        state.append((tick, rel))
        if all(samples in window have rel > threshold):
            return TRIGGER_UTURN
        return OK
```

20 ticks ≈ 20 wall-seconds — long enough to avoid false positives
during a transient bow swing, short enough to recover before the
bot covers a meaningful wrong-direction distance.

**Why this is orthogonal to §13.27:** §13.27 catches "stuck in
place"; this catches "freely moving the wrong way."  Together they
cover both halves of "the controller is failing."

**Why this doesn't undermine Bug2 commitment:** §13.27 was disabled
in favour of Bug2 commitment because admitting astern sectors caused
oscillation at bends.  The wrong-direction detector doesn't admit
astern sectors at the picker — it triggers a dedicated *recovery
primitive* (UTurnRecovery) that suspends normal steering for the
turn duration.  No conflict.

### 4c. Phase 4 work plan

| Step | Output | Risk |
| ---- | ------ | ---- |
| 4a.1 | Decide CBF (hyperbolic) vs adaptive-weight vs panic-stop.  Recommend panic-stop first — smallest commit, biggest correctness win.| Low — design only. |
| 4a.2 | Implement chosen fix in `brain/steering/avoider.py` (or `hug_shore.py` for panic-stop).  Tests. | Medium — touches steering math. |
| 4a.3 | Voyage validation: re-run with the avoider fix; verify bot doesn't bury into south bank at t142-area positions. | — |
| 4b.1 | New `brain/goals/wrong_direction_detector.py` — pure module, same shape as `coverage_tracker.py` / `m_line_monitor.py`. | Low. |
| 4b.2 | Tests for: transient bow swing not flagged; sustained wp-behind triggers; window resets on rel < threshold. | Low. |
| 4b.3 | Wire into HugShoreGoal as observer first (trace gains `wrong_direction_verdict`); validate against t301-t385 of the affected voyage. | Low. |
| 4b.4 | Hook the trigger to UTurnRecovery (the same recovery primitive §13.27 uses). | Medium — recovery state machine. |
| 4b.5 | Voyage validation: bot at t320-equivalent of next voyage should UTurn back south within 20 ticks. | — |

4a and 4b are independent.  4b (recovery) is the higher-leverage win
for the current Cairo→endpoint voyage because the wrong-direction
failure has been observed across multiple runs.  4a fixes a
different problem class (channel-bank collisions) that also recurs.

### 4d. What stays the same

- `destination_generator`, `Lyapunov`, `JunctionDetector`,
  `JunctionGraph`, `TremauxPicker`, `YamauchiPicker`,
  `CoverageTracker`: all unchanged.
- §13.27 UTurnRecovery: re-used as the recovery primitive triggered
  by 4b.  Its detector logic stays; we just add a second way for it
  to fire.
- M-line monitor: still deprecated.

────────────────────────────────────────────────────────────────────

## Phase 5 plan — Trémaux state machine: dead-ends, backtracking,
## exhaustion

The Phase 3 implementation handles the *most common* Trémaux case
— first visit to a junction and re-entry with unexplored exits —
but does not handle several states the algorithm requires for
correctness.  This phase fills in the missing pieces.

### 5a. Mode taxonomy

Trémaux is properly a **state machine** with the following modes:

| Mode | Condition | Picker behavior |
| ---- | --------- | --------------- |
| **pre_topological** | `ever_visited == 0` (no junction has been encountered yet) | Sit out.  Normal shore-following. |
| **between_junctions** | Bot is on a known edge, traversing between two known junctions | Sit out.  Destination-generator handles. |
| **at_junction_first_visit** | `cell` not in graph | Push onto stack; record exits; mark entrance explored; pick leftmost unexplored exit. |
| **at_junction_re_entry** | `cell` in graph AND `unexplored != ∅` | Mark entrance exit explored; pick next leftmost unexplored. |
| **at_junction_exhausted** | `cell` in graph AND `unexplored == ∅` | Pop stack; signal "backtrack via entrance"; transition to backtracking mode. |
| **at_dead_end** | `JunctionDetector` reports ≤ 1 forward arc | Mark current edge as exhausted at the parent junction; signal "backtrack via current heading + 180°"; transition to backtracking mode. |
| **backtracking** | Stack non-empty AND following a recorded edge in reverse | Use `BacktrackingPlanner` to override destination with reverse-trajectory waypoint; exit when arrived at the next junction in the stack. |
| **exhausted** | `ever_visited > 0 AND stack == ∅` | Signal "topology done"; caller decides what to do (return to start, terminate, escalate). |

The current implementation only handles the "first visit" and
"re-entry" rows.  All others are either silent (no-op) or broken
(picker fires nonsense via the previous Yamauchi-fallback design,
since-removed).

### 5b. New modules

| Module | Purpose |
| ------ | ------- |
| `brain/goals/junction_detector.py` (extend) | Classify each tick as `JUNCTION`, `DEAD_END`, or `CHANNEL` (none).  Currently it only returns descriptor or None for junctions; needs an enum return type. |
| `brain/goals/junction_graph.py` (extend) | Add `topology_state` accessor (pre_topological / exploring / exhausted), `ever_visited` counter, and edge recording.  Each edge stores the trajectory between two junctions. |
| `brain/goals/backtrack_planner.py` (new) | Given a recorded edge and a direction, yield the next reverse waypoint until the parent junction is reached. |
| `brain/goals/frontier_picker.py` (extend) | TremauxPicker becomes a proper state machine dispatching on mode. |

### 5c. The edge model

Each edge of the topological graph is the path between two
junctions.  Recording the edge's geometry lets the bot follow it
in reverse during backtracking — without it, "go back through the
entrance" is just a bearing, which gets stale as the bot moves.

```
Edge:
    from_junction:    CellIdx
    to_junction:      CellIdx    # might be None if currently being traversed
    waypoints:        List[Tuple[lat, lon]]    # samples along the trajectory
    enter_heading:    float       # bearing the bot had when leaving from_junction
```

The caller (HugShoreGoal) feeds in position samples each tick.  The
edge being recorded is the "active edge" — newest in the graph.

During backtracking the planner walks the waypoints list in reverse,
emitting the next one (downsampled to be at lookahead distance
ahead of the current position) as the override target.

### 5d. The "exhausted" state and what to do about it

When `ever_visited > 0 AND stack == ∅`, all known topological
choices have been explored.  Three reasonable behaviors depending on
voyage type:

| Voyage type | Behavior on exhausted |
| ----------- | -------------------- |
| **Endpoint voyage** (endpoint set, not yet reached) | Escalate (Claude consult, or return-to-start, or terminate with failure).  Topology exhausted but goal not met — something is wrong (endpoint unreachable from start, or perception missed a junction). |
| **Pure exploration** (no endpoint) | Voyage complete.  Sail back to start; mark task done.  This is the canonical Trémaux termination. |
| **Endpoint reached** | Already complete; exhaustion is moot. |

### 5e. Testing strategy — pure exploration mode

Per the user request, the next validation voyage drops the
endpoint and runs in pure exploration mode.  This is the right test
configuration because it exercises every mode in the state machine
in a clean, deterministic way:

- `pre_topological`: bot starts at sea, follows shore until first
  fork.
- `at_junction_first_visit`: at first fork, Trémaux picks an exit.
- `between_junctions`: bot follows shore on the chosen branch.
- `at_dead_end`: bot hits the end of a branch.
- `backtracking`: bot follows recorded edge back to the parent
  junction.
- `at_junction_re_entry`: at parent junction, picks next
  unexplored exit.
- `exhausted`: after all forks walked, signal "done"; caller
  initiates return-to-start.

An endpoint voyage couldn't cleanly test the "exhausted" mode —
endpoint arrival would terminate the voyage before exhaustion.

### 5f. Phase 5 work plan

| Step | Output | Risk |
| ---- | ------ | ---- |
| 5.1 | Extend `JunctionDetector` to classify `JUNCTION` / `DEAD_END` / `CHANNEL`.  Add detection of dead-ends as a 1-arc case (water behind only). | Low — pure logic. |
| 5.2 | Extend `JunctionGraph`: `ever_visited` counter, `topology_state` accessor, edge recording. | Low. |
| 5.3 | New `BacktrackPlanner` — pure module that takes an edge + position and yields reverse waypoints. | Low. |
| 5.4 | Re-implement `TremauxPicker.pick()` as a state machine dispatching on mode.  Trace fields gain `tremaux_mode`. | Medium — replaces current TremauxPicker logic. |
| 5.5 | Tests: each mode transition.  Synthetic Y, T, and dead-end scenarios. | Low. |
| 5.6 | Voyage validation: pure exploration (no endpoint), Nile from Cairo.  Bot should explore all visible forks, hit dead-ends, backtrack, exhaust, then signal done. | — |

5a-5e are independent design notes; 5.1-5.5 are independently
shippable code commits.  5.6 is the integration test.

### 5g. What stays the same after Phase 5

- `destination_generator`, `Lyapunov`, `VFH+` avoider,
  `CoverageTracker`, `YamauchiPicker`: all unchanged.
- Wire-in pattern (TremauxPicker held by HugShoreGoal, invoked when
  STUCK or — new — when junction/dead-end perceived): same shape.
- `m_line_monitor`: still deprecated.

────────────────────────────────────────────────────────────────────

## Endpoint reach guarantee — the M-line progress monitor

⚠ The remainder of this section describes the original M-line
monitor design.  It is kept as a historical/conceptual reference;
the monitor is no longer wired in.  See "Phase 2 update" above for
the actual current design.

────────────────────────────────────────────────────────────────────


The destination-generator alone gives no guarantee that a voyage
will *reach* the endpoint.  Pure pursuit on a sensed shore gives
bounded cross-track error to the shore (Lyapunov stability), but the
shore is not the endpoint — it's just the path the bot follows.
Without an additional check, the bot would happily follow a river
that loops away from the endpoint forever.

This is exactly the question Bug-family algorithms (Lumelsky 1987,
Kamon-Rivlin 1998) answer: how do you guarantee a reactive boundary-
follower eventually reaches a goal?

### The conceptual model: two paths, two roles

There are **two distinct geometric objects** in a goal-driven voyage:

| | What it is                                              | Role             |
| ----------------- | ------------------------------------------------------- | ---------------- |
| **The shore**     | The river coast itself, sensed per tick from the mini-map | The path the bot *follows*.  Carrot rides on it.  Lyapunov converges to it. |
| **The M-line**    | Straight line from start to endpoint, computed once at voyage init | An abstract progress *reference*.  Not followed.  Used only to measure "are we getting closer to the endpoint?" |

The bot's track will cross and recross the M-line as the shore
meanders.  Each crossing is a checkpoint; at each one we compare the
current distance-to-endpoint with the distance recorded at the
previous crossing.

### The monitor

A separate, very small module — `brain/goals/m_line_monitor.py` — owns
one float of state per voyage (`d_at_last_hit`) and emits a verdict
per tick:

- `MAKING_PROGRESS`    — fresh M-line crossing, distance smaller than
                          at the last crossing
- `KEEP_FOLLOWING`     — between crossings; distance free to grow
                          (canonical Bug2 boundary-follow)
- `REGRESSING_AT_HIT`  — fresh crossing, distance larger than at the
                          last crossing.  Trigger condition for the
                          recovery layer.

The monitor does not move the bot.  Recovery actions (stop, U-turn,
switch hug-side, escalate to Claude) are owned by the goal layer
above, which decides what `REGRESSING_AT_HIT` means.

### Why not just "distance always decreases"

Euclidean distance to endpoint can grow temporarily during honest
geodesic progress — going around a bend in a river is the obvious
example.  Bug2 handles this by requiring monotone distance only at
**M-line re-crossings**, not at every tick.  Between crossings the
boundary-follower is free to take whatever path the shore demands.

A naïve "global minimum distance" tracker (no M-line) would flag
every bend as a regression and trigger spurious recoveries.  The
M-line discipline is what makes the metric honest.

### Geodesic vs euclidean — known limitation

The M-line uses straight-line (euclidean) distance.  A truly
navigable path through Sudan's geography is not a straight line.
Bug2's leave-point rule tolerates this — it doesn't require the
intermediate path to be straight, only that re-crossings are
strictly closer in straight-line distance.

A future upgrade path: if a coarse Nile centerline becomes available,
distance can be measured **along** that path instead of by straight
line.  The monitor's interface (record-and-verdict) does not change;
only the distance function changes.

### When the monitor is dormant

The monitor only operates when the goal layer supplies an endpoint.
For pure-exploration voyages (no `endpoint_lat`/`endpoint_lon` in the
YAML), the monitor is not constructed and never consulted.  The
shore-following stack runs as a Roomba-style boundary-follower with
external termination (max_ticks, user stop).

| Voyage mode                                  | Endpoint? | Monitor? | Termination                              |
| -------------------------------------------- | --------- | -------- | ---------------------------------------- |
| **Endpoint voyage** (Cairo → upper Nile)     | yes       | active   | arrival check OR recovery exhaustion     |
| **Pure exploration** (hug south, no target)  | no        | inactive | external (max_ticks, manual interrupt)   |
| **Patrol / coverage** (visit area)           | (different mode) | different monitor | coverage map fills            |

This means the destination-generator + Lyapunov + VFH+ stack
supports both goal-driven and exploration modes with the same
low-level controller.  The M-line monitor is a *bolt-on* that the
goal layer turns on or off per voyage.

### Recovery menu (engineering, not Bug2)

When the monitor flags `REGRESSING_AT_HIT`, the goal layer escalates
through a fixed recovery menu in order of disruption:

1. **Stop and wait** — let perception stabilize for a few ticks.
2. **U-turn** — invoke §13.27 UTurnRecovery; reverse and retry.
3. **Switch hug side** — try the opposite bank (port ↔ starboard).
4. **Backtrack** — return to the position that held `d_best`.
5. **Escalate to Claude / human** — admit we cannot solve it locally.

The order and gating between these steps is data-driven.  First
ship the monitor as observer-only (logs the verdict, takes no
action), run one voyage, then decide from real triggers which
recovery steps to wire up.

## Open questions

- **Initial hugging-side bias when endpoint is far.**  At voyage
  start, choosing port-side vs starboard-side hug along a long river
  may need a coarse direction-of-progress signal from the endpoint
  bearing.  This is handled at the goal layer above the generator,
  not inside it.
- **TangentBug-style range gate.**  If we later add a "switch to
  direct-pursuit when endpoint is locally visible" mode, that lives
  as an outer switch above the generator — the generator itself
  stays pure shore-following.
- **Map data integration.**  If a Nile centerline becomes available
  (e.g., baked from voyage trace, or imported from external
  geometry), it slots in as an additional shore-tangent source —
  the generator's interface and algorithm shape do not change.
  Until then, perception is the sole map.
