# SEARCH-mode Continuity Tracker — Design & Behaviour

Status: **OBSOLETE substrate (2026-07-28)** — historical.

> ⚠️ **SUBSTRATE RETIRED.**  This tracker consumed anchors from the
> skeleton-endpoint walker, which is retired.  The tactical layer now
> runs on the **bank tracer** (`tools/bank_tracer.py` + the
> `LookaheadTactical` anchor policy — sticky / migrate / new_anchor /
> hold(pocket_ahead) / reach-gate); see the "Tactical anchor substrate —
> CURRENT" pointer in `CLAUDE.md`.  The tracker's core idea — "if the
> current destination is still on a water frame edge, keep it" —
> survives, because the bank tracer's exits are frame-edge by
> construction.  Original design below, kept for context.

> ⚠️ **UPSTREAM SUBSTRATE BEING REPLACED (2026-07-24)** — this
> tracker consumes anchors from the skeleton-endpoint walker
> (`extract_tree_from_mask` + `pick_waypoint`).  That walker is
> being retired in favour of the edge-anchored centerline
> extractor documented in `docs/edge_anchored_centerline_design.md`.
> The tracker's role — "if the current destination is still on a
> water frame edge, keep it" — carries over cleanly to the new
> substrate (openings are frame-edge by construction).  Details
> of the source-to-sink migration are in the new design doc.

## Motivation

The original `LookaheadTactical` re-ran the full skeleton walker
(`extract_tree_from_mask` + `pick_waypoint`) every tick in SEARCH mode
(reason ∈ {`frame_edge`, `mid_walk`, `not_newly_visible`,
`visited_downgrade`}).  That approach exposed two systemic problems:

1. **Walker instability at Y-junctions.**  When the ship's projected
   edge on the skeleton has two ends with near-tied `align` scores
   against the initial-bearing tie-break, a 5-pixel ship-position
   shift or a 1-pixel mask fluctuation flips the winner and the
   tactical dest jumps hundreds of km between consecutive ticks.
   Canonical failing tick: **t224 in session
   `ai_nav_2026-07-22T08-28-30`** — tactical dest went from lon 33.45
   (NE) at t223 to lon 30.55 (NW) at t224, a ~320 km jump.  The
   tie-break alignments were `−0.60` vs `−0.62` — essentially random.

2. **The walker is expensive** (~10-15 ms per tick for skeleton
   extraction + polyline routing) and its output is nearly always the
   same as the previous tick's when the ship is sailing along a
   frame-edge exit.

## Core insight

In SEARCH mode, the tactical dest sits on a frame edge and marks
"the exit we're heading toward."  Between consecutive ticks the ship
moves ~0.02 km (top speed 9 kt × ~3 s dt), so **the same exit is
still the exit next tick** — the dest lat/lon shouldn't change at
all, or by more than a few pixels of projected position.

Rather than re-derive the exit from scratch each tick (running the
walker), we can **track** the current dest across ticks: check that
it still lies on a water frame edge in the current view, and only
call the walker when that check fails.

## Algorithm

```
maybe_consult(state):
    if state.lat is None: hold_prior_commit; return

    is_lock = current_dest_reason in {"narrow_choke", "dead_end"}
    need_walker = False
    if current_dest is None:
        need_walker = True                   # first tick
    elif is_lock:
        need_walker = reached_dest(state)    # LOCK — walker on arrival
    else:                                    # SEARCH — tracker first
        if not track_search_dest(state):
            need_walker = True

    if need_walker:
        new_dest, reason = pick_new_dest(state)      # full walker
        # continuity guard: reject >MAX_DEST_JUMP_KM jumps
        # (SEARCH mode only; LOCK dests can be arbitrarily far)
        if ok: update current_dest, current_walked_path_latlon

    # commit direction = ship→current_dest bearing (recomputed each tick)
    state.tactical_dest_latlon = current_dest
    state.tactical_walked_path = current_walked_path_latlon
```

### `track_search_dest(state)` — the SEARCH tracker

Cheap (~2 ms); no skeleton, no polyline routing.

1. Project `current_dest` lat/lon → current-frame pixel `(row, col)`
   using ship's current lat/lon and `PX_PER_DEG=100`.

2. **Case A — still valid**: `(row, col)` is in-frame AND on a
   frame edge (within `TRACKER_EDGE_MARGIN_PX=3`) AND
   `mask[row, col] == True`.  Keep the same lat/lon; nothing else
   changes.  This is the common case.

3. **Case B — corner rotation / small edge shift**: dest scrolled
   just off a frame edge, but there's a water run on a frame edge
   within `TRACKER_SEARCH_RADIUS_PX=40` of the projected position.
   Pick the midpoint of that run (in current-frame pixels), convert
   back to lat/lon, and set as the new dest.

4. **Case C — topology change**: no nearby edge-water run found →
   return False → caller falls back to the full walker.  This is
   also what fires when a dead end scrolls into view (the water body
   no longer touches the frame edge the dest was on).

### Continuity guard

Applied to the walker's output (only in SEARCH mode; LOCK dests are
never guarded because dead-ends can legitimately be far).

If the walker returns a candidate more than `MAX_DEST_JUMP_KM=30 km`
from the current dest, log a warning and **hold the previous dest**.
Ship movement is ~0.02 km/tick, so a 30 km jump can only come from
the walker's tie-break flipping — not legitimate anchor movement.

### Walker `init_bearing` (secondary fix)

When the walker DOES fire, its first-edge tie-break now uses
`self._last_commit_deg` (previous tick's committed direction) instead
of the CLI's static `--commit-bearing`.  Keeps the walker's branch
choice consistent with what tactical was already committed to.
Falls back to `goal_bearing_deg` → `heading` → 180° if
`_last_commit_deg` is None (first tick).

## Natural handling of dead-ends

When the water body no longer touches the frame edge the dest was on
(e.g., a dead-end scrolls in from a direction the ship was heading),
Case C fires:

- Tracker returns False.
- Walker runs and finds a `leaf_dead_end` at the deepest inland point
  of the water body.
- `_pick_new_dest` returns `reason="dead_end"` → LOCK mode.
- Ship approaches; `_reached_dest` checks for arrival by `REACHED_PX`.
- On arrival, `DeadEndMemoryMission` records the dead-end into
  `state.visited_dead_ends`.  Next walker call filters out candidates
  within `VISITED_REJECT_KM` of that record → picks a different
  direction (back the way we came, or the other Y-branch).

So the SEARCH → LOCK → arrival → back-out flow is fully covered by
existing machinery + the new tracker; no separate "turning point"
concept was added.  Internally the code still calls these
`dead_end` — see [naming caveat](#naming-caveat) below.

## Parameters

| constant | value | rationale |
|----------|-------|-----------|
| `TRACKER_EDGE_MARGIN_PX` | 3 | how close to the frame boundary counts as "on edge"; 3 px absorbs sub-pixel ship-position rounding |
| `TRACKER_SEARCH_RADIUS_PX` | 40 | radius for Case B corner-rotation search; ~20 % of mini-map height |
| `TRACKER_MIN_EDGE_RUN_PX` | 6 | minimum contiguous water pixels on an edge to count as an exit run; matches `_frame_edge_anchors` |
| `MAX_DEST_JUMP_KM` | 30.0 | ~1500× ship's per-tick movement; catches only true walker flips |

## Test evidence

Session `ai_nav_2026-07-22T08-28-30`, ticks 220-231:

- **Before**: walker ran every tick.  t223 dest at (19.11, 33.45) NE,
  t224 flipped to (19.13, 30.55) NW (~320 km), sim commit swung
  57° → 305°.
- **After**: walker runs once (at t220 init).  Tracker handles t221-t231.
  Dest drifts smoothly from (19.09, 33.37) to (19.07, 33.49).  Sim
  commit stays 55°-60° NE throughout.  No dest flip.

## Naming caveat

The code uses `dead_end` for both:
- a genuine terrain terminus (a bay, a headwater), and
- a "turning point" — a place the ship must approach then reverse from.

Semantically these are the same for the mission layer's LOCK/visited-
memory logic.  If the naming becomes a source of confusion, the
suggested rename would be `dead_end` → `turning_point` throughout
tactical, mission, and state files.  Deferred; see the tail of
`docs/tactical_lock_mission_refactor.md` for the fuller discussion.

## Related

- `docs/lookahead_tactical_design.md` — original (2026-07-20) design
  describing the per-tick walker + EMA-smoothed commit.  Superseded
  in most respects by this document; kept for historical context.
- `docs/tactical_lock_mission_refactor.md` — sketch of moving LOCK
  decisions from tactical to mission layer.  Not implemented; still
  applicable as a future clean-up.
- `data/reference/color_edge_cases_2026-07-22/` — regression fixture
  of raw mini-map crops (t185-187, t250-261) that stressed the mask
  pipeline and choke detection.
