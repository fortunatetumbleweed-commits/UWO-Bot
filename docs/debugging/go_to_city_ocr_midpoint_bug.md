# Bug: "Go to City" button tap landed in the ocean

**Date:** 2026-04-20  
**File:** `actions/sail_actions.py` — `_extract_go_to_city_pos`

## What happened

After selecting a city on the world map (Ceylon), the map panned to center it and the
city info panel appeared on the right side of the screen.  The bot needed to tap the
"Go to City" button to start sailing.

EasyOCR split the button label into two tokens:

```
'Go to'  conf=1.00  @ (1228, 1014)   ← left half of the button
'City'   conf=0.96  @ (1301, 1015)   ← right half of the button (correct pair)
'city'   conf=0.89  @ (2061,  141)   ← "City" in "City Info" panel title, top-right
```

The detection algorithm searched for a `'go to'` token and then paired it with the
**next token in the OCR list** that contained "city".  The OCR list order does not
reflect spatial (reading) order — EasyOCR returned the "City Info" panel title token
`(2061, 141)` *before* the button's own `'City'` token `(1301, 1015)` in the list.

Result: the algorithm paired `'Go to'@(1228,1014)` with `'city'@(2061,141)` and
computed the **midpoint** as the tap target:

```
x = (1228 + 2061) / 2 = 1644
y = (1014 +  141) / 2 =  577
```

Tap at `(1644, 577)` — well inside the ocean area of the world map.  The tap
selected a sea-coordinate waypoint, switching the button label to "Move".  The
bot then detected the "Move" state and tried to recover.

## Context: City Info panel

When a city is selected on the world map, a "City Info" panel appears on the **right
side** of the screen.  Its title "City Info" is at approximately `(2061, 141)` — upper
right corner.  The panel shows market goods, sell prices for cargo already loaded, and
other city details.  This is useful data for route planning and will be parsed in a
future milestone.

For now, the stray "City" token from the panel title confused the button-detection
algorithm.

## Why the full-frame OCR was also unreliable

A separate investigation found that `_ocr_frame(full_frame)` sometimes fails to
detect the "Go to"/"City" tokens at all, while OCR on a frame cropped by 40 px on
top and bottom (matching the approach in `_find_port_on_world_map`) reliably finds
them.  The root cause is unclear — EasyOCR's internal paragraph-grouping heuristics
behave differently on the full 2400×1080 image vs the 2400×1000 crop.

The detection was therefore switched to the cropped approach, with both scans logged
so the discrepancy can be monitored.

## Fix

Replaced "next in list" pairing with **nearest by screen distance on the same row**:

```python
# For each 'go to' token at (cx, cy):
same_row = [
    (nxt_cx, nxt_cy, nxt_text, nxt_conf)
    for nxt_text, nxt_conf, nxt_cx, nxt_cy in toks
    if "city" in nxt_text.lower() and abs(nxt_cy - cy) <= 100
]
nxt_cx, nxt_cy, ... = min(same_row, key=lambda t: abs(t[0] - cx))
```

`|Δy| ≤ 100` excludes the "City Info" title at y=141 (873 px away from the button
at y=1014).  The nearest `'City'` on the same row is the button's own label at
y=1015, giving the correct tap position `(1264, 1014)`.

This matches the hardcoded fallback `_GO_TO_CITY = _fs(527, 421) = (1264, 1010)`
almost exactly — confirming the coordinate was always right; only the OCR pairing
was wrong.
