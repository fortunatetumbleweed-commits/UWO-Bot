# brain/states/idle.py
# Idle state — entered at startup or after an unexpected reset.
# Responsibility: orient the bot (figure out where we are) and transition out.

from __future__ import annotations

from state.game_state import GameState


def on_enter(game_state: GameState) -> None:
    """Called when the FSM enters the idle state."""
    raise NotImplementedError


def tick(game_state: GameState) -> str | None:
    """
    Evaluate the current game state and return a trigger name to fire,
    or None to stay in idle another tick.
    """
    return None  # TODO: implement
