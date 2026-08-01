# Bug: Bot declared "on overworld" while still inside a building

**Date:** 2026-04  
**File:** `actions/sail_actions.py` — `_is_on_overworld`

## What happened

After the sell step completed at the market, `_exit_to_overworld()` was called.
The bot polled `_is_on_overworld()` and declared success, then tried to navigate
as if it were on the port overworld — but it was actually still on the market's
main screen (the interior lobby, not a sub-menu).

## Root cause

The original `_is_on_overworld` only checked one condition:

```python
return not chrome.has_home
```

The Home button (⌂) appears inside sub-menus and purchase screens, so its absence
was used as the overworld signal.  However, the market main screen (the first screen
after entering the market building) shows:

- `has_home = False` — no Home button on this screen
- `has_back_arrow = True` — back arrow is present to return to the overworld

The back arrow is only visible inside a building's main screen, never on the
overworld itself.  So `has_home=False, has_back=True` unambiguously means
"inside building main screen".

The bot's single-condition check passed `has_home=False` as overworld, causing it
to proceed as if it had exited — while it was still inside the market.

## Fix

Extended `_is_on_overworld` to require **both** conditions:

```python
if chrome.has_back_arrow:
    title = read_screen_title(frame)
    logger.info(f"  Not overworld: back arrow visible (title={title!r})")
    return False
logger.info("  Overworld: no home, no back arrow")
return True
```

The invariants are:
- Port overworld: `home=False, back=False, right_panel=True`  
- Building main screen: `home=False, back=True`  
- Building sub-menu: `home=True, back=True`

## Lesson

A single chrome element absence is not a unique identifier for a screen.  Use the
combination of present and absent elements to distinguish screens that share partial
chrome signatures.
