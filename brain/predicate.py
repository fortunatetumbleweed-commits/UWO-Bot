# brain/predicate.py
#
# L4 — Goal predicate evaluator.
#
# Reads a small expression language defined per-goal in
# memory/knowledge/goals/<goal>.json:local_predicate and evaluates it
# against an L3 ScreenState (vision/state_extractor.py).
#
# Returns True / False / None where:
#   True  → goal achieved
#   False → goal not achieved (state visible, predicate fails)
#   None  → can't tell from this screen (required fields not present)
#
# The runtime contract: callers that want a definitive "yes/no" treat
# None as "fall back to a heavier check" (e.g. Claude heavy_check).
#
# Expression language is intentionally tiny — comparing extracted
# fields and checking presence is enough for every goal we currently
# care about.  Spec lives in docs/four_layer_perception.md.

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from vision.state_extractor import ScreenState


_GOALS_DIR = Path("memory/knowledge/goals")


# ── Spec language ─────────────────────────────────────────────────────────
#
# A predicate is a dict.  Two top-level shapes are supported:
#
#   {"kind": "compare",
#    "lhs":  {"extract": "fleet_current"} | {"value": 786},
#    "op":   ">=" | ">" | "<=" | "<" | "==" | "!=",
#    "rhs":  ...same as lhs...}
#
#   {"kind": "present", "field": "min_crew"}   # truthy iff field is not None / empty
#
#   {"kind": "all", "of": [<predicate>, <predicate>, ...]}
#   {"kind": "any", "of": [<predicate>, <predicate>, ...]}
#   {"kind": "not", "of": <predicate>}
#
# Extracts:
#   "fleet_current" / "fleet_max"  → ScreenState.fleet_pair[0] / [1]
#   "min_crew"                     → ScreenState.min_crew
#   "standby_crew"                 → ScreenState.standby_crew
#   "has_dialog"                   → bool(ScreenState.dialog_text_cues)
#   "has_blocker"                  → bool(ScreenState.blocker_phrases)
#   "crew_capacities[<i>][<j>]"    → indexed access
#
# Any extract that can't be resolved (field missing on the state) yields
# None at that subexpression level — the predicate result is then None
# (we can't say true or false), which the caller treats as "ask Claude."


def _extract(state: ScreenState, expr: str) -> Optional[Any]:
    """Pull a single field out of *state*, returning None when unavailable."""
    if expr == "fleet_current":
        return state.fleet_current
    if expr == "fleet_max":
        return state.fleet_max
    if expr == "min_crew":
        return state.min_crew
    if expr == "standby_crew":
        return state.standby_crew
    if expr == "has_dialog":
        return state.has_dialog
    if expr == "has_blocker":
        return state.has_blocker

    # crew_capacities[<i>][<j>] — explicit list indexing.
    m = re.fullmatch(r"crew_capacities\[(\d+)\]\[(\d+)\]", expr)
    if m:
        i, j = int(m.group(1)), int(m.group(2))
        caps = state.crew_capacities
        if i < 0 or i >= len(caps):
            return None
        if j not in (0, 1):
            return None
        return caps[i][j]

    logger.warning(f"[predicate] unknown extract path: {expr!r}")
    return None


def _resolve(state: ScreenState, ref: dict) -> Optional[Any]:
    """Resolve a value reference: literal `{"value": X}` or `{"extract": "..."}`."""
    if "value" in ref:
        return ref["value"]
    if "extract" in ref:
        return _extract(state, ref["extract"])
    logger.warning(f"[predicate] malformed value reference: {ref!r}")
    return None


_COMPARE_OPS = {
    ">=": lambda a, b: a >= b,
    ">":  lambda a, b: a >  b,
    "<=": lambda a, b: a <= b,
    "<":  lambda a, b: a <  b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def evaluate(state: ScreenState, spec: dict) -> Optional[bool]:
    """Recursively evaluate *spec* against *state*.

    Returns:
      True  — the predicate is satisfied.
      False — the predicate is unsatisfied (with confidence).
      None  — at least one required field was absent on the state, so the
              predicate cannot be evaluated; the caller should fall back
              to a heavier check.
    """
    if not isinstance(spec, dict) or "kind" not in spec:
        logger.warning(f"[predicate] malformed spec: {spec!r}")
        return None

    kind = spec["kind"]

    if kind == "compare":
        lhs = _resolve(state, spec.get("lhs", {}))
        rhs = _resolve(state, spec.get("rhs", {}))
        if lhs is None or rhs is None:
            return None
        op = spec.get("op", "==")
        fn = _COMPARE_OPS.get(op)
        if fn is None:
            logger.warning(f"[predicate] unknown compare op: {op!r}")
            return None
        try:
            return bool(fn(lhs, rhs))
        except TypeError:
            logger.debug(f"[predicate] incompatible types in compare: {lhs!r} {op} {rhs!r}")
            return None

    if kind == "present":
        field = spec.get("field")
        if not field:
            return None
        v = _extract(state, field)
        if v is None:
            # explicit None — distinguishes "couldn't tell" from "empty list"
            return None
        # truthy means present (covers numbers, non-empty lists/strings)
        return bool(v)

    if kind == "all":
        results = [evaluate(state, sub) for sub in spec.get("of", [])]
        if any(r is None for r in results):
            return None
        return all(results)

    if kind == "any":
        results = [evaluate(state, sub) for sub in spec.get("of", [])]
        # short-circuit: any True wins even with Nones present;
        # all-False-with-Nones is inconclusive.
        if any(r is True for r in results):
            return True
        if any(r is None for r in results):
            return None
        return False

    if kind == "not":
        sub = evaluate(state, spec.get("of", {}))
        if sub is None:
            return None
        return not sub

    logger.warning(f"[predicate] unknown kind: {kind!r}")
    return None


# ── Goal-file loader ──────────────────────────────────────────────────────

def load_local_predicate(goal_id: str) -> Optional[dict]:
    """Load the local_predicate spec for *goal_id* from disk.  Returns
    None if the goal file is missing or has no `local_predicate` field
    — in that case the caller falls back to Claude heavy_check."""
    path = _GOALS_DIR / f"{goal_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"[predicate] failed to read {path}: {e}")
        return None
    return data.get("local_predicate")


def evaluate_goal(goal_id: str, state: ScreenState) -> Optional[bool]:
    """Convenience: load the predicate for *goal_id*, evaluate against
    *state*.  Returns the predicate result or None if no predicate is
    defined / state is insufficient."""
    spec = load_local_predicate(goal_id)
    if spec is None:
        return None
    return evaluate(state, spec)
