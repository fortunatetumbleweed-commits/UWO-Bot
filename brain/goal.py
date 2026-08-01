# brain/goal.py
# Goal representation for the agent.
#
# Goals are hierarchical — the agent pushes sub-goals onto the stack to
# break down complex tasks. Completing a sub-goal pops back to the parent.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Goal:
    """A single entry in the agent's goal stack."""

    type: str                              # explore_port, go_to_building, learn_screen,
                                           # sail_to, trade, collect_reward, idle, …
    target: str = ""                       # building name, port name, etc.
    context: dict[str, Any] = field(default_factory=dict)
    description: str = ""                  # human-readable summary

    def __str__(self) -> str:
        if self.target:
            return f"{self.type}({self.target!r})"
        return self.type

    # ── factory helpers ───────────────────────────────────────────────────────

    @classmethod
    def explore_port(cls, port: str) -> "Goal":
        return cls(
            type="explore_port",
            target=port,
            description=f"Visit every building in {port} and learn what each one does",
        )

    @classmethod
    def go_to_building(cls, building: str, port: str = "") -> "Goal":
        return cls(
            type="go_to_building",
            target=building,
            context={"port": port},
            description=f"Navigate to the {building}",
        )

    @classmethod
    def learn_screen(cls, screen_title: str) -> "Goal":
        return cls(
            type="learn_screen",
            target=screen_title,
            description=f"Understand what the '{screen_title}' screen offers",
        )

    @classmethod
    def sail_to(cls, destination: str) -> "Goal":
        return cls(
            type="sail_to",
            target=destination,
            description=f"Sail to {destination}",
        )

    @classmethod
    def idle(cls) -> "Goal":
        """Default goal when nothing specific is queued — observe and learn."""
        return cls(
            type="idle",
            description="Observe current screen and learn anything useful",
        )


class GoalStack:
    """
    LIFO stack of goals. The top is the current active goal.
    Sub-goals are pushed on top; completing them pops back to the parent.
    """

    def __init__(self) -> None:
        self._stack: list[Goal] = []

    def push(self, goal: Goal) -> None:
        self._stack.append(goal)

    def pop(self) -> Goal | None:
        return self._stack.pop() if self._stack else None

    def current(self) -> Goal | None:
        return self._stack[-1] if self._stack else None

    def is_empty(self) -> bool:
        return not self._stack

    def __len__(self) -> int:
        return len(self._stack)

    def __repr__(self) -> str:
        if not self._stack:
            return "(empty)"
        return " → ".join(str(g) for g in reversed(self._stack))
