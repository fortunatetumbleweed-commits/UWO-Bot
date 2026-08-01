# Ship-heading detection — `pca_yellow_anchor` strategy

Production change spec for replacing the unreliable `pca_fallback` in
`ShipHeadingDetector` with a hull-axis + yellow-anchor strategy, plus
a hull-reconstruction pre-pass for text-occluded frames.

Derived from the 2026-06-01 diagnostic (`tools/diag_ship_heading.py`,
`tools/diag_wedge_closeup.py`) against the upper-Nile voyage frames
t50 / t134 / t135.  Truth angles confirmed visually by the user.

## What's broken today

`MinimapShipHeadingDetector.detect()` tries three strategies in order:

1. `_sail_pair` — bisector of two yellow sail blobs (high confidence)
2. `_bow_wedge` — small yellow triangle at bow tip (medium confidence)
3. `_pca_fallback` — PCA on ship_green + tip-asymmetry tiebreaker
   (low confidence, **180° ambiguous**)

When the ship icon is occluded by text labels (e.g. "Nubia Village"
overlapping the bow), the green hull gets fragmented (40% to 90% of
pixels overwritten by text glyphs), the yellow candidates fail
`sail_pair`'s 68° separation check, `bow_wedge`'s area filter, or both.
The flow falls to `pca_fallback`, which:

- Computes PCA on a fragmented green blob (101 px at t134, 38 px at
  t135 in the diagnostic frames)
- Picks the bow end by "side with more pixels" — which is **inverted
  for ship icons** because the bow is wide (sails fan out) and the
  stern narrow

Result: 50-50 coin flip on bow direction.  Live trace from
`hug_debug_20260531_234502` shows t128→t135 producing ±90-180° heading
swings between consecutive ticks.

## The fix

Replace `pca_fallback` with two changes:

### 1. Hull reconstruction (text_close pre-pass)

When OCR finds text bboxes inside the mini-map disc, run morphological
closing on `ship_green` **through the text-bbox region only** before
applying `_clean_ship_green`.  This bridges the gaps text glyphs cut
through the ship hull.

Diagnostic evidence (text_close green vs baseline green):

| Tick | baseline green | text_close green |
|---|---|---|
| t50  | 403 px (no text) | 403 px (no change) |
| t134 | 101 px (fragmented) | 259 px (2.5× more) |
| t135 | 38 px (fragmented) | 383 px (10× more) |

Implementation: scipy `binary_closing` with a 7×7 kernel, applied only
where text_bbox is True; original `ship_green` elsewhere.

### 2. `pca_yellow_anchor` strategy

```
strategy = pca_yellow_anchor:
  1. Run text_close to reconstruct the green hull
  2. PCA on the reconstructed green → principal axis (undirected)
  3. Find yellow CCs that touch the green hull (within 2px dilation)
     — filters NPC sprites + village shadows
  4. Compute touching-yellow centroid
  5. Project (yellow_centroid − green_centroid) onto principal axis;
     bow_end = whichever axis end has positive projection
  6. bow_direction = unit vector along principal axis pointing to
     bow_end
  7. Confidence = 0.65
```

This separates the two concerns the user identified:
- **Direction** comes from the hull's principal axis, which is robust
  to "which sail survived"
- **Bow-vs-stern choice** comes from the yellow position, which is
  ALWAYS at the bow side regardless of which sail is visible

Diagnostic results vs truth:

| Tick | Truth | sail_pair | yc_touches | **pca_yellow_anchor** |
|---|---|---|---|---|
| t50  | 158° | 158° (sail_pair conf 0.99) | 164° (6° err) | **155° (3° err)** |
| t134 | 255° | 255° (lucky pca_fallback) | 207° (48° err) | **220° (35° err)** |
| t135 | 312° | **124° (180° wrong)** | 334° (22° err) | **312° (0° err — exact)** |

Strict improvement over `pca_fallback` on all three frames.

### 3. Confidence-based bot-layer fallback

When the new strategy returns conf < 0.5 (e.g. very few touching-yellow
pixels), honor the existing `heading_rejected` flag in the bot layer:
hold the previous good heading instead of propagating noisy values to
the steering controller.  Today the flag is set but the value flows
through anyway (see §13.18 trace analysis 2026-06-01).

## Files affected

- `vision/minimap_navigation_view.py`
  - Add `_text_close_green(green, text_bboxes)` helper.
  - Add `ShipHeadingDetector._pca_yellow_anchor` method.
  - Insert it in `detect()` chain BEFORE `_pca_fallback`.
  - Keep `_pca_fallback` as last-resort (e.g. no yellow at all).
- `brain/perceive.py`
  - Pass OmniParser-detected text bboxes that overlap the mini-map
    crop into `MinimapNavigationView`.  Coordinate translation only;
    no new vision work.
- `brain/goals/hug_shore.py` (or the new `Voyage` loop layer when it
  lands)
  - Honor `heading_rejected` — when set, hold prior heading instead of
    using the rejected value.  Already gated by `heading_confidence`
    in the detector; just needs the consumer side to act on it.

## Cost analysis

| Stage | Cost | When run |
|---|---|---|
| color_masks (existing) | ~1 ms | always |
| ship_green clean (existing) | ~5 ms | always |
| sail_pair / bow_wedge (existing) | ~1 ms | always |
| text_close + bbox lookup | ~5-10 ms (OmniParser reuse, ~0 ms) | only when sail_pair / bow_wedge fail |
| PCA + yellow anchor | ~3-5 ms | only when sail_pair / bow_wedge fail |

From the §13.17 trace data, `sail_pair` fires on ~40% of ticks; the
new cost is paid on the remaining ~60%, which are exactly the ticks
that today produce noise.  Average overhead **~5-10 ms/tick** — within
the existing budget.

OmniParser already runs per frame and produces text bboxes for the
full screen.  Reusing those (translating to mini-map coordinates) is
free.  If OmniParser isn't available, fall back to a per-mini-map
EasyOCR call (~50-200 ms) only on sail_pair-failing ticks.

## Test plan

1. **Unit tests** (`tests/test_minimap_heading.py`, extend existing):
   - text_close: applies closing only inside passed text bbox;
     identity outside.
   - pca_yellow_anchor: synthetic frames with known ship orientation
     and one or two yellow blobs — verify hull-axis direction +
     yellow-end disambiguation.
   - Regression: t50 / t134 / t135 fixture frames (already saved in
     `data/sessions/hug_debug_20260531_234502/`) with the documented
     truth angles.
2. **Integration**: re-run the diagnostic tool after the change to
   confirm `pca_yellow_anchor` is being invoked and producing the same
   angles as the standalone diagnostic.
3. **Live test**: re-run the upper-Nile voyage
   (`--side port --destination-lat 5.71 --destination-lon 32.12`).
   Expectation: the ±90-180° heading swings at t128-t135 disappear;
   trace shows `pca_yellow_anchor` firing instead of `pca_fallback`,
   with conf 0.65; bot's commanded heading evolves smoothly.

## Risk + rollout

- **Risk: degrades a frame that currently works.** Mitigation: keep
  `_pca_fallback` as a last-resort after `_pca_yellow_anchor` (so any
  frame the new method refuses falls back to current behavior).  And
  introduce behind a flag (`HEADING_PCA_YELLOW_ANCHOR_ENABLED`)
  initially.
- **Risk: OmniParser cache misses on a mini-map text label.**
  Mitigation: if OmniParser returned no text in the mini-map region,
  text_close becomes a no-op and the strategy degrades to standard
  PCA + yellow-end-of-axis — still better than tip-asymmetry pca.
- **Rollout**: ship with the flag default-off; toggle on after the
  unit tests + a live-validated upper-Nile run.  After two clean
  voyages, default-on; remove the flag.

## Future work (not in this change)

1. **t134 residual error (35°)**: the reconstructed hull at t134 has
   the wrong principal axis because morphological closing merges
   unrelated fragments.  Fitting PCA only on the LARGEST connected
   component of the reconstructed green should help.  Defer until live
   data shows it matters.
2. **Temporal anchor**: when conf < 0.5, reject any new heading more
   than 60° away from the previous-tick heading (physical ship turn
   rate cap).  Catches t134-class blunders structurally.  Belongs at
   the Voyage / Steering Loop layer once the §13.17 refactor lands.
3. **Sail-pair separation hint**: when one sail visible and prior
   heading available, use the ±34° offset rule (half of 68° sail-pair
   separation) to refine the yellow_centroid bearing.  Compensation
   sign decided by which side of principal axis the visible yellow
   lies.  Marginal improvement on t135-class frames.
