"""GoalContext stack — Phase B1 of the goal-aware perception redesign.

A `GoalContext` records what the bot is currently trying to achieve.
The stack lets nested intents compose (depart → recruit crew → tap
Hire) and lets perception layers read the active intent to interpret
ambiguous screens.

This module provides ONLY the slot.  No consumer is wired yet —
Phase B2 (LLM consult) and Phase C (re-check goal after action) will
read it.  Tasks and goals can begin pushing contexts immediately;
unread contexts cost nothing.

Origin: 2026-05-15.  Replaces the narrower "current_blocker"
notion from the structural-interruptor design with a richer goal
record that carries enough information to interpret a screen
(NPC overlay, dialog, popup) in the light of what the bot was
trying to do — not just whether something happens to be in the way.

Thread safety: the stack is module-level state, intended for the
single-threaded agent main loop.  Background threads (Claude
analysis, learning hooks) MUST NOT push/pop — they should snapshot
`current_goal()` at start and use the immutable record.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional


@dataclass(frozen=True)
class GoalContext:
    """Immutable record describing the bot's current intent.

    Fields:
        intent:           Short verb-noun describing the goal — e.g.
                          'set_sail', 'recruit_crew', 'sell_all_cargo',
                          'negotiate_price', 'explore_port'.
        target:           Goal-specific payload (port name, goods list,
                          building, threshold values, etc.).  Free-form
                          dict; consumers know which keys apply.
        success_signals:  Names of predicates that, when all true,
                          mean the goal is complete.  e.g.
                          ['cargo_empty', 'ducats_increased'].
        progress_signals: Names of observable mid-flow events that
                          indicate the goal is progressing but not
                          yet complete.  e.g. ['sell_panel_open',
                          'good_selected'].  Consumers can use these
                          to keep retry loops moving instead of
                          collapsing on the first non-success tick.
        parent:           The GoalContext that pushed this one, if
                          any.  Forms an implicit stack via linked
                          list — supports `set_sail` pushing
                          `recruit_crew` as a recovery sub-goal that
                          pops back when complete.

    Frozen so consumers (e.g. a Claude analysis thread) can snapshot
    a reference at start-of-tick and trust it won't mutate
    underneath them.
    """
    intent:           str
    target:           Dict[str, Any] = field(default_factory=dict)
    success_signals:  tuple = ()
    progress_signals: tuple = ()
    parent:           Optional["GoalContext"] = None

    def chain(self) -> List["GoalContext"]:
        """Return the goal chain from this context up to the root —
        useful for logging and for handing context to an LLM."""
        out: List[GoalContext] = []
        cur: Optional[GoalContext] = self
        while cur is not None:
            out.append(cur)
            cur = cur.parent
        return out

    def summary(self) -> str:
        """One-line representation for logs.

        Format: 'sell_all_cargo @ Lisbon → set_sail @ Lisbon'
        (innermost goal first, parents follow with ' → ').
        """
        parts: List[str] = []
        for g in self.chain():
            tgt = ""
            for key in ("port", "destination", "building", "goods"):
                if key in g.target:
                    tgt = f" @ {g.target[key]}"
                    break
            parts.append(f"{g.intent}{tgt}")
        return " → ".join(parts)


# ── Module-level stack ─────────────────────────────────────────────────────


_stack: List[GoalContext] = []


def push_goal(
    intent: str,
    target: Optional[Dict[str, Any]] = None,
    success_signals: Optional[List[str]] = None,
    progress_signals: Optional[List[str]] = None,
) -> GoalContext:
    """Push a new GoalContext, linked to the current top as its
    parent.  Returns the new context."""
    parent = _stack[-1] if _stack else None
    goal = GoalContext(
        intent=intent,
        target=dict(target or {}),
        success_signals=tuple(success_signals or ()),
        progress_signals=tuple(progress_signals or ()),
        parent=parent,
    )
    _stack.append(goal)
    return goal


def pop_goal() -> Optional[GoalContext]:
    """Pop the top GoalContext.  Returns it, or None if the stack
    was already empty (no-op)."""
    if not _stack:
        return None
    return _stack.pop()


def current_goal() -> Optional[GoalContext]:
    """Return the active GoalContext (top of stack), or None when
    the stack is empty."""
    if not _stack:
        return None
    return _stack[-1]


def goal_stack() -> List[GoalContext]:
    """Return a snapshot of the full stack, top-most last.  Mostly
    for tests and diagnostics."""
    return list(_stack)


def clear_goal_stack() -> None:
    """Reset to empty.  Use sparingly — mainly between independent
    task runs to avoid stale state leaking."""
    _stack.clear()


# ── Context manager ───────────────────────────────────────────────────────


@contextmanager
def goal(
    intent: str,
    target: Optional[Dict[str, Any]] = None,
    success_signals: Optional[List[str]] = None,
    progress_signals: Optional[List[str]] = None,
) -> Iterator[GoalContext]:
    """Push a GoalContext for the duration of a `with` block; pop on
    exit (including on exception).  The preferred way to bracket
    action chains so the stack stays balanced even if recovery
    code raises.

    Example:
        with goal("set_sail", target={"destination": "Lisbon"},
                  success_signals=["at_sea"]):
            sail_to_port("Lisbon")
            # current_goal() reads "set_sail" throughout
    """
    ctx = push_goal(intent, target, success_signals, progress_signals)
    try:
        yield ctx
    finally:
        # Defensive: only pop our own context.  If something else
        # popped above us, restore the stack rather than corrupting
        # it further.
        if _stack and _stack[-1] is ctx:
            _stack.pop()
