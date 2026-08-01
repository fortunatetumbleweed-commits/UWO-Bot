# brain/states/entering_building.py
# Entering building state — proximity dialog is visible, bot taps it to enter.
# Responsibility: tap the dialog and wait for the screen to transition inside.

from __future__ import annotations

from state.game_state import GameState


def on_enter(game_state: GameState) -> None:
    """Called when the FSM enters the entering_building state."""
    return None  # TODO: implement


def tick(game_state: GameState) -> str | None:
    """
    Tap the proximity dialog if still visible, then watch for the interior
    screen to appear. Returns "entered_building" on success, None to keep waiting.
    """
    return None  # TODO: implement
