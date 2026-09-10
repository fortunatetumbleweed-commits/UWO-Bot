# Detecting game elements — boundaries, not boxes (DESIGN, 2026-09-10)

**Status: not decided, nothing built.** Options and a recommended order, written after two
live money-losing defects in one afternoon that were both the same failure: an element's
BOUNDARY was taken from OmniParser and never checked against anything.

Companion to `screen_knowledge_as_kb.md`, which proposes the layer ABOVE this one
(screen → regions → panels → fields). This note is about the layer that finds the boxes.

---

## 1. The problem, measured

Both of 2026-09-10's defects came from a box being the wrong size or the wrong thing. Neither
was an OCR failure — in both cases the text was read correctly.

**A tile boxed SHORT.** At Lisboa the Almond card runs y 195-425, but OmniParser boxed only
its top, y 195-369; the `Specialties` bar and the `109%` row fell outside. Tile-relative
positions are scaled by dividing by the detected height, so every token read as LOWER in the
tile than it is: the owned-count badge sits 119px down, which against the true 230 pitch
scales to 119 and lands in the (80,150) band, but against the clipped 174 scales to 157 and
misses. The badge `1,841` was read correctly by OmniParser and discarded for being in the
wrong place. The ledger then read `have: 0` against a want of 870 and bought to **1,841 —
2.1x target**.

**A banner boxed ALONE.** The same card carries a gold `Specialties` banner. A positive
button in this game is identified by its GOLD BACKGROUND rather than its wording, and the
guard against tile highlights is a SHAPE test — "a squarish, weakly-yellow thing is a
goods-tile highlight". Boxed on its own the banner is 508x46 and strongly gold, i.e. a wide
pill, so it passed. With the basket empty the real Sell button was greyed and undetectable,
leaving the banner as the only gold thing on screen, and `_find_sell_commit`'s bare
`commits[0]` returned it. The tap landed inside the tile, Put In Bulk staged the whole stack,
and the next tick **sold all 1,841 Almond** — during a trim whose keep list named Almond.

Measured, same detector, same code:

```
Lisboa  Almond (a Lisboa SPECIALTY)  -> detect_commit_buttons: 1  ['Specialties'@(571,355)]
San     Pig + Raisin (not specials)  -> detect_commit_buttons: 0
```

The second is why it had never fired: a trim runs BEFORE gathering, so the hold normally
holds goods with no relationship to the port being trimmed at.

**Neither is "OmniParser is bad at OCR".** It read `1,841` exactly, in the right place. It is
generic about BOUNDARIES and CATEGORIES — its vocabulary is `button` / `text` / `icon`, and a
banner is a `button` to it because it looks like one.

## 2. What exists today

| | what it is | how it decides |
|---|---|---|
| OmniParser | a general YOLO + OCR pass | learned, generic classes, boxes vary between captures |
| `DialogModel` (`region_detectors/dialog.py`) | **NOT a model** — classical numpy | brown title bar + cream body, structural |
| `grid_detector.detect_grid` | congruent-cell finder | takes OmniParser ELEMENTS as input, so it inherits their box errors |
| `commit_button` | gold-fraction + aspect | colour and shape, hand-tuned (`YELLOW_MIN_FRAC = 0.25`, margins of 0.03-0.04) |
| ~13 other `region_detectors/` | classical | colour, position, vocabulary |
| family classifier, minimap, shoreline, heading | trained CNNs (MobileNetV3 etc.) | learned, in production |

Assets already on hand: **28,101 frames across 338 sessions**, `training/` infrastructure, and
labelling tools (`tools/annotate_gallery.py`, `label_minimap.py`, `audit_training_labels.py`).

Worth stating plainly because it shapes the options: **the detector this project trusts most,
`DialogModel`, is hand-written structure, not ML.** Dialogs have a strong stable visual
grammar and classical rules read them reliably. Market cards have the same property.

## 3. Options

### A. A structural TileModel — DialogModel's sibling

Find the cards directly: cream rounded rectangles on dark wood, by contour/threshold, rather
than asking OmniParser where they are.

- **Fixes both defects.** The true extent fixes the scaling; and once a card's box is known,
  "is this gold thing INSIDE a card?" makes the banner structurally not a control.
- No training data. Deterministic, inspectable, fast, and it matches the one precedent here
  that has held up.
- Cost: days. Risk: hand-tuned thresholds — the very thing that keeps biting (`0.25` with a
  0.04 margin above and 0.03 below). Mitigated by the separation being much stronger here:
  cream card against dark wood is not a 4% margin.

### B. Anchor + arithmetic grid

The grid is regular (`_TILE_W = 450`, `_TILE_H = 230`). Measure one anchor per frame and
derive every cell from the pitch.

- Cheapest, and fixes clipped boxes BY CONSTRUCTION rather than by detection.
- Only answers *where the cells are*, not *what an element is* — it would not have stopped
  the banner.
- The anchor must be measured every frame, never baked: the Village Info panel rendered
  ~110px apart between two sessions when the camera-cutout offset changed, and every absolute
  band in the trade-list parser rejected every row.

### C. Train a UWO-specific detector

OmniParser already IS a YOLO; this replaces or augments it with game classes — `tile`,
`commit_button`, `banner`, `badge`, `panel`, `tab`, `dialog`.

- The highest ceiling, and literally "specialized for this game": one consistent source of
  boundaries AND categories for every element.
- The 28k frames are the asset. Labelling is the cost, and it can be **bootstrapped**:
  OmniParser and the classical detectors agree on most frames, so auto-label from agreement
  and hand-correct the disagreements — which are also exactly the interesting cases.
- Precedent exists: four CNNs already in production with training infra.
- Cost: weeks. **Real risk: a model that is confidently wrong is harder to debug than a rule
  you can read.** Today's defects were found by reading code and frames; a mis-detection from
  a net offers no such thread to pull.

### D. Fine-tune OmniParser on UWO frames

Same runtime, better boxes, least new code.

- Fixes box QUALITY (the clipped tile).
- Does NOT fix category confusion: you inherit `button` / `text` / `icon`, and `button` is
  precisely the label the banner carried.

### E. The panel/KB layer (`screen_knowledge_as_kb.md`)

Not a detector — the layer above one. Identity comes from *which group, in which region, of
which screen*, so nothing is decided by a box's size or a single word.

- This is what makes handling land in the right boundary, which is the actual goal.
- Needs a detector underneath it that reports stable regions, i.e. A or C first.

## 4. Recommended order

1. **A, plus the containment rule.** Measure the card extent structurally, and encode the
   invariant *nothing inside a goods tile is a control* POSITIONALLY. That closes the whole
   class and replaces the word-list exclusion committed on 2026-09-10 (`a81fe6a`), which
   catches `Specialties` and would miss the next banner.
2. **E**, since it is already specified and it is the part that puts handling in the right
   boundary.
3. **C**, bootstrapped from the 28k frames with A and B as the auto-labeller — by then the
   structural detectors are good enough to generate labels cheaply.

B is worth doing inside A wherever the layout is genuinely regular; it is a technique, not a
rival.

## 5. The invariants are needed either way

A better detector raises the floor. It does not remove the need for checks that turn a bad
reading into a REFUSAL rather than an action, and today's damage came from their absence, not
from the detector alone:

- **A goods tile contains no controls** (user, 2026-09-10). Would have stopped the sale.
- **A failed lookup is a refusal, not a guess** (`a-fallback-fires-when-guessing-is-worst`).
  The bare `commits[0]` is what turned a mis-detection into a tap.
- **A hold only shrinks when you sell.** The seed sequence 526 → 7 → 4 → 8 → 457 → 0 happened
  while continuously BUYING, and nothing objected. (Note the naive form is wrong — trimming
  and selling do shrink it — so this has to reconcile against the ledger's own
  bought/sold accumulation, which is a design change rather than a guard.)
- **A recipe cannot GAIN an ingredient** any more than lose one — the half that let a
  contaminated American Bison recipe persist through a clean re-read.

`constraints-beat-votes` is the general form: check a reading against what the world can
contain before weighing how confident the reader was.
