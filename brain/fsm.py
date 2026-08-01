# brain/fsm.py
# Core finite state machine using the `transitions` library.
# Each state's logic lives in its own file under brain/states/.

from __future__ import annotations

from transitions import Machine

from state.game_state import GameState
from brain.states import idle, docked, entering_building, in_building, navigating, trading


STATES = ["idle", "docked", "entering_building", "in_building", "navigating", "trading"]

TRANSITIONS = [
    # source               trigger                   dest
    {"source": "idle",             "trigger": "arrive_at_port",    "dest": "docked"},
    {"source": "docked",           "trigger": "approach_building", "dest": "entering_building"},
    {"source": "docked",           "trigger": "start_navigation",  "dest": "navigating"},
    {"source": "entering_building","trigger": "entered_building",  "dest": "in_building"},
    {"source": "in_building",      "trigger": "start_trade",       "dest": "trading"},
    {"source": "in_building",      "trigger": "exit_building",     "dest": "docked"},
    {"source": "trading",          "trigger": "finish_trade",      "dest": "in_building"},
    {"source": "navigating",       "trigger": "arrive_at_port",    "dest": "docked"},
    {"source": "*",                "trigger": "reset",             "dest": "idle"},
]


class BotFSM:
    """Wraps the transitions Machine and exposes a single tick() method."""

    def __init__(self, game_state: GameState) -> None:
        self.game_state = game_state
        self.machine = Machine(
            model=self,
            states=STATES,
            transitions=TRANSITIONS,
            initial="idle",
            auto_transitions=False,
        )

    def tick(self) -> None:
        """Called once per capture cycle — dispatch to the current state module."""
        _dispatch = {
            "idle":              idle,
            "docked":            docked,
            "entering_building": entering_building,
            "in_building":       in_building,
            "navigating":        navigating,
            "trading":           trading,
        }
        module = _dispatch.get(self.state)
        if module is None:
            return

        trigger = module.tick(self.game_state)
        if trigger:
            self.trigger(trigger)
