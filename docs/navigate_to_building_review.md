# `navigate_to_building` — loop-structure review

Status: **review / proposal** (no code changed yet). Trigger: the Malé→Goa
run failed "Could not enter Harbour" a second time — not from the give-up path
we'd just hardened, but from the `building` handler mis-reading a **successful
Back** (market → port_overworld) as failure and Home-escaping until timeout.

This note reviews the whole loop, names the one structural offender, and
proposes a single-dispatch target shape. It follows the project's
`perceive → act → perceive` rule (no blind subloops) — see
`memory/feedback_no_subloops_perceive_act_perceive.md`.

---

## 1. What the function is

`navigate_to_building(building_name, timeout=60)` in `actions/sail_actions.py`
is a **perceive → dispatch loop**:

```
while time.time() < deadline:
    frame = capture_screen()          # ONE capture per iteration
    clear_blockers(frame)             # idle-lock / promo popups
    if _harbor_panel_open(frame): return True   # harbor = right-panel special case
    loc = perceive(frame).to_location_dict()    # ONE perceive per iteration
    dispatch on loc["location"]:
        sub_menu        → ...
        building        → ...
        loading         → pass
        port_overworld  → ...
        sea/world_map/main_menu → press_back()
        (sea_cinematic/unknown) → wait
return False   # "Could not enter after Ns"
```

The **top of the loop is correct**: capture once, perceive once, then branch.
The problem is what some branches do *after* they decide.

## 2. The three control models present in the loop today

| Model | Where | Correct? |
|---|---|---|
| **A. Single-dispatch** — take ONE action, `continue`, let the loop re-perceive | `sub_menu` (`press_back(); continue`), `sea/world_map/main_menu` (`press_back()`), the nameplate tap in `port_overworld` | ✅ this is the target pattern |
| **B. Inline re-perceive + branch** — take an action, then `capture()`+`perceive()` and branch on the result *inside the same block* | `building` handler (twice) | ❌ the anti-pattern; source of the bug |
| **C. Cross-iteration stuck model** — snapshot a signature + timers after a tap; on later iterations compare, extend patience, re-tap, or give up | `port_overworld` (`last_tap_signature`, `first_tap_time`, `MAX_WAIT_AFTER_TAP`, `TAP_RETRY_COOLDOWN`) | ✅ correct idea, but complex and **not shared** with other handlers |

## 3. The bug, precisely

The `building` handler (when the current building's title ≠ target):

1. known-sub-screen-of-target case → `press_back(); frame_back=capture(); loc_back=perceive()`; branch on `loc_back`.
2. blind-Back case → `press_back(); frame=capture(); loc2=perceive()`; branch on `loc2`:
   - `loc2` is target building → `return True`
   - `loc2` is still *some* building → learned-recovery → `continue`
   - **else → falls through to "Cannot reach → Home"**

Outcome (2) enumerates only two post-Back states. The **most common** real
outcome — *Back returned to `port_overworld`* — is not enumerated, so it hits
the Home-escape. But `port_overworld` is exactly the state the loop already
handles well (tap `harbor` from the building list → harbor panel opens →
`return True`). So a **successful** Back is treated as failure, Home is tapped,
and the 60s deadline drains.

Why the deadline drains so fast: each inline `perceive()` is another
~20–30s OmniParser+Moondream pass. With one perceive at the loop top **plus**
one or two inline re-perceives per `building` iteration, only ~2–3 iterations
fit in 60s. The inline re-perceives don't just misroute — they **burn the
time budget** that the retry logic needs.

**Root cause:** model B re-implements a *subset* of the top-of-loop dispatcher.
Any state it forgets to list routes to the wrong fallback. This is the same
class of blind-subloop bug already fixed in buy/sell.

## 4. Other smells found while mapping the loop

- **Home-escape fires on the wrong signal.** It should be a *last resort when
  stuck* (no progress across N iterations), not "Back didn't leave me in a
  building." Today it's an inline consequence of model B.
- **Two stuck models coexist.** `port_overworld` has a rich signature+timer
  model; `building` has none (it tries Back/Home once per visit and relies on
  the outer deadline). They should be one model.
- **`deadline` is reset in ~6 places** (`= time.time() + timeout`) scattered
  across branches. Total time budget is hard to reason about; a run can extend
  well past the nominal 60s. Resets should correspond to an explicit
  "progress happened" event, not be sprinkled per-branch.
- **`_always_present` reparse (just added) only lives in `port_overworld`.**
  The `building` handler doesn't benefit from it. A unified model would apply it
  once.
- **`port_overworld` itself is dense** — nested `force_retap` / signature-change
  / give-up with the `_tap_from_list() or _tap_from_port_map()` + `_reparse` +
  `return False` block duplicated twice. Correct, but a good candidate to fold
  into the same escalation ladder.

## 5. Proposed target structure

**Principle:** one `capture` + one `perceive` per iteration. Each handler picks
**exactly one** action from the *current* perceived state and `continue`s.
No handler perceives again inline. All "am I stuck?" decisions come from a
**single cross-iteration progress tracker**, not from post-action branching.

### 5a. Progress tracker (shared)

Track one value across iterations:

```
sig = (loc["location"], bld_title_short_or_menu_or_signature)
if sig == last_sig: no_progress_count += 1
else:               no_progress_count = 0; last_sig = sig
```

Escalation ladder keyed on `no_progress_count` (and the post-tap patience
window for `port_overworld`), shared by every handler:

```
wait (inside patience window)  →  re-issue the branch's action (re-tap / Back)
   →  Home-escape (bounded, once)  →  return False (deadline or count cap)
```

### 5b. `building` handler collapses to single-dispatch

```
if loc == "building":
    if title matches target:            return True
    if learned_recovery(this_screen):   execute; continue     # keyed on LIVE screen
    press_back();                       continue              # wrong building → Back → loop re-dispatches
```

Back from a wrong building normally lands in `port_overworld`, which the loop
already drives to success. The known-sub-screen distinction becomes an
*informational log* — the action is the same (Back) either way, so it no longer
needs its own inline-perceive arm. Home-escape moves out of this block into the
shared ladder (fires only when `no_progress_count` shows Back isn't changing
the screen — e.g. a modal eating Back).

### 5c. Keep as-is

- The `_harbor_panel_open` early return (harbor is a right-panel overlay).
- `clear_blockers` at the top.
- `_tap_from_list` / `_tap_from_port_map` / `_tap_nameplate_if_visible` — these
  are pure actions returning bool; they don't perceive-and-branch.
- `_always_present` reparse intent — folds into the shared ladder's
  "don't give up on a basic building" rule.

## 6. Migration plan (incremental, test-backed)

1. **Fix the live bug minimally + structurally:** replace the `building`
   handler's blind-Back inline arm with `press_back(); continue`. Move the
   Home-escape to fire on a `building`-handler no-progress counter (N=2–3),
   not on post-Back state. This alone unblocks Malé→Goa.
2. **Unify the stuck model:** introduce the shared progress tracker (5a) and
   route both `building` and `port_overworld` escalation through it; remove the
   scattered `deadline` resets in favour of "reset on progress."
3. **Simplify `port_overworld`** onto the same ladder (optional, once 1–2 land).

Each step is guarded by `tests/test_sail_to_arrival.py` (+ any building-nav
replay we add). Step 1 is surgical and low-risk; 2–3 are refactors to do
deliberately, not bundled with a bug fix (surgical-changes rule).

## 7. Test gap

`tests/test_sail_to_arrival.py` covers arrival, but there is no replay that
drives `navigate_to_building` through **wrong-building → Back → port_overworld
→ tap target**. Add a mock-`perceive` sequence asserting that a Back which
reaches `port_overworld` leads to a `_tap_from_list` on the target (not a Home
tap). This locks in the fix and documents the intended control flow.
