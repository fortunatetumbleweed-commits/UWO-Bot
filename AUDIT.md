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

## Next step
Verify the `market_reader` grid against a **current** market screenshot
(post-update) — overlay `_GRID_*` tile centers on a live frame and check
they still land on tiles. Confirms whether the grid is the break before
any code changes.
