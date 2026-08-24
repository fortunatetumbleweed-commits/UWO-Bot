# Opportunity-driven architecture: decision → planning → execution

Status: **north-star design** (2026-08-17). No code yet — this frames how autonomous
money-making should be structured so that **barter is one instance, not the whole
thing**. Written because the user's intent is bigger than barter: *the bot should scan
market opportunities, decide which is most profitable, plan how to capture it under
time/space constraints, execute with recovery, and learn.*

The concrete motivating example: each **investment season** has a category boom (this
season = **spice** → the seasonal barter good is **Box of Nutmeg**). Profitable
**market events** appear on the **world map** at **specific cities during specific
time windows**. The bot should notice them, pick the best one it can actually reach in
time, and go execute it — barter being the current best-fit for the spice season.

---

## 1. The loop

```
        ┌───────────────────────────────────────────────────────────────┐
 DECIDE │ Perceive OPPORTUNITIES (world-map market events, seasonal      │
        │ category, standing goals) → rank by expected net value under   │
        │ reachability → choose a goal (an Opportunity).                  │
        └───────────────────────────────┬───────────────────────────────┘
        ┌───────────────────────────────▼───────────────────────────────┐
  PLAN  │ Decompose the Opportunity into a SUB-TASK GRAPH (partial order) │
        │ + a SCHEDULE under SPATIOTEMPORAL constraints (arrive at the    │
        │ event city before its window closes; gather inputs en route,    │
        │ ordered by sailing cost from the CURRENT position).             │
        └───────────────────────────────┬───────────────────────────────┘
        ┌───────────────────────────────▼───────────────────────────────┐
EXECUTE │ Run runnable sub-tasks (cheapest-from-here first). On failure   │
        │ EVALUATE → recover (retry / re-route / re-plan / escalate),     │
        │ then re-plan the remainder. Report on unrecoverable abort.      │
        └───────────────────────────────┬───────────────────────────────┘
        ┌───────────────────────────────▼───────────────────────────────┐
 LEARN  │ Realized profit/XP + what-happened → calibrate estimates, grow  │
        │ the KB (event history, profitability priors, recovery playbook).│
        └───────────────────────────────────────────────────────────────┘
   MEMORY + REASONING is cross-cutting: the KB feeds DECIDE/PLAN; LLM reasoning
   plugs into DECIDE (rank), PLAN (feasibility), and EXECUTE (recover) as the
   escalation tier above deterministic logic.
```

## 2. The core abstraction: `Opportunity`

A common type so DECIDE can compare unlike money-makers (barter, trade boom,
exploration bounty, combat contract) on one axis. Sketch:

```
Opportunity
  kind            # "seasonal_barter" | "trade_boom" | "exploration" | ...
  where           # city / village / region (may be several: gather + act + sell)
  window          # (start, end) in game-time, or None if standing
  inputs          # preconditions / materials required (may be empty)
  est_value       # expected net ducats/XP  (from KB priors + solvers)
  est_time        # gather + travel + act, from CURRENT position
  reachable       # est_arrival ≤ window.end  (feasibility gate)
  plan_fn         # opportunity-type → a PLAN (task graph)   [the plugin hook]
```

Barter is the first `kind`. Adding a new money-maker = a new `Opportunity` kind + its
`plan_fn`; DECIDE and EXECUTE stay generic. This is the extension point the user wants.

## 3. The layers

### DECIDE — pick the goal
- **Input:** world state (position, fleet, ducats, cargo), the KB (seasonal category,
  event history, profitability priors), and freshly-perceived opportunities.
- **New capability — event reader:** read world-map **market events** into
  `{city, category, magnitude, window}`. Partly there already (world-map OCR has
  surfaced `Trade Event`, `Boom occurred in Male`, `Major Trend`, `Schedule`); needs a
  structured reader + the **time window**.
- **Ranking:** expected **net value per unit time**, gated by **reachability** (can't
  pick an event that closes before arrival). Season/category fit is a strong prior
  (spice season → weight spice events / the seasonal barter). Deterministic score
  first; LLM reasoning to break ties / handle novel events.
- **Output:** the chosen `Opportunity` (or a ranked shortlist).
- **Reuse:** `barter_strategy` #29, `growth_strategy` #30, seasonal KB
  ([[project_investment_season_vs_weather]]).

### PLAN — how to capture it (space + time)
- **Input:** chosen `Opportunity` + current position/state + KB (sources, coords,
  ratios).
- **Sub-task graph (partial order):** e.g. barter =
  `{gather(m) ∀ materials} → sail_to(event_city/village) → act(barter N) →
  sail_to(sell_port) → sell`. The gathers are **mutually unordered** — order is a cost
  decision, not fixed.
- **Dynamic cost scheduling:** order the runnable sub-tasks by sailing cost **from
  where the bot is now** — this is exactly `gathering_solver.plan_gathering(start=cur)`
  used **dynamically** (re-run per leg) instead of once up-front. → "coral or ebony
  first" varies with the start position, for free.
- **New capability — temporal feasibility:** estimate `gather_time + sail_time` and
  check `now + est ≤ window.end`. Requires a **game-clock read** and a **travel-time
  model** (distance → in-game days; 1 game day ≈ 12 real min —
  [[project_supply_subsystem_phase0_2026-08-14]]). This is the single biggest new
  concept; nothing models time today.
- **Reuse:** `gathering_solver` (cost route from position), `plan_barter_task`
  (needs → route → purchases).

### EXECUTE — do it, recover, re-plan
- **Loop:** `runnable = deps-met sub-tasks; next = cheapest-from-current-position;
  run(next); on ok → mark done, update position; on fail → recover; re-plan remainder`.
- **Recovery ladder** (replaced the old 5-phase runner's early-stop):
  | Failure | Recovery |
  |---|---|
  | transient (dialog / nav hiccup) | bounded retry |
  | source out of stock | **re-route** to an alternative source port → re-plan |
  | can't reach a screen | `recover_to_port_overworld` → retry |
  | window will be missed | re-decide (maybe abandon for a better opportunity) |
  | unknown / hard | **escalate to Claude** (`replan.py`: continue/insert/replace/escalate) |
  | unrecoverable | abort — report which sub-task + why |
- **Reuse:** validated executors (`purchase_goods` / `sell_goods` / `SailToGoal` /
  `barter_commit_verified`), `replan.py`, `recover_to_port_overworld`, and the
  `achieve_goal` perceive→act→verify→replan loop as the per-sub-task engine.

### LEARN — close the loop
- Realized vs estimated profit/XP → calibrate `est_value`/`est_time`.
- Grow KB: **event history** (city, category, window, outcome), **profitability
  priors**, **recovery playbook** (what recovered what).
- **Reuse:** flywheel #32, `kb_ingest` #28, the game-KB vision
  ([[project_game_knowledge_base_vision]]).

## 4. What exists vs. what's new

| Layer | Reuse (exists) | New |
|---|---|---|
| DECIDE | barter/growth strategy #29/#30, seasonal KB, partial world-map event OCR | structured **event reader** (+time window), **reachability-gated ranking**, `Opportunity` type |
| PLAN | `gathering_solver` (cost route from live position), `plan_barter_task` | **temporal model** (game clock + travel-time + window feasibility), general **sub-task/dependency graph**, dynamic scheduler |
| EXECUTE | executors, `replan.py`, `recovery.py`, `achieve_goal` | recoverable **sub-task container** (replaces early-stop) |
| LEARN | flywheel #32, `kb_ingest` #28 | **event/opportunity history** schema, priors, recovery playbook |

No task-graph/DAG/scheduler and **no notion of time** exist today — those are the two
foundational new pieces. Everything else is wiring existing, validated parts together.

## 5. Barter as the first vertical slice
Barter is `Opportunity.kind == "seasonal_barter"` with `plan_fn = plan_barter_task`
and sub-tasks `gather×N → sail_to_village → barter → sail_to_sell → sell`, executors
already validated live ([[project_male_goa_trade_task_complete]]). Building barter
through these layers (rather than the deleted rigid 5-phase runner) exercises the whole
frame end-to-end while touching only one `kind`.

## 6. Suggested build order (when we decide to build)
1. **EXECUTE container** — sub-task/dependency model + dynamic cost scheduler
   (reusing `plan_gathering` from live position) + recovery ladder. Barter-shaped but
   generic; unit-tested with mocked executors. *(No time yet — assume reachable.)*
2. **Temporal model** — game-clock read + travel-time estimator + window feasibility;
   fold into the scheduler and add the "window will be missed" recovery.
3. **DECIDE** — world-map event reader + `Opportunity` type + reachability-gated
   ranking; barter selected automatically in spice season.
4. **LEARN** — event-history / priors / recovery-playbook KB schemas + flywheel wiring.
5. **Generalize** — add a second `Opportunity.kind` (e.g. trade boom) to prove the
   plugin seams.

## 7. Open design questions (resolve per layer at build time)
- **Recovery intelligence:** deterministic-first (retry / re-route / re-plan) with LLM
  only on escalation, vs LLM-in-the-loop earlier?
- **Re-plan cadence:** re-schedule after *every* sub-task, or only on failure / stock
  change?
- **Sequencing authority:** pure cost solver, or can strategy/LLM override (grab a
  limited-stock or perishable good first regardless of distance)?
- **DECIDE fidelity:** how much ranking is deterministic vs LLM-judged?
- **Time model fidelity:** how precise must the travel-time estimate be to gate on
  event windows safely (buffer for bounces / resupply)?
- **Generality now vs later:** build the `Opportunity`/graph abstraction up front, or
  barter-specific then refactor once a second `kind` exists?

## Related
[[project_barter_separate_from_task_runner]] (barter is a separate planner→phase
machine today) · [[project_barter_nav_pipeline_complete_2026-08-15]] (the solvers +
executors this reuses) · [[project_investment_season_vs_weather]] (seasonal category
= the DECIDE prior) · [[project_village_info_readable_remotely]] (remote reads feed
PLAN inputs) · [[project_game_knowledge_base_vision]] (the KB both LLMs consume) ·
`docs/barter_task_orchestration_review.md` (why the current barter mission is rigid).
