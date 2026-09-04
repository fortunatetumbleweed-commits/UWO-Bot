# Channel-perception test frames

Stable corpus of 15 minimap crops (381×184 RGB) used by the V11
channel-extraction prototype work in 2026-06-08/09 and the
production-perception comparison.  Two subsets — the **v13_scenarios**
iteration corpus (clean / village / dawn / dusk / junction cases) and
the **hug_debug_specials** stress cases (transient overlays, UI bleed,
dawn/dusk).  All frames are 381×184 minimap crops as captured by the
bot's sea-tick loop.

## v13_scenarios/ (9 frames)

Hand-labelled iteration set used while building V8 → V13 of the
colour-rule channel mask, plus V11's brightness-threshold pipeline.
Each tick exercises a different perception challenge.

| frame | tick | scenario |
|---|---:|---|
| `tick_0005.png` | t5 | **delta_apex** — Cairo delta, multiple yellow pirates near top, bright Med water |
| `tick_0100.png` | t100 | **clean_channel** — straight Nile reach, baseline |
| `tick_0152.png` | t152 | **nubia_village** — village structure + "Nubia Village" text label on river bank |
| `tick_0300.png` | t300 | **viewport_bend** — channel turns sharply within the minimap |
| `tick_0475.png` | t475 | **Y_junction** — two channels meet, pirate sprites in water |
| `tick_0500.png` | t500 | **dead_end** — narrow tail of a side channel |
| `tick_0571_bari_village.png` | t571 | **bari_village** — village marker + label on river bank |
| `tick_0750_y_branch.png` | t750 | **U_bend** — sharp U-shaped channel |
| `tick_0786_lake_at_end.png` | t786 | **lake_mouth** — channel opens into a larger water body |

Hand-drawn ground-truth bank masks for these scenarios lived under
`/tmp/bank_ground_truth/` during the iteration and were used for the
bank-precision metric — not preserved here because they're hand-labels
from a single annotator and shouldn't be treated as canonical.

## hug_debug_specials/ (6 frames)

From `data/sessions/hug_debug_20260601_231040/` — frames the user
flagged during the V10/V11 evaluation as edge cases that broke
perception.  Lighting variety + UI overlays.

| frame | tick | scenario |
|---|---:|---|
| `tick_0079.png` | t79 | **transient blue-ribbon overlay** — game notice ribbon translucent over channel; splits the V11 water mask into top+bottom CCs |
| `tick_0083.png` | t83 | **dawn — bright water** — orange-tinted bright water, V10 colour gate returns 0 channel px |
| `tick_0084.png` | t84 | **dawn — bright water** — same lighting class as t83 |
| `tick_0086.png` | t86 | **Ottoman flag bleed-through** — UI flag overlays channel area |
| `tick_0087.png` | t87 | **normal sailing (sanity)** — control case at the same session |
| `tick_0153.png` | t153 | **dusk — orange/red hue** — dusk lighting, V10 colour gate fails again |

## How they're used

- `tools/perception_v10_channel_prototype.py` — V10 + V11 prototype
  pipelines; demo block iterates over the v13_scenarios.
- `tools/replay_v10_on_voyage.py` — runs V10/V11 over a whole voyage
  for stats; uses these frames as a quick smoke test against
  `--variant v11`.
- Ad-hoc diagnostic scripts during the V11 iteration referenced these
  frames by absolute paths under `/tmp/`; future work that adds new
  diagnostics should point at this directory instead so the
  diagnostics stay reproducible across machine rebuilds.

## Why preserved

These frames cover the cases that drove the V8 → V13 iteration
(colour-rule failures), validated V11 (brightness + multi-CC keep),
and exposed the production perception's punch-hole bug.  They're a
small (908K) but high-signal regression set for any future channel-
perception change.
