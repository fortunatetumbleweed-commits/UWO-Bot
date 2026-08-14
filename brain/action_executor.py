"""Execute a reasoning-layer action via the existing primitives — safety-gated.

Maps a whitelisted reasoning action `{op, arg, why}` to the real primitive
(navigate_to_building / exit_to_overworld / _find_button+tap / press_back) and
**refuses real-money commits** — the red-gem discipline backstop. Two gates:
a verb-token gate (Confirm/Pay/…) and a currency gate that refuses any commit
whose cost is in RED GEMS (UWO's real commit button just says 'Purchase'/
'Recruit', which the token gate can't catch — the cost-icon colour can). This
is the "act" half of reason → execute → re-perceive → repeat.

Primitives are injectable (`prims=`) so the mapping + safety logic are testable
without a phone. Real transactions (buy/sell) are the deterministic skills' job,
with their own red-gem discipline — not this executor. See
docs/reasoning_fallback_layer_design.md §5–6.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from loguru import logger

# Tap labels that COMMIT a spend / irreversible confirm — refuse unless a
# confirm_fn approves. Deliberately narrow (commit phrases) so navigation labels
# like the 'Purchase' tab or 'Recruit' aren't false-flagged.
COMMIT_TOKENS = ("confirm", "pay", "checkout", "buy now", "purchase now")


@dataclass
class ExecResult:
    ok:      bool
    note:    str = ""
    refused: bool = False


def _is_commit_label(label: str) -> bool:
    l = " ".join((label or "").lower().split())
    return any(tok in l for tok in COMMIT_TOKENS)


def _resolve_element(arg: str, elements: Optional[list]) -> Optional[dict]:
    """Map the LLM's chosen label (or element id) to the SAME element it saw.

    Prefers an exact-label BUTTON in a real action region over a same-text title
    in the top bar — so `tap('Recruit')` hits the right-panel button, not the
    'Recruit Crew' screen title. Returns None if nothing matches (caller falls
    back to a text search).
    """
    if not elements or not arg:
        return None
    a = " ".join(str(arg).lower().replace("[commit]", "").split())
    for e in elements:                         # explicit element id (e.g. "e3")
        if e.get("id") == arg:
            return e
    exact   = [e for e in elements if e["label"].lower() == a]
    # substring either way: handles "[COMMIT] Recruit" ⇄ label "Recruit"
    partial = [e for e in elements if a in e["label"].lower()
               or e["label"].lower() in a]
    cands = exact or partial
    if not cands:
        return None
    # actionable (button/commit) first, commit ahead of button, then avoid
    # top-bar titles, then higher confidence
    cands.sort(key=lambda e: (e["type"] not in ("button", "commit"),
                              e["type"] != "commit",
                              e["region"] == "TOP-BAR",
                              -e.get("conf", 0.0)))
    return cands[0]


def execute(
    action: dict,
    frame=None,
    *,
    elements: Optional[list] = None,
    prims: Optional[dict] = None,
    confirm_fn: Optional[Callable[[str], bool]] = None,
) -> ExecResult:
    """Run one whitelisted reasoning action.

    `elements` — the observation's tap inventory (label/type/region/cx/cy); a
    `tap` resolves against it and taps the chosen element's coords directly.
    `prims` — dict of primitive callables (injectable for tests; real ones by
    default). `confirm_fn(label) -> bool` approves a commit/spend tap; default is
    to REFUSE (safe for autonomous/shadow — never auto-spend).
    """
    prims = prims or _default_prims()
    op  = (action or {}).get("op")
    arg = (action or {}).get("arg")

    if op == "abort":
        return ExecResult(False, f"abort: {(action or {}).get('why', '')}")
    if op == "wait":
        prims["sleep"]()
        return ExecResult(True, "waited")
    if op == "back":
        prims["back"]()
        return ExecResult(True, "back")
    if op == "exit_building":
        return ExecResult(bool(prims["exit_to_overworld"]()), "exit_building")
    if op == "go_to_building":
        if not arg:
            return ExecResult(False, "go_to_building: missing building name")
        return ExecResult(bool(prims["navigate_to_building"](arg)),
                          f"go_to_building({arg})")
    if op == "tap":
        if not arg:
            return ExecResult(False, "tap: missing label")
        if _is_commit_label(arg) and not (confirm_fn and confirm_fn(arg)):
            logger.info(f"[executor] REFUSED commit/spend tap {arg!r} — needs confirmation")
            return ExecResult(False, f"refused commit tap '{arg}'", refused=True)
        tgt = _resolve_element(arg, elements)
        # RED-GEM discipline: a commit whose cost is in red gems spends REAL MONEY.
        # Refuse it regardless of the verb (UWO's real commit button just says
        # 'Purchase'/'Recruit', which the token gate above can't catch). The
        # currency comes from the commit_button detector's cost-icon colour.
        if tgt and tgt.get("currency") == "red_gem" and not (confirm_fn and confirm_fn(arg)):
            logger.info(f"[executor] REFUSED red-gem (real-money) commit {arg!r}")
            return ExecResult(False, f"refused red-gem commit '{arg}'", refused=True)
        if tgt:
            prims["tap_xy"](tgt["cx"], tgt["cy"])
            return ExecResult(True, f"tap({arg}) @({tgt['cx']},{tgt['cy']}) "
                                    f"[{tgt['region']}/{tgt['type']}]")
        return ExecResult(bool(prims["tap_label"](frame, arg)), f"tap({arg}) [text-search]")

    return ExecResult(False, f"unknown op {op!r}")


def _default_prims() -> dict:
    """Wire the whitelisted ops to the real primitives (lazy import)."""
    import time
    import random
    from actions.sail_actions import (
        navigate_to_building, exit_to_overworld, _find_button,
    )
    from actions.adb_actions import tap as adb_tap, press_back

    def tap_label(frame, label) -> bool:
        if frame is None:
            return False
        coords = _find_button(frame, label)
        if not coords:
            logger.info(f"[executor] tap: button {label!r} not found on screen")
            return False
        adb_tap(coords[0], coords[1])
        return True

    return {
        "navigate_to_building": navigate_to_building,
        "exit_to_overworld":    exit_to_overworld,
        "back":                 press_back,
        "sleep":                lambda: time.sleep(random.uniform(0.5, 1.0)),
        "tap_label":            tap_label,
        "tap_xy":               adb_tap,
    }
