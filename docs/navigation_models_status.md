# Navigation perception models — corpus + training status

**Last updated:** 2026-05-27
**Models:** `data/models/minimap_detector.pt`, `data/models/shoreline_classifier.pt`
**Trainers:** `tools/train_minimap_detector.py`, `tools/train_shoreline_classifier.py`
**Inference modules:** `vision/minimap_reader.py`, `vision/shoreline_reader.py`
**Wire-in:** `brain/perceive.py::_publish_observation` populates
`BotObservation.minimap` and `BotObservation.shoreline` whenever
`result.state == "sea"`.

## Labelled corpus

|  | count |
|---|---|
| Labelled sea frames | **409** |
| All labelled frames | ~900 |
| Sessions covering sea | 22 |

### Sea-frame per-tag counts

These are the per-tag positive counts on the latest row of every sea
frame in `data/labels.jsonl`.

#### Shoreline (ship-relative, 4 binaries)

| Tag | Positives | % of sea frames |
|---|---|---|
| `shore:land_ahead` | 87 | 21.3 |
| `shore:land_port` | 109 | 26.7 |
| `shore:land_starboard` | 101 | 24.7 |
| `shore:faint_distant` | 25 | 6.1 |
| (open sea — no shore tag) | 165 | 40.3 |

The legacy `yolo:S1..S7` single-select schema was retired on
2026-05-27 in favour of these ship-relative binaries.  Mapping is
documented in `tools/migrate_shoreline_to_binary.py`.

#### Beam (advisory only)

| Tag | Positives |
|---|---|
| any `yolo:B*` (beam present) | 44 |

Demoted from primary discovery signal — see
`memory/project_beam_vs_minimap_marker_priority.md`.  The mini-map's
`undiscovered_marker` covers the discovery role.

#### Time + weather

| Tag | Positives | % |
|---|---|---|
| `time:day` | 300 | 73.3 |
| `time:night` | 91 | 22.2 |
| `weather:rain` | 77 | 18.8 |
| `weather:snow` | 12 | 2.9 |

#### Mini-map (10 multi-label classes)

| Tag | Positives | % |
|---|---|---|
| `minimap:land` | 245 | 59.9 |
| `minimap:port_anchor` | 119 | 29.1 |
| `minimap:settlement_name_text` | 69 | 16.9 |
| `minimap:village_building` | 18 | 4.4 |
| `minimap:undiscovered_marker` | 75 | 18.3 |
| `minimap:fleet_merchant` | 304 | 74.3 |
| `minimap:fleet_pirate_regular` | 229 | 56.0 |
| `minimap:fleet_pirate_special` | 81 | 19.8 |
| `minimap:player_neutral` | 33 | 8.1 |
| `minimap:player_guild` | 6 | 1.5 |

## Training results

Both models use **MobileNetV3-small + ImageNet pretrained**, 224×224
input, AdamW, BCEWithLogitsLoss with per-class `pos_weight`, 25 epochs,
80/20 random split.  Best checkpoint by validation macro-F1.

### Mini-map detector — `data/models/minimap_detector.pt`

**Best val macro-F1: 0.74**

| Tag | P | R | F1 | val #pos |
|---|---|---|---|---|
| `port_anchor` | 0.93 | 0.90 | **0.92** | 31 |
| `undiscovered_marker` | 1.00 | 0.86 | **0.92** | 21 |
| `fleet_merchant` | 0.86 | 0.95 | **0.90** | 57 |
| `settlement_name_text` | 0.92 | 0.75 | **0.83** | 16 |
| `land` | 0.86 | 0.76 | **0.81** | 50 |
| `village_building` | 1.00 | 0.62 | **0.77** | 8 |
| `fleet_pirate_regular` | 0.66 | 0.73 | **0.69** | 45 |
| `fleet_pirate_special` | 0.61 | 0.58 | **0.59** | 19 |
| `player_neutral` | 0.33 | 0.17 | 0.22 | 6 |
| `player_guild` | — | — | 0.00 | 0 (no val pos) |

**Input:** mini-map crop `(2055, 140, 2400, 360)` resized to 224×224.

### Shoreline + beam — `data/models/shoreline_classifier.pt`

**Best val macro-F1: 0.65**

| Tag | P | R | F1 | val #pos |
|---|---|---|---|---|
| `shore:land_port` | 0.93 | 0.67 | **0.78** | 21 |
| `shore:land_ahead` | 0.68 | 0.71 | **0.70** | 21 |
| `shore:land_starboard` | 0.92 | 0.48 | **0.63** | 25 |
| `beam_present` | 0.55 | 0.43 | 0.48 | 14 |

**Input:** full sea frame resized to 224×224.

`faint_distant` is currently not trained (25 positives, similar visual
to `land_ahead`; deferred until usage proves it's needed).  Could be
added as a 5th sigmoid output later without architecture change.

## Inference cost

Both models are MobileNetV3-small, ~2 M params, ~10 ms / forward pass
on Apple MPS.  Per-frame caches keyed on `id(frame)` so multiple
callers within one perceive tick share the forward pass.  Sea ticks
add ~25 ms total (mini-map crop + forward + shoreline forward); other
screens skip both.

## Consumer integration

`brain/perceive.py::_publish_observation` calls the readers when the
classified screen is `sea`, then writes the verdicts into
`BotObservation.minimap` / `.shoreline`.  Consumers read them via
`brain.observation.current()`:

```python
from brain import observation
obs = observation.current()
if obs.shoreline and obs.shoreline.land_ahead:
    # bow path blocked — must turn
if obs.minimap and "minimap:undiscovered_marker" in obs.minimap.tags:
    # ??? marker visible on radar — consider detour for discovery
```

Both fields are `None` outside sea frames, so consumers must null-check.

The verdicts are computed once per tick (lazy-loaded, cached by frame
id) and are immutable for the lifetime of the observation.

## What's still short for training

| Tag | Now | Target | Capture priority |
|---|---|---|---|
| `minimap:village_building` | 18 | ~50 | Medium — capture one village-pass session |
| `minimap:fleet_pirate_special` | 81 | ~120 | Low — usable today (F1 0.59) |
| `minimap:player_neutral` / `player_guild` | 33 / 6 | not blocking | Opportunistic, not nav-critical |
| `weather:snow` | 12 | n/a | Arctic skipped per project decision |
| `beam_present` | 44 | n/a | Demoted to advisory |
| `shore:faint_distant` | 25 | depends on usage | Defer until consumer needs it |

The model architecture for both already accepts more positives without
schema change — just retrain after new captures land.
