# Barter task orchestration — review (2026-08-17)

> ⚠️ **SUPERSEDED 2026-08-20 — historical review, do not build from it.**
> Its three findings have all been answered, and the code it describes is gone:
> - *"No launcher"* → `run_barter.py "barter <good> at <village>[…]"`
>   (`brain/barter_command.run_barter_command`).
> - *"Barter-phase state read is STUBBED"* → the loop is panel-driven; the arrival
>   read bounds the round target (`brain.barter_quantity.panel_barter_state`).
> - *"Entry = `run_barter_task_live`"* → **deleted**, along with `run_barter_mission`
>   and the `make_*_fn` phase factories, once nothing called them.
>
> Current design + status: **`docs/barter_command_flow.md`**.

Status: **review only** (no launcher built — user deferred the decision). Question:
*does the YAML task runner have a barter task setup?* **No.** Barter is a separate,
planner-driven system that today has no launcher outside tests.

## Two parallel orchestrators

| | **YAML task runner** | **Barter mission** |
|---|---|---|
| Entry | `run_task.py <task>.yaml` → `actions/task_runner.run_task` | `brain/barter_mission_live.run_barter_task_live(recipe, village, sell_port, rounds)` |
| Shape | flat **step list** authored in YAML | **planner → phase machine** in code |
| Vocabulary | `sail_to`, `buy_all`, `sell_all`, `explore` | phases `gather → sail_to_village → barter → sail_to_sell → sell` (`brain/barter_mission.run_barter_mission`, early-stop on first failure) |
| Launchable | ✅ YAML + CLI | ❌ **only tests** call it — no CLI, no YAML, no task_runner action |
| Buy / sell | `purchase_goods` / `sell_goods` | **same** `purchase_goods` / `sell_goods` — converged ✅ |
| Sailing | `SailToGoal` tick-loop (validated live, has the harbour fix) | `sail_to_port` free-sail (older path) — diverged ❌ |

## Why barter is separate (the "complicated" part)

A trade YAML is *linear* — hand-write "sail A, buy, sail B, sell." Barter can't be,
because it needs a **planner first** (`plan_barter_task`):

```
recipe ratios × rounds  →  compute_material_needs   {material: qty}
                        →  plan_gathering (set-cover + route)   ordered source ports
                        →  assign_purchases           {port: {material: qty}}
                        →  TaskPlan(needs, gather_route, purchases, unsourced)
```

That plan then drives the phase machine (`gather` sails to each source port and buys
its assignment; `barter` runs N verified rounds at the village; `sell` dumps the
output far away). So barter is `solver output → phases`, not a fixed step list — which
is exactly why it lives outside the linear YAML runner.

## Where the phase fns land (brain/barter_mission_live.py)

| Phase | Live fn | Calls |
|---|---|---|
| gather | `make_gather_fn` | per source port: `sail_to_port` → `navigate_to_building("Market")` → `buy_materials_at_port` (→ `purchase_goods`) |
| sail_to_village | `make_sail_to_village_fn` | `pan_to_village` → tap → `Move to Village` |
| barter | `make_barter_fn` → `run_barter_phase` | `read_barter_panel` + `barter_commit_verified` |
| sail_to_sell | `make_sail_to_sell_fn` | `sail_to_port` |
| sell | `make_sell_fn` | `navigate_to_building("Market")` → `sell_goods(exclude=barter materials)` |

## Gaps (what stands between "exists" and "runnable live")

1. **No launcher.** `run_barter_task_live` is unit-tested but never invoked in
   production — there is no `run_barter.py`, no YAML, no task_runner action.
2. **Sailing diverged.** gather/sail_to_sell use `sail_to_port`, not the `SailToGoal`
   tick-loop that was just fixed + validated (harbour Back-handling). A live barter
   run would not share that path 1:1.
3. **Barter-phase state read is STUBBED.** `make_barter_fn.read_state()` returns a
   hard-coded `{"rounds_remaining": 1, "overflow": 0}` — it does NOT read the real
   panel, so the loop can't stop on rounds-exhausted and never detects/ jettisons
   overflow. This must be filled (a real `read_barter_panel`-backed state read) before
   a live barter run is trustworthy.
4. **Unsourced materials** early-fail with "read pins first" — the remote pin-source
   read ([[project_village_info_readable_remotely]]) has to have populated the KB, or
   the plan aborts.

## Options to give the task runner a barter setup

- **A — `barter` action in the YAML runner (recommended).** One `task_runner` action
  that delegates to `run_barter_task_live(recipe, village, sell_port, rounds)` (fields
  from the YAML step). Launchable via YAML like the trade; keeps the planner + phase
  machine + all the solver work (#18–#22, #31) intact. Least code.
- **B — decompose barter into YAML steps.** Add `sail_to_village` + `barter` step
  actions and write the whole run as YAML. Fully unifies under one runner, but the
  multi-port gather loop + route optimisation would have to be re-expressed (or kept
  as a planner pre-step anyway) — more work, loses the auto-planner's value.
- **C — standalone `run_barter.py`.** Thin CLI mirroring `run_task.py`. No runner
  change; barter stays its own system but becomes runnable. Leaves two launchers.

**Recommendation: A**, coupled with fixing gaps 2 + 3 first —
- point the barter sail phases at the same validated sail the task runner uses
  (`run_sail_to` / `SailToGoal`) so there's one sailing path, and
- replace the stubbed `read_state` with a real `read_barter_panel`-backed round /
  overflow read.
Then a barter run is `run_task.py tasks/barter_nutmeg.yaml`, consistent with the
trade, with no duplicated sailing and a trustworthy round loop.

See [[project_barter_nav_pipeline_complete_2026-08-15]] for the full pipeline and
[[project_male_goa_trade_task_complete]] for the trade-runner precedent + the
goal-aware buy/sell wiring a barter YAML would reuse.
