"""Task done-conditions — OBSERVABLE success predicates for the task executor.

A task "succeeds" only when reality confirms it, not when the procedure runs to the
end (docs/next_phase_architecture_2026-08-09.md §3a). Each factory returns a
`done(world_model, observation) -> bool` predicate that `resolve()` takes as its
`done_fn`. These read the HUD-populated world model (cargo/ducats/crew — see
vision.hud_readers + world_model_updater.update_from_hud) and the PerceivedState.

"Increased/decreased" conditions need a BEFORE value: capture `snapshot(wm)` when
the task starts, pass it to the factory. Ground truth (recruit=crew up; buy=cargo
up & ducats down; sell=ducats up; sail=arrived) is what stops the flailing loops.
"""
from __future__ import annotations

from typing import Callable, Optional

# done_fn signature: (WorldModel, Observation) -> bool
DoneFn = Callable[[object, object], bool]


def snapshot(wm) -> dict:
    """Capture the baseline for 'increased/decreased' done-conditions."""
    f = getattr(wm, "fleet", None)
    return {
        "crew": getattr(f, "crew_current", None) if f else None,
        "cargo": getattr(f, "cargo_used", None) if f else None,
        "ducats": (wm.currencies or {}).get("ducat"),
    }


def _fleet(wm):
    return getattr(wm, "fleet", None)


def crew_increased(baseline: Optional[dict] = None) -> DoneFn:
    """Recruit done: crew went UP vs baseline, OR reached capacity (full)."""
    base = (baseline or {}).get("crew")

    def done(wm, obs) -> bool:
        f = _fleet(wm)
        if f is None or f.crew_current is None:
            return False
        if f.crew_capacity is not None and f.crew_current >= f.crew_capacity:
            return True
        return base is not None and f.crew_current > base
    return done


def cargo_bought(baseline: Optional[dict] = None) -> DoneFn:
    """Buy done: cargo went UP AND ducats went DOWN vs baseline (a real purchase)."""
    b = baseline or {}
    base_cargo, base_ducats = b.get("cargo"), b.get("ducats")

    def done(wm, obs) -> bool:
        f = _fleet(wm)
        cargo_up = (base_cargo is not None and f is not None
                    and f.cargo_used is not None and f.cargo_used > base_cargo)
        d = (wm.currencies or {}).get("ducat")
        ducats_down = (base_ducats is not None and d is not None and d < base_ducats)
        return bool(cargo_up and ducats_down)
    return done


def cargo_sold(baseline: Optional[dict] = None) -> DoneFn:
    """Sell done: ducats went UP AND cargo went DOWN vs baseline."""
    b = baseline or {}
    base_cargo, base_ducats = b.get("cargo"), b.get("ducats")

    def done(wm, obs) -> bool:
        f = _fleet(wm)
        cargo_down = (base_cargo is not None and f is not None
                      and f.cargo_used is not None and f.cargo_used < base_cargo)
        d = (wm.currencies or {}).get("ducat")
        ducats_up = (base_ducats is not None and d is not None and d > base_ducats)
        return bool(cargo_down and ducats_up)
    return done


def arrived_at(port: str) -> DoneFn:
    """Sail done: the fleet's location matches the destination port."""
    target = (port or "").strip().lower()

    def done(wm, obs) -> bool:
        f = _fleet(wm)
        return bool(f and (f.location or "").strip().lower() == target)
    return done


# Task name -> done-condition factory (the task-KB done-condition column; pairs
# with skill_registry's task->building map). 'sail' uses arrived_at(port) directly.
_TASK_DONE = {
    "recruit_crew": crew_increased, "recruit": crew_increased,
    "buy_goods": cargo_bought, "buy": cargo_bought, "buy_all": cargo_bought,
    "sell_goods": cargo_sold, "sell": cargo_sold, "sell_all": cargo_sold,
}


def done_condition_for(task: str, baseline: Optional[dict] = None) -> Optional[DoneFn]:
    """The done-condition for a named task (recruit / buy / sell), given a baseline
    snapshot captured at task start. Returns None for tasks with no world-state
    predicate here (e.g. 'sail' -> use arrived_at(port); navigation -> on_screen)."""
    factory = _TASK_DONE.get((task or "").strip().lower())
    return factory(baseline) if factory else None


def on_screen(base: Optional[str] = None, context: Optional[str] = None,
              menu_item: Optional[str] = None) -> DoneFn:
    """Generic reach-a-screen done (e.g. 'on the Market Purchase grid'). Matches
    the PerceivedState fields (substring, case-insensitive)."""
    def done(wm, obs) -> bool:
        p = getattr(obs, "perceived", None)
        if p is None:
            return False
        if base and p.base != base:
            return False
        if context and context.lower() not in (p.context or "").lower():
            return False
        if menu_item and menu_item.lower() not in (p.menu_item or "").lower():
            return False
        return True
    return done
