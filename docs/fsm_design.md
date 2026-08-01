# UWO Bot — FSM & Recovery Architecture Design

> **STATUS — SUPERSEDED (2026-05-02).**  The FSM/recovery split this
> document describes is collapsed into a single plan-execute-verify-replan
> loop in [`planner_architecture.md`](planner_architecture.md).  The state
> graph (states.json) and interruptor catalogue still apply as inputs to
> the planner; the dual-track "navigation vs recovery" framing does not.
>
> Retained for historical context — useful for understanding why the
> Phase 1-3 migration commits exist and what they were undoing.

## Background

The original `brain/agent.py` described a `FSM + perceive → reason → act → learn` loop.
In practice what was built is a series of **independent action functions** that each call
`where_am_i()` in a local loop and make local decisions. There is no shared state, no
transition map, and no global picture of what the bot is doing.

This becomes fragile at the edges: an unexpected departure lands the bot at sea, recovery
declares `sea` stable, the next step tries to navigate to a building, and the bot spins
in an unrecognised state for minutes. This document defines the architecture to fix that.

---

## Three Distinct Categories of Game Situations

The current code treats all situations as the same problem. They are fundamentally different.

### 1. Navigation States

Free-to-leave states. The bot can press Back or tap Home at any time.

```
port_overworld → building:market → port_overworld   (Home works)
port_overworld → world_map       → port_overworld   (Back works)
port_overworld → main_menu       → port_overworld   (Back works)
```

### 2. Atomic Flows (Committed Transactions)

Once entered, the bot is locked in. Navigation controls (Back, Home) are suppressed or
non-functional. The **only exit is completing the sequence**.

```
Market purchase flow:
  [tap Purchase] → confirm dialog → (optional) negotiation rounds → Result dialog → [tap OK]
                    ↑ Back blocked             ↑ must respond          ↑ must tap OK

Harbor departure:
  [tap Depart] → confirmation → loading screen → sea
                  ↑ Back blocked   ↑ can't interrupt
```

Recovery must **never try to break out of an atomic flow**. It must advance through flow
steps until the terminal state, then resume normal navigation.

### 3. Interruptors (Overlays)

Events that fire on top of any state at any time. The underlying state is **preserved** —
the overlay does not represent a state change. Must be dismissed before the bot can continue.

```
Discovery notice:         first visit to port/region — tap to dismiss (non-blocking)
Daily login reward:       popup over any screen — tap OK/Collect
Attendance event:         same
Server announcement:      same
Level-up notification:    auto-dismisses (non-blocking)
Android connection dialog: tap OK, wait for reload
Union request (at sea):   must Accept/Decline before sailing continues
Supply exhausted (at sea): emergency dialog, must handle
```

---

## The Floor: `port_overworld`

`port_overworld` is the **only true stable floor**. From it, everything is reachable:

- Any building → tap building list
- World map → tap mini map → tap globe
- Harbor → depart

`sea` and `sea_cinematic` are valid states **only inside `sail_to_port`**. Everywhere else
they indicate something went wrong and recovery must return the bot to `port_overworld`.

The current `_STABLE` set `{"port_overworld", "sea", "sea_cinematic", "world_map"}` is wrong.
The new target for recovery is `port_overworld` exclusively.

---

## FSM Structure

### States (Navigation Layer)

| State | Detection | Valid Exits |
|---|---|---|
| `port_overworld` | hamburger visible OR (no home, no back, port name readable) | tap building, tap mini map, tap hamburger |
| `building:<type>` | home button visible + screen title | tap Home → overworld, press Back → overworld |
| `port_map` | home button visible + port map chrome | tap Home → overworld, tap globe → world_map |
| `world_map` | home visible + "World Map" title in top-left | press Back → overworld, tap city → sea |
| `main_menu` | hamburger ≡ changes to × when menu is open; building list shows menu items (guild/auction/combat/…) | tap × button OR press Back → overworld |
| `sea` | sailing HUD visible | open world map → navigate to port → arrive → overworld |
| `sea_cinematic` | no HUD, no port name | tap screen → sea or overworld |
| `loading` | transition animation | wait → next state |
| `unknown` | nothing matched | press Back + tap Home region |

### Flows (Atomic Sequences Within States)

Each flow is triggered by a specific UI event within a parent state.
Once triggered, it must complete before the parent state is navigable again.

| Flow ID | Parent State | Trigger | Steps | Terminal State |
|---|---|---|---|---|
| `market_purchase` | `building:market` | purchase confirm dialog appears | confirm → (negotiate?) → result → ok | `building:market` |
| `market_sell` | `building:market` | sell confirm dialog appears | confirm → (negotiate?) → result → ok | `building:market` |
| `harbor_departure` | `building:harbor` | depart button tapped | confirm → loading → sea | `sea` |
| `daily_reward` | any | login reward popup | collect → ok | `current_state` |
| `quest_completion` | `building:<type>` | quest reward dialog | view → confirm | `building:<type>` |

Flows not yet fully mapped (to be discovered via Claude Vision):
- Bank loan/deposit
- Shipyard upgrade confirmation
- Inn crew recruitment
- Seasonal event participation
- Combat initiation dialog sequence

### Interruptors (Overlay Events)

| Interruptor ID | Detection | Atomic? | Dismissal | Resumes |
|---|---|---|---|---|
| `discovery_notice` | first-visit overlay | no | tap anywhere | current state |
| `daily_login_reward` | login reward popup | yes | tap OK/Collect | current state |
| `attendance_popup` | attendance event | yes | tap OK | current state |
| `server_announcement` | server notice | yes | tap OK/Close | current state |
| `level_up_notice` | level-up overlay | no | auto-dismisses | current state |
| `android_connection_dialog` | white modal + "connection unstable" | yes | tap OK, wait reload | current state |
| `union_request` | union request dialog | yes | Accept or Decline | current state |
| `supply_exhausted` | supply dialog at sea | yes | handle | `sea` |

---

## Perception Layer — Ordered

`where_am_i()` becomes a three-pass perceiver. Order is strict:

```
1. Detect interruptors
   → if found: dismiss them, then re-perceive from step 1
   
2. Detect active flow
   → if inside atomic flow: return {state, flow_id, flow_step}
   → bot must advance flow, normal navigation recovery does not apply
   
3. Detect navigation state
   → return {state} from the state registry
   → recovery graph applies here
```

**Why order matters**: the bot currently tries to press Back from inside a negotiation
dialog (step 2) because it reaches step 3 and treats it as a navigation state. The flow
check must come first.

Returns:
```python
{
    "state":        str,           # navigation state
    "flow":         str | None,    # active flow id, if any
    "flow_step":    str | None,    # current step within flow
    "interruptors": list[str],     # any active interruptors (should be empty after pass 1)
    "port":         str | None,    # current port name if readable
    "detail":       str,           # human-readable explanation
}
```

---

## Recovery Layer

### `recover_to_port_overworld(home_port, timeout=300)`

The new core primitive. Replaces `recover_to_known_state`. Targets `port_overworld`
exclusively. Handles all states including `sea`.

```
State               Recovery Action
────────────────────────────────────────────────────────────
port_overworld      already at floor — done
building:<type>     tap Home button → port_overworld
port_map            tap Home button → port_overworld
world_map           press Back → port_overworld
main_menu           tap × (hamburger becomes ×) OR press Back → port_overworld
sea / cinematic     open world map → navigate to home_port → wait for arrival
loading             wait → re-check
unknown             press Back → tap Home region → re-check
inside_flow         advance flow to terminal_state → then recover normally
```

The `sea → port_overworld` path reuses existing machinery:
`_open_world_map_from_sea` + `_navigate_world_map_to_port` + `_wait_for_arrival`.

### `assert_at_port(context, home_port) → bool`

Replaces `assert_stable`. Calls `recover_to_port_overworld` if not already at
`port_overworld`. Returns False if recovery fails after timeout.

All action entry points call this:
```python
# run_sell_all, run_buy_all, _navigate_to_market, navigate_to_building
if not assert_at_port(context="sell_all", home_port=home_port):
    return StepResult(..., ok=False, notes="could not reach port overworld")
```

### `navigate_to_building` loop changes

Currently presses Back blindly when it sees `sea`, `world_map`, etc. — which is what
caused the main menu to open. Replace with `recover_to_port_overworld`:

```
loop:
    frame = capture_screen()
    loc = perceive(frame)          # new three-pass perceiver
    
    if interruptors:    dismiss, continue
    if in_flow:         advance flow, continue
    if port_overworld:  find building → tap
    if building:<X>:    check title match → success or recover
    if loading:         wait
    else:               recover_to_port_overworld(home_port)
```

---

## Knowledge Base Schema

```
memory/knowledge/fsm/
  states.json          ← navigation states + detection rules + valid exits
  flows.json           ← atomic sequences: trigger, steps, terminal_state
  interruptors.json    ← overlays: detection rule, dismissal, resumes
  recovery_paths.json  ← BFS-precomputed shortest paths to port_overworld (cache)
```

### states.json entry
```json
{
  "id": "building:market",
  "detection": {
    "chrome": {"has_home": true},
    "screen_title_contains": ["market", "marketplace"]
  },
  "exits": [
    {"action": "tap_home",    "to": "port_overworld"},
    {"action": "press_back",  "to": "port_overworld"}
  ],
  "flows": ["market_purchase", "market_sell"]
}
```

### flows.json entry
```json
{
  "id": "market_purchase",
  "parent_state": "building:market",
  "trigger_detection": "purchase confirm dialog visible",
  "atomic": true,
  "steps": [
    {
      "id": "confirm",
      "detection": "Load/Max/Purchase buttons visible",
      "action": "tap_purchase_button"
    },
    {
      "id": "negotiate",
      "optional": true,
      "detection": "negotiation dialog visible",
      "action": "respond_to_negotiation"
    },
    {
      "id": "result",
      "detection": "Result dialog visible",
      "action": "tap_ok"
    }
  ],
  "terminal_state": "building:market"
}
```

### interruptors.json entry
```json
{
  "id": "discovery_notice",
  "detection": "discovery/first visit overlay visible",
  "atomic": false,
  "dismissal": "tap_anywhere",
  "resumes": "current_state_unchanged"
}
```

---

## Learning Path: Unknown Flows and States

The bot will encounter situations not in the KB. When it does:

1. `perceive()` returns `{state: "unknown", flow: null}`
2. Bot calls Claude Vision: "What state is this? What actions are available?"
3. Claude identifies: navigation state OR flow step OR interruptor
4. Result is saved to the appropriate KB file (`states.json`, `flows.json`, or `interruptors.json`)
5. Next encounter: bot recognises it immediately from the registry

This is the same "learn once, cache forever" principle as scene understanding — applied
to the state machine itself.

---

## Design Questions — Resolved

### 1. Flow Detection Reliability

**Primary signal**: the game chrome (top-right control bar) disappears or becomes
untappable under a semitranslucent curtain when a flow is active.

**Practical detection**: try an action (press Back). If `perceive()` before and after
returns the same state and same detail, Back is blocked → bot is inside a flow.
Retry by scanning for actionable buttons on the current screen.

**Known atomic flows:**
- Market negotiation (standard purchase/sell)
- Smuggler negotiation (not yet encountered)
- Detained by soldiers at harbour when smuggling (not yet encountered)

**Unknown flows**: Claude Vision fallback finds the most actionable button (prefer
close/OK, otherwise any button), takes that action, records it as a new edge.
Accumulates in the KB. See §Learning Path.

### 2. Mid-Flow Restart After Crash

The game screen stays at the exact crash point. The bot cannot go back.

On restart: `perceive()` reads the current screen. If it matches a known flow step
(by screen content match against flow KB), the bot resumes that step. If unknown,
Claude Vision fallback applies (§3). Flow definitions support entry from any step.

### 3. Flow Timeouts / Stuck in Flow

There are always some actionable buttons. Escalation order:
1. Registry: expected next button → tap
2. Claude Vision: find close/X button → tap
3. Claude Vision: find any button → tap
4. Repeat until `perceive()` returns a known navigable state
5. Record each (perceived_screen, action_taken) pair as a new flow edge in the KB

This is the primary learning mechanism for new flows.

### 4. Interruptor Stacking

Two interruptors can appear in sequence. Processing is one at a time:
- **Daily news popup** ("Main" title): must use the close button — tapping elsewhere
  does not dismiss it
- **Subsequent game dialogs** (login reward, event, etc.): X button or Back/Esc

After each dismissal, re-capture and re-check before proceeding to flow/state detection.

### 5. Sea Is a State, Not a Flow

Sea is a **navigation state** — the bot can interact from it (world map, main menu,
right panel). It is not locked in. Two sub-cases:

| Sub-case | Condition | Meaning |
|---|---|---|
| `sea:sailing` | Destination set, ship moving | Long **transition** — just wait, ship arrives automatically |
| `sea:idle` | No destination | **Task decision** needed — open world map, navigate somewhere |

Recovery from either sub-case → `port_overworld`:
open world map → navigate to `home_port` → wait for arrival.
The same path works for both; no need to distinguish for recovery purposes.

---

## Implementation Order

1. **FSM Registry** (`brain/fsm_registry.py`) — loads KB files, provides
   `detect_state(frame)`, `path_to(target)`, `flows_for(state)`, `add_state()`,
   `add_transition()`
2. **Seed KB** — extract all known states/flows/interruptors from current code into
   `memory/knowledge/fsm/*.json`
3. **Rewrite `perceive()`** — three-pass ordered detection using registry
4. **`recover_to_port_overworld()`** — BFS on state graph, sea→port path
5. **`assert_at_port()`** — entry guard for all action functions
6. **Update `navigate_to_building`** — replace blind back-press with
   `recover_to_port_overworld`
7. **Update `_run_trade_round`** — `assert_at_port` guard between steps
8. **Claude Vision integration** — unknown state → classify → add to KB

---

## FSM Architecture Audit — `sail_actions.py`

**Date**: 2026-04-29

### The Problem

`sail_actions.py` is a 2,573-line file that implements sailing as a **procedural script
with nested polling loops**. Each sub-function captures its own frames, runs its own OCR,
makes its own state assumptions, and loops internally until it succeeds or times out.
The perceive → state + goal → one action → loop model is bypassed entirely.

This is the root cause of every stuck-bot bug observed so far:
- Bot assumed it was at sea but was actually looking at a Perk Event dialog
- Bot repeatedly tapped departure while harbor had no crew
- Bot couldn't recover from an unexpected building state during sailing

**Root cause**: once execution enters `sail_to_port()`, it descends into nested
function calls that each contain their own polling loop. Ground truth (perceive)
is only consulted locally within each function, not at the top level. When any
function's assumption about the current state is wrong, the bot is trapped in
that function's loop with no way to re-derive correct state.

### Architecture Violation Pattern

The FSM principle states:
```
Every tick:  perceive() → (state, goal) → dispatch ONE action → return to perceive()
```

The current code does:
```
sail_to_port():
  exit_to_overworld():          # inner loop — polls until overworld
    _navigate_to_harbour():     # inner loop — polls until harbour
      _ensure_fleet_ready():    # inner loop — polls blocking signals
        _depart_from_harbour(): # inner loop — polls until sea confirmed
          _navigate_sea_to_destination():
            _open_world_map_from_sea():    # inner loop — 3 attempts
              _navigate_world_map_to_port(): # inner loop — pan/search
                _wait_for_arrival():         # inner loop — 15 min poll
```

Each layer traps the bot deeper. If `_depart_from_harbour` enters its polling
loop but the game is actually showing a popup, the bot cannot escape to perceive()
to detect the popup — it's stuck checking for sea HUD in a tight loop.

### Violating Functions — Complete Inventory

| Function | Lines | Inner Loop? | Bypasses perceive()? | Reimplements Recovery? | Specific Violation |
|---|---|---|---|---|---|
| `sail_to_port()` | 2471–2573 | No (orchestrator) | Yes — ad-hoc `where_am_i()` | Yes — inline building/sea branching | Sequential function chain; cannot re-derive state between phases |
| `exit_to_overworld()` | 582–636 | Yes — deadline loop | Partial — uses chrome | Yes — 2-stuck-back → recovery delegation | Should be a single action per tick, not a loop |
| `navigate_to_building()` | 641–810 | Yes — deadline loop | Partial — chrome + OCR | Yes — fallback strategies inline | Building list scroll + port map + fallback all in one loop |
| `_ensure_fleet_ready()` | 930–999 | Yes — 3-attempt loop | No — only reads OCR | Yes — re-navigates to harbour inline | Should be an FSM state: "fleet_readiness_check" |
| `_depart_from_harbour()` | 1002–1094 | Yes — 90s deadline | Partial — `where_am_i()` at end | Yes — re-navigates to harbour on failure | Should be an FSM state: "departing" |
| `_ensure_active_sea_view()` | 1172–1248 | Yes — 10s poll | No | Yes — Moondream + dismiss + wake | Should be part of perceive: sea confirmation |
| `_open_world_map_from_sea()` | 1287–1382 | Yes — 3 attempts × 10 polls | Partial — `where_am_i()` per attempt | Yes — main menu dismiss, false-positive handling | Should be a single tap action; world_map detection belongs in perceive |
| `_navigate_world_map_to_port()` | 1822–2247 | Yes — complex multi-phase | Partial — state guards | Yes — keyboard dismiss, panel detection | Largest function; panning/searching should be tick-driven |
| `_wait_for_arrival()` | 2304–2432 | Yes — 15-min poll | Partial — `where_am_i()` periodic | Yes — stall detection, re-navigation, popup dismiss | Should be a goal ("arrive at X") checked each tick by the task runner |
| `_navigate_sea_to_destination()` | 2437–2468 | No (orchestrator) | No | No | Sequential chain of `_open_world_map` → `_navigate` → `_wait` |
| `_handle_post_departure_sea()` | 1099–1109 | No | Delegates to `dismiss_interruptors` | No | Fine — thin wrapper |
| `_clear_sea_popups()` | 1129–1137 | No | Delegates to `dismiss_interruptors` | No | Fine — thin wrapper |
| `_tap_depart_button()` | 817–928 | Yes — 5-attempt retry | No | Yes — yellow button search, blocking signal check | Button search should be a utility; blocking signals belong in perceive |

### What Should NOT Change

These functions are **correctly scoped** as utilities or one-shot actions:

- `_ocr_frame()`, `_screen_contains()`, `_wait_for_screen()` — perception utilities
- `_find_yellow_button()`, `_find_button()` — UI element finders
- `_is_loading_screen()`, `_is_idle_cinematic()`, `_is_on_overworld()` — state predicates
- `_is_on_world_map()`, `_world_map_port_labels_visible()` — state predicates
- `read_sea_hud()` — data extraction from current frame
- `_read_sea_speed()` — data extraction from current frame
- `_dismiss_arrival_overlay()` — single action (one tap)
- `_confirm_at_sea_moondream()` — vision query (no loop)
- `_port_names_to_search()` — data utility
- `_find_port_on_world_map()` — OCR scan (returns position, no loop)
- `_try_port_search()` — compound but bounded (search panel interaction)
- `where_am_i()` — the ground-truth oracle (this IS perceive for navigation)
- `_fs()` — coordinate scaling

### Proposed Architecture: Goal-Driven Tick Loop

Replace the nested call chain with a flat dispatch table driven by the task runner.

**State diagram for `sail_to` goal:**

```
                    ┌──────────────┐
                    │ SAIL_TO goal │
                    │  destination │
                    └──────┬───────┘
                           │ perceive()
                           ▼
              ┌────────────────────────┐
              │ What state are we in?  │
              └────────────┬───────────┘
                           │
         ┌─────────┬───────┼───────┬──────────┬───────────┐
         ▼         ▼       ▼       ▼          ▼           ▼
     building   overworld  harbor  sea    world_map    loading
         │         │       │       │          │           │
    exit_to_   navigate   check    open     find_and    wait
    overworld  to_harbor  fleet→   world    tap_go_to
    (1 back)   (1 tap)    depart   map      city
                          (1 tap)  (1 tap)  (1 action)
         │         │       │       │          │           │
         └─────────┴───────┴───────┼──────────┴───────────┘
                                   ▼
                            return to perceive()
```

**Pseudocode:**

```python
# In task_runner (the ONLY loop)

goal = SailToGoal(destination="Aceh")

while not goal.is_complete():
    state = perceive()                    # ground truth every tick
    state = dismiss_interruptors(state)   # clear popups first
    action = goal.dispatch(state)         # single action for this state

    if action is None:
        goal.mark_failed("unrecoverable state")
        break

    action.execute()                      # ONE tap/swipe/wait — then return
    time.sleep(random.uniform(5, 10))     # anti-cheat delay
```

**Dispatch table for `SailToGoal`:**

```python
class SailToGoal:
    def dispatch(self, state: PerceiveResult) -> Optional[Action]:
        match (state.location, self.phase):

            # Phase: PORT_PREP
            case ("building", _):
                return TapHomeAction()         # exit to overworld
            case ("port_overworld", "PORT_PREP"):
                return NavigateToBuildingAction("harbor")
            case ("building:harbor", "PORT_PREP"):
                if not self.fleet_checked:
                    return CheckFleetReadyAction()
                return TapDepartAction()

            # Phase: SEA_NAV
            case ("sea" | "sea_cinematic", "SEA_NAV"):
                if not self.world_map_opened:
                    return TapMiniMapAction()
                return WaitAction()            # sailing in progress
            case ("world_map", "SEA_NAV"):
                if not self.destination_selected:
                    return FindAndTapCityAction(self.destination)
                return WaitAction()            # Go to City was tapped

            # Phase: ARRIVING
            case ("loading", _):
                return WaitAction()
            case ("port_overworld", "ARRIVING"):
                if state.port == self.destination:
                    self.mark_complete()
                    return None
                return WaitAction()            # wrong port — still arriving

            # Unexpected
            case _:
                return RecoverToOverworldAction()
```

**Key properties of this architecture:**

1. **Every action is one tick** — tap, swipe, or wait. Then return to perceive().
2. **No nested loops** — the task runner IS the only loop.
3. **Self-correcting** — if the bot is in an unexpected state, the next tick's
   perceive() detects it and dispatch routes to recovery.
4. **Popup-proof** — interruptors are cleared at the top of every tick, before
   dispatch. No function can get trapped behind a popup.
5. **Observable** — every tick logs: state, phase, action. Debugging is reading
   a flat log, not tracing nested call stacks.
6. **Testable** — each action is a pure function of (state, goal). Unit tests
   can feed synthetic states and verify the correct action is returned.

### What `perceive()` and `where_am_i()` Become

Currently `where_am_i()` and `perceive()` are separate systems that overlap:

- `where_am_i()` → navigation state (port_overworld, sea, building, …)
- `perceive()` → three-pass pipeline (interruptors → flows → navigation state)

In the target architecture, these merge:

```python
def perceive(frame=None) -> PerceiveResult:
    """Single ground-truth call per tick. Replaces where_am_i()."""
    if frame is None:
        frame = capture_screen()

    # Pass 1: interruptors (detect only — caller decides whether to dismiss)
    interruptors = detect_interruptors(frame)

    # Pass 2: active flow (market transaction, departure sequence, etc.)
    flow = detect_active_flow(frame)

    # Pass 3: navigation state
    nav = detect_nav_state(frame)   # what where_am_i() does today

    return PerceiveResult(
        location=nav.location,
        port=nav.port,
        detail=nav.detail,
        flow=flow,
        interruptors=interruptors,
    )
```

Recovery is the corrective-action counterpart to perceive. If perceive says
"you're in state X" but the goal expects state Y, recovery provides the action
to get from X → Y. In the dispatch table, this is just another case:

```python
case _:
    return RecoverToOverworldAction()
```

No separate `recover_to_port_overworld()` polling loop — recovery is one action
per tick, like everything else.

### Migration Path

The refactor can be done incrementally without breaking the existing bot:

1. **Define `SailToGoal` with dispatch table** — initially delegates to existing
   functions for each action (e.g. `TapDepartAction` calls `_tap_depart_button`).
2. **Wire task runner to call `goal.dispatch(perceive())` in a loop** — replaces
   the direct `sail_to_port()` call.
3. **Verify**: existing behavior preserved, but now tick-driven.
4. **Slim down sub-functions** — remove internal loops from `_depart_from_harbour`,
   `_open_world_map_from_sea`, etc. Each becomes a single-action function.
5. **Remove `sail_to_port()`** — the sequential orchestrator is replaced by the
   dispatch table.
6. **Repeat for `trade_round`** — same pattern: `BuyGoal`, `SellGoal` with
   dispatch tables.

### Fleet Readiness and Departure as FSM States

Currently `_ensure_fleet_ready()` and `_depart_from_harbour()` are sequential
function calls inside `sail_to_port()`. They should be independent FSM states:

```
                    ┌──────────────────┐
                    │  port_overworld   │
                    └────────┬─────────┘
                             │ navigate_to_building("harbor")
                             ▼
                    ┌──────────────────┐
                    │ building:harbor   │──── perceive confirms harbor
                    └────────┬─────────┘
                             │ check fleet readiness
                             ▼
              ┌──────────────────────────────┐
              │ fleet_readiness_check         │
              │ (reads blocking signals)      │
              └──────┬───────────┬────────────┘
                     │           │
              no blockers    blocker found
                     │           │
                     │    ┌──────▼────────────────┐
                     │    │ resolve_blocker         │
                     │    │ (navigate to inn/harbor │
                     │    │  sub-menu, take action) │
                     │    └──────┬─────────────────┘
                     │           │ re-navigate to harbor
                     │           ▼
                     │    ┌──────────────────┐
                     │    │ building:harbor   │ ← back to readiness check
                     │    └──────────────────┘
                     │
                     ▼
              ┌──────────────────┐
              │ departing         │
              │ (tap Depart Now)  │
              └──────┬───────────┘
                     │ perceive confirms sea
                     ▼
              ┌──────────────────┐
              │ sea               │
              └──────────────────┘
```

Each box is a state. Each arrow is a transition triggered by perceive() output.
The task runner drives the transitions — no function calls another function.

### Summary of Bugs This Architecture Prevents

| Bug | Root Cause | How Tick Loop Prevents It |
|---|---|---|
| Stuck on Perk Event dialog | `_depart_from_harbour` polling loop never checks for popups | Every tick runs `dismiss_interruptors()` before dispatch |
| Departure with no crew | `_tap_depart_button` retries departure without checking blockers | Fleet readiness is a separate state; departure only reached after readiness confirmed |
| False market flow in harbor | Inline flow detection inside departure loop | `perceive()` validates flow consistency with `parent_state_detail_contains` before reporting flow |
| Sea assumption when in port | `_ensure_active_sea_view` assumes cinematic when HUD absent | `perceive()` returns actual state; dispatch table handles each state correctly |
| 15-minute stuck loop | `_wait_for_arrival` re-navigates from inside its own poll | Task runner checks arrival each tick; re-navigation is just another dispatch action |
