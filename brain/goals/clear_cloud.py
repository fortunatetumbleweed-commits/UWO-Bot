# brain/goals/clear_cloud.py
#
# Coast-exploration goal: depart current port without a destination,
# manually sail (no auto-route to a chosen city) until either:
#   - the lookouts spot a new settlement and an `Enter New City` /
#     `Enter New Village` label appears → tap to enter → discovery
#   - the per-task wall-clock budget expires (default 20 min) →
#     navigate home via the existing SailToGoal flow.
#
# Unlike SailToGoal, this goal NEVER opens the world map to set a
# destination during the discovery phase.  It departs the harbor and
# then drives the ship using the Phase-1 sea-control primitives
# (actions/sea_actions.py: sail_start / sail_stop / turn_left /
# turn_right).  Return-home delegates to SailToGoal so all the
# already-working sail-to-port plumbing is reused.
#
# Per the user's 2026-05-24 plan note:
#   • The PRIMARY discovery signal is the OmniParser-detected text
#     element 'Enter New City' (port) or 'Enter New Village'
#     (village).  When the fleet is close enough to a settlement the
#     game shows this label; tapping it commits entry.
#   • SECONDARY signals — sailor speech bubble 'Wow there is a new
#     port', new rows in the right-panel destinations list — are
#     logged for diagnostics but do not gate the FSM.
#
# Origin: 2026-05-24, clear-cloud Phase 4.

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from loguru import logger


# ── Phases ────────────────────────────────────────────────────────────────

class ClearCloudPhase(Enum):
    INIT             = auto()
    GO_TO_HARBOR     = auto()
    DEPART           = auto()
    WAIT_FOR_SEA     = auto()
    SAILING          = auto()
    ENTERING         = auto()   # tapped Enter New ... — waiting for transition
    AT_NEW_PLACE     = auto()   # confirmed inside a newly-discovered place
    RETURN_HOME      = auto()
    ARRIVED_HOME     = auto()
    FAILED           = auto()


@dataclass
class TickResult:
    action: str
    phase:  ClearCloudPhase
    ok:     bool  = True
    note:   str   = ""
    delay:  float = 0.0


# ── Goal ──────────────────────────────────────────────────────────────────

@dataclass
class ClearCloudGoal:
    """Sail away from a port without a destination, log any new
    settlements that the lookouts spot and the bot manages to enter,
    head home after `duration_minutes`.

    Caller pattern is the same as SailToGoal:
        goal = ClearCloudGoal()
        while not goal.is_complete and not goal.is_failed:
            result = goal.tick()
            time.sleep(result.delay or random.uniform(1.5, 2.5))
    """
    duration_minutes:    int           = 20
    home_port:           Optional[str] = None   # used for RETURN_HOME; resolved on first tick if None
    # ── Internal state (not set by caller) ───────────────────────────
    phase:               ClearCloudPhase = field(default=ClearCloudPhase.INIT, init=False)
    _start_time:         float           = field(default_factory=time.monotonic, init=False)
    _tick_count:         int             = field(default=0, init=False)
    _max_ticks:          int             = field(default=600, init=False)  # ~30 min at 3s/tick
    _last_known_seen:    set             = field(default_factory=set, init=False)
    _new_discoveries:    list            = field(default_factory=list, init=False)
    _return_goal:        Optional[object] = field(default=None, init=False)
    _fail_reason:        str             = field(default="", init=False)
    _last_enter_tap_at:  float           = field(default=0.0, init=False)

    @property
    def is_complete(self) -> bool:
        return self.phase == ClearCloudPhase.ARRIVED_HOME

    @property
    def is_failed(self) -> bool:
        return self.phase == ClearCloudPhase.FAILED

    def mark_failed(self, reason: str) -> None:
        self._fail_reason = reason
        self.phase = ClearCloudPhase.FAILED
        logger.error(f"[clear_cloud] Goal failed: {reason}")
        self._pop_goal_safely()

    def _pop_goal_safely(self) -> None:
        try:
            from brain.goal_context import pop_goal
            pop_goal()
        except Exception:
            pass

    # ── Tick ───────────────────────────────────────────────────────────

    def tick(self) -> TickResult:
        from brain.perceive import perceive
        from capture.adb_capture import capture_screen
        from vision.omniparser import parse_fast_cached

        self._tick_count += 1
        if self._tick_count > self._max_ticks:
            self.mark_failed(f"exceeded {self._max_ticks} ticks")
            return TickResult("timeout", self.phase, ok=False)

        # Push goal context on tick 1 so perceive's Qwen layer can
        # incorporate the task into its reasoning.
        if self._tick_count == 1:
            try:
                from brain.goal_context import push_goal
                push_goal(
                    intent="clear_cloud",
                    target={"duration_minutes": self.duration_minutes},
                )
            except Exception:
                pass

        # PERF: capture ONCE per tick and pass the frame to perceive()
        # so its internal parse_fast_cached hits the cache for any
        # downstream consumer that reuses the same frame.  Previously
        # _find_enter_new_label and _diff_destinations each did their
        # own ADB capture + OmniParser parse, producing 3 captures +
        # 3 parses per tick.  See 2026-05-24 perf note.
        frame = capture_screen()
        try:
            elements = parse_fast_cached(frame)
        except Exception:
            elements = []
        state = perceive(frame)
        elapsed_min = (time.monotonic() - self._start_time) / 60.0
        logger.info(
            f"[clear_cloud] tick={self._tick_count}  phase={self.phase.name}  "
            f"state={state.state!r}  port={state.port!r}  "
            f"elapsed={elapsed_min:.1f}min  discoveries={len(self._new_discoveries)}"
        )

        # ── Always-on triggers (apply regardless of phase) ────────────

        # 1. Look for 'Enter New City' / 'Enter New Village' label.
        # When the lookouts have spotted a settlement and the fleet
        # has closed to entry range, the game shows this label
        # somewhere on the sea screen.  Tapping it enters the place.
        if self.phase in (ClearCloudPhase.SAILING, ClearCloudPhase.WAIT_FOR_SEA):
            enter_target = self._find_enter_new_label(elements)
            if enter_target is not None:
                label, cx, cy = enter_target
                logger.info(
                    f"[clear_cloud] FOUND entry trigger {label!r} @ ({cx}, {cy}) — tapping"
                )
                from actions.adb_actions import tap
                tap(cx, cy)
                self._last_enter_tap_at = time.monotonic()
                self.phase = ClearCloudPhase.ENTERING
                return TickResult(f"tap:{label}", self.phase, delay=4.0)

        # ── Phase dispatch ───────────────────────────────────────────

        if self.phase == ClearCloudPhase.INIT:
            return self._tick_init(state)
        if self.phase == ClearCloudPhase.GO_TO_HARBOR:
            return self._tick_go_to_harbor(state)
        if self.phase == ClearCloudPhase.DEPART:
            return self._tick_depart(state)
        if self.phase == ClearCloudPhase.WAIT_FOR_SEA:
            return self._tick_wait_for_sea(state, elapsed_min)
        if self.phase == ClearCloudPhase.SAILING:
            return self._tick_sailing(state, elapsed_min, frame)
        if self.phase == ClearCloudPhase.ENTERING:
            return self._tick_entering(state)
        if self.phase == ClearCloudPhase.AT_NEW_PLACE:
            return self._tick_at_new_place(state)
        if self.phase == ClearCloudPhase.RETURN_HOME:
            return self._tick_return_home(state)

        # Fallback — shouldn't reach here
        self.mark_failed(f"unhandled phase {self.phase}")
        return TickResult("unhandled", self.phase, ok=False)

    # ── Per-phase logic ────────────────────────────────────────────────

    def _tick_init(self, state) -> TickResult:
        # Resolve home port from current location if not provided.
        if self.home_port is None:
            if state.state == "port_overworld" and state.port:
                self.home_port = state.port
                logger.info(f"[clear_cloud] home_port resolved → {self.home_port!r}")
        if state.state == "port_overworld":
            self.phase = ClearCloudPhase.GO_TO_HARBOR
            return TickResult("init→go_to_harbor", self.phase, delay=1.0)
        if state.state in ("sea", "sea_cinematic"):
            # Already at sea — skip harbor flow.
            logger.info("[clear_cloud] starting at sea; skipping harbor flow")
            self.phase = ClearCloudPhase.SAILING
            return TickResult("init→sailing", self.phase, delay=1.0)
        # In a building or other state — try to recover to overworld.
        logger.warning(
            f"[clear_cloud] INIT in unexpected state {state.state!r} — waiting"
        )
        return TickResult("init_wait", self.phase, delay=3.0)

    def _tick_go_to_harbor(self, state) -> TickResult:
        if state.state == "building":
            # Already in a building — assume it's harbor (or close enough).
            self.phase = ClearCloudPhase.DEPART
            return TickResult("→depart", self.phase, delay=1.0)
        if state.state != "port_overworld":
            logger.warning(
                f"[clear_cloud] GO_TO_HARBOR expected port_overworld, got {state.state!r}"
            )
            return TickResult("waiting_overworld", self.phase, delay=3.0)
        from actions.sail_actions import navigate_to_building
        logger.info("[clear_cloud] navigating to harbor")
        try:
            navigate_to_building("harbor")
        except Exception as e:
            logger.warning(f"[clear_cloud] navigate_to_building raised: {e}")
        # The next tick will re-perceive; if we're in a building (likely
        # harbor) the dispatch above advances to DEPART.
        return TickResult("nav_harbor", self.phase, delay=2.0)

    def _tick_depart(self, state) -> TickResult:
        # Reuse SailToGoal's depart action (it taps Depart Now / Supply
        # Departure without requiring a destination to be set).
        from actions.sail_actions import _depart_from_harbour
        logger.info("[clear_cloud] tapping Depart Now")
        try:
            _depart_from_harbour()
        except Exception as e:
            logger.warning(f"[clear_cloud] _depart_from_harbour raised: {e}")
        self.phase = ClearCloudPhase.WAIT_FOR_SEA
        return TickResult("depart", self.phase, delay=5.0)

    def _tick_wait_for_sea(self, state, elapsed_min: float) -> TickResult:
        if state.state in ("sea", "sea_cinematic"):
            self.phase = ClearCloudPhase.SAILING
            return TickResult("→sailing", self.phase, delay=1.0)
        # If we somehow ended up back at port_overworld, retry.
        if state.state == "port_overworld":
            logger.warning("[clear_cloud] back on port_overworld after depart — retrying")
            self.phase = ClearCloudPhase.GO_TO_HARBOR
            return TickResult("retry_harbor", self.phase, delay=2.0)
        return TickResult("waiting_sea", self.phase, delay=3.0)

    def _tick_sailing(self, state, elapsed_min: float, frame) -> TickResult:
        # Time's up?
        if elapsed_min >= self.duration_minutes:
            logger.info(
                f"[clear_cloud] {self.duration_minutes}-min budget exhausted — "
                f"heading home  (discoveries this run: {len(self._new_discoveries)})"
            )
            self.phase = ClearCloudPhase.RETURN_HOME
            return TickResult("→return_home", self.phase, delay=1.0)

        # Refresh log of currently-seen destinations (secondary signal).
        # Pass the already-captured frame to avoid a redundant ADB capture.
        self._diff_destinations(frame)

        # Ensure we're moving forward.
        from actions.sea_actions import sail_start, is_ship_moving
        moving = is_ship_moving()
        if moving is False:
            r = sail_start()
            logger.info(f"[clear_cloud] sail_start: {r.detail}")
        elif moving is None:
            logger.debug("[clear_cloud] ship-movement state unknown")

        return TickResult("sailing", self.phase, delay=8.0)

    def _tick_entering(self, state) -> TickResult:
        # After tapping Enter New City/Village, the game loads the
        # newly-discovered place.  Wait until perceive() reports a
        # location-style state (port_overworld or village).
        if state.state in ("port_overworld", "village"):
            name = state.port or "(unnamed)"
            self._new_discoveries.append({
                "name":  name,
                "type":  "village" if state.state == "village" else "port",
                "time":  time.monotonic() - self._start_time,
            })
            logger.info(
                f"[clear_cloud] ✅ discovered {name!r} ({state.state}) — "
                f"total this run: {len(self._new_discoveries)}"
            )
            self.phase = ClearCloudPhase.AT_NEW_PLACE
            return TickResult("entered", self.phase, delay=2.0)
        # Still in transition — wait.
        if time.monotonic() - self._last_enter_tap_at > 60.0:
            logger.warning(
                "[clear_cloud] no transition after Enter tap (60s) — back to sailing"
            )
            self.phase = ClearCloudPhase.SAILING
        return TickResult("entering_wait", self.phase, delay=3.0)

    def _tick_at_new_place(self, state) -> TickResult:
        # Per user note: success criterion is just entering the place.
        # We don't go inside buildings here — just leave back to sea
        # so the goal can continue exploring.
        if state.state == "village":
            from actions.adb_actions import press_back
            logger.info("[clear_cloud] leaving village back to sea")
            press_back()
            self.phase = ClearCloudPhase.WAIT_FOR_SEA
            return TickResult("leave_village", self.phase, delay=3.0)
        if state.state == "port_overworld":
            # Need to navigate to harbor → depart again.
            self.phase = ClearCloudPhase.GO_TO_HARBOR
            return TickResult("leave_port", self.phase, delay=2.0)
        return TickResult("at_new_wait", self.phase, delay=3.0)

    def _tick_return_home(self, state) -> TickResult:
        # Delegate to SailToGoal for the home leg — it knows how to
        # open the world map, select a destination, and ride out the
        # arrival.  We pick the home port resolved at INIT (or fall
        # back to the nearest entry in the destinations panel).
        if self._return_goal is None:
            from brain.goals.sail_to import SailToGoal
            home = self.home_port or self._nearest_known_port(state)
            if not home:
                self.mark_failed("RETURN_HOME has no known destination")
                return TickResult("no_home", self.phase, ok=False)
            logger.info(f"[clear_cloud] delegating return to SailToGoal('{home}')")
            self._return_goal = SailToGoal(destination=home)

        rg = self._return_goal
        result = rg.tick()
        if rg.is_complete:
            self.phase = ClearCloudPhase.ARRIVED_HOME
            self._pop_goal_safely()
            return TickResult("home", self.phase)
        if rg.is_failed:
            self.mark_failed(f"return-home SailToGoal failed: {rg._fail_reason}")
            return TickResult("home_failed", self.phase, ok=False)
        return TickResult(f"return_home:{result.action}", self.phase,
                          delay=result.delay or 2.0)

    # ── Helpers ────────────────────────────────────────────────────────

    def _find_enter_new_label(self, elements):
        """Scan pre-parsed OmniParser elements for the 'Enter New
        City/Village' label.

        PERF: takes pre-parsed `elements` from the tick's shared frame
        instead of capturing + parsing again.  Was the third of three
        OmniParser passes per tick; now zero extra passes.

        Returns (label_text, cx, cy) or None.
        """
        if not elements:
            return None
        for e in elements:
            if not getattr(e, "label", None):
                continue
            text = e.label.strip().lower()
            if any(k in text for k in (
                "enter new city",
                "enter new village",
                "enter new town",
                "enter new port",
                "enter village",  # in case of OCR drop
            )):
                return (e.label.strip(), e.cx, e.cy)
        return None

    def _diff_destinations(self, frame) -> None:
        """Log new entries that appeared in the right-panel list.

        PERF: takes the tick's shared *frame* — read_destinations
        internally calls parse_fast_cached, which is a cache hit on
        the same frame object (no second OmniParser pass).  Was the
        second of three OmniParser passes per tick; now zero extra.

        Best-effort.  destinations_panel reader is v1 and noisy; this
        is purely informational for now.
        """
        try:
            from vision.destinations_panel import read_destinations, names
            rows = read_destinations(frame)
            current = set(names(rows))
            if not current:
                return
            new = current - self._last_known_seen
            if self._last_known_seen and new:
                # only after first tick has populated baseline
                filtered = [n for n in new if len(n) > 2 and any(c.isalpha() for c in n)]
                if filtered:
                    logger.info(
                        f"[clear_cloud] destinations panel: new entries {filtered}"
                    )
            self._last_known_seen.update(current)
        except Exception as e:
            logger.debug(f"[clear_cloud] destinations diff failed: {e}")

    def _nearest_known_port(self, state) -> Optional[str]:
        """Fallback for home_port: take the first entry of the
        destinations panel.  Returns None if nothing parseable.

        This is on the cold path (RETURN_HOME initialisation, called
        once per goal lifetime), so the extra capture is fine.  The
        per-tick helpers above all reuse the shared frame instead.
        """
        try:
            from capture.adb_capture import capture_screen
            from vision.destinations_panel import read_destinations
            rows = read_destinations(capture_screen())
            for r in rows:
                # Filter chrome noise
                if len(r.name) > 2 and any(c.isalpha() for c in r.name):
                    return r.name
        except Exception:
            pass
        return None
