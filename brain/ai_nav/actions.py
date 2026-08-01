"""Action layer — symmetric counterpart to VisionSource.

The pipeline owns perception + planning.  Action firing is delegated
to a swappable `ActionLayer` so the same pipeline can drive:

  - ADB taps against the real game (`AdbActionLayer`)
  - a dry-run logger (`NoOpActionLayer`, useful for replay + tests)
  - a simulator (future, when the closed-loop sim adds an action sink)

Keeping the abstraction tiny — three primitives — is deliberate.
The pipeline emits "hold_left" / "hold_right" with a duration; the
ActionLayer decides how to realise that.  No other layer should
ever need to know about ADB.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional, Protocol

log = logging.getLogger(__name__)


@dataclass
class ActionResult:
    """Outcome of one action firing.  Mirrors `actions.sea_actions.SeaActionResult`
    so existing tools can keep their schema."""
    ok: bool
    action: str
    detail: str = ""
    icon_state: Optional[str] = None


class ActionLayer(Protocol):
    """Three primitives are enough for sea navigation.  Add more
    methods only when a new layer genuinely needs them."""

    def sail_start(self) -> ActionResult: ...
    def sail_stop(self) -> ActionResult: ...
    def fire(self, command: str, hold_ms: int) -> ActionResult:
        """Execute a steering command.  Valid commands:
        'hold_left' | 'hold_right' | None / '' (no-op)."""
        ...


class AdbActionLayer:
    """Default — drives the real game via actions.sea_actions."""
    name = "adb"

    def sail_start(self) -> ActionResult:
        from actions.sea_actions import sail_start
        r = sail_start()
        return ActionResult(r.ok, r.action, r.detail, r.icon_state)

    def sail_stop(self) -> ActionResult:
        from actions.sea_actions import sail_stop
        r = sail_stop()
        return ActionResult(r.ok, r.action, r.detail, r.icon_state)

    def fire(self, command: str, hold_ms: int) -> ActionResult:
        if not command or hold_ms <= 0:
            return ActionResult(True, "noop", "no command issued")
        from actions.sea_actions import hold_left, hold_right
        if command == "hold_left":
            r = hold_left(hold_ms)
        elif command == "hold_right":
            r = hold_right(hold_ms)
        else:
            return ActionResult(False, command, f"unknown command")
        return ActionResult(r.ok, r.action, r.detail, r.icon_state)


class NoOpActionLayer:
    """Logs the action that *would* be fired but doesn't touch ADB.
    Used for tests, dry-run replays, and offline trace generation."""
    name = "noop"

    def __init__(self, sleep_for_real: bool = False):
        # When True, sleeps for hold_ms so the runner's timing matches a
        # live run.  Default False — fast for tests/replays.
        self.sleep_for_real = sleep_for_real
        self.history: list[tuple[str, int, ActionResult]] = []

    def sail_start(self) -> ActionResult:
        r = ActionResult(True, "sail_start", "noop")
        self.history.append(("sail_start", 0, r))
        log.info("[noop] sail_start")
        return r

    def sail_stop(self) -> ActionResult:
        r = ActionResult(True, "sail_stop", "noop")
        self.history.append(("sail_stop", 0, r))
        log.info("[noop] sail_stop")
        return r

    def fire(self, command: str, hold_ms: int) -> ActionResult:
        if not command or hold_ms <= 0:
            r = ActionResult(True, "noop", "no command")
            self.history.append((command or "", hold_ms, r))
            return r
        if self.sleep_for_real:
            time.sleep(hold_ms / 1000.0)
        r = ActionResult(True, command, "noop")
        self.history.append((command, hold_ms, r))
        log.info("[noop] %s @ %dms", command, hold_ms)
        return r
