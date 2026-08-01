# brain/states/docked.py
# Docked state — the bot has arrived at a port and must decide what to do next.
# Responsibility: choose between trading at this port or navigating to another.

from __future__ import annotations

from state.game_state import GameState


def on_enter(game_state: GameState) -> None:
    """Called when the FSM enters the docked state."""
    return None  # TODO: implement


def tick(game_state: GameState) -> str | None:
    """
    Decide next action.
    Returns "start_trade", "start_navigation", or None to wait another tick.
    """
    return None  # TODO: implement
