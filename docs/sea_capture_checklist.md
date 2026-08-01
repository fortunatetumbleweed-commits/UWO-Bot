# Sea-Frame Capture Checklist

Reference for capturing labelled training frames for the sea-view vision
models (shoreline classifier + beam / reef / ice YOLO detectors).

**Last counts taken: 2026-05-24 — 76 yolo-tagged sea frames out of 160 total.**

Capture in the labeller (`screen_type = sea`), pick the matching `yolo:`
tag(s), and add any condition tags (`time:day`, `weather:rain`, etc.).
Multi-select is allowed for B / R / I groups (frames often qualify for
several at once); shoreline (S) is single-select — pick the dominant
geometry.

---

## Class targets

### Shoreline (S1–S7)

For the MobileNetV3 classifier that decides "where is land?".

| Tag | Description | Min | **Good** | Great |
|---|---|---|---|---|
| S1 | No land visible | 8 | 20 | 40 |
| S2 | Land on left only | 10 | 25 | 40 |
| S3 | Land on right only | 10 | **25** | 40 |
| S4 | Land directly ahead | 6 | 15 | 25 |
| S5 | Land both sides (strait/river) | 5 | 12 | 20 |
| S6 | Land ahead + one side | 6 | 12 | 20 |
| S7 | Faint / distant land in mist | 4 | 10 | 20 |
| **TOTAL** | | 49 | **119** | 205 |

### Port Beam (B1–B6)

For the YOLO `beam` class plus brightness post-check.

| Tag | Description | Min | **Good** | Great |
|---|---|---|---|---|
| B1 | Bright + tall (undiscovered port) | 8 | 20 | 35 |
| B2 | Dim + short (already-known port) | 8 | 20 | 35 |
| B3 | Beam at frame edge (left/right) | 6 | 15 | 25 |
| B4 | Multiple beams (cluster of ports) | 3 | 8 | 15 |
| B5 | Beam in rain / fog | 4 | 10 | 20 |
| B6 | Beam at night | 4 | 10 | 20 |
| **TOTAL** | | 33 | **83** | 150 |

### Reef (R1–R6) — *deferred, capture opportunistically*

For the YOLO `reef` class. Reefy zones are rare; capture when you find one.

| Tag | Description | Min | **Good** | Great |
|---|---|---|---|---|
| R1 | Reef ahead (collision path) | 8 | 20 | 30 |
| R2 | Reef to side (no collision) | 6 | 15 | 25 |
| R3 | Reefs scattered around ship | 6 | 15 | 25 |
| R4 | Reef near coastline | 4 | 10 | 20 |
| R5 | Reef at sunset / night | 4 | 10 | 20 |
| R6 | Reef in fog / rain | 3 | 8 | 15 |
| **TOTAL** | | 31 | **78** | 135 |

### Floating Ice (I1–I4) — *deferred*

For YOLO `floating_ice` negative-action class (detect but don't avoid).

| Tag | Description | Min | **Good** | Great |
|---|---|---|---|---|
| I1 | Single ice slab | 6 | 15 | 25 |
| I2 | Multiple ice slabs | 4 | 12 | 20 |
| I3 | Ice + reefs in same frame | 3 | 6 | 12 |
| I4 | Ice in different lighting | 4 | 10 | 20 |
| **TOTAL** | | 17 | **43** | 77 |

### Occlusion notes — *flag, don't count toward totals*

Tag whenever a target (land / beam / reef) is partially hidden by the
right panel.  The training pipeline can choose to exclude, weight, or
augment these frames.

| Tag | Description |
|---|---|
| occ_right_panel_land | Land hidden by right panel |
| occ_right_panel_beam | Beam hidden by right panel |
| occ_right_panel_reef | Reef hidden by right panel |
| occ_other | Other occlusion (free-text note recommended) |

---

## Current status snapshot (2026-05-24)

| Group | Have | Min | Good | Great | Δ to Good |
|---|---|---|---|---|---|
| Shoreline | 69 | 49 | 119 | 205 | +50 |
| Beam | 31 | 33 | 83 | 150 | +52 |
| Reef | 7 | 31 | 78 | 135 | +24 to min |
| Ice | 6 | 17 | 43 | 77 | +11 to min |

---

## Prioritised "next 100 frames" recipe

If you want one focused goal that gets both shoreline & beam to **Good**
tier:

| Tag | Reason | Add |
|---|---|---|
| **S3** Land on right | Bot's main steering policy is "coast on starboard"; only 7 today | +18 |
| **B2** Dim beam (known port) | Only 3 today; without this the model thinks every beam = undiscovered | +17 |
| **B3** Beam at frame edge | None today; needed for off-centre detection | +15 |
| S7 Faint distant land | None today; mist / horizon edge cases | +10 |
| B5 Beam in rain/fog | Tests low-vis robustness | +9 |
| B4 Multiple beams | Cluster of ports, rare | +8 |
| **Other top-ups** (mix S1/S4/S6/B1) | Round out the long tail | ~23 |
| **TOTAL** | | **~100** |

Fastest path if time-pressed: **just S3, B2, B3 = 50 frames** = the
smallest meaningful unblock for the v1 model.

---

## Capture conventions (reminder)

- One distinct scene per frame; combinations are fine if natural
  (multi-tag), but don't try to engineer multi-purpose shots.
- Stratify across regions / time-of-day / weather (`time:day`,
  `time:night`, `weather:rain`, `weather:snow`).  These tags drive the
  train/val split.
- For S5 (strait/river) — capture whenever you find one; rare geometry.
- For B2 (known-port beam) — easiest path: sail toward an
  already-discovered port and capture while the beam is visible but
  before "Enter New" appears.

---

## Hardest-to-find scenes (capture opportunistically)

- **S5** strait/river — both shores at once
- **S7** faint distant land in mist
- **B4** multiple beams visible together
- **B6 + R5** any night-time scene
- **I3** reefs + ice in same frame
- Any **occlusion** case (target hidden behind right panel)

---

## When to ping me

- Once **shoreline reaches Good (119)**: ready to run
  `tools/claude_label_sea_frames.py` and train v1 of the shoreline
  classifier.
- Once **beam reaches Good (83)**: ready to start the YOLO beam class
  training pipeline.
- Reef and ice can run separately whenever they reach min tier.

— *Generated 2026-05-24; revisit when models start training.*
