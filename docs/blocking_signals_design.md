# Blocking Signal Resolution Design

> **STATUS — SUPERSEDED (2026-05-02).**  The recipe-based resolution
> pattern this document introduced (e.g. the 6-step "not enough crew"
> resolution in flows.json) is reframed as a *Plan* in
> [`planner_architecture.md`](planner_architecture.md), executed by the
> central plan / verify / replan loop.  Blocking signals still trigger
> the resolution; what changed is that the resolution is now a Plan
> with per-step expected_progress, optional checkpoint expected_goal,
> and a replan-on-uncertainty mechanism rather than a rigid script.
>
> The 6-step "not enough crew" recipe stays in flows.json deliberately
> — it is the canonical test case for the new architecture's auto-
> correction behaviour (replan inserts the missing tap-OK step the
> recipe failed to specify).

## Problem: Blocking Signals Dead-End at Recovery

When the bot detects a blocking condition before departure (e.g. "not enough crew"),
the current flow is:

```
detect blocking signal
    → return "blocked" from _tap_depart_button
    → _depart_from_harbour calls recover_to_port_overworld()
    → bot lands at port_overworld
    → outer loop retries sail_to_port
    → tries to depart again
    → hits same blocking signal again
    → infinite loop → escalate to human
```

The bot knows *what is wrong* but has no way to *fix it*.  Recovery is the wrong
tool here — recovery is for getting back to a known stable state after something
unexpected happened.  A blocking signal is not unexpected: the game is telling the
bot exactly what prerequisite is missing.

---

## Observed Cases

From live runs so far:

| Blocking signal | Meaning | Required resolution |
|---|---|---|
| `not_enough_crew` | Fleet crew below minimum for departure | Navigate to Recruit Crew sub-menu → recruit → return to Departure |
| `not_enough_supply` | Supplies insufficient, auto-supply unavailable | Navigate to Supply sub-menu → buy supplies → return to Departure |

Both follow the same pattern: **switch to a different harbor sub-menu, complete a
transaction, switch back, retry departure.**

---

## Proposed Design: Resolution Sequences in KB

Each blocking signal entry in `flows.json` (harbor_departure) gains an optional
`resolution` field: an ordered list of steps the bot should execute to satisfy the
prerequisite before retrying.

```json
"blocking_signals": [
  {
    "text": "not enough crew",
    "description": "Fleet crew below departure minimum",
    "action": "resolve",
    "resolution": [
      { "action": "navigate_sub_menu", "target": "recruit_crew" },
      { "action": "tap_action_button" },
      { "action": "return_to_sub_menu", "target": "departure" },
      { "action": "retry" }
    ]
  },
  {
    "text": "not enough supply",
    "description": "Insufficient supplies and auto-supply unavailable",
    "action": "resolve",
    "resolution": [
      { "action": "navigate_sub_menu", "target": "supply" },
      { "action": "tap_action_button" },
      { "action": "return_to_sub_menu", "target": "departure" },
      { "action": "retry" }
    ]
  }
]
```

`"action": "resolve"` replaces `"action": "escalate"` for known-fixable conditions.
`"action": "escalate"` remains for conditions the bot cannot resolve autonomously
(e.g. insufficient ducats to recruit, port has no inn).

---

## Resolution Step Types

| Step action | Meaning | How executed |
|---|---|---|
| `navigate_sub_menu` | Tap the named sub-menu tab in the current building | OCR + tap the highlighted tab label from the top-left tab bar |
| `tap_action_button` | Tap the primary action button on the current sub-menu | L2.5 identifies the composite button; tap it |
| `return_to_sub_menu` | Navigate back to the named sub-menu tab | Same as navigate_sub_menu |
| `retry` | Re-attempt the original action that was blocked | Re-enter the departure flow |
| `wait` | Wait for a condition to clear (loading, animation) | `time.sleep` |
| `escalate` | Human/Claude escalation — resolution not possible automatically | Existing escalation path |

`navigate_sub_menu` depends on **L2.5** (the perception reasoning layer) to confirm
the sub-menu switch was successful before proceeding to the next step.  Without L2.5,
the bot cannot reliably tell which sub-menu is now active.

---

## Execution Flow with Resolution

```
detect blocking signal "not enough crew"
    │
    ├── action == "resolve"?
    │       Yes
    │       ▼
    │   execute resolution sequence:
    │     1. navigate_sub_menu("recruit_crew")
    │        → tap "Recruit Crew" tab
    │        → perceive() confirms sub_menu == "recruit_crew"
    │     2. tap_action_button
    │        → L2.5 identifies composite Recruit button
    │        → tap it
    │        → perceive() confirms action completed (crew count changed)
    │     3. return_to_sub_menu("departure")
    │        → tap "Departure" tab
    │        → perceive() confirms sub_menu == "departure"
    │     4. retry
    │        → re-enter departure flow
    │        → check blocking signals again
    │        → if clear: depart normally
    │        → if still blocked: escalate (something else went wrong)
    │
    └── action == "escalate"?
            Yes → existing human escalation path
```

Each step verifies the outcome via `perceive()` before proceeding.  If any step
fails to produce the expected state within a timeout, the sequence aborts and falls
through to escalation.  This prevents silent failures from compounding.

---

## Dependency on L2.5

Resolution sequences depend on L2.5 being available to:
1. **Identify that a sub-menu switch succeeded** — after tapping the Recruit Crew
   tab, `perceive()` must return `sub_menu == "recruit_crew"` to confirm.  Without
   L2.5 this can only be verified by keyword scan, which is the source of the
   current misclassification bugs.
2. **Locate the action button** — the composite button label changes per sub-menu
   ("Recruit", "Buy Supplies", "Repair").  L2.5 reads the action label from OCR in
   context; `_find_button` then taps by label.

Resolution can be partially implemented before L2.5 using the tab label OCR as a
simpler substitute, but L2.5 makes it robust.  Implementation order:

1. Implement L2.5 perception for harbor sub-menus (see `perception_reasoning_layer.md`)
2. Add resolution sequences to `harbor_departure.blocking_signals` in `flows.json`
3. Add `_execute_resolution(blocking_signal, frame)` to `sail_actions.py` or a new
   `actions/harbor_actions.py`
4. Update `_tap_depart_button` to dispatch to `_execute_resolution` instead of
   returning `"blocked"`

---

## KB Extension Required

`building_types/harbor.json` must include the sub-menu structure so both L2.5 and
the resolution executor can reference it by ID:

```json
{
  "building_type": "harbor",
  "sub_menus": [
    { "id": "supply",       "labels": ["supply", "supplies"],       "flow": null },
    { "id": "repair",       "labels": ["repair"],                   "flow": null },
    { "id": "recruit_crew", "labels": ["recruit crew", "recruit"],  "flow": "harbor_recruit_crew" },
    { "id": "departure",    "labels": ["departure", "depart"],      "flow": "harbor_departure" }
  ]
}
```

`navigate_sub_menu(target)` looks up `target` in this list, reads the `labels`
array, and uses `_find_button` to tap the matching tab.

---

## Learning New Blocking Signals

Not all blocking conditions are known in advance.  When a new one appears:

1. The bot detects that the departure button is disabled (greyed out — requires the
   disabled-state detection described in omniparser improvements).
2. The bot calls `reclassify_with_claude` to understand why.
3. Claude identifies the blocking text and suggests a resolution.
4. The bot saves a new blocking_signal entry (with resolution) to `flows.json`
   via the same `_save_flow_step_to_kb` pattern used for flow step learning.

This closes the learning loop: new blocking conditions are discovered and resolved
autonomously without requiring a human to manually add KB entries.

---

## Relationship to Other Design Docs

- `perception_reasoning_layer.md` — L2.5 is a prerequisite for robust sub-menu
  navigation and confirmation in resolution sequences.
- `kb_design.md` — resolution sequences are KB-driven; no game strings hardcoded
  in Python.  New blocking conditions and resolutions are added to `flows.json`
  only, with no code changes required.
- `fsm_design.md` — resolution sequences are NOT FSM transitions.  They are
  sub-procedures that execute within a single FSM state (building) and return to
  the same state.  The FSM does not need new states for "recruiting" or "supplying"
  — those are internal to the harbor building.
