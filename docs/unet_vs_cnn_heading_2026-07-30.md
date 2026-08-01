# Heading model: region-parts U-Net vs CNN regressor (2026-07-30)

Replaced the production heading estimator — the scalar-regressing **CNN**
(ensemble with reconstruct-match) — with the **region-parts U-Net**
(`ship_parts`, vendored to `brain/ai_nav/learned/ship_parts/`). This doc
records why, and the offline + live A/B that justified it.

## The two approaches

| | CNN regressor (old) | Region-parts U-Net (new) |
|---|---|---|
| architecture | small CNN → `(sin, cos)` of heading (a scalar angle) | U-Net segments the ship into parts (bow / hull / stern / sail_l / sail_r) → rigid part-layout fit → heading |
| how direction is fixed | learned end-to-end; the bow/stern bit is implicit and can flip | whichever distinctive part survives the occlusion *names* the direction; an open-water penalty enforces scene consistency |
| interpretability | black box (a number) | per-pixel part labels + which parts were seen |
| latency | ~36 ms | ~40 ms |
| convention | 0 = N, CW | 0 = N, CW (native match) |

The CNN's weakness is the **bow/stern 180° ambiguity**: on a blunt/occluded
fragment it has to guess which end is the bow, and a confident-but-wrong guess
is indistinguishable from a right one. The U-Net reads the *parts* instead, so
the direction is recovered from evidence, not a coin-flip — and it refuses to
place the hull on open water where no ship was seen.

## Offline accuracy (from the `ships` repo's labelled validation)

| set | template matcher | **CNN** | **region-parts U-Net** |
|---|---|---|---|
| clean `synth_*` (12) | ~0–1° | 2.2° | **0.7°** |
| occluded `hard_*` (9) | 7/9 within 20° | 8/9, 9.3° | **9/9, 6.1°** |

The U-Net is the only approach that read the mislabelled `t083` case correctly
(the CNN and matcher both agreed with a bad label that put the ship on open
water). "A black-box regressor agreeing with a label is not the same as being
right."

## De-risking on real UWO frames (before wiring)

Ran the U-Net on real minimap crops from a recorded voyage and compared to the
known-good trace headings:

- convention + crop scale **correct** — no 180° flip, no offset
- agreement with the trace: **median ~6°**, all 5 parts seen every frame
- latency **~40 ms/tick** (≈ the CNN's ~36 ms)

## Live A/B — two full Cairo→Y-tip→Cairo round trips, everything else equal

Both runs share **all** the same steering + perception fixes (300ms PID
min-hold, reflex direct-to-anchor, hold-aim-at-tip, artifact-reversal guard,
saturation water rule). The **only difference is the heading model**, so this
isolates its effect.

| metric | **CNN ensemble** | **U-Net** |
|---|---|---|
| session | `ai_nav_2026-07-29T19-12-30` | `ai_nav_2026-07-30T13-29-08` |
| ticks | 1000 | 1000 |
| **median heading jitter** (tick-to-tick) | 1.66° | **0.0°** |
| **heading >40° swings** | 42 | **14** (⅓) |
| **PID fired a hold** | 35% | **21%** (40% fewer corrections) |
| **bounces** | 3 | **1** (best on record) |
| Cairo return | overshot into the Med | **hit target (30.22, 30.40) at t818** |

### Reading

A steadier, more accurate heading feeds **less error** into the PID, so it
issues far fewer steering commands (21% vs 35% holds) — which shows up directly
as smoother motion, ⅓ the large heading swings, and down to a single bounce.
The accurate heading also kept the ship *on the line*, landing the Cairo return
essentially on target for the first time.

This complements — rather than replaces — the earlier steering fixes. The
`min-hold` change removed the *amplification* (the deadband-defeating whiplash
cascade); the U-Net removes the *input noise* that was driving corrections in
the first place. A noisy heading injects error into even a well-tuned loop, so
the sharper reading is an independent, measurable smoothness gain.

## Integration

- Vendored the 4-file drop-in → `brain/ai_nav/learned/ship_parts/`
  (`estimator.py` + `__init__.py` + `part_model_v1.pt` + `canonical.npz`).
- `ShipPartsHeading` HeadingLayer in `brain/ai_nav/layers/heading.py`: reuses
  the 80×80 green-centroid crop, maps `Result.heading`→`bearing_deg`, derives
  confidence from `parts_seen` (5 parts → 0.95; no bow/stern → cap 0.45).
- `--heading unet` added and made the **default**.
- Tactical scenarios replay on the same U-Net (production parity); all 9 pass.
- `tick_viewer.py`: yellow "model raw" arrow now re-runs the U-Net live
  (distinct from the picked cyan `heading_deg`, which may be the motion bearing
  after correction).

## Open items (deferred)

- **Return-to-Cairo arrival detection** — after reaching Cairo the ship
  overshoots into the Med (the mission never registers "done"). Not a nav
  failure; the ship keeps exploring, which is useful — it surfaced some
  **open-water bank-hugging** roughness to address later.
- The residual reactive-avoider whiplash in tight channels (a small
  proximity-scaled bias, deferred earlier) is largely masked now that the
  heading is steady, but remains the next steering lever.
