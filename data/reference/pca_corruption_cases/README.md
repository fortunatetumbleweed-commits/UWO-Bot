# PCA heading-corruption reference cases

Preserved from `data/sessions/ai_nav_live_palma/` (Palma live L4 test,
2026-06-21).  Each frame is a minimap crop (400×190) demonstrating
a specific class of overlay that corrupts the legacy PCA heading
reader in `tools/centerline_waypoint_prototype.py::estimate_ship_heading`.

Use these for:
- Verifying any fix to the PCA reader (prior-heading tiebreaker, CNN
  replacement, etc.)
- Training data when bootstrapping a learned heading regressor
- Regression tests when touching `estimate_ship_heading` or
  `brain/ai_nav/layers/heading.py::PCAHeading`

## Cases

### Case A — translucent text overlay (fleet name)

| Tick | Heading reported | Truth |
|---|---|---|
| t34 | 88.7° (E)    | E — clean prior |
| **t35** | **268.9° (W)** | E — **180° wrong** |
| t36 | 79.3° (E)    | E — recovered |

Corruption source: NPC fleet's name "Wattasid Dy…" (Wattasid
Dynasty) rendered in bright cyan/white across the minimap width,
its horizontal stroke crossing through the ship icon.  The bright
text pixels asymmetrically brighten one half of the ship's
principal axis → PCA's brightness tiebreaker flips bow/stern.

### Case B — NPC sprite + port anchor sprite overlays

| Tick | Heading reported | Truth |
|---|---|---|
| t84 | 266.0° (W)   | W — clean prior |
| **t85** | **66.3° (NE)**  | W — **180° wrong** |
| t86 | 266.0° (W)   | W — recovered momentarily |
| t87 | 258.6° (W)   | W |
| t88 | 252.9° (W)   | W |
| **t89** | **65.0° (NE)**  | W — **180° wrong** |
| t90 | 247.5° (W)   | W |
| **t91** | **71.5° (E)**   | W — **180° wrong** |
| t92 | 232.6° (SW)  | W-ish — recovered |

Corruption source: yellow NPC pirate faces (sprites) directly
adjacent to or partially overlapping the green ship icon, plus a
yellow port anchor icon nearby and a "???" undiscovered-port label
text below.  As the bot rotates and the relative position of these
yellow blobs shifts, PCA's brightness tiebreaker flips bow/stern
every tick.

## Files

```
tick_0034.png    — clean prior to t35 corruption
tick_0035.png    — Case A: Wattasid text overlay
tick_0036.png    — recovered

tick_0084.png    — clean prior to t85 burst
tick_0085.png    — Case B: NPC + anchor (flip)
tick_0088.png    — clean within burst
tick_0089.png    — Case B: NPC + anchor (flip)
tick_0090.png    — clean within burst
tick_0091.png    — Case B: NPC + anchor (flip)
```

## Verification recipe

For any PCA replacement, the test is simple: feed `tick_NNNN.png`
through the new heading reader together with the prior frame's
heading, and check that the corrupted ticks (t35, t85, t89, t91)
report a heading within ±45° of the prior heading rather than
flipping ~180°.

```python
from PIL import Image
import json

cases = [
    # (corrupted tick, prior tick, expected_truth_deg)
    (35, 34,  90),    # Case A
    (85, 84, 266),    # Case B
    (89, 88, 253),
    (91, 90, 248),
]
for corrupt, prior, truth in cases:
    img = Image.open(f"tick_{corrupt:04d}.png")
    prior_img = Image.open(f"tick_{prior:04d}.png")
    prior_hdg = read_heading(prior_img)            # should be ~truth
    new_hdg = read_heading(img, prior_deg=prior_hdg)
    delta = shortest_angle_delta(new_hdg, truth)
    assert delta < 45, f"t{corrupt}: hdg={new_hdg}° truth={truth}° Δ={delta}°"
```

## Provenance

Live ai_nav run at Palma (39.48, 3.25) on 2026-06-21.  Pipeline:
PCAHeading + V11Segmenter + ShoreHugPlanner + MoondreamTactical
(actually llava:7b) + NoOpStrategic.  Bot circumnavigated an
island counter-clockwise; these frames are from when the bot was
hugging the south side of the island and rotating past clusters of
NPC sprites + port markers.
