# LearnedSegmenter — Plan

Future direction.  Replaces L2's brittle V11 brightness threshold
with a tiny learned per-pixel classifier.  Not near-term — draft is
for revisiting later.

> **Practical position (2026-06-23)**: V11 is "good enough" on the
> scenes we actually navigate; the documented failures are rare
> and transient (1–3 ticks).  Near-term recommendation is to handle
> them with the **temporal-continuity strategies in §"Near-term
> alternative"** below, and revisit the learned segmenter only if
> those fall short.

## Why (long term)

V11 (`gray <= 175 = water`) works on most scenes but fails on:
- **Region/lighting variation** — open Med vs Nile vs dawn/dusk vs
  night all shift the bimodal split.  Per-region threshold tuning
  is unmaintainable (`feedback_sea_visuals_highly_variable.md`).
- **Translucent overlays** — region-restriction ribbons, weather
  scrim, ad banners (`project_translucent_overlay_corrupts_perception.md`).
  No pixel-only rule can tell "ribbon over water" from "ribbon over
  land."  Demonstrated on t79 of `hug_debug_20260601_231040`.

A small U-Net trained on 500–2000 labeled minimap crops with
synthetic overlay augmentation handles both classes of failure.
Drop-in replacement for `V11Segmenter` — same input (RGB minimap),
same output (binary mask + bookkeeping), same downstream consumers.

## Near-term alternative — temporal-continuity gating on V11

V11's failures are rare AND transient (typical ribbon overlay lasts
1–3 ticks).  Cheap temporal gating absorbs them without retraining
anything.  Options, ordered by simplicity:

### 1. Frame-rejection on mask delta  *(cheapest, recommended)*

Compute `water_frac` per tick.  If `|water_frac[t] - water_frac[t-1]|
> 10%`, flag the tick as untrusted: return the prior mask (or have
the planner emit `skip_reason="mask_anomaly"`).  One signal, one
threshold.  Reuses the existing skip-machinery in `ShoreHugPlanner`.
Recovers automatically when the ribbon disappears next tick.  Cap
"held" ticks at e.g. 5 so the bot doesn't get stuck on stale
perception if the change turns out to be legitimate.

**Failure mode**: a *legitimate* sudden mask change (entering a
port, crossing a strait) gets rejected too — but those are usually
multi-tick, so the 5-tick rejection budget admits them after the
brief hold.

### 2. Ship-CC stability check  *(more targeted)*

The ship's connected-component is what the planner actually cares
about.  Track its centroid + area across ticks.  If the ship's CC
suddenly disappears or shrinks by >50%, hold the prior mask.  More
surgical than frame-level — ignores irrelevant mask changes outside
the ship's local water region.

### 3. Per-pixel EMA on the mask  *(smoothing — not recommended)*

Keep a float-valued running mask, update with
`mask_avg = α·mask_avg + (1-α)·current`, threshold at 0.5.  α=0.7
averages over ~3 ticks.  Smooths transient corruption but also
smears legitimate motion — shore appears to "lag" as the ship
moves.  Probably not the right tool for a moving ship.

### 4. Detect-the-source  *(per-artifact, narrow scope)*

Build a dedicated detector for each known transient artifact (the
blue notice ribbon has a distinctive color signature — bright blue
+ white across a wide horizontal band).  When detected, hold prior
mask.  Adds a per-failure-mode detector that has to grow with each
new transient.

### Recommendation

Ship **#1 + #2 together**.  Frame-rejection catches "everything
changed at once" (ribbon, weather scrim, accidental popup);
ship-CC check catches "the local water region collapsed" (more
surgical, doesn't reject legitimate region transitions).  Both
reuse existing per-tick state, no model retraining, ~30 lines of
code total.  Recovery is automatic and bounded by a max-hold-ticks
budget so the bot can't get stuck on stale perception.

This pairs naturally with `project_translucent_overlay_corrupts_perception`
which already recommended consumer-side gating for the heading
layer — same pattern, applied to L2.

### When to escalate to LearnedSegmenter (Stage A–D below)

Only when temporal gating is provably insufficient.  Triggers:
- Multi-tick failures (>5 ticks) become common in a region we want
  to navigate
- A region exists where V11 is systematically wrong (not transiently)
- Multiple new transient artifacts appear in rapid succession,
  making per-artifact rules untenable

## Goal / success criteria

- Match V11's accuracy on the clean Nile reference voyage (within
  ±1% water-fraction on the 800-tick replay)
- Beat V11 on the documented failure frames (t79 ribbon case +
  whatever new failures the labeling pass turns up)
- Inference ≤ 20ms/tick on Mac CPU (no Metal needed)
- Model size ≤ 2 MB (shipped with the repo)

## Stage A — data + labeling  (~2 days of work)

1. **Seed corpus from existing sessions.**  Pull ~2000 minimap
   crops by uniform-sampling ticks from `data/sessions/` covering:
   - Nile river (Cairo → south, dawn through midnight)
   - Open Mediterranean (Palma, Cairo→Anatolia)
   - Coastal hugging (Nubia voyage)
   - Known V11 failures (t79 ribbon, any others surfaced)
2. **Bootstrap labels from V11** on the frames it gets right.
   Hand-review every 50th frame to confirm the V11 mask is correct;
   discard the rest.  Expected yield: ~1000–1500 usable bootstrap
   labels.
3. **Hand-label the V11 failures.**  ~50–200 frames where V11
   visibly mis-segments.  Build a lightweight labeling tool —
   probably a Streamlit/matplotlib app that lets the user click
   shore points and auto-fills the polygon with `floodfill` / SAM.
4. **Hold out 10% for validation**, 10% for test.  Stratify the
   splits by region (don't put all the dawn frames in train) so we
   detect overfitting to specific lighting.

## Stage B — model + training  (~1–2 days)

1. **Architecture**: tiny U-Net, ~100k–500k params.  Input 400×190
   RGB, output 400×190 binary mask.  Encoder 4 down-blocks @
   16/32/64/128 channels, mirror decoder.  Quick to train, easy to
   debug.  Reference: `unet-pytorch` minimal implementation, ~150
   LOC.
2. **Loss**: BCE + Dice (Dice handles the class imbalance — water
   is often 30–70% of the frame).
3. **Augmentation, in priority order**:
   - **Synthetic translucent overlays**: paste random
     bright-blue/white rectangles or trapezoids over labeled
     frames; the underlying water/land labels stay unchanged.
     This is the load-bearing trick for the t79 case.
   - Horizontal flip, slight rotation (±5°), brightness/contrast
     jitter (handles region + time-of-day shift)
   - Random text occlusions (city labels) — paste rendered text
     glyphs over patches
4. **Training**: AdamW, lr 1e-3, batch 16, ~50 epochs.  Single
   CPU run takes ~30 min.  Save best checkpoint by validation Dice.
5. **Distillation experiments worth trying**:
   - Train with V11 as teacher on the bootstrap labels — student
     learns V11's mask on clean frames cheaply, then fine-tune on
     the hand-labeled hard cases
   - DINOv2 backbone (frozen) + tiny decoder — data-efficient,
     might unlock results with < 500 labels

## Stage C — evaluation  (~half day)

1. **Quantitative**: IoU, Dice, water-fraction error on val + test
   splits.  Per-region breakdown (Nile vs Med vs night vs dawn).
2. **Replay regression**: run `tools/evaluate_voyage` against the
   canonical Nile reference with the learned segmenter swapped in.
   Compare to V11 baseline.
3. **Failure-case spot check**: render side-by-side comparisons on
   the held-out hard cases (t79 ribbon, any others).  Confirm the
   learned model "looks through" the overlay.

## Stage D — integration  (~half day)

1. Implement `LearnedSegmenter` in `brain/ai_nav/layers/segmentation.py`
   following the existing `V11Segmenter` interface (already stubbed
   per README).
2. Add `--segmenter` flag to `tools/run_ai_nav_live.py` mirroring
   the `--heading` flag pattern.  Default stays V11 until learned
   model is validated end-to-end.
3. Save weights to `data/reference/segmenter_v1.pt` (similar to
   the `ship_template_raw.png` pattern).
4. Update README's L2 table row to mark `LearnedSegmenter` as
   working.

## Open questions to revisit before starting

- **Do we need temporal context?**  Stage B's overlay augmentation
  may handle transient occlusions adequately.  Only build a
  multi-frame architecture if the single-frame model fails on the
  ribbon case after augmentation training.  Single-frame is simpler
  to ship and reason about.
- **SAM 2 / CLIPSeg shortcut?**  Worth a one-day spike *before*
  committing to the labeling pass — if CLIPSeg ("water") on a
  handful of minimap crops looks decent, that bypasses the entire
  labeling+training pipeline at the cost of ~100ms/tick.  Acceptable
  only if accuracy on the ribbon case is comparable.
- **Where does this live in inference?**  Likely a saved PyTorch
  model loaded via `torch.jit` for fast inference without the
  training-time framework overhead.  ~2 MB binary.

## Effort estimate

Total ~5–7 days end-to-end if no surprises.  Labeling is the
largest chunk and the long-pole; everything else is small.  The
work is parallelisable with other ai_nav phases — training a
segmenter doesn't block heading or planner improvements.

## Connections

- `brain/ai_nav/layers/segmentation.py` — existing scaffold +
  V11Segmenter
- `tools/perception_v10_channel_prototype.py` —
  `v11_brightness_channel_mask` implementation to replace
- `docs/ai_navigation_landscape.md` §5.2 — original L2 design notes
- `docs/navigation_models_status.md` — existing trained nav models
  (mini-map detector, shoreline classifier) — same training
  infrastructure can be reused
- Memory: `feedback_prefer_learned_segmentation_over_color_rules`,
  `feedback_sea_visuals_highly_variable`,
  `project_translucent_overlay_corrupts_perception`
