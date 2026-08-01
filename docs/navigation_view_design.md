# Navigation view — source-agnostic spatial perception

**Last updated:** 2026-05-27
**Status:** Protocol defined; mini-map backend in development; YOLO backend deferred.

## Why this exists

The bot needs to make steering decisions ("hug shoreline", "approach port",
"don't crash") from per-tick spatial information.  Two reasonable
perception sources exist:

1. **Mini-map** (the top-right radar): a compass-fixed top-down view.
   Land/water are visually distinguishable; sprites for ports/villages
   are colored.  Sees a full 360° around the ship.
2. **Main 3D scene** (the sea-view): rendered from a fixed third-person
   camera over the ship.  Land appears with texture, lighting, and
   weather.  Sees only a ~120° forward arc.

For navigation in UWO, the **mini-map is the simpler, more deterministic
source** — image processing on a fixed crop can extract everything
needed.  For other games (or future UWO scenarios where the mini-map
isn't present), the 3D scene is the universal fallback — but it
requires a trained YOLO detector and only covers the bow arc.

Rather than couple steering logic to either source, define a thin
**`NavigationView` Protocol** that both sources implement.  Steering
goals consume the Protocol; the concrete backend is chosen by config.

## Protocol shape

Defined in `vision/navigation_view.py`:

```python
@dataclass(frozen=True)
class SectorReading:
    bearing_deg:     float    # ship-relative: 0=ahead, 90=starboard, 180=astern, 270=port
    land_fraction:   float    # 0..1, % of sector pixels that are land
    nearest_dist:    float    # 0..1, normalized to view radius (None if no land)
    is_observed:     bool     # False if source can't see this sector

@dataclass(frozen=True)
class Target:
    kind:            str      # "port_known" | "port_unvisited" | "village"
    bearing_deg:     float    # ship-relative compass bearing
    distance:        float    # 0..1, normalized to view radius
    name:            Optional[str] = None

class NavigationView(Protocol):
    ship_heading_deg: Optional[float]    # absolute compass deg, or None if unknown
    sectors:          list[SectorReading]
    targets:          list[Target]
    def is_reachable(self, target: Target) -> bool: ...
```

The Protocol is **deliberately small** — exactly what steering decisions
need.  Anything else (the raw land mask, the OCR'd port name, the
sprite confidence scores) is implementation detail of one specific
backend and stays there.

### Sector convention

8 uniformly-distributed sectors at 45° each, indexed by bearing:

| Bearing | Index | Steering meaning |
|---|---|---|
| 0°   | 0 | dead ahead |
| 45°  | 1 | ahead-starboard |
| 90°  | 2 | starboard beam |
| 135° | 3 | astern-starboard |
| 180° | 4 | dead astern |
| 225° | 5 | astern-port |
| 270° | 6 | port beam |
| 315° | 7 | ahead-port |

Eight is a reasonable compromise: fine enough for "hug starboard"
(check sector 2), coarse enough to be cheap to populate.

### `is_observed` — the partial-view escape valve

The mini-map fills all 8 sectors; the YOLO 3D-scene backend only fills
sectors 7 / 0 / 1 (the bow arc) and marks the rest `is_observed=False`.
Steering logic must check this — "turn into a sector I haven't seen"
is a different decision than "turn into a clear sector."

## Backends

### `MinimapNavigationView` (under development)

**File:** `vision/minimap_navigation_view.py` (TBD)

Image processing on the radar crop.  Pipeline:

1. Crop the radar from the input frame.
2. Locate the ship icon by color (saturated green); centroid is the
   radar's geometric center.
3. Estimate ship heading by analyzing the green-icon's orientation
   (PCA on the icon's pixels, with bow tie-breaker).
4. Build a land mask via luminance / texture analysis of the radar
   area, masking out sprites first.
5. Bin land mask into 8 angular sectors → `SectorReading` per sector.
6. Detect port / village / unvisited sprites by HSV color filtering +
   shape templates → `Target` list.
7. `is_reachable`: flood-fill over water pixels from ship center;
   target reachable iff it touches the fill region.

**Inference cost:** ~3–5 ms per frame, no model file, no training data.

### `YoloNavigationView` (deferred)

**File:** `vision/yolo_navigation_view.py` (deferred)

Wraps a YOLOv8n trained on `land` bboxes in the 3D scene.

1. Run YOLO on the full sea frame.
2. For each bbox, classify which forward sector it falls into based on
   its `x_center` relative to frame width.
3. Compute `land_fraction` per sector from total bbox area in that
   sector.
4. Compute `nearest_dist` from bbox `y_max` (lower = closer).
5. Sectors 2–6 (sides + astern) marked `is_observed=False` since the
   camera can't see them.
6. `Target` detections come from a separate "port_marker" / "village"
   class if added to the YOLO model; otherwise empty.
7. `is_reachable`: not directly answerable from the 3D scene alone —
   returns `True` for visible targets (caller must cross-check with
   world-map / mini-map if available).

**Inference cost:** ~10–20 ms per frame; needs trained `data/models/land_yolo.pt`.

## Selecting a backend

A factory in `brain/perceive.py` picks the active backend per tick:

```python
NAV_SOURCE = config.get("nav_source", "minimap")    # "minimap" | "yolo"
if result.state == "sea":
    if NAV_SOURCE == "yolo":
        nav = YoloNavigationView.from_frame(frame)
    else:
        nav = MinimapNavigationView.from_frame(frame)
else:
    nav = None
```

The config flag lets you A/B test by simply flipping `nav_source` in
`config/settings.py` — no code change in any consumer.

## Wire-in to `BotObservation`

Replaces the existing `minimap` + `shoreline` fields with a single
`nav` field:

```python
@dataclass(frozen=True)
class BotObservation:
    ...
    nav: Optional[NavigationView] = None
```

Old fields stay for one cycle as deprecation aliases, then are
removed once consumers migrate.

## Consumer pattern

```python
from brain import observation

obs = observation.current()
if obs.nav is None:
    return                               # not at sea, no nav signal

heading = obs.nav.ship_heading_deg
if heading is None:
    return                               # backend doesn't know heading

# Hug starboard shoreline
target_sector = next(s for s in obs.nav.sectors if s.bearing_deg == 90)
if not target_sector.is_observed:
    bot.turn_left(taps=1)                # can't see starboard, swing toward
elif target_sector.land_fraction < 0.15:
    bot.turn_right(taps=1)               # losing land, swing toward
elif target_sector.land_fraction > 0.45 or target_sector.nearest_dist < 0.15:
    bot.turn_left(taps=1)                # too close, swing away
else:
    bot.center_rudder()                  # hold course
```

Goal-level controllers like `HugShoreGoal` consume only this Protocol;
they have no idea whether the underlying source is mini-map image
processing or a YOLO model.

## Why not just expose both fields separately?

Tempting to keep `obs.minimap` (current MobileNet) AND add
`obs.land_detector` (YOLO) on top, both populated.  But:

- Consumers then have to choose which one to trust, recreating the
  abstraction at every consumer site.
- "Two readings disagree" becomes a per-consumer policy decision.
- Adding a third backend (e.g., depth-camera, world-map planner)
  multiplies the explosion.

One field, one Protocol, one selected backend per tick is cleaner.

## Migration order

1. Define Protocol + dataclasses (`vision/navigation_view.py`).  ← starting point
2. Build `tools/explore_minimap_geometry.py` to validate image
   processing on real frames.  Iterate on thresholds.
3. Promote validated code to `MinimapNavigationView`.
4. Add `nav` field to `BotObservation`; route through perceive.
5. Build `HugShoreGoal` consuming the Protocol.
6. Drop the deprecated `obs.minimap` / `obs.shoreline` aliases.
7. *Later*: train YOLO; add `YoloNavigationView`; flip config flag to
   A/B test.
