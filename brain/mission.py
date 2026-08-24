"""Opportunity-driven mission plumbing (EXECUTE layer) — see
docs/opportunity_driven_architecture.md.

This replaced the rigid 5-phase mission runner (fixed phase line, early-stop on failure;
deleted 2026-08-20) with:
  1. an `Opportunity` (what to pursue) — for now DECIDE is a DEFAULT
     (`default_opportunity`: event=None → seasonal barter, sell at Jakarta, time
     ignored). Real world-map event scanning + temporal planning come later.
  2. a SUB-TASK GRAPH (partial order) — the gathers are mutually unordered.
  3. a DYNAMIC SCHEDULER — at each step pick the cheapest runnable sub-task FROM THE
     CURRENT POSITION, so "coral or ebony first" is decided by sailing cost and varies
     with where the bot starts (re-evaluated per leg, not a static route).
  4. per-sub-task RECOVERY — on failure EVALUATE → recover (retry / reroute / abort)
     instead of aborting the whole mission on the first hiccup.

Executors are INJECTED (kind -> callable), so the scheduler + recovery are unit-
testable with mocks and never touch the device here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence

from loguru import logger

from memory.barter_kb import BarterRecipe, load_recipe


# ── DECIDE (defaulted for now) ────────────────────────────────────────────────

@dataclass
class Opportunity:
    """A money-making opportunity DECIDE picks. Barter is the first `kind`; new
    money-makers add a kind + a graph builder. `window`/`reachable` are the seats for
    the temporal model — defaulted (None / True = "time doesn't matter") until it's
    built. See docs/opportunity_driven_architecture.md §2."""
    kind: str                                   # "seasonal_barter" | ... (future)
    good: str                                   # the output good (e.g. "Box of Nutmeg")
    sell_port: str
    village: Optional[str] = None
    rounds: int = 6
    recipe: Optional[BarterRecipe] = None
    window: Optional[tuple] = None              # (start, end) game-time; None = anytime
    reachable: bool = True                      # temporal feasibility; default True
    est_value: Optional[float] = None


def default_opportunity(good: str = "Box of Nutmeg",
                        village: str = "Melanesian Village",
                        sell_port: str = "Jakarta",
                        rounds: int = 6) -> Opportunity:
    """DECIDE default: event=None → the current seasonal barter, sold at a fixed port,
    time ignored. Loads the recipe from the KB. When real event-scanning + temporal
    planning land, this is replaced by a ranked pick over world-map market events."""
    recipe = load_recipe(good)
    if recipe is None:
        logger.warning(f"[mission] no KB recipe for {good!r} — opportunity has no recipe")
    return Opportunity(kind="seasonal_barter", good=good, sell_port=sell_port,
                       village=village, rounds=rounds, recipe=recipe,
                       window=None, reachable=True)


# ── Sub-task graph (partial order) ────────────────────────────────────────────

@dataclass
class SubTask:
    """One unit of a mission. `deps` are ids that must be `done` first; independent
    sub-tasks (the gathers) have no deps and are ordered dynamically by cost."""
    id: str
    kind: str                                   # gather|sail_to_village|barter|sail_to_sell|sell
    location: str                               # port/village name — used for cost
    params: dict = field(default_factory=dict)
    deps: tuple = ()
    done: bool = False


@dataclass
class MissionTail:
    """How the mission gets from the village to the market that sells the output.

    `kind='route'` runs a pre-planned in-game route (Route tab) — it auto-resupplies at
    its port waypoints, so the destination is wherever the route ends: `sell_port` stays
    None and the sell node resolves the arrival port live.  `kind='sail'` free-sails to a
    named port, which IS the sell port.  `kind='none'` ends the mission at the village
    (nothing is sold) — for a command that named no destination."""
    kind: str = "sail"                          # 'route' | 'sail' | 'none'
    value: str = ""                             # route name | destination port
    sell_port: Optional[str] = None             # None → resolve on arrival (route tail)


def build_barter_graph(opp: Opportunity, plan, tail: Optional[MissionTail] = None) -> list[SubTask]:
    """Barter plugin: TaskPlan (from plan_barter_task) → a sub-task graph.
      {gather(port) ∀ source ports} → sell_surplus → supply_verify → sail_to_village
        → barter → <tail> → sell
    The gathers are mutually unordered (deps=()); the tail is a dependency chain.

    `supply_verify` sits between the last gather and the village leg because a VILLAGE
    CANNOT RESUPPLY — the 2026-08-20 fleet death (~75% of cargo lost) happened on a leg
    nobody checked.  `tail` selects the route vs free-sail ending; the default reproduces
    the original free-sail-to-`opp.sell_port` shape."""
    tail = tail or MissionTail(kind="sail", value=opp.sell_port, sell_port=opp.sell_port)
    gathers = [
        SubTask(id=f"gather:{port}", kind="gather", location=port,
                params={"port": port, "orders": dict(orders)})
        for port, orders in plan.purchases.items()
    ]
    gather_ids = tuple(g.id for g in gathers)
    chain = [
        # location="" — these sub-tasks do not move the fleet; they run where the last
        # gather ended, so they must not attract (or repel) the cheapest-next scheduler.
        # TRIM before the village: buy_to_goal buys whole shelves, so the hold arrives
        # over-stocked (live: 1,644 Textiles against 900 needed) and the barter OUTPUT
        # has nowhere to go. Trimming to the plan's own needs is pure surplus disposal.
        SubTask("sell_surplus", "sell_surplus", "",
                params={"good": opp.good, "keep_qty": dict(getattr(plan, "needs", {}) or {})},
                deps=gather_ids),
        SubTask("supply_verify", "supply_verify", "",
                params={"village": opp.village}, deps=("sell_surplus",)),
        SubTask("sail_to_village", "sail_to_village", opp_loc(opp.village),
                params={"village": opp.village}, deps=("supply_verify",)),
        SubTask("barter", "barter", opp_loc(opp.village),
                params={"rounds": opp.rounds, "good": opp.good}, deps=("sail_to_village",)),
    ]
    if tail.kind == "none":
        return gathers + chain               # ends at the village; nothing to sell
    if tail.kind == "route":
        chain.append(SubTask("sail_route", "sail_route", tail.sell_port or "",
                             params={"route": tail.value}, deps=("barter",)))
        last = "sail_route"
    else:
        chain.append(SubTask("sail_to_sell", "sail_to_sell", tail.value,
                             params={"sell_port": tail.value}, deps=("barter",)))
        last = "sail_to_sell"
    chain.append(SubTask("sell", "sell", tail.sell_port or "",
                         params={"sell_port": tail.sell_port, "good": opp.good},
                         deps=(last,)))
    return gathers + chain


def opp_loc(village: Optional[str]) -> str:
    """Village name doubles as its own location key in the coord table."""
    return village or "unknown"


# ── Recovery ──────────────────────────────────────────────────────────────────

@dataclass
class RecoveryDecision:
    action: str                                 # "abort" | "reroute" | "skip"
    new_tasks: list = field(default_factory=list)
    reason: str = ""


def default_recover(task: SubTask, result: dict, subtasks, coords) -> RecoveryDecision:
    """Conservative default: abort the mission and report which sub-task + why. Callers
    inject a smarter recover (e.g. make_barter_recover) to re-route instead."""
    return RecoveryDecision(action="abort", reason=result.get("reason", "unknown"))


def make_barter_recover(recipe: Optional[BarterRecipe]) -> Callable:
    """Barter recovery: a gather that failed on OUT-OF-STOCK re-routes to an untried
    source port for the same material(s) (from the recipe pins). Everything else
    aborts. This is the "evaluate → recover, don't just fail" behaviour for the one
    case that has a clear alternative."""
    sources = {i.material: list(i.source_ports or []) for i in (recipe.inputs if recipe else [])}

    def recover(task: SubTask, result: dict, subtasks, coords) -> RecoveryDecision:
        reason = (result.get("reason") or "").lower()
        out_of_stock = "stock" in reason or "sold out" in reason or "out of" in reason
        if task.kind != "gather" or not out_of_stock:
            return RecoveryDecision(action="abort", reason=result.get("reason", "unknown"))
        tried = {t.params.get("port") for t in subtasks if t.kind == "gather"}
        new_tasks = []
        for material, qty in task.params.get("orders", {}).items():
            alt = next((p for p in sources.get(material, [])
                        if p not in tried and p in coords), None)
            if alt is None:
                return RecoveryDecision(action="abort",
                                        reason=f"no alternative source for {material}")
            new_tasks.append(SubTask(id=f"gather:{alt}:{material}", kind="gather",
                                     location=alt, params={"port": alt,
                                                           "orders": {material: qty}}))
            tried.add(alt)
        return RecoveryDecision(action="reroute", new_tasks=new_tasks,
                                reason=f"{task.location} out of stock → reroute")
    return recover


# ── Dynamic scheduler + executor loop (EXECUTE) ───────────────────────────────

@dataclass
class MissionResult:
    ok: bool
    reason: str = ""
    steps: list = field(default_factory=list)   # per-sub-task outcomes, in run order
    completed: list = field(default_factory=list)


def _euclid(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def run_mission(subtasks: list[SubTask],
                coords: Mapping[str, tuple],
                executors: Mapping[str, Callable],
                *,
                start: tuple,
                recover: Optional[Callable] = None,
                max_retries: int = 1,
                dist_fn: Callable = _euclid) -> MissionResult:
    """Run the sub-task graph: repeatedly pick the CHEAPEST runnable sub-task from the
    current position, execute it (retrying transient failures), and on hard failure
    EVALUATE via `recover` (retry already done → reroute / abort). Re-evaluates the
    cheapest choice every step, so ordering is dynamic w.r.t. where the bot is now.

    executors[kind](task) -> dict with at least {"ok": bool[, "reason": str]}.
    coords[location] -> (x, y). `start` is the bot's CURRENT position.
    """
    recover = recover or default_recover
    tasks = list(subtasks)
    pos = start
    steps: list = []
    completed: list = []

    while True:
        pending = [t for t in tasks if not t.done]
        if not pending:
            return MissionResult(ok=True, reason="all sub-tasks complete",
                                 steps=steps, completed=completed)
        done_ids = {t.id for t in tasks if t.done}
        runnable = [t for t in pending if all(d in done_ids for d in t.deps)]
        if not runnable:
            return MissionResult(ok=False, reason="deadlock: no runnable sub-task "
                                 f"(pending={[t.id for t in pending]})",
                                 steps=steps, completed=completed)

        nxt = min(runnable, key=lambda t: dist_fn(pos, coords.get(t.location, pos)))
        result = _run_with_retry(nxt, executors, max_retries)
        steps.append({"id": nxt.id, "kind": nxt.kind, "ok": bool(result.get("ok")),
                      "reason": result.get("reason", "")})

        if result.get("ok"):
            nxt.done = True
            completed.append(nxt.id)
            pos = coords.get(nxt.location, pos)
            continue

        decision = recover(nxt, result, tasks, coords)
        logger.warning(f"[mission] {nxt.id} failed ({result.get('reason')}) "
                       f"→ recover={decision.action} ({decision.reason})")
        if decision.action == "reroute":
            nxt.done = True                       # supplanted by the alternatives
            for nt in decision.new_tasks:
                tasks.append(nt)
            continue
        if decision.action == "skip":
            nxt.done = True
            continue
        return MissionResult(ok=False, reason=f"{nxt.id} failed: {decision.reason}",
                             steps=steps, completed=completed)


def _run_with_retry(task: SubTask, executors: Mapping[str, Callable],
                    max_retries: int) -> dict:
    ex = executors.get(task.kind)
    if ex is None:
        return {"ok": False, "reason": f"no executor for kind {task.kind!r}"}
    last = {"ok": False, "reason": "not run"}
    for attempt in range(max_retries + 1):
        last = ex(task) or {"ok": False, "reason": "executor returned None"}
        if last.get("ok"):
            return last
        logger.info(f"[mission] {task.id} attempt {attempt + 1} failed: {last.get('reason')}")
    return last
