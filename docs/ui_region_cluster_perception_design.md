# UI region-cluster perception — design

Status: **design** (2026-08-18). A robust, rotation-invariant perception substrate that
replaces flaky OmniParser-element scanning and notch-fragile absolute crops with a small,
fixed set of semantic **UI regions**. It is the localization + change-detection layer
under the "never act blind" rule (CLAUDE.md) and the action-verification ladder
(`docs/action_verification_and_recovery_design.md`).

## 1. Why

- **OmniParser gives elements, not clusters.** It returns scattered
  `DetectedElement{type, bbox, label}` and *excludes* panel-sized boxes
  (`vision/omniparser.py:417`). There is no "top-left cluster" bbox — you must cluster
  elements yourself. And its element detection is **non-deterministic / flaky** (it
  missed the "Malé" title element entirely).
- **Absolute crops break on rotation.** `read_port_name`'s fallback
  `OCR_PORT_NAME_REGION=(310,10,650,75)` and the `CHROME_*_REGION` boxes are absolute
  pixels. The **118px camera-notch / landscape shift**
  ([[project_notch_orientation_shifts_ui_118px]]) moves content out of them → the port
  name read as `None`, which caused the Malé "sail to where I already am" round-trip.
- We keep re-running the *whole* heavy stack (OmniParser + Moondream + Qwen) just to ask
  cheap structural questions ("is there a dialog?", "did a panel toggle?", "am I in a
  building?"). Non-deterministic and slow.

## 2. The region model

The game UI always lays out into the **same small set of semantic regions**; every
control lives in one of them:

```
┌──────────────── TOP_HUD (currency / clock) ─────────────────┐
│ TITLE(top-left)                           CHROME(top-right ☰/⌂)│
│ LEFT_MENU  ┌─────────────────────────────┐        RIGHT_PANEL │
│  (tabs)    │      CENTRE / popup zone     │   (list/cart/map)  │
│            └─────────────────────────────┘                    │
│                    BOTTOM (button row)                        │
└───────────────────────────────────────────────────────────────┘
```

| region | normalized bounds (approx) | holds |
|---|---|---|
| `TOP_HUD` | y<0.09 | ducats / gems / clock (ignored for change; read for currency) |
| `TITLE` | x<0.40, y<0.10 | port / building / screen name |
| `CHROME` | top-right + top-left back | ☰ / ⌂ / ← nav affordances |
| `LEFT_MENU` | x<0.15, 0.1<y<0.9 | building/market left tabs |
| `RIGHT_PANEL` | x>0.78 | building list / cart / destination list / mini-map |
| `BOTTOM` | y>0.88 | Depart / Purchase / commit / Go-to-Port |
| `CENTRE` | 0.22<x<0.72, 0.12<y<0.86 | main content **and blocking popups** |

Regions are **normalized fractions**, so the 118px shift (0.049 of width) is absorbed —
we cluster the *actual* elements inside a wide-ish region rather than reading a tight
absolute box.

## 3. The API

```python
@dataclass
class RegionState:
    populated: bool          # does this region hold a UI cluster right now?
    bbox: Optional[tuple]    # tight bbox of the cluster (from its member elements)
    n: int                   # element count
    signature: str           # cheap content hash (for change-detection)
    text: list[str]          # OCR-able tokens in the cluster (lazy)

def perceive_regions(frame, elements=None) -> dict[str, RegionState]
```

One call answers both **"where am I / read this"** and **"what changed"**.

## 4. How each region is populated (cheap-first, no new model)

Reuse what exists; add a thin clustering layer:
- **Elements → regions:** bucket OmniParser elements by their centre into the normalized
  regions; a region is `populated` if its element count/area exceeds a threshold; `bbox`
  = union of its members (the real cluster extent). Reuses `grid_detector`'s clustering
  idea and `_top_left_title_from_elements`' region filter.
- **`CENTRE` popup:** OmniParser under-detects modals, so detect the popup **structurally**
  — a bright, bounded rounded rectangle over the middle (reuse the dialog detector /
  `region_detectors/dialog`). This is the deterministic signal OmniParser can't give.
- **`TITLE`:** the largest-height text cluster in the `TITLE` region → its bbox is the
  read target. Robust to rotation (normalized) and to OmniParser missing individual
  glyphs (cluster, not single element).
- **`CHROME`:** find the affordance cluster in the top corners (replaces the absolute
  `CHROME_*_REGION`), and combine with the Home-toggle rule
  ([[project_home_button_is_chromed_only_escape]]).
- **`signature`:** `f(n, sorted top tokens, coarse pixel hash)` — cheap, ML-free, stable
  across OmniParser noise (so it's a better change signal than diffing element labels).

## 5. What it folds in

- **`read_port_name`** → read the `TITLE` cluster's bbox. If `TITLE` is populated but OCR
  fails → retry/escalate (the invariant: a `port_overworld` ALWAYS has a title). No more
  notch-fragile absolute crop.
- **`classify_action_outcome`** (already built, `vision/frame_diff.py`) → its centre /
  right / bottom deltas become the **semantic** version: *which region's cluster
  appeared/disappeared* (dialog = `CENTRE` gained a popup; panel toggled = `RIGHT_PANEL`
  populated flipped; enter/exit building = `LEFT_MENU`/`TITLE` changed).
- **State classification** → the populated-region *pattern* is a fast structural prior:
  `TITLE+CHROME+RIGHT(minimap)` = port_overworld; `TITLE+LEFT_MENU+CENTRE` = building;
  `CENTRE popup` over anything = dialog; HUD-only, no TITLE/panels = sea. Complements the
  family CNN and lets most ticks skip Moondream/Qwen.
- **Absolute-crop retirement** → `OCR_PORT_NAME_REGION`, `CHROME_*_REGION`,
  `_BUILDING_MENU_REGION_NORM` etc. become region-cluster lookups → the notch/rotation
  fragility across the codebase goes away.

## 6. Invariants (assert, don't silently drop)
- `port_overworld` ⇒ `TITLE` populated (a name). `building` ⇒ `TITLE` + `LEFT_MENU`.
  A dialog ⇒ `CENTRE` popup. If perception yields a state whose required regions are
  empty → **anomaly** → re-perceive, then escalate. (Per the never-act-blind rule.)

## 7. Build path
1. **`perceive_regions` deterministic** — element bucketing + structural popup detection
   + title cluster + signatures. Reuse OmniParser/grid/dialog/frame_diff. Unit-test on
   captured frames per screen type (overworld / market / building / dialog / sea / world
   map) across BOTH notch orientations.
2. **Wire it in** — `read_port_name` (title cluster + invariant retry), state
   classification prior, `classify_action_outcome` semantics; retire absolute crops
   incrementally.
3. **Optional model later** — a lightweight region-segmentation model that emits the
   clusters directly, if OmniParser element detection stays too flaky. Bootstrap its
   training labels from Phase-1 output + human correction (the flywheel).

## 8. Reuse
`vision/grid_detector` (element clustering) · `vision/ocr._top_left_title_from_elements`
(title region) · `vision/region_detectors/dialog` (centre popup) · `vision/chrome_detector`
(affordances → replace absolute regions) · `vision/frame_diff.classify_action_outcome`
(change) · the family CNN (coarse family).

## 9. Open questions
- Region-populated **thresholds** per screen (calibrate on real captures, both orientations).
- Does the popup detector need Moondream confirm for tricky promos, or is the bright-rect
  structural signal enough (with the announcement text signature)?
- Cost budget: `perceive_regions` should be < the current OmniParser tick so it can run
  every tick as the fast structural gate ahead of heavy perception.

## Related
[[feedback_never_act_blind_know_location_and_state]] ·
[[feedback_action_outcome_frame_diff_triage]] · [[project_notch_orientation_shifts_ui_118px]] ·
`docs/action_verification_and_recovery_design.md` · `docs/vision_pipeline.md`.
