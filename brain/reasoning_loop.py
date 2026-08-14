"""Reasoning loop — ties perceive → world-model → reason → execute → re-perceive.

`resolve(goal, world_model, ...)` is the orchestration: observe the screen, fold
it into the world model, ask the reasoning layer for the next action, execute it
(safety-gated), re-perceive, repeat — bounded. **Shadow mode is the default**:
it logs one reasoning trace and does NOT act, so the first live runs gather
evidence safely (methodology §12).

All I/O is injectable (`observe_fn`, `llm_fn`, `execute_fn`, `done_fn`) so the
loop is testable offline; the live defaults wire the real perception + Claude +
executor. See docs/reasoning_fallback_layer_design.md, bot_architecture_layers.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional

from loguru import logger

from brain.perceived_state import PerceivedState
from brain.world_model import WorldModel
from brain.world_model_updater import update_from_perceived, update_from_hud
from brain.reasoning import ReasoningContext, reason, _TRACE_PATH
from brain.game_primer import GAME_PRIMER

# Consecutive no-progress steps at which the task executor declares itself STUCK
# and escalates rather than flailing (docs/next_phase_architecture §3).
_STUCK_LIMIT = 3


@dataclass
class Observation:
    perceived: Optional[PerceivedState] = None
    port:      Optional[str] = None
    menu:      List[str] = field(default_factory=list)   # left-menu item labels
    buttons:   List[str] = field(default_factory=list)   # visible action buttons
    elements:  List[dict] = field(default_factory=list)  # full tap inventory (see _element_inventory)
    hud:       dict = field(default_factory=dict)         # {ducats, cargo:(u,c), crew:(cur,cap)} — ground truth
    frame:     Any = None

    @property
    def text(self) -> str:
        return describe_perceived(self.perceived, self.menu, self.buttons,
                                  self.elements)


# Screen region from an element centre — gives the LLM the spatial grouping
# (left menu vs the right action panel vs the top bar) it needs to tell a title
# from the actual action button. Fractions are of the 2400×1080 landscape frame.
def _region(cx: float, cy: float, w: int, h: int) -> str:
    fx, fy = cx / max(w, 1), cy / max(h, 1)
    if fy < 0.10:  return "TOP-BAR"
    if fx < 0.22:  return "LEFT-MENU"
    if fx > 0.72:  return "RIGHT-PANEL"
    return "CENTER"


def _element_inventory(els, w: int, h: int, limit: int = 28) -> List[dict]:
    """Structured tap inventory from OmniParser elements — the LLM's affordances.

    Keeps every BUTTON (never truncates the actionable ones away — the `[:12]`
    bug hid the real 'Recruit' button) plus numeric value texts (costs / amounts
    like '205,848', '953/2,275'), each tagged with region + centre coords so the
    reasoning layer can disambiguate same-text elements (title vs button) and the
    executor can tap the chosen one directly instead of re-searching by text.
    """
    inv: List[dict] = []
    for e in els:
        lab = " ".join((getattr(e, "label", "") or "").split())
        if not lab:
            continue
        et = getattr(e, "element_type", "") or "text"
        is_value = lab.replace(",", "").replace("%", "").replace("/", "").isdigit()
        if et != "button" and not is_value:
            continue
        cx, cy = getattr(e, "cx", None), getattr(e, "cy", None)
        if cx is None or cy is None:
            continue
        inv.append({"label": lab, "type": et,
                    "region": _region(cx, cy, w, h),
                    "cx": int(cx), "cy": int(cy),
                    "conf": float(getattr(e, "confidence", 0.0) or 0.0)})
    # buttons first (the actionable ones survive the cap), then value texts
    inv.sort(key=lambda d: (d["type"] != "button", -d["conf"]))
    inv = inv[:limit]
    for i, d in enumerate(inv, 1):
        d["id"] = f"e{i}"
    return inv


def _merge_commit_buttons(inv: List[dict], els, frame) -> List[dict]:
    """Normalise UWO's yellow commit button into the inventory.

    OmniParser's single OCR label for that button is unstable (grabs the cost OR
    the verb). `detect_commit_buttons` re-OCRs the yellow button into {verb, cost};
    here we overwrite the matching inventory entry with a stable `commit`-typed
    affordance (label = verb, plus `cost`) so the LLM ALWAYS sees a tappable
    `[COMMIT] Recruit` instead of a bare price. See commit_button.py.
    """
    try:
        from vision.region_detectors.commit_button import detect_commit_buttons
        commits = detect_commit_buttons(els, frame)
    except Exception as exc:
        logger.debug(f"[reasoning_loop] commit-button detect failed: {exc}")
        return inv
    for c in commits:
        match = next((e for e in inv if abs(e["cx"] - c.cx) < 40
                      and abs(e["cy"] - c.cy) < 40), None)
        entry = match if match is not None else {"id": f"c{len(inv) + 1}"}
        entry.update({"label": c.label, "type": "commit", "cost": c.cost,
                      "currency": c.currency,
                      "region": _region(c.cx, c.cy, frame.width, frame.height),
                      "cx": c.cx, "cy": c.cy, "conf": c.yellow_frac})
        if match is None:
            inv.append(entry)
    return inv


def describe_perceived(perceived: Optional[PerceivedState],
                       menu: Optional[List[str]] = None,
                       buttons: Optional[List[str]] = None,
                       elements: Optional[List[dict]] = None) -> str:
    """The structured screen state as compact text for the reasoning context."""
    if perceived is None:
        return "(no perception)"
    p = perceived
    parts = [f"base={p.base}"]
    if p.mode:                          parts.append(f"mode={p.mode}")
    if p.context:                       parts.append(f"context={p.context}")
    if p.menu_item:                     parts.append(f"selected={p.menu_item}")
    if p.overlay and p.overlay != "none": parts.append(f"overlay={p.overlay}")
    if p.identity:                      parts.append(f"title={p.identity!r}")
    line = " ".join(parts)
    if menu:
        line += f"\n  menu items: {', '.join(menu)}"
    if elements:
        # group by region so the LLM reads the layout, not a flat bag of labels;
        # value texts (costs/amounts) are tagged so it can associate a price with
        # the button in the same region.
        line += "\n  interactive elements (tap the label of the one you want):"
        for reg in ("LEFT-MENU", "CENTER", "RIGHT-PANEL", "TOP-BAR"):
            here = [e for e in elements if e["region"] == reg]
            if not here:
                continue
            parts_r = []
            for e in here:
                if e["type"] == "commit":
                    cost = f" — cost {e['cost']}" if e.get("cost") else ""
                    cur = e.get("currency")
                    cur = f" ({cur.replace('_', ' ')})" if cur else ""
                    parts_r.append(f"[COMMIT] {e['label']}{cost}{cur}")
                elif e["type"] == "button":
                    parts_r.append(e["label"])
                else:
                    parts_r.append(f"{e['label']}[{e['type']}]")
            line += f"\n    {reg}: {', '.join(parts_r)}"
    elif buttons:
        line += f"\n  buttons: {', '.join(buttons)}"
    return line


def resolve(
    goal: str,
    world_model: WorldModel,
    *,
    observe_fn: Optional[Callable[[], Observation]] = None,
    game_knowledge: str = GAME_PRIMER,
    llm_fn: Optional[Callable[[str], str]] = None,
    execute_fn: Optional[Callable] = None,
    done_fn: Optional[Callable[[WorldModel, Observation], bool]] = None,
    shadow: bool = True,
    max_steps: int = 4,
    trigger: str = "stuck",
    trace_path: Path = _TRACE_PATH,
    decision_cache=None,
) -> dict:
    """Run the reasoning loop until the goal is done / it aborts / max_steps.

    Returns {resolved, reason, steps:[{perceived, action, exec?}]}. In shadow mode
    it logs ONE trace and returns without acting.
    """
    observe_fn = observe_fn or live_observe
    if llm_fn is None:
        from brain.llm_client import claude_llm_fn
        llm_fn = claude_llm_fn
    if execute_fn is None:
        from brain.action_executor import execute as execute_fn

    steps: List[dict] = []
    attempt_memory: dict = {}            # screen signature -> {ineffective action keys}
    prev_obs = prev_sig = prev_action = None
    no_progress = 0
    for _ in range(max_steps):
        obs = observe_fn()
        if obs.perceived is not None:
            update_from_perceived(world_model, obs.perceived, port_name=obs.port)
        update_from_hud(world_model, obs.hud)          # cargo/crew/ducats ground truth
        if done_fn is not None and done_fn(world_model, obs):
            return {"resolved": True, "reason": "goal satisfied", "steps": steps}

        sig = _screen_sig(obs)
        # Did the PREVIOUS action make progress? If not, remember it as ineffective
        # at that screen and count toward "stuck".
        if not shadow and prev_obs is not None:
            if _progressed(prev_obs, obs):
                no_progress = 0
                if decision_cache is not None:
                    decision_cache.record(prev_sig, prev_action)      # LEARN what worked
            else:
                attempt_memory.setdefault(prev_sig, set()).add(_action_key(prev_action))
                if decision_cache is not None:
                    decision_cache.invalidate(prev_sig, prev_action)  # UNLEARN what didn't
                no_progress += 1
                if no_progress >= _STUCK_LIMIT:
                    return {"resolved": False, "steps": steps,
                            "reason": f"stuck: {no_progress} steps without progress "
                                      f"(tried {sorted(attempt_memory.get(sig, set()))})"}

        # DECISION: replay a LEARNED decision for this exact screen (no LLM call),
        # unless it's already been marked ineffective here; else reason.
        cached = (decision_cache.get(sig)
                  if (decision_cache is not None and not shadow) else None)
        if cached is not None and _action_key(cached) not in attempt_memory.get(sig, set()):
            action = {"op": cached["op"], "arg": cached["arg"], "why": "learned (cache hit)"}
            steps.append({"perceived": obs.text.splitlines()[0], "action": action, "cached": True})
        else:
            ctx = ReasoningContext(goal=goal, world_model=world_model.to_prompt(),
                                   perception=obs.text, game_knowledge=game_knowledge,
                                   avoid=sorted(attempt_memory.get(sig, set())))
            action = reason(ctx, llm_fn=llm_fn, trigger=trigger, shadow=shadow,
                            trace_path=trace_path)
            steps.append({"perceived": obs.text.splitlines()[0], "action": action})

        if shadow:
            return {"resolved": False, "reason": "shadow (logged, no action)", "steps": steps}
        if action is None:
            return {"resolved": False, "reason": "no usable action from LLM", "steps": steps}
        if action.get("op") == "abort":
            return {"resolved": False, "reason": f"aborted: {action.get('why', '')}", "steps": steps}

        res = execute_fn(action, frame=obs.frame, elements=obs.elements)
        steps[-1]["exec"] = {"ok": res.ok, "note": res.note, "refused": res.refused}
        if res.refused:
            return {"resolved": False, "reason": "refused (needs confirmation)", "steps": steps}

        prev_obs, prev_sig, prev_action = obs, sig, action

    return {"resolved": False, "reason": "max_steps reached", "steps": steps}


def live_observe() -> Observation:
    """Capture + perceive the current screen into an Observation (live default)."""
    from capture.adb_capture import capture_screen
    from brain.perceive import perceive
    frame = capture_screen()
    # Clear unexpected NON-GAME blockers first (the lock/screensaver, promo/store
    # popups) — the game idles into the lock between steps and throws promos that
    # block flows. See brain/unexpected_dialog.clear_blockers.
    try:
        from brain.unexpected_dialog import clear_blockers
        if clear_blockers(frame).get("cleared"):
            import time
            time.sleep(1.0)
            frame = capture_screen()
    except Exception as exc:
        logger.debug(f"[reasoning_loop] clear_blockers failed: {exc}")
    try:
        result = perceive(frame)
        perceived, port = result.perceived, result.port
    except Exception as exc:
        logger.debug(f"[reasoning_loop] perceive failed: {exc}")
        perceived, port = None, None
    menu: List[str] = []
    elements: List[dict] = []
    els = []                              # raw OmniParser elements (uncapped)
    try:
        from vision.omniparser import parse_fast_cached
        from vision.region_detectors.left_menu import detect_left_menu
        els = parse_fast_cached(frame) or []
        if els:
            lm = detect_left_menu(els, frame.width, frame.height)
            if lm and lm.items:
                menu = [it["label"] for it in lm.items]
            elements = _element_inventory(els, frame.width, frame.height)
            elements = _merge_commit_buttons(elements, els, frame)
    except Exception as exc:
        logger.debug(f"[reasoning_loop] element read failed: {exc}")
    buttons = [e["label"] for e in elements if e["type"] == "button"]
    hud: dict = {}
    try:
        # Read HUD from the RAW OmniParser elements (objects, uncapped) — NOT the
        # filtered/capped inventory dicts (the currency + cargo texts get dropped).
        from vision.hud_readers import read_ducats, read_cargo, read_crew
        hud = {"ducats": read_ducats(els), "cargo": read_cargo(els),
               "crew": read_crew(frame)}   # crew OCR reuses perceive's cached OCR
    except Exception as exc:
        logger.debug(f"[reasoning_loop] HUD read failed: {exc}")
    return Observation(perceived=perceived, port=port, menu=menu, buttons=buttons,
                       elements=elements, hud=hud, frame=frame)


# ── task-executor signals: screen signature + progress ────────────────────────
def _screen_sig(obs: "Observation"):
    """Coarse STRUCTURAL signature of a screen — its identity + the BUTTON/COMMIT
    affordances present (stable; ignores value-text churn like price ticks). Two
    equal signatures = "same screen"; a change = structural progress."""
    p = obs.perceived
    ident = ((p.base, p.mode, p.context, p.menu_item, p.overlay)
             if p is not None else ("unknown",))
    acts = tuple(sorted(e["label"] for e in (obs.elements or [])
                        if e.get("type") in ("button", "commit")))
    return (ident, acts)


def _hud_changed(a: dict, b: dict) -> bool:
    for k in ("ducats", "cargo", "crew"):
        va, vb = (a or {}).get(k), (b or {}).get(k)
        if va is not None and vb is not None and va != vb:
            return True
    return False


def _progressed(prev: "Observation", cur: "Observation") -> bool:
    """Did the previous action do anything? Structural signature changed OR a HUD
    value (cargo/ducats/crew) changed. See docs/state_change_detection.md."""
    return _screen_sig(prev) != _screen_sig(cur) or _hud_changed(prev.hud, cur.hud)


def _action_key(action: Optional[dict]) -> str:
    a = action or {}
    return f"{a.get('op')}:{a.get('arg')}"
