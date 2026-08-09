"""Reasoning layer — the LLM 'infer the next action' operator + the trace microscope.

Assembles the context the LLM reasons over (GOAL + world model + structured
perception + game-knowledge primer + action vocabulary), optionally calls an LLM,
and **logs a reasoning-trace** of exactly what went in and what came out. Build
this first: the trace log is the microscope that tells you what knowledge/
perception the LLM still lacks (docs/reasoning_fallback_layer_design.md §12).

Deliberately minimal: the LLM call is a pluggable `llm_fn`, `shadow=True` logs
without acting, and the prompt wording / game primer are meant to be **iterated
from the traces**, not perfected up front.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, List, Optional

from loguru import logger

# Whitelisted next-action ops — the LLM must pick from these (they map 1:1 to
# existing primitives). No free-form taps. See reasoning doc §5.
ACTION_OPS = frozenset({
    "go_to_building", "exit_building", "tap", "back", "wait", "abort",
})

_TRACE_PATH = Path("memory/knowledge/reasoning_traces/traces.jsonl")


@dataclass
class ReasoningContext:
    goal:           str
    world_model:    str                       # WorldModel.to_prompt()
    perception:     str                       # structured PerceivedState, as text
    game_knowledge: str = ""                  # compact primer (iterated from traces)
    action_vocab:   List[str] = field(default_factory=lambda: sorted(ACTION_OPS))

    def to_prompt(self) -> str:
        return (
            f"GOAL:\n  {self.goal}\n\n"
            f"WORLD MODEL:\n{self.world_model}\n\n"
            f"OBSERVED (current screen):\n{self.perception}\n\n"
            f"GAME KNOWLEDGE:\n{self.game_knowledge or '(none provided)'}\n\n"
            f"ALLOWED ACTIONS (pick exactly one; reply as JSON "
            f'{{\"op\": <one of these>, \"arg\": <string or null>, '
            f'\"why\": <short>}}):\n  {", ".join(self.action_vocab)}\n'
        )


def parse_action(raw: str) -> Optional[dict]:
    """Parse the LLM reply into a validated {op, arg, why}; None if invalid.

    Tolerant of prose around the JSON (grabs the first {...} block).
    """
    if not raw:
        return None
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        obj = json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError):
        return None
    op = (obj.get("op") or "").strip()
    if op not in ACTION_OPS:
        return None
    return {"op": op, "arg": obj.get("arg"), "why": obj.get("why", "")}


def log_trace(record: dict, path: Path = _TRACE_PATH) -> None:
    """Append one reasoning-trace (JSONL). The microscope — logs the full context
    sent and the response, so gaps (knowledge vs perception) are diagnosable."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.debug(f"[reasoning] trace log failed: {exc}")


def reason(
    ctx: ReasoningContext,
    llm_fn: Optional[Callable[[str], str]] = None,
    *,
    trigger: str = "unknown",
    shadow: bool = True,
    trace_path: Path = _TRACE_PATH,
) -> Optional[dict]:
    """Assemble context → ask the LLM → parse → LOG the trace; return the action.

    ALWAYS calls the LLM (when `llm_fn` is given) so the trace captures the model's
    decision. `shadow` is recorded on the trace and signals the *caller/loop* not
    to ACT on the returned action — it does NOT suppress the LLM call (that's how
    shadow runs gather "what would it do" evidence).
    """
    prompt = ctx.to_prompt()
    raw, action = None, None
    if llm_fn is not None:
        try:
            raw = llm_fn(prompt)
            action = parse_action(raw)
        except Exception as exc:
            logger.debug(f"[reasoning] llm_fn failed: {exc}")

    log_trace({
        "trigger":       trigger,
        "goal":          ctx.goal,
        "context_sent":  asdict(ctx),
        "llm_response":  raw,
        "parsed_action": action,
        "shadow":        shadow,
    }, path=trace_path)
    return action
