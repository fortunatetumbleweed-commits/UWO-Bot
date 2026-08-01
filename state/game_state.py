# state/game_state.py
# Single source of truth for everything the bot knows about the current game state.
# Updated from vision output each tick before the FSM makes a decision.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CargoItem:
    name: str
    quantity: int
    buy_price: Optional[int] = None   # price paid (ducats)


@dataclass
class GameState:
    # Location
    current_port: Optional[str] = None          # OCR'd port name; None when at sea
    destination_port: Optional[str] = None      # target port the bot is sailing toward
    at_sea: bool = False

    # Resources
    ducats: int = 0
    cargo: list[CargoItem] = field(default_factory=list)
    cargo_capacity: int = 0

    # Fleet
    fleet_hp: int = 100                          # percentage, 0–100

    # Building context
    current_building: Optional[str] = None      # "harbor", "market", "shipyard", "inn", etc.
    building_dialog_visible: bool = False        # proximity dialog showing in overworld
    building_dialog_title: Optional[str] = None # title text from the proximity dialog

    # Screen context — set by screen_classifier each tick
    current_screen: Optional[str] = None   # e.g. "port_overworld", "market", "harbor", None = unknown

    # UI context
    map_open: bool = False
    market_open: bool = False
    dialog_open: bool = False

    # Meta
    tick: int = 0                                # incremented each capture cycle

    def update_tick(self) -> None:
        self.tick += 1
