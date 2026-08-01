# Knowledge Base Design

> **STATUS — NEEDS MINOR UPDATE (2026-05-02).**  The two-namespace split
> (control / strategy) is still correct.  Two new KB structures are
> introduced in [`planner_architecture.md`](planner_architecture.md) and
> should be reflected here when the planner lands:
>
>   - **Plan KB** (`memory/knowledge/plans/<goal_id>/<plan_id>.json`):
>     persisted Plan records with provenance, confidence, history, steps.
>     Subsumes today's `flows.json:learned_*` and `learned_recoveries.json`
>     entries.
>   - **Cue Catalog** (`memory/knowledge/verification/<goal_id>.json`):
>     per-goal cue list with provenance tracking; grows lazily through
>     heavy-check observations.
>
> Until the planner lands, this doc continues to describe the current KB
> shape correctly.

The bot's knowledge is split into two namespaces with distinct ownership rules.

## Two Namespaces

### `control()` — Game Control Intelligence
**Question answered:** *How do I operate the game UI?*

Covers everything the bot needs to navigate the game without getting lost:
navigation signals, dialog keywords, button labels, flow detection patterns,
interruptor detection, and dismissal strategies.

**Owned by:** the UI structure of the game. Changes when the game UI changes.
**Updated by:** human observation, Claude vision analysis of new screens.

Files under `memory/knowledge/control/`:
```
control/
  ui_signals.json          # sea HUD tokens, loading keywords, zone words,
                           # main menu / harbor panel detection, building name variants
  dialogs/
    market.json            # dialog confirmation keywords (basket_loaded, negotiation,
                           # sell_result, buy_result, …), result field maps,
                           # negotiation button labels
    world_map.json         # city info panel keywords, culture/facility/tax keywords
```

FSM-level control knowledge lives alongside the FSM registry:
```
memory/knowledge/fsm/
  states.json              # navigation states, detection criteria, BFS recovery edges
  flows.json               # atomic flows: step detection keywords, departure buttons
  interruptors.json        # overlay detection keywords, dismissal methods
  learned_recoveries.json  # human/Claude-taught recovery plans (grows at runtime)
```

### `strategy()` — Game Play Intelligence
**Question answered:** *What should I do to grow the company?*

Covers how to play the game well: trade rules, price events, market trends,
sea region progression, investment strategy, cultural trade restrictions.

**Owned by:** game mechanics + bot-learned observations.
**Updated by:** CLAUDE.md documentation, bot data collection, human corrections.

Files under `memory/knowledge/strategy/`:
```
strategy/
  trade_rules.json         # negotiation defaults, price events, tax exemptions,
                           # culture restrictions (no pork/wine in Islamic cities)
  market_trends.json       # 8 Major Trend types: affected goods, cycle duration, example prices
  growth.json              # sea region progression ladder, growth tracks (adventure/trade/combat)
  investment.json          # port investment strategy, mayor benefits, reward types
```

### Verified routes
```
memory/knowledge/config/
  routes.json              # ONLY routes the bot has actually run (verified: true)
                           # Do not add inferred/planned routes here
```

---

## Design Rules

### No game strings in Python code
**Any string used to match, identify, or label a game state, action, flow, dialog, or
button must live in a KB JSON file — never hardcoded in Python.**

This includes:
- Button label text used for OCR/vision matching (e.g. `"purchase"`, `"depart now"`)
- Dialog detection keywords (e.g. `"purchase cost"`, `"negotiat"`)
- Flow and step identifiers used as KB keys (e.g. `"market_purchase"`, `"confirm"`)
- Interruptor detection keywords (e.g. `"not enough crew"`)
- Any text compared against OCR output to identify a screen or state

**Why:** hardcoded strings scatter game knowledge across Python files. When the game
UI changes (text, localisation, new variant), you have to hunt through code.
With KB-driven strings, you edit one JSON file and every caller updates automatically.

**How to comply:**
1. The string belongs in a KB JSON file under `memory/knowledge/`
2. `ControlKB` (in `brain/kb.py`) provides a **semantic accessor** — a named method
   that describes *what* is being looked up, not *where* in the KB it lives.
   Action code calls the semantic method; only `ControlKB` knows the KB path.
3. Action code never constructs KB paths inline (e.g. no
   `control().flow_step_button_labels("market_purchase", "confirm")` in action code —
   that leaks KB structure into callers).

**Example — correct:**
```python
# market_actions.py — caller knows what it wants, not where it is
labels = _ckb_lazy("purchase")   # resolves via ControlKB.market_action_button_labels

# brain/kb.py — only kb.py knows the JSON path
def market_action_button_labels(self, action: str) -> list[str]:
    return self._market.get("action_buttons", {}).get(action, [])
```

**Example — wrong:**
```python
# Hardcoded button text in action code
pos = _find_button(frame, "purchase", "Purchase")

# Hardcoded KB path in action code
labels = control().flow_step_button_labels("market_purchase", "confirm")

# Hardcoded flow check order in logic code
_FLOW_CHECK_ORDER = [("market_purchase", "basket"), ("market_sell", "negotiate"), ...]

# Hardcoded button labels per flow step
if step == "negotiate":
    btn = _find_button(frame, "no", "decline", "cancel")
```

### No repeated string literals
**Any string literal that appears more than once in Python code must be defined as a named
constant.  Repeat occurrences are a typo hazard and make renaming error-prone.**

This applies to:
- Internal protocol identifiers shared across modules: recovery action types
  (`RECOVERY_TAP_BUTTON`, `RECOVERY_WAIT`, `RECOVERY_TAP_OK_OR_X` in `brain/kb.py`),
  confidence levels (`CONFIDENCE_HIGH`, `CONFIDENCE_LOW` in `brain/perceive.py`)
- Dict key strings used in both data (JSON) and code: region constraint keys
  (`RGN_Y_MIN`, `RGN_Y_MAX`, `RGN_X_MIN`, `RGN_X_MAX` in `brain/kb.py`)

**How to comply:**
1. Define the constant at module level in the module that *owns* the concept.
   - `brain/kb.py` owns recovery action types and region keys (it defines the schema)
   - `brain/perceive.py` owns confidence levels (it defines `PerceiveResult`)
2. Import the constant everywhere else — never redefine it.
3. The JSON side (flows.json, states.json) uses the same string value; comments in the
   JSON file should note which Python constant it corresponds to.

**Example — correct:**
```python
# brain/kb.py — defines the constant once
RECOVERY_TAP_BUTTON = "tap_button"

# brain/recovery.py — imports and uses it
from brain.kb import RECOVERY_TAP_BUTTON
if action == RECOVERY_TAP_BUTTON:
    ...
```

**Example — wrong:**
```python
# recovery.py — redefines the string in two places
if action == "tap_button":
    ...
elif action == "wait":
    ...
```

### Exception — navigation state IDs
Internal FSM state IDs (`"port_overworld"`, `"sea"`, `"building"`, `"loading"`, etc.) appear as
comparison keys throughout the codebase and are exempt from this rule. They are defined as
identifiers in `states.json` and used as opaque keys — not matched against game text.
Requiring every `state == "port_overworld"` check to go through a KB lookup would add
churn with no practical benefit since state IDs are stable and not localisation-sensitive.

### What belongs in control
- Any keyword the bot checks against OCR output to detect a screen or dialog
- Any button label the bot taps by text (not by fixed coordinate)
- Any flow detection pattern (what makes the bot know it's mid-purchase?)
- Any interruptor pattern (what makes the bot know a popup appeared?)

### What belongs in strategy
- Rules about when to buy or sell (based on margin, not absolute price index)
- Which goods are restricted by culture
- Which sea regions unlock as the company grows
- Market trend timing and affected goods

### What does NOT belong in either KB
- Fixed pixel coordinates — those are layout constants in code, not knowledge
- OCR engine config, ADB device IDs, timeouts — that's infrastructure config
- Routes the bot has never run — add to `routes.json` only after a confirmed run

### Price threshold rule (removed)
The old "buy ≤90%, sell ≥110%" threshold rule was removed. It is wrong:
selling at 90% at a distant high-margin port is more profitable than selling
at 110% at a nearby low-margin port. Profitability is determined by the spread
between buy price and sell price across the voyage, not by price index alone.

---

## Accessing the KB in code

```python
from brain.kb import control, strategy

# Control KB — UI navigation
tokens  = control().sea_hud_tokens()          # tuple[str]
confirm = control().dialog_confirmation("basket_loaded")  # tuple[str]
btns    = control().negotiation_button_labels("use_one")  # list[str]
kws     = control().flow_step_keywords("market_purchase", "negotiate")

# Strategy KB — game play
default = strategy().negotiation_default("buy")   # "once"
trends  = strategy().market_trends()               # list of trend dicts
goods   = strategy().trend_goods("festival")       # ["foods", "seasonings", "luxuries"]
regions = strategy().sea_region_progression()      # ordered list of sea regions
```

Both are lazy singletons — loaded once on first access, free on every subsequent call.
Call `reload()` after editing KB files during a live run:

```python
from brain.kb import reload
reload()  # clears both caches; next access reloads from disk
```

---

## Updating KB at runtime

The bot writes to KB files in two cases:

1. **Learned recoveries** (`fsm/learned_recoveries.json`) — when human escalation
   teaches the bot a new recovery procedure, it is saved here so the next run
   can handle the same situation automatically without human input.

2. **Market prices** (`markets/<port>__market.json`) — each market visit appends
   a price snapshot. This is strategy data but stored in its own directory because
   it is high-volume append-only data.

All other KB files are edited by humans or by Claude during design sessions.
The bot reads them; it does not overwrite them during normal operation.
