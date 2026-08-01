# Bug: Garbled sea zone label accepted as port name

**Date:** 2026-04  
**File:** `actions/sail_actions.py` — `where_am_i`

## What happened

While sailing, `where_am_i()` read the top-left region of the screen for the port
name.  On the sea view the same region displays the current sea zone name (e.g.
"Lawless Waters", "Dangerous Waters").  EasyOCR garbled "Lawless Waters" into
"Iless Waiters" or similar, and the bot accepted it as a valid port name, declaring
`location = "port_overworld"` while still at sea.

This caused `_open_world_map_from_sea` to immediately abort on attempt 0 with:

```
'port_overworld' right after departure (likely false positive) — waiting 3s
```

On retry the garbled zone name was again misread and again triggered the abort,
preventing the world map from ever opening.

## Token examples seen in logs

| Raw OCR | Intended text |
|---|---|
| `'Iless Waiters'` | "Lawless Waters" |
| `'Dangerous Waters Pass'` | accepted correctly |
| `'wOrta Map'` | "World Map" (on world map screen) |

## Fix

Added a `_ZONE_WORDS` keyword filter to discard OCR results that look like zone
labels rather than port names:

```python
_ZONE_WORDS = ("waters", "ocean", "lawless", "dangerous", "safe water",
               "waiter", "waiters")   # include common garbled forms

if any(w in port.lower() for w in _ZONE_WORDS):
    logger.debug(f"  Discarding zone label mis-read as port name: {port!r}")
    # fall through to other location checks
```

The `has_right_panel` chrome signal was also used as a secondary guard: the right
panel (mini-map + building list) is present on the port overworld but absent at sea.
However, this was made **advisory rather than blocking** after it caused a regression:
at Aceh, the right-panel template occasionally failed to match (day/night cycle,
weather variation in the template), causing the bot to discard a valid "Aceh" port
name and get stuck in `sea_cinematic` for several minutes waiting for an arrival that
had already happened.

## Final state

- Zone keyword filter: hard block (garbled or real zone names always discarded)
- `has_right_panel` absent: advisory only — a clean port name is still accepted
  with a log note "right panel unconfirmed"

## Lesson

OCR on the sea HUD zone label region is unreliable because:
1. Zone names appear in the same screen position as port names
2. OCR garbles multi-word zone names in unpredictable ways
3. Template matching for the right panel can fail under lighting/weather variation

Defence in depth: use multiple independent signals (keyword filter + chrome +
context from recent state machine transitions) rather than any single check.
