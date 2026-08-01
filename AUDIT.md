# Trading Code — Absolute-Position Audit

**Branch:** `trading_revisit`  ·  **Date:** 2026-08-01
**Goal:** find where the trading flow depends on hardcoded/absolute screen
positions, so it can be hardened against game UI-version updates (the same
class of fragility as the heading/rotation work).

## Scope
Core trading files audited:

| File | LOC | Role |
|---|---|---|
| `actions/market_actions.py` | 1978 | buy / sell / negotiate / keypad taps |
| `vision/market_reader.py` | 507 | reads the market price grid (OCR/Claude) |
| `config/settings.py` | — | hardcoded coordinate constants |
| `brain/states/trading.py` | 20 | FSM state shell |
| `memory/market_kb.py`, `memory/trade_priors.py` | 186 / 281 | data only (no positions) |

## Headline
`market_actions.py` is **already tiered** — most taps try structural
detection first and fall back to positions. Absolute-position dependence
still runs through three layers, and **two spots have no structural
fallback at all** (the most likely culprits if a game update broke trading):

1. `vision/market_reader.py` hardcoded tile grid (all price reads).
2. The 3 direct `MARKET_COORDS` taps in `market_actions.py`.

## Position-resolution tally (`market_actions.py`)

| Source | Call sites | Fragility |
|---|---|---|
| `_find_button` / `_find_tile_pos` (OmniParser/OCR, live) | 16 | 🟢 robust |
| `_resolve_dialog_ok` / `_dialog_ok_pos` (typed DialogModel first) | ~6 | 🟢 robust |
| `_flow_btn_coords` / `_flow_coords` (recorded `flow_coords.json`, scaled) | 27 | 🟡 record-time layout |
| `MARKET_COORDS` (raw hardcoded pixels) | 4 | 🔴 absolute |
| raw literals (`(1350,100)` crop, `(1200,700)` swipe) | 2 | 🔴 absolute |

`~35 call sites` total, clustering into **~8 hardcoded constants** +
**~27 flow-coord sites**.

## Tier 1 — pure hardcoded absolutes (break outright on layout shift)

### `config/settings.py`
- `MARKET_COORDS` = `purchase (70,145)`, `sell (65,225)`, `home (2350,40)`
- `MARKET_CONTENT_REGION = (50,130,2380,980)`
- `MARKET_SCROLL_START/END = (1500,800)/(1500,300)`

Tapped **directly, no structural fallback**, at:
- `market_actions.py:906`  `tap(*MARKET_COORDS["sell"])`
- `market_actions.py:1617` `tap(*MARKET_COORDS["purchase"])`
- `market_actions.py:1847` `tap(*MARKET_COORDS["purchase"])`

### `vision/market_reader.py` — the tile grid (NO fallback)
```
_GRID_LEFT = 460   _GRID_TOP = 215   _TILE_W = 450   _TILE_H = 230
_IMG_ZONE_W = 160  _BOTTOM_ZONE = 80
```
Drives **every** price read + tile-center tap:
- crop @ `268-270`
- name/category OCR offsets @ `286-287`
- tile center taps @ `296-297`

If a game update shifted or resized the market grid, every price read and
every tile tap is off by a fixed amount and **nothing recovers**. This is
the #1 suspect for "trading broke after the update."

### `market_actions.py` raw literals
- `1091`  `x_off, y_off = 1350, 100` — numeric-keypad crop region
- `1749`  `adb_swipe(1200, 700, 1200, 300, …)` — fallback scroll swipe

## Tier 2 — recorded flow-coords (dominant path, ~27 sites)
`_flow_btn_coords` (18) + `_flow_coords` (9) read button positions from a
recorded session (`flow_coords.json`), thumbnail→screen scaled. "Ground
truth" but frozen to the layout at record time — a moved button silently
misses unless the structural path catches it first. Spread across
negotiation, buy, sell, keypad-enter, load, max, and dialog-OK fallbacks.

## Tier 3 — structural / dynamic (already robust)
- `_find_button(frame, label…)` ×11 — OmniParser/OCR button finding
- `_find_tile_pos(frame, name)` ×5 — finds the market tile by item name
- `_resolve_dialog_ok` / `_dialog_ok_pos` — typed `DialogModel.actions` first
- "Put in Bulk" checkbox — anchor-relative (`chk_x = put_x - 40`)

These survive layout shifts and are the model to extend the fragile spots
toward.

## Recommended hardening order (highest leverage first)
1. **`market_reader.py` grid** — replace the hardcoded `_GRID_*` geometry
   with a detected grid (find tile bounding boxes via OmniParser / contour
   segmentation, derive pitch from detected tiles). Single biggest risk,
   zero current fallback.
2. **`MARKET_COORDS` direct taps** (906 / 1617 / 1847) — route through
   `_find_button` with the existing constants only as last-resort fallback,
   matching the pattern already used elsewhere in the file.
3. **Keypad crop `(1350,100)` + digit grid** — anchor to the detected
   keypad region rather than a fixed offset.
4. **flow-coords (Tier 2)** — lower priority; already backed by structural
   detection in most call paths. Audit which `_flow_*` sites lack a
   `_find_button` attempt and add one.

## Live verification (2026-08-01, current game build — Seville market)
Captured the live market on the phone (`Seville → Market → Purchase`) and
compared the hardcoded positions to detected element positions. **The UI
has moved — both absolute-position layers are affected.**

### `MARKET_COORDS` tabs — BROKEN (taps miss the button)
| Button | Hardcoded | Live center | Live bbox (x) | Result |
|---|---|---|---|---|
| `purchase` | (70, 145) | (225, 161) | 149–301 | ❌ x=70 is 79px left of the button |
| `sell` | (65, 225) | (182, 275) | 149–215 | ❌ left & above the button |

The Purchase/Sell tabs shifted right ~+150px. The 3 direct
`tap(*MARKET_COORDS[...])` calls (`market_actions.py:906/1617/1847`) now
land left of the tabs and hit nothing.

### `market_reader` tile grid — DRIFTED (reads corrupted, not off-screen)
Measured item-name centers on the live Purchase grid vs the hardcoded grid:

| Axis | Hardcoded | Live | Drift |
|---|---|---|---|
| column centers (x) | 685 / 1135 / 1585 (pitch 450) | 688 / 1127 / 1566 (pitch **439**) | −11px/col |
| row origin (y) | row0 name ≈221 *(code comment)* | row0 name **316** | **grid ↓ ~95px** |
| row pitch (y) | 230 | **240** | +10px/row |

Tile-center *taps* still land inside the (450-wide) tiles, but the ~95px
vertical shift + pitch change misalign the per-tile name/price OCR
sub-crops, so price reads are unreliable. `market_reader` is a real
casualty of the update.

### Conclusion — ROOT CAUSE IS PHONE ORIENTATION (notch), not a game update
Re-checked after physically rotating the phone. The entire market UI
shifts **118px horizontally** (y unchanged) between the two landscape
orientations. Cause, from `dumpsys display`:

```
cutout DisplayCutout{insets=Rect(0, 118 - 0, 0)}   # 118px camera cutout
mDisplayRotation = ROTATION_270 / ROTATION_90       # two landscape modes
```

The 118px camera notch sits on the **left in one landscape mode, right in
the other**; the game reserves that safe-area, so its content translates
118px sideways when the phone flips. This is the same class as the nav
"rotation issue" — **every hardcoded X coordinate is orientation-dependent.**

Two corrections to the first-pass live verification above:
- The grid's **vertical is fine** — live rows `316/558/797` match the
  hardcoded tile centers `330/560/790` (±15px). The earlier "↓95px drift"
  compared against a stale code *comment* (`y=221`), not the real
  `_GRID_TOP`/pitch constants. Grid pitch is essentially unchanged.
- The horizontal mismatch is the **notch flip**, not a game build change.

**The trap:** the constants are split across orientations —
`MARKET_COORDS` tabs match in ROTATION_270 (live sell x=65 = hardcoded 65),
but `market_reader`'s grid matches in ROTATION_90 (live col0 x=688 ≈
hardcoded 685). So no single orientation makes them all correct; whichever
way the phone sits, ~half the market coords are 118px off. That's why
trading breaks either way.

### Revised fix direction
1. **Lock/normalise phone orientation** before any run, and make the
   capture pipeline **notch-aware**: read the cutout inset from
   `dumpsys display` (or detect the safe-area) and offset all absolute X
   coords by the notch width for the current rotation. One correction fixes
   every absolute-X site at once. (Highest leverage — also helps nav.)
2. **Prefer detected positions** (still the durable fix): route tab taps
   through `_find_button` and derive the tile grid from detected tile
   bounding boxes, so orientation/notch is irrelevant.
3. Re-calibrating the raw constants is NOT enough on its own — it just
   picks one orientation and re-breaks on a flip.
