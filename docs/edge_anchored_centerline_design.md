# Edge-anchored centerline extraction

> ⚠️ **OBSOLETE (2026-07-28) — never wired.**  This was proposed to
> retire the skeleton-endpoint substrate, but the tactical layer instead
> moved to the **bank tracer** (`tools/bank_tracer.py` `trace_bank_to_edge`
> + the `LookaheadTactical` anchor policy: sticky / migrate / new_anchor /
> hold(pocket_ahead) / reach-gate).  See the "Tactical anchor substrate —
> CURRENT" pointer in `CLAUDE.md`.  Kept for history; the prototype in
> `tools/edge_centerline_prototype.py` is not on the live path.

**Status (historical):** design, prototype in
`tools/edge_centerline_prototype.py`.  Never wired into the pipeline.

**Supersedes:** the skeleton-endpoint anchor extraction in
`tools/centerline_extraction_prototype.py`
(`_anchors_from_pruned_skeleton`, `_frame_edge_anchors`,
`_dead_end_anchors`) and the associated `edge` / `dead_end` /
`turning_point` anchor taxonomy consumed by
`brain/ai_nav/layers/tactical.py`.

## Motivation

The current tactical layer works by:
1. `skimage.morphology.skeletonize(water_mask)` → 1-pixel skeleton
   with all branches, spurs, and dead-ends.
2. Prune skeleton branches shorter than 30 px.
3. Classify remaining degree-1 nodes:
   - `edge` if within 3 px of the frame boundary
   - `dead_end` otherwise
4. Tactical picker chooses one anchor as the "target" based on
   distance, prior selection, edge continuity, etc.

Failure modes we hit repeatedly:

| symptom | root cause |
|---|---|
| SSW branch truncates 26 px short of frame bottom on Cairo t1 | `skeletonize` treats the image border as a natural boundary and erodes wide open regions from that boundary inward.  The medial axis never extends into the widening. |
| 5 anchors on the S3-t444 corner-water case | Sprite fragments (pirate/ship sprites in the water body) generate short skeleton spurs. |
| Phantom "east" anchor on Cairo t1 | The closing operation bridges the "Cairo" village text.  The bridged region reads as a narrow channel leading to a small pocket → a spurious `dead_end`. |
| Tactical picks nearest turning-point instead of goal-aligned exit | The picker has no notion of "which anchor leads to the goal" — it just sees a bag of anchors and their distances. |

We tried patching these one at a time (tighter frame-margin, spur
pruning, DT-at-tip pruning) and each patch either failed to catch
the real problem, or mislabelled legitimate outlets.  The
skeleton-endpoint substrate is the wrong abstraction for
navigation.

## Framing

For UWO navigation the right question is:

> *"Where can the ship go from here — what are the openings out of
> this frame, and what channel connects the ship to each?"*

Not:

> *"Enumerate every skeleton branch of the visible water body."*

The two questions have overlap but the second one produces spurious
work (spurs, artifacts, wide-basin truncation) and misses real
answers (D208 on Cairo t1 that should be the south exit).

## Algorithm

**Inputs:**
- `mask` — water mask (V11 → `binary_closing(disk(7))` →
  ship-CC → narrow-water filter `DT ≥ 5` → ship-CC).
- `dt` — distance transform of `mask` (pixels-to-nearest-shore).
- `ship_yx` — ship position in the mini-map (`(H//2, W//2)` after
  calibration).

**Steps:**

1. **Find openings.**  For each frame edge (top, bottom, left,
   right), scan `mask[edge, :]` (or `mask[:, edge]`) for
   contiguous strips of water pixels.  Each strip = one
   *opening*.  Anchor point = midpoint of the strip.  Metadata:
   `{side, y, x, span_px}`.

2. **Snap ship to water** if the ship pixel is on land (sprite
   occlusion) — nearest water pixel.

3. **For each opening**, compute the shortest path from
   `ship_snapped` to the opening's anchor in a cost field of
   `1 / (DT + 1)` over the water mask (8-connected).  Uses
   `skimage.graph.route_through_array(cost, start, end,
   fully_connected=True, geometric=True)`.

   The cost field's minimum path naturally follows the widest
   water (highest DT = lowest cost).  That path IS the centerline.

4. **Output** = one `Centerline{opening, polyline}` per opening.
   Zero enumeration of branches, spurs, dead-ends.

**Cost:** dominated by the Dijkstra in `route_through_array` —
~50-100 ms per opening pair on a 400×190 mask.  Most ticks have 2-3
openings, so total ~100-300 ms per tick.  Comparable to current
skeleton pipeline.

## Why this fixes the failure modes

| symptom | fix |
|---|---|
| SSW branch truncates 26 px short | We never call `skeletonize`.  The bottom opening is detected directly from `mask[H-1, :]` and Dijkstra runs the full centerline. |
| Sprite fragments generate 5 anchors | We never enumerate branches.  Sprites disturb the mask locally but don't affect the number of openings or the Dijkstra path (path just routes around them via the high-DT interior). |
| Closing bridge creates phantom "east" anchor | The bridged pocket isn't a frame opening — the algorithm doesn't look at it. |
| Picker chooses nearest turning-point over goal-aligned exit | The picker now scores between *centerlines* (each labelled with its opening side).  Goal-aligned scoring is a simple bearing check on the opening's direction relative to the ship. |

## Validation

Ran the prototype on all 31 tactical-regression gallery ticks:
- 5 ticks with 1 opening (Y-tip / dead-end pockets — single
  centerline back the way we came)
- 20 ticks with 2 openings (straight channels — top+bottom or
  top+right etc.)
- 6 ticks with 3 openings (real junctions)

Every centerline computed; no failures.  Visual comparison in
`/tmp/centerline_viz/index.html` (regenerate with
`python -m tools.edge_centerline_prototype`).

## Migration path (not yet done)

1. **New data type:** replace the `Anchor(y, x, kind)` NamedTuple
   in `tools/centerline_extraction_prototype.py` with
   `Centerline(opening: Opening, polyline: list[(y, x)])`.  Opening
   carries `side`, `y`, `x`, `span_px`.

2. **Tactical layer picker:** update
   `brain/ai_nav/layers/tactical.py` to consume `Centerline`s.
   - Scoring: `alignment(opening_bearing, goal_bearing)` +
     `channel_width` (mean DT along polyline) + `edge continuity`
     (was the same opening picked last tick?).
   - Remove the `edge` / `dead_end` / `turning_point` classification
     — no longer needed; every opening IS a frame exit by
     construction.

3. **Reflex planner:** `HugPathPlanner` already walks a
   `tactical_walked_path`; the polyline of the chosen `Centerline`
   is exactly that.  Should be a drop-in replacement.

4. **Retire the skeleton pipeline** in
   `tools/centerline_extraction_prototype.py` and its consumers
   (`brain/ai_nav/layers/segmentation.py` still calls
   `channel_mask_from_rgb` — that stays, since it's just the mask
   builder; only the anchor extractor is retired).

5. **Y-tip / dead-end pocket case** (1 opening only): tactical's
   arrival test today conflates "reached turning point" with "at
   destination pocket".  With centerlines, "1 opening only" means
   "you're at a pocket — the only way out is back".  The mission
   layer decides whether to enter, retreat, or scan around.

## Related

- Prototype: `tools/edge_centerline_prototype.py`
- Memory note: `project_edge_anchored_centerlines.md`
- Old skeleton design: `docs/lookahead_tactical_design.md`
  (obsolete — supersede when migration lands)
