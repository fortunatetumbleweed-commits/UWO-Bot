# Bot Decision Architecture — `where_am_i()`

The bot's decision loop is **location-driven, not sequence-driven**.

Fixed step sequences are fragile: one unexpected popup, a slow load, or
a misclick leaves the bot stuck with no recovery path.  Instead, on
every tick the bot calls `where_am_i()` to get ground truth about its
current location, then derives the next action from
**(current location, current goal)** — not from a step index.

```python
# brain/agent.py — conceptual loop
while True:
    loc  = where_am_i()      # ground truth: where are we right now?
    goal = current_goal()    # what are we trying to accomplish?

    if loc contradicts expected state:
        recover(loc, goal)   # re-orient, don't crash
    else:
        next_action(loc, goal)
```

## Location vocabulary
Defined in `actions/sail_actions.py`:

| location | meaning |
|---|---|
| `"building"` | Inside a building (Home button visible) |
| `"port_overworld"` | On a port's town overworld |
| `"sea"` | Open sea with sailing HUD visible |
| `"sea_cinematic"` | Idle / cinematic sea view (HUD hidden; tap to restore) |
| `"world_map"` | World map is open |
| `"loading"` | Transition loading screen |
| `"unknown"` | Cannot determine |

Returns `{"location": str, "port": str|None, "detail": str}`.

**Design principle:** transient states (loading screens, arrival
overlays, idle cinematic) are not special-cased — the bot just waits
and polls again.  They all eventually resolve to a stable location that
the goal logic knows how to act on.

The vocabulary grows over time (e.g. adding `"market"`, `"harbour"`,
`"port_map"`) as the chrome detector and OCR improve.  Goal logic that
consumes `where_am_i()` doesn't need to change as location precision
improves.
