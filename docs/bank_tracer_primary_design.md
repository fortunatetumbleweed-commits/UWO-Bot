# Bank tracer as primary picker — experimental redesign

**Branch:** `bank-tracer-primary`
**Base:** `skeleton-refactor` @ c37eba9

## Motivation

Current `LookaheadTactical.maybe_consult` (Tracker-first) has failure
modes at P4, P12, P14, P15, P16, P17.  Recurring pattern: the SEARCH
tracker holds a dest that's stale relative to current bank topology,
and the various fallbacks (corner-split, tracker-fail-then-repick)
either misfire or fire too late.

Bank tracer costs ~1 ms per call.  Running it every tick is trivial.
This design flips the roles: **bank tracer is authoritative every
tick, tracker becomes a cross-frame verifier**.

## New maybe_consult flow

```
Every tick:

  A. Determine approach_bearing (existing priority chain +
     cross-check safety net).

  B. Run trace_left_bank → polyline_new.

  C. If polyline_new is None:
       - Log fatal (unusual — mask is empty or ship on land).
       - Hold prior state.tactical_walked_path (shift-integrated).

  D. Verification (if we have a prev polyline):
       - Shift prev polyline endpoints by measured frame_shift.
       - Compute endpoint delta between prev-shifted and current.
       - If delta < VERIFICATION_TOLERANCE_PX (e.g. 20):
           → stable topology, high confidence.
       - Else:
           → topology transition OR approach corrupted.
           → Log at INFO with delta and both endpoints.
           → Decision: prefer current polyline (fresher) unless a
             specific corrupted-approach signal fires (see F).

  E. Anchor snap (edge continuity, replacing SEARCH tracker's role):
       - If polyline endpoint lands within TRACKER_MIN_EDGE_RUN_PX (6)
         of a frame-edge water-run midpoint (in ship's CC), snap the
         endpoint to that midpoint.  Same "world-lock the dest to a
         physical exit" property the tracker provided.
       - Otherwise (dead-end truncation), keep the raw endpoint.

  F. Corrupted-approach guard (optional stability rule):
       - If bounce this tick (speed drop ≥70%), OR
         approach direction differs from prev tick's by > CORRUPTED_TOL_DEG (90):
           → run tracer with HEADING instead of motion signals.
           → if heading-trace endpoint is closer to prev-shifted
             endpoint, prefer heading-trace.
           → this catches P17-class (post-bounce approach flip).

  G. Store:
       - _current_walked_path_latlon = polyline (converted to lat/lon)
       - _current_dest_latlon = polyline endpoint
       - _current_dest_px_offset = endpoint offset from ship
       - state.tactical_walked_path = polyline (HugPathPlanner reflex reads this)

  H. Compute commit_direction = ship → endpoint bearing.
```

## What we're removing

- `_track_search_dest` — its edge-midpoint-nearest logic becomes Step E.
- `_corner_split_since_last_tick` — replaced by fresh trace every tick.
  Corner-split is implicit: when a corner splits, the bank tracer's
  polyline naturally follows the new continuation.
- The tracker-vs-fresh-pick branching in maybe_consult.

## What we're keeping

- `trace_left_bank` (unchanged — already correct at P10/P11 level).
- Shift-integration of stored offset (Step 0 of old flow) — used
  as the anchor for anchor-snap (Step E).
- Cross-check safety net (shift vs motion, > 90° diff → prefer motion).
- HugPathPlanner's `_pick_from_path` reflex WP picker.
- Pipeline-side speed-consistency shift gate.

## Verification and diagnostics

For each tick log:
- polyline length, endpoint, approach used
- verification delta vs prev-shifted
- corrupted-approach guard fired (Y/N)
- anchor-snap applied (Y/N + which frame edge)

## Regression cases to re-test

- **P16 (t206)**: expect bank tracer with correct approach to produce
  NE endpoint, not the LEFT-edge midpoint the old buggy tracker gave.
- **P17 (t401)**: bounce tick.  Corrupted-approach guard should fire
  because approach jumped from steady-south to post-bounce east.
  Guard swaps to heading (177° S), tracer produces bot-right endpoint,
  no flip.
- **P12 (t445-t448)**: bank tracer following bank continuation should
  naturally identify the new left-edge dest when the top-edge run
  starts to close off.  Corner-split rule no longer needed.
- **Stable ticks**: e.g. t391 in ai_nav_2026-07-25T15-54-02 (already
  correct under current code) should produce IDENTICAL behavior —
  don't regress the boring good cases.

## Cost budget

- Bank tracer: ~1 ms per call
- Anchor snap: ~0.1 ms (small edge scan)
- Verification: ~0.02 ms (endpoint delta)
- Total per-tick tactical layer: ~1.2 ms (up from ~0.5 ms of the
  old tracker-first path when tracker hit)
- Bank tracer might be called twice per tick if corrupted-approach
  guard fires: still ~2.5 ms.  Negligible against 2 s tick budget.

## Success criteria

1. All P14-P17 flip cases produce correct dest (empirically the
   right frame-edge exit for that geometry) under the new flow.
2. P12 case works without a corner-split rule.
3. Stable ticks produce dests within 5 px of what current code
   produces (no regression on good cases).
4. In a live voyage: 0 P17-class flips; ≤ current voyage's flip
   count on all other classes.

## Investigation log (2026-07-25 → 2026-07-27)

### Variant 1: bank tracer per tick, no override
- Approach = shifted-prev-endpoint bearing
- No override rule
- Result: cliff sensitivity → once trace flips to wrong side, prev
  endpoint's bearing → same wrong side next tick → perpetuates
- All 3 flip zones (P16, P17, stable-t391) failed to recover

### Variant 2: sticky anchor-held override
- Override tracer if shifted-prev-anchor still in reachable water
- Result: perpetuates ANY bootstrap error → all zones drift to
  wrong-side anchors and hold there forever

### Variant 3: sticky shifted-prev-polyline override
- Override tracer if shifted-prev-polyline endpoint still valid
- Result: same perpetuation problem as Variant 2

### Variant 4: `trace_to_wp` (shorter contour arc from ship to WP)
- 4 of 6 test cases correct
- 2 failures: "shorter arc" is not always the correct navigable
  path — can go the long way around the water body, or hug the
  starboard bank when we want port
- Cannot respect hug_side geometrically

### Variant 5: `trace_left_bank` with anchor-bearing as approach
- Uses trial-walk with approach = bearing-to-anchor
- Fundamental issue: for a SE-heading ship wanting to reach a
  BOT-right anchor (on the SW/starboard side of a SE-going ship),
  port-hug traces the NE bank UP, never reaching BOT-right
- Port bank of desired direction may not lead to the anchor when
  ship's direction has changed from when the anchor was picked
- All 6 test cases flipped to TOP area

### Variant 6: `trace_bank_to_edge` — hug_side fixed, approach for projection
- Walker direction fixed by hug_side (no trial-walk cliff)
- approach used only for `_project_ship`
- Verification: shifted-prev-WP should lie on the traceline
- Turning-point detection: >90° angle change in trace near prev-WP
  signals ship should pass through it
- Result: cv2 CCW contour convention makes step=-1 for port
  potentially incorrect — needs experimental verification per
  water-body geometry.  Also `_project_ship` scoring may pick the
  wrong contour side when approach direction is ambiguous.

## What we learned

- **Approach direction as trial-walk hint is fragile at Y-junctions.**
  Cliff sensitivity is inherent to the trial-walk-picks-direction
  heuristic; any approach signal can fall on the wrong side.
- **Sticky overrides perpetuate bootstrap errors.** Any rule that
  "hold the last state if it's still valid" carries forward whatever
  bad decision was made at bootstrap.
- **Port/starboard is ship-relative, not geometry-relative.** When
  the ship's forward direction changes (or the ship spins during a
  bounce), the "port bank" changes with it — the port bank of a
  SE-going ship isn't the same shore as the port bank of a S-going
  ship.  So a tactical WP picked when ship was going one direction
  may not be reachable via port-hug when the ship's forward has
  changed.  This is a fundamental tension.
- **Tactical WP should be a persistent anchor** that survives
  changes in ship orientation.  Re-tracing the bank every tick to
  reach an anchor doesn't guarantee reachability under all ship
  orientations.  This is why the ORIGINAL SEARCH tracker design
  (cache the anchor, don't re-trace unless invalidated) had its
  strengths — it decoupled the anchor's identity from the trace.

## Recommendation

Retire this branch.  The insights inform future work but the
implementation approaches attempted did not produce a stable
improvement over the master branch's fixes (P14/P15/P16/P17).

Master branch tactical layer already handles the reported flip cases
via:
- True-2D distance in SEARCH tracker (P16)
- Stricter corner-split gate (P17)
- Cross-check safety net (P14/P15)

Further work should be at the perception layer (mask topology, shore
model) or at the mission layer (turning-point locks) rather than
re-architecting the tactical layer's bank tracer.
