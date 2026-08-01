# Bug: Tapping "Supply Departure" opened the main menu

**Date:** 2026-04  
**File:** `actions/sail_actions.py` — `_depart_from_harbour`, `_tap_depart_button`

## What happened

After arriving at the harbour, the bot detected "Supply Departure" in the right panel
and tapped it to depart.  Instead of sailing, the main menu appeared and the bot
got stuck, eventually aborting.

## Root cause

The original departure logic contained a post-tap "supply warning dialog" handler:

```python
# If a supply warning dialog appeared, dismiss it
if _screen_contains(frame, "supply", "warning", "food", "water"):
    press_back()
```

The harbour UI always shows food and water supply levels as **part of its normal
permanent display** — not as a dialog.  The text "food", "water", and supply numbers
are always visible in the harbour right panel whether supply is full, partial, or empty.

After tapping "Supply Departure" the bot immediately checked for these keywords,
found them (because the harbour UI was still visible during the brief transition),
and called `press_back()`.  On this screen, `press_back()` opened the main menu
rather than dismissing any dialog.

## How "Supply Departure" actually works

The game has two departure buttons depending on supply state:

| State | Button shown | Behaviour on tap |
|---|---|---|
| Full supply | "Depart Now" only | Departs immediately, no dialog |
| Partial supply | Both buttons | "Supply Departure" departs immediately with no dialog; "Depart Now" shows a warning dialog |
| No supply | "Supply Departure" only | Departs immediately, no dialog |

The harbour food/water text is **always present** in the normal UI — it is not a dialog
and should never trigger a `press_back()`.

## Fix

Removed all post-tap supply warning dialog handling.  The only condition that needs
handling is the actual departure:

```python
tap(supply_departure_pos)
# No dialog handling needed — Supply Departure always departs immediately.
# Food/water text visible in harbour UI is permanent, not a dialog.
```

Prefer "Supply Departure" over "Depart Now" in all cases:
- With partial/no supply: "Supply Departure" is the only safe option (no dialog)
- With full supply: "Supply Departure" is not shown; "Depart Now" works (no dialog either)

## Lesson

Distinguishing permanent UI text from dialog text requires knowing whether a screen
transition has occurred.  The same keywords ("supply", "food") can appear in both
contexts.  Check for a modal dialog frame (e.g. dimmed background, explicit dialog
chrome) rather than relying on keyword presence alone.
