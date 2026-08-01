# brain/states/navigating.py
# Navigating state — the bot has opened the world map and tapped a destination.
# Responsibility: wait for arrival, detect it, then fire arrive_at_port.

from __future__ import annotations

from state.game_state import GameState


def on_enter(game_state: GameState) -> None:
    """Called when the FSM enters the navigating state."""
    return None  # TODO: implement


def tick(game_state: GameState) -> str | None:
    """
    Poll for arrival (OCR port name changed from None to a known port).
    Returns "arrive_at_port" when detected, or None to keep waiting.
    """
    return None  # TODO: implement
