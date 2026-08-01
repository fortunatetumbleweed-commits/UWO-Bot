# brain/states/trading.py
# Trading state — the bot is interacting with the market at a port.
# Responsibility: read prices, decide buy/sell, execute, then fire finish_trade.

from __future__ import annotations

from state.game_state import GameState


def on_enter(game_state: GameState) -> None:
    """Called when the FSM enters the trading state."""
    return None  # TODO: implement


def tick(game_state: GameState) -> str | None:
    """
    Step through the buy/sell flow.
    Returns "finish_trade" when the trading session is complete, or None to continue.
    """
    return None  # TODO: implement
