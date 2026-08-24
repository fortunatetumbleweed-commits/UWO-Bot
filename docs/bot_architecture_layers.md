# Bot architecture — the layered design

> **Status (reviewed 2026-08-23): TARGET design, not built.** The task/reasoning/skills/
> perception split is still the intended shape; the navigation-layer half of it is
> detailed in `docs/one_loop_task_drives_state.md`.

How the bot should be structured, top to bottom: a **task layer** that takes
natural-language tasks and manages them, a **reasoning/mission layer** that
figures out *where the bot is and how to achieve a goal*, a **skills layer** of
parameterized capabilities, and the **perception + action** substrate. Written
2026-08-07, from a design discussion.

Much of the lower half exists (goals, missions, planner, perception); the top
(a general task layer) and the middle (a consolidated reasoning layer) are the
pieces to build/formalize. This doc names the layers, their contracts, what
already maps to each, and what's new.

## The stack

```
┌──────────────────────────────────────────────────────────────────────┐
│ TASK LAYER — natural-language tasks · scheduling · monitors            │
│   "barter bison at the Sioux village daily, sell it at Calcata"        │
│   "explore the shoreline hugging left; if supply < 5 days, go to the   │
│    nearest port"                                                        │
│   • parse NL → Task Spec (goals + schedule + monitors + params)        │
│   • manage: sequence, loop, daily schedule, concurrent interrupts      │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  emits ONE active GOAL (+ intent/context)
┌───────────────────────────────▼──────────────────────────────────────┐
│ REASONING / MISSION LAYER — "where am I, and how do I achieve this?"   │
│   • world model: position (port/region/sea), cargo, supply, discovered │
│   • plan: route + UI-operation sequence to reach the goal              │
│   • reason & recover on deviation  → reasoning_fallback_layer_design.md │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  invokes SKILLS (params)
┌───────────────────────────────▼──────────────────────────────────────┐
│ SKILLS LAYER — parameterized capabilities with preconditions          │
│   sail_to(port) · barter(village,get) · sell_all/buy_all · hug_shore   │
│   (side) · resupply · navigate_to_building(name) · depart · …          │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  uses
┌───────────────────────────────▼──────────────────────────────────────┐
│ PERCEPTION (structured PerceivedState) + ACTIONS (adb tap/swipe/OCR)   │
└──────────────────────────────────────────────────────────────────────┘
```

## Layer 1 — Task Layer (new)

**Owns:** *what* to do, *when*, and *under what conditions* — expressed in
natural language.

- **Parse** NL → a **Task Spec**: an ordered/looping list of GOALS + a SCHEDULE
  (once / daily / continuous) + MONITORS (conditions that interrupt). Done once
  by a capable LLM (task decomposition is off the hot-path, so use a strong
  model). Extends today's `llm_parser.py`, which only maps NL → a *single*
  canonical command.
- **Manage** the spec: sequence goals, loop, honor the schedule, and run
  **monitors concurrently** — a monitor firing interrupts the main task, runs an
  interrupt-goal, then resumes. The "check supply, if < 5 days go to the nearest
  port" clause is a *monitor*, not a sequential step.

Task Spec (example — the bison task):
```yaml
task: bison_trade
schedule: daily
loop:
  - goto: "Sioux Village"
  - barter: { get: bison }
  - goto: "Calcata"
  - sell: bison
monitors:
  - when: supply_days < 5
    do: { goto: nearest_port, then: resupply }
```

**Contract down:** emits ONE active GOAL at a time + intent/context
("task=bison_trade, step=barter bison at Sioux, expected: in Sioux Village").
**Contract up:** receives goal outcomes (done / failed / blocked) and advances,
retries, reschedules, or surfaces to the user.

*Maps today to:* `tasks/*.yaml` + `self_grow` (declarative/hardcoded loops — to
be generalized into NL + monitors) and `llm_parser` (to extend to multi-step).

## Layer 2 — Reasoning / Mission Layer (consolidate)

**Owns:** *where am I in the game world*, and *how do I get from here to the
goal* — including operating the UI and recovering from surprises. This is the
layer the user described sitting below the task layer.

- **World model = the Company object** — a first-class, hierarchical belief
  state (Company → Fleets → Ships), maintained from `PerceivedState` + memory,
  read/updated by the reasoning layer. (Today it's scattered across
  `goal_context`, `observation`, per-goal state, and the KB.) Structure:
  - **Company (root):** currencies — `ducat` (trade), `blue_gem` (items / ship
    restore / blueprints — free), `red_gem` (**REAL MONEY — always confirm before
    spending**), `guild_coin` and other coins (each spends on its own things);
    plus `discovered_ports`/`villages` (KB, with coords) and `active_fleet`.
  - **Fleet:** `location` (at_sea | port), `supply_days`, `cargo` (goods +
    used/capacity). The unit that sails; the "supply < 5" monitor reads it.
  - **Ship:** `class`, `life`/durability (0–100, drops while sailing),
    `can_sail = life ≥ 20`. **A ship with life < 20 blocks the whole fleet from
    departing** — restore via `blue_gem` or a `blueprint`. This is the real
    `_ensure_fleet_ready` / "fleet cannot depart" condition — a company/fleet
    *state*, not a UI-nav problem.
  - **Monitors this enables:** `any ship.life < 20 → restore (blueprint else
    blue_gem)`; `supply_days < 5 → goto nearest_port, resupply`. Currency
    discipline lives here: blue_gem/ducat auto-spendable; **red_gem requires
    explicit user confirmation**.
- **Plan** — given a goal + world model, produce the skill sequence: a *goto*
  becomes route-plan (world-map nav) + arrival; a *barter* becomes enter-village
  → Barter menu → select good → confirm. Uses the planner (`plan_loop`,
  `replan`).
- **Reason & recover** — when the observed state deviates from the plan (wrong
  building, unfamiliar screen, plan no-progress), engage the **reasoning
  fallback**: serialize the structured state + intent → LLM → whitelisted next
  action. Full design in `reasoning_fallback_layer_design.md`. **Consolidate**
  the existing scattered pieces here into one reasoning entry:
  `claude_guidance.py` (frame-by-frame Claude on unknown screens),
  `reclassify_with_claude`, `_resolve_blocker_with_reasoning`, and `replan`.

**Contract down:** invokes skills with params; monitors outcomes; re-plans.
**Contract up:** reports goal done/failed/blocked to the task layer (never
silently absorbs a failure — "escalate, don't absorb").

*Maps today to:* `plan_loop`/`plan`/`replan`, `claude_guidance`,
`brain/ai_nav/mission.py` (nav-specific missions), `goal_context`.

## Layer 3 — Skills Layer (formalize)

**Owns:** *how to do one thing reliably.* Parameterized capabilities, each with
a **precondition**, a self-contained run, and a **structured result**. A skill
never decides *what task to do next* — it does its one thing and reports (the
primitive-discipline rule). A **skill registry** with uniform signatures lets the
reasoning layer compose them and lets the LLM reference them by name.

**Functions ↔ buildings are MANY-TO-MANY, not 1:1.** A function is offered by
several buildings and a building offers several functions — e.g. `recruit_crew`
is available at **Inn, Harbor, and Village** (Village in limited numbers); a
Harbor offers `{Supply, Repair, Recruit Crew, …}`. So the registry stores both
directions:
- *function → locations* (a **set**, with per-location attributes) for planning
  "where can I do X?";
- *building → functions* for perceiving "what can I do here?" (the affordance KB).

A skill's precondition is therefore a **location set**, and it carries a
**success-check + location fallback**:
```
recruit_crew(count):
  available_at:  [Inn, Harbor, Village(limited)]   precondition: at one of them
  effect:        crew increases    success-check: did crew actually rise?
```
So a function that doesn't yield at one location (the Inn had no crew) →
**success-check fails → try another location of the same function** (Harbor),
instead of dead-looping. This is what the crew-shortage failure needed: not a
"wrong building" fix, but "right function, this location didn't yield, try
another."

**Location selection is cost-aware, and the top rule is "prefer where you already
are."** The locations for a function differ in efficiency (Harbor recruit is
faster than Inn/Village; repair is at both Shipyard *and* Harbor), so pick:
```
choose_location(function, world_model):
  if world_model.current_building in function.available_at:
      return current_building              # already here → zero travel, do it now
  return rank(available_at) by efficiency_for(function,loc), availability, travel_cost
```
The actual crew failure was at the **Harbor** — which recruits crew — yet the bot
**left** it for the Inn. "Prefer current" keeps it at the Harbor and recruits
there. This is also **why the world model must track `current_building`**: "do it
where I already am" is only possible if the bot knows where it is.

*Maps today to:* `brain/goals/*` (sail_to, hug_shore, uturn_recovery),
`market_actions` (buy/sell), `sail_actions`, `explore_actions`,
`navigate_to_building`. These become registered skills with a common interface.

## Layer 4 — Perception + Actions (built)

Structured `PerceivedState` (base/overlay/mode/context/menu_item + panels +
title) and the OmniParser-based readers (grid detector, market reader) — the
"detect, don't hardcode" substrate. ADB tap/swipe/OCR below that.

## Cross-cutting principles

- **NL at the top, structure below.** NL is parsed once into a Task Spec (or
  re-interpreted per-cycle only for genuinely ambiguous tasks); every layer below
  works on structured goals/skills.
- **Monitors are concurrent interrupts**, not steps — the supply/weather/attack
  conditions run alongside the main task.
- **Reasoning is escalation, not the default.** Skills run deterministically;
  the reasoning layer engages only when stuck/ambiguous — off the hot-path, so a
  slower/stronger model is fine (same "nav fast, precision when stopped"
  principle as the OmniParser readers).
- **The reasoning layer owns "where am I + how to navigate/operate the UI."**
- **Learn once.** Successful reasoning resolutions and task decompositions get
  cached (learned recoveries / skills) so the LLM isn't re-invoked for a solved
  situation.

## The two example tasks, traced through the stack

**"barter bison at Sioux daily, sell at Calcata"**
1. *Task layer:* schedule=daily; loop=[goto Sioux, barter bison, goto Calcata,
   sell bison].
2. *Reasoning layer* per goal: `goto Sioux` → route-plan + village entry;
   `barter bison` → world-model says "in Sioux Village" → skills: enter village,
   open Barter (context reader confirms the menu), select bison, confirm; on any
   surprise (wrong menu, dialog) → reasoning fallback.
3. *Skills:* `sail_to`, `enter_village`, `barter`, `sell_all`.

**"explore shoreline hugging left; if supply < 5 days, go to nearest port"**
1. *Task layer:* main = `hug_shore(left)` continuous; monitor =
   [`supply_days < 5` → goto(nearest_port) + resupply], evaluated each cycle.
2. *Reasoning layer:* runs the hug-shore skill; reads supply from perception into
   the world model; on the monitor firing, computes nearest discovered port
   (world model) and routes there, then resumes exploring.
3. *Skills:* `hug_shore`, `resupply`, `sail_to`.

## Build order (suggested)

1. **Formalize the Skill registry** (uniform interface over existing goals/
   actions) — lets everything above reference skills by name.
2. **Reasoning-fallback Phase 1** (`reasoning_fallback_layer_design.md`) —
   the middle layer's recovery, consolidating claude_guidance/reclassify.
3. **World model** — a first-class belief state the reasoning layer reads/writes.
4. **Task Manager** — sequencing + scheduling + concurrent monitors, over Task
   Specs (start with hand-written specs, like today's yaml but with monitors).
5. **NL task parser** — extend `llm_parser` to decompose NL → Task Spec (the
   top). Do this last: once the structured task layer works, NL is a thin front.
```
