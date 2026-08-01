"""Layer 5 — slow strategic replanner.

Fires on goal failure or on a much slower heartbeat (~60 s).  Used
for open-ended questions Layer 4 can't handle:

  - "The bot has been stuck near (lat, lon) for 60 ticks.  Here's
    the minimap + recent trace.  Write a Python primitive to
    handle this."
  - "Voyage from A to B was supposed to take 500 ticks; we're at
    800.  What went wrong?"

Output is a `StrategicDirective`: a new commit_heading, a new
target, an abort, or (Voyager-style) a new Python skill saved into
`learned_recoveries.json`.

Pluggable: today's default is a no-op; the real impl calls the
Claude API.  Aligned with the existing `brain/recovery.py` Claude
consult path — eventually this layer becomes the navigation hook
into that subsystem.
"""
from __future__ import annotations

import time
from typing import Optional, Protocol

from brain.ai_nav.state import NavState, StrategicDirective
from brain.ai_nav.vision_input import VisionFrame


class StrategicLayer(Protocol):
    name: str

    def maybe_replan(self, frame: VisionFrame, state: NavState
                     ) -> NavState: ...


class NoOpStrategic:
    name = "noop"

    def maybe_replan(self, frame, state):
        return state


class BaseStrategic:
    """Provides cadence + trigger gate.  Subclasses implement
    `_replan(frame, state) -> StrategicDirective | None`.
    """

    def __init__(self, heartbeat_sec: float = 60.0):
        self.heartbeat_sec = heartbeat_sec
        self._last_fire_ts: float = 0.0

    def _should_fire(self, frame: VisionFrame, state: NavState) -> Optional[str]:
        if (frame.wall_ts - self._last_fire_ts) >= self.heartbeat_sec:
            return "heartbeat"
        # TODO: failure triggers — extend when goal-failure protocol
        # is wired into NavState.
        return None

    def maybe_replan(self, frame: VisionFrame, state: NavState) -> NavState:
        reason = self._should_fire(frame, state)
        if reason is None:
            return state
        self._last_fire_ts = frame.wall_ts
        directive = self._replan(frame, state)
        if directive is None:
            return state
        state.strategic = directive
        state.strategic_history.append(directive)
        return state

    def _replan(self, frame, state):
        raise NotImplementedError


# ── Loop detector: trajectory-based stuck recovery ─────────────────────


class LoopDetectorStrategic:
    """Watches the recent (lat, lon) trajectory and fires a commit-
    override when the bot has been bouncing around the same region
    without escaping.

    Trigger: bounding-box diagonal of the last `window_ticks` positions
    is below `stuck_diag_km` AND we've had ≥ window_ticks of data.

    On trigger: rotate commit_direction by `rotation_deg` (default
    +90°) for `breakout_ticks` ticks, then release.  During breakout
    we don't re-fire.

    Cooldown: after breakout ends, wait `cooldown_ticks` before
    re-arming so the bot has a chance to actually escape before we
    re-evaluate.

    Mission layer continues to manage commit_direction via the
    junction graph; the strategic override takes precedence while
    active.  When the override releases, the mission's next update
    reasserts whatever it thinks the commit should be.
    """
    name = "loop_detector"

    def __init__(
        self,
        window_ticks: int = 50,
        stuck_diag_km: float = 25.0,
        rotation_deg: float = 90.0,
        breakout_ticks: int = 30,
        cooldown_ticks: int = 50,
    ):
        from collections import deque
        self.window_ticks = window_ticks
        self.stuck_diag_km = stuck_diag_km
        self.rotation_deg = rotation_deg
        self.breakout_ticks = breakout_ticks
        self.cooldown_ticks = cooldown_ticks
        self._trail = deque(maxlen=window_ticks)
        self._breakout_until_tick: int = -1
        self._cooldown_until_tick: int = -1
        self._breakout_commit: Optional[float] = None

    def _diag_km(self) -> float:
        if len(self._trail) < 2:
            return 0.0
        lats = [p[0] for p in self._trail]
        lons = [p[1] for p in self._trail]
        import math
        cos_lat = math.cos(math.radians(sum(lats) / len(lats)))
        d_lat = (max(lats) - min(lats)) * 111.0
        d_lon = (max(lons) - min(lons)) * 111.0 * cos_lat
        return math.hypot(d_lat, d_lon)

    def maybe_replan(self, frame: VisionFrame, state: NavState) -> NavState:
        if state.lat is None or state.lon is None:
            return state
        self._trail.append((state.lat, state.lon, state.tick))

        # Active breakout: hold the rotated commit until breakout window
        # expires.  Mission layer will write commit each tick; we
        # override.
        if state.tick < self._breakout_until_tick:
            if state.commit_direction is None or \
                    abs(state.commit_direction.bearing_deg
                        - self._breakout_commit) > 1.0:
                from brain.ai_nav.state import CommitDirection
                state.commit_direction = CommitDirection(
                    bearing_deg=self._breakout_commit,
                    reason="loop_detector_breakout",
                    set_at_tick=state.tick,
                )
            return state

        # Just ended breakout? Clear so mission can resume.
        if state.tick == self._breakout_until_tick:
            self._breakout_commit = None

        # Cooldown: don't re-evaluate.
        if state.tick < self._cooldown_until_tick:
            return state

        # Need a full window to assess.
        if len(self._trail) < self.window_ticks:
            return state

        diag = self._diag_km()
        if diag >= self.stuck_diag_km:
            return state

        # Stuck — fire breakout.
        current_commit = (state.commit_direction.bearing_deg
                         if state.commit_direction is not None else 180.0)
        rotated = (current_commit + self.rotation_deg) % 360.0
        self._breakout_commit = rotated
        self._breakout_until_tick = state.tick + self.breakout_ticks
        self._cooldown_until_tick = (
            state.tick + self.breakout_ticks + self.cooldown_ticks
        )
        state.strategic = StrategicDirective(
            kind="loop_breakout",
            payload={
                "rotated_to": rotated,
                "from": current_commit,
                "diag_km": round(diag, 2),
                "breakout_ticks": self.breakout_ticks,
            },
            decided_at_tick=state.tick,
        )
        state.strategic_history.append(state.strategic)
        from brain.ai_nav.state import CommitDirection
        state.commit_direction = CommitDirection(
            bearing_deg=rotated,
            reason="loop_detector_breakout",
            set_at_tick=state.tick,
        )
        return state


# ── Stub: Claude API replanner ─────────────────────────────────────────


class ClaudeStrategic(BaseStrategic):
    """Calls the Claude API on the slow heartbeat.  Reuses the
    existing Claude consult substrate in `brain/recovery.py`.

    Reads `frame.full_screen()` plus the recent trace + map context.
    Output parsed into a StrategicDirective.

    Not implemented yet.
    """
    name = "claude_strategic"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        raise NotImplementedError(
            "ClaudeStrategic not implemented yet — see docs/ai_navigation_landscape.md §5.5"
        )

    def _replan(self, frame, state):
        raise NotImplementedError
