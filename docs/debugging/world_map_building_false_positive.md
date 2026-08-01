# Bug: Chrome detector classifies world map as "building"

**Date:** 2026-04  
**File:** `actions/sail_actions.py` — `_navigate_world_map_to_port`, `where_am_i`

## What happened

The chrome detector uses template matching on fixed screen regions to identify stable
UI elements (`has_home`, `has_back_arrow`, `has_hamburger`, `has_right_panel`).

On the world map, the chrome detector would sometimes fire `has_home = True`,
causing `where_am_i()` to return `location = "building"` instead of `"world_map"`.

This caused `_navigate_world_map_to_port` to abort immediately:

```
Not on world map (got 'building') — aborting to avoid sea camera rotation
```

## Root cause

The world map has UI elements in the top-right corner (close button, filter icons)
whose visual appearance is similar enough to the Home button template that the
template matcher exceeded the confidence threshold.

The port overworld's building name panel is another source of interference: when the
character is standing near a building entrance, a floating name panel appears above
the building.  After the port map closes and the overworld is visible again, this
panel can still be on screen during the brief window before the next `where_am_i()`
poll.  If the panel's top-right corner overlaps the Home button search region, it
produces a false `has_home` signal.

## Fix

Added `_world_map_port_labels_visible(frame)` — a guard that checks whether port
city labels are visible in the **left** portion of the screen (x < 1200).  Port
labels never appear on the left side of a building interior; they only appear on the
world map.

```python
if loc["location"] == "building" and _world_map_port_labels_visible(frame):
    logger.info("  'building' false positive confirmed as world map — proceeding")
```

This check is applied before every swipe in the world map pan loop, since swiping
on the sea view rotates the 3D camera — incorrectly swiping due to a false positive
would be very disruptive.

## Lesson

Chrome template matching is fast but brittle when UI elements share visual
similarities across screens.  Always pair chrome detection with a content-based
sanity check (OCR of distinctive text, visible port labels, etc.) before taking
irreversible actions like swiping.
