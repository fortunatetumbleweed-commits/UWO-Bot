# brain/goals/sail_to.py
#
# Goal-driven sailing: perceive → dispatch → one action → loop.
#
# Replaces the sequential sail_to_port() with a tick-driven state machine.
# Each tick calls perceive() for ground truth, then dispatches one action
# based on (current_state, goal_phase).  Phase transitions are driven by
# perceive() results, not by sequential function calls.
#
# Migration step 1: phase-level actions delegate to existing functions
# in sail_actions.py (which still have internal loops).  The improvement
# is at PHASE BOUNDARIES — between each phase, perceive() re-derives
# ground truth, dismisses interruptors, and handles unexpected states.
#
# Usage (in task_runner):
#     goal = SailToGoal(destination="Aceh", from_port="Ceylon")
#     while not goal.is_complete and not goal.is_failed:
#         result = goal.tick()
#         time.sleep(result.delay or random.uniform(5, 10))

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from loguru import logger


# Stall detection: only re-navigate when the ship is CONFIRMED not moving — speed reads 0 AND the
# lat/lon hasn't advanced over a tick.  `_STALL_POS_EPS` is the min |Δlat|+|Δlon| (degrees) over a
# ~20s tick that counts as "moving"; below it (with speed 0) the ship is treated as stopped.
_STALL_POS_EPS = 0.02
_STALL_TICKS = 2


# ── Phase enum ───────────────────────────────────────────────────────────────

class SailPhase(Enum):
    """Sailing goal phases — tracked for logging, not for control flow.

    The dispatch table trusts perceive() state, not phase.  Phase records
    where the goal *thinks* it is so that ambiguous states can be resolved
    and failure tracking works correctly.
    """
    INIT           = auto()
    EXIT_BUILDING  = auto()
    GO_TO_HARBOR   = auto()
    FLEET_CHECK    = auto()
    DEPART         = auto()
    SEA_NAVIGATE   = auto()   # at sea, need to open world map
    WORLD_MAP      = auto()   # on world map, need to select destination
    SAILING        = auto()   # destination selected, waiting for arrival
    ARRIVED        = auto()
    FAILED         = auto()


# ── Tick result ──────────────────────────────────────────────────────────────

@dataclass
class TickResult:
    """Result of one tick of the sail_to goal."""
    action: str
    phase:  SailPhase
    ok:     bool  = True
    note:   str   = ""
    delay:  float = 0.0    # suggested extra delay (0 = use default 5-10s)


# ── SailToGoal ───────────────────────────────────────────────────────────────

@dataclass
class SailToGoal:
    """
    Goal-driven sailing from current location to destination port.

    Call tick() in a loop.  Each tick calls perceive() for ground truth,
    dispatches one action, and returns.  Phase transitions are driven by
    perceive() results between ticks — not by sequential function calls.
    """
    destination: str
    from_port:   Optional[str] = None

    # ── Internal state (not set by caller) ───────────────────────────────
    phase: SailPhase = field(default=SailPhase.INIT, init=False)

    _fleet_checked:       bool = field(default=False, init=False)
    _destination_selected: bool = field(default=False, init=False)
    # Picking the destination FROM PORT makes the game run to the harbour, supply and
    # sail in one operation. Tried once per goal; on failure we fall back to the older
    # harbour → fleet-check → depart path rather than looping on it.
    _port_departure_tried: bool = field(default=False, init=False)
    _fail_reason:          str  = field(default="", init=False)
    _tick_count:           int  = field(default=0, init=False)
    _max_ticks:            int  = field(default=300, init=False)  # ~25-50 min
    _zero_speed_count:     int  = field(default=0, init=False)
    _last_pos: Optional[tuple] = field(default=None, init=False)  # last (lat,lon) — stall confirm

    # Failure tracking: consecutive failures in the same phase → give up
    _consecutive_failures: int = field(default=0, init=False)
    _last_failed_phase: Optional[SailPhase] = field(default=None, init=False)

    @property
    def is_complete(self) -> bool:
        return self.phase == SailPhase.ARRIVED

    @property
    def is_failed(self) -> bool:
        return self.phase == SailPhase.FAILED

    def mark_failed(self, reason: str) -> None:
        self._fail_reason = reason
        self.phase = SailPhase.FAILED
        logger.error(f"[sail_to] Goal failed: {reason}")
        self._pop_goal_safely()

    def _pop_goal_safely(self) -> None:
        """Pop the GoalContext that this goal pushed on tick 1, if any.
        Safe to call multiple times — pop_goal is a no-op when stack is empty.
        """
        try:
            from brain.goal_context import pop_goal
            pop_goal()
        except Exception as _exc:
            logger.debug(f"[sail_to] pop_goal failed: {_exc}")

    def _track_failure(self, phase: SailPhase) -> bool:
        """Track consecutive failures.  Returns True if limit (3) exceeded."""
        if phase == self._last_failed_phase:
            self._consecutive_failures += 1
        else:
            self._consecutive_failures = 1
            self._last_failed_phase = phase
        if self._consecutive_failures >= 3:
            self.mark_failed(f"phase {phase.name} failed 3 consecutive times")
            return True
        return False

    def _reset_failures(self) -> None:
        self._consecutive_failures = 0
        self._last_failed_phase = None

    # ── Main tick ────────────────────────────────────────────────────────

    def tick(self) -> TickResult:
        """
        One tick: perceive() → dispatch one action → return result.
        Caller sleeps between ticks for anti-cheat delay.
        """
        from brain.perceive import perceive
        from brain.planner import get_planner

        self._tick_count += 1
        if self._tick_count > self._max_ticks:
            self.mark_failed(f"exceeded {self._max_ticks} ticks")
            return TickResult("timeout", self.phase, ok=False)

        # Publish goal context on the very first tick so perceive() can
        # hand the destination to Qwen as a task_hint.  See 2026-05-23
        # Berber sail: without this, Qwen has no way to recognise
        # arrival when local classifiers misidentify the village
        # interior, and SailToGoal stays in SAILING indefinitely.
        if self._tick_count == 1:
            try:
                from brain.goal_context import push_goal
                push_goal(
                    intent="sail_to",
                    target={"destination": self.destination},
                )
            except Exception as _exc:
                logger.debug(f"[sail_to] push_goal failed: {_exc}")

        # Dismiss non-game blockers BEFORE perceiving — the daily-news / event /
        # anniversary popups appear over the sea + world map during a voyage, and
        # perceive()'s pixel+Moondream daily-news detector misfires on them (and
        # then suppresses itself for 60 min), so the world-map nav gets stuck. The
        # text-based clear_blockers catches them (Updates/Notices, the Perk/Season/
        # Competition tab row) and is un-suppressed. See brain/unexpected_dialog.
        try:
            from capture.adb_capture import capture_screen
            from brain.unexpected_dialog import clear_blockers
            if clear_blockers(capture_screen()).get("cleared"):
                import time as _t
                _t.sleep(1.0)
        except Exception as _exc:
            logger.debug(f"[sail_to] clear_blockers failed: {_exc}")

        # Ground truth — includes interruptor dismissal (pass 1)
        state = perceive()
        # Flow-completeness bookkeeping (CLAUDE.md guideline): keep the
        # module-level flow tracker in sync with what perceive sees.  This
        # detects flow exit even when the goal advances flows directly via
        # _advance_active_flow without going through recover_to_port_overworld.
        from brain.recovery import notify_perceive_result
        notify_perceive_result(state)
        # Feed history into the planner so dynamic-state edge predicates
        # (Phase 4) and any planner-side stall detection can read it.
        get_planner().record(state)

        logger.info(
            f"[sail_to] tick={self._tick_count}  phase={self.phase.name}  "
            f"state={state.state!r}  port={state.port!r}  "
            f"flow={state.flow!r}  detail={state.detail[:60]!r}"
        )

        # Already at destination?  port_overworld for ports, village
        # for the new village interior state (introduced 2026-05-22).
        if (state.state in ("port_overworld", "village")
                and self._matches_destination(state.port)):
            self.phase = SailPhase.ARRIVED
            logger.info(
                f"[sail_to] Arrived at {self.destination!r} "
                f"(state={state.state}, port={state.port!r})"
            )
            self._pop_goal_safely()
            return TickResult("arrived", SailPhase.ARRIVED)

        # Phase-aware arrival heuristic — once we've reached SAILING
        # phase, the Go-to-City tap already committed the destination,
        # so the next port_overworld state IS the destination by
        # construction.  The OCR-confirmed check above is the
        # high-confidence path; this is the fallback for the race where
        # the goal-loop tick lands on a port_overworld transition frame
        # before port-name OCR has caught up (state.port returns None
        # or a partial like "City" / "Harbor").
        #
        # Origin: 2026-05-25 sail-to-London run.  Bot arrived at London
        # (visible in subsequent logs as port='London') but the
        # goal-loop tick at the transition frame saw port=None, the
        # OCR-confirmed check failed, dispatcher fell through to
        # navigate_to_building, and the goal never terminated — the
        # bot kept re-entering harbor and re-departing in a loop.
        if (state.state == "port_overworld"
                and self.phase.value >= SailPhase.SAILING.value):
            self.phase = SailPhase.ARRIVED
            logger.info(
                f"[sail_to] Arrived at {self.destination!r} "
                f"(phase-aware: state=port_overworld with phase>=SAILING; "
                f"port reading was {state.port!r})"
            )
            self._pop_goal_safely()
            return TickResult("arrived", SailPhase.ARRIVED)

        # Village arrival with no readable identity — the in-game
        # village screen shows literally "Village" as the title (no
        # specific name), so state.port is None but state.state is
        # 'village'.  When we've been sailing toward a village
        # destination and now land in a village interior, that is the
        # destination by construction.  The phase guard prevents this
        # from firing on pre-departure ticks where state.state could
        # transiently be 'village' for unrelated reasons.
        # Origin: 2026-05-23 Berber sail.
        if (state.state == "village"
                and state.port is None
                and self.phase.value >= SailPhase.SAILING.value
                and self._destination_is_likely_village()):
            self.phase = SailPhase.ARRIVED
            logger.info(
                f"[sail_to] Arrived at {self.destination!r} "
                f"(state=village with unknown identity; sailed-to a village "
                "destination → treating as arrival)"
            )
            self._pop_goal_safely()
            return TickResult("arrived", SailPhase.ARRIVED)

        # Secondary arrival signal — Qwen's goal-aware task_complete.
        # Triggers when:
        #   • Qwen was given the task_hint (we pushed GoalContext)
        #   • Qwen returned task_complete=True with high confidence
        #   • We've actually been sailing (phase ≥ SAILING) — guards
        #     against premature completion on world-map / pre-departure
        #     frames where the destination name happens to be on screen
        #   • scene_type is village-or-port-like (sanity check; if Qwen
        #     thinks task_complete on a 'sea' frame, ignore it)
        if (state.task_complete is True
                and state.confidence == "high"
                and self.phase.value >= SailPhase.SAILING.value
                and state.scene_type in ("village", "port_overworld")):
            self.phase = SailPhase.ARRIVED
            logger.info(
                f"[sail_to] Arrived at {self.destination!r} "
                f"(qwen task_complete=true, scene_type={state.scene_type!r}, "
                f"state.port={state.port!r}, detail={state.detail[:80]!r})"
            )
            self._pop_goal_safely()
            return TickResult("arrived", SailPhase.ARRIVED)

        # Active flow — must advance before navigation.
        # BUT: if the goal hasn't reached DEPART phase yet, a harbor_departure
        # flow detection is a false positive (the bot hasn't tapped Depart).
        # In pre-departure phases, ignore harbor_departure and fall through
        # to the dispatch table so fleet readiness checks can run.
        if state.in_flow:
            pre_departure = self.phase in (
                SailPhase.INIT, SailPhase.EXIT_BUILDING,
                SailPhase.GO_TO_HARBOR, SailPhase.FLEET_CHECK,
            )
            if state.flow == "harbor_departure" and pre_departure:
                logger.info(
                    f"[sail_to] Ignoring false flow {state.flow!r}/{state.flow_step!r} "
                    f"— goal is in pre-departure phase {self.phase.name}"
                )
            else:
                return self._handle_flow(state)

        # Dispatch based on current state (trust ground truth)
        return self._dispatch(state)

    def _matches_destination(self, port: Optional[str]) -> bool:
        if not port:
            return False
        # Same heuristic as the existing sail_to_port code
        return self.destination.lower()[:5] in port.lower()

    def _destination_is_likely_village(self) -> bool:
        """True when this goal's destination matches a known village.

        Checked against the village catalogue so we don't treat
        port destinations (e.g. 'london') as villages just because the
        screen happens to be misclassified.
        """
        try:
            from vision.text_correction import correct_village_name
            match, _ratio = correct_village_name(self.destination)
            return match is not None
        except Exception:
            return False

    # ── Flow handling ────────────────────────────────────────────────────

    def _handle_flow(self, state) -> TickResult:
        """Advance an active atomic flow one step."""
        from brain.recovery import _advance_active_flow
        from capture.adb_capture import capture_screen

        logger.info(
            f"[sail_to] Active flow {state.flow!r}/{state.flow_step!r} — advancing"
        )
        frame = capture_screen()
        _advance_active_flow(frame, state)
        return TickResult(f"advance_flow:{state.flow}", self.phase, delay=2.0)

    # ── State dispatch ───────────────────────────────────────────────────

    def _dispatch(self, state) -> TickResult:
        """Route to the appropriate handler based on perceive() state."""
        loc = state.state

        if loc == "building":
            return self._handle_building(state)
        if loc == "port_overworld":
            return self._handle_overworld(state)
        if loc == "village":
            return self._handle_village(state)
        if loc in ("sea", "sea_cinematic"):
            return self._handle_sea(state)
        if loc == "world_map":
            return self._handle_world_map(state)
        if loc == "loading":
            return TickResult("wait_loading", self.phase, delay=3.0)
        if loc == "main_menu":
            return self._handle_main_menu()
        if loc == "sub_menu":
            # A sub_menu is a KNOWN state (inside a menu, e.g. the market's purchase
            # sub-menu). Exit it with ONE Back per tick — tick-level perceive→act→
            # perceive, NOT the blocking recover_to_port_overworld subloop. See
            # feedback_subloops_only_known_states / docs/action_verification_and_
            # recovery_design.md. (Upstream, the gather step now exits the market so we
            # rarely reach here — this is the defensive tick-level path.)
            return self._action_back_out_of_submenu(state)

        # Truly UNKNOWN state (novel / unrecognised) — recovery is the last resort.
        return self._handle_unknown(state)

    def _action_back_out_of_submenu(self, state) -> TickResult:
        from actions.adb_actions import tap, press_back
        from vision.chrome_detector import get_chrome_detector
        from capture.adb_capture import capture_screen
        logger.info(f"[sail_to] state=sub_menu ({state.detail!r}) — backing out one level")
        try:
            chrome = get_chrome_detector().detect(capture_screen())
        except Exception:
            chrome = None
        if chrome is not None and chrome.has_back_arrow:
            tap(110, 40)                                   # in-game back arrow (top-left)
        elif chrome is not None and chrome.has_home and not chrome.has_hamburger:
            tap(2300, 45)                                  # Home — only on chromed screens
        else:
            press_back()
        return TickResult("back_out_submenu", self.phase, delay=2.0)

    # ── State handlers ───────────────────────────────────────────────────

    def _handle_building(self, state) -> TickResult:
        from brain.kb import control as _ckb
        detail = state.detail or ""

        # Inside harbor — handle harbor sub-phases
        if _ckb().detail_mentions_building(detail, "harbor"):
            # NOTE: we used to press Back here whenever Qwen tagged the
            # nav state as a non-departure sub-menu (e.g. sub_menu='supply').
            # That was wrong: when the bot first enters the harbor, the
            # default view shows the Supply tab highlighted alongside the
            # always-visible Depart Now button — Qwen interpreted this as
            # "inside supply sub-menu" and the Back press exited the harbor
            # entirely, creating an enter/exit loop.
            #
            # _ensure_fleet_ready (called below) already invokes
            # _ensure_harbor_top_level, which uses the OCR screen TITLE
            # (reliable, not Qwen's freeform tag) to decide whether to back
            # out of an actual sub-menu.  Trust that path; don't pre-empt it.
            if not self._fleet_checked:
                self.phase = SailPhase.FLEET_CHECK
                return self._action_fleet_check()
            self.phase = SailPhase.DEPART
            return self._action_depart()

        # Any other building — exit to overworld
        self.phase = SailPhase.EXIT_BUILDING
        return self._action_exit_building()

    def _handle_overworld(self, state) -> TickResult:
        """From a PORT, pick the destination on the world map and let the game depart.

        Selecting Move to City / Move to Village from inside a port makes the game run to
        the harbour, SUPPLY the fleet, and set sail — one operation instead of harbour →
        fleet check → depart → then pan the map at sea, which costs a second harbour trip
        and burns supply while the bot navigates (user 2026-08-21). The world-map work is
        identical; only the consequence differs.

        `depart_from_port_via_world_map` owns the two ways this misbehaves — staying
        ashore, and reaching the sea with speed 0 — and falls back to the harbour flow
        below if it cannot get the fleet under way at all."""
        if not self.from_port and state.port:
            self.from_port = state.port
        self._reset_failures()

        if not self._port_departure_tried:
            self._port_departure_tried = True
            from actions.sail_actions import depart_from_port_via_world_map
            res = depart_from_port_via_world_map(self.destination)
            if res.get("ok"):
                self._destination_selected = True
                self.phase = SailPhase.SAILING
                return TickResult(f"departed:{res.get('departed_via')}", SailPhase.SAILING,
                                  note=res.get("reason"), delay=3.0)
            logger.warning(f"[sail_to] port departure did not get under way "
                           f"({res.get('reason')}) — falling back to the harbour flow")

        self.phase = SailPhase.GO_TO_HARBOR
        return self._action_navigate_to_harbor()

    def _handle_sea(self, state) -> TickResult:
        if self._destination_selected:
            self.phase = SailPhase.SAILING
            return self._action_sailing(state)

        # Need to open world map and select destination
        self.phase = SailPhase.SEA_NAVIGATE
        return self._action_open_world_map()

    def _handle_world_map(self, state) -> TickResult:
        self.phase = SailPhase.WORLD_MAP
        return self._action_select_destination()

    def _handle_main_menu(self) -> TickResult:
        """Close the main menu (or whatever was misclassified as it).

        Routes through the canonical exit_current_screen() helper rather
        than blind press_back().  The helper prefers an on-screen close
        target (dialog X, Home/main-menu X icon, in-game back arrow)
        and only falls back to system Back as a last resort with a
        warning.

        This fixes the 2026-05-25 cascade where the daily-news popup
        blocked perception, made the underlying screen look like
        main_menu, and the previous blind press_back closed the popup
        unpredictably (system back is interpreted by whichever modal
        intercepts it first).
        """
        from actions.screen_exit import exit_current_screen
        logger.info("[sail_to] state=main_menu — exit_current_screen")
        result = exit_current_screen()
        logger.info(f"[sail_to]   exit_current_screen → {result.method!r} (ok={result.ok})")
        return TickResult("close_main_menu", self.phase, delay=2.0)

    def _handle_village(self, state) -> TickResult:
        """Starting state was inside a village interior — exit to sea
        by pressing Back.  Villages are top-level locations: Back
        returns directly to open sea (per memory
        `project_village_back_press_unsafe.md`).  From sea, the next
        tick's _handle_sea will take over and open the world map.

        Origin: 2026-05-23.  After arriving at Berber village the bot
        was asked to `sail las palmas` and tick 1 found state='village'
        with no dispatch handler, falling through to _handle_unknown
        and triggering a Back+Home recovery that overshot into
        main_menu and ultimately escalated.
        """
        from actions.adb_actions import press_back
        logger.info(
            f"[sail_to] Inside village (state.port={state.port!r}) — "
            "pressing Back to exit to sea"
        )
        press_back()
        # Phase deliberately unchanged — let the next perceive observe
        # the resulting state.  If it's sea, _handle_sea advances to
        # SEA_NAVIGATE; if perception lags and we still see village,
        # the dispatch loop will retry Back.
        return TickResult("exit_village", self.phase, delay=3.0)

    # States that _dispatch has an explicit handler for.  When
    # _handle_unknown is invoked but the state is actually one of
    # these, hand control back to the tick loop instead of running
    # recovery — the next perceive will route to the correct handler.
    # Keep this in sync with the _dispatch ladder above.
    _DISPATCHABLE_STATES = frozenset({
        "building", "port_overworld", "village",
        "sea", "sea_cinematic", "world_map", "loading", "main_menu",
    })

    def _handle_unknown(self, state) -> TickResult:
        # Early return — if perceive() landed us on a state that
        # _dispatch actually knows how to advance, don't call
        # recovery.  Recovery is a blocking inner loop with a fixed
        # `port_overworld` target; once it starts it won't release
        # control to the outer tick until it reaches port_overworld
        # or times out.  When the bot is at e.g. `sea`, recovery
        # would treat sea as "still not there" and over-act, even
        # though the sail goal could happily proceed from sea via
        # _handle_sea.  Origin: 2026-05-23 `sail las palmas` from
        # Berber village, where recovery bounced through village →
        # sea → main_menu → escalation, even though sea was a
        # perfectly fine launching point.
        if state.state in self._DISPATCHABLE_STATES:
            logger.info(
                f"[sail_to] state={state.state!r} reached _handle_unknown "
                "but it IS a dispatchable state — re-yielding to the tick "
                "loop so the next perceive routes to the correct handler"
            )
            return TickResult("re_dispatch", self.phase, delay=2.0)

        # IS SOMETHING JUST IN THE WAY? An unreadable state is not evidence that the fleet is
        # in the wrong place — it is often evidence that a popup is covering the evidence.
        # Clear it and re-perceive BEFORE planning any navigation.
        #
        # Live 2026-08-23: an announcement popup covered the screen as the fleet ARRIVED at
        # Melanesian Village. The state read 'unknown', this branch planned "back to
        # overworld", and from a village that means sailing away — the position the mission
        # had just spent a voyage on. The popup never blocked the GAME (the fleet arrived
        # fine); it only blocked the bot's reading, and the bot navigated on the blindness.
        #
        # The popup is orthogonal to the state, never a state of its own: it can sit over a
        # village, a market or the sea, and collapsing all of those into 'unknown' is what
        # destroys the information the recovery needed. See docs/one_loop_task_drives_state.md
        # and brain/unexpected.py.
        try:
            from brain.unexpected_dialog import clear_blockers
            from capture.adb_capture import capture_screen
            if clear_blockers(capture_screen()).get("cleared"):
                logger.info("[sail_to] a blocker was covering the screen — cleared it; "
                            "re-perceiving instead of planning a recovery")
                return TickResult("blocker_cleared", self.phase, delay=1.5)
        except Exception as exc:
            logger.debug(f"[sail_to] blocker check failed: {exc}")

        from brain.planner import get_planner

        logger.warning(
            f"[sail_to] Unexpected state {state.state!r} — planning back to overworld"
        )
        ok = get_planner().plan_to(
            "port_overworld", current_state=state.state,
            home_port=self.from_port, timeout=60.0,
        )
        if ok:
            self._reset_failures()
            return TickResult("recovered", self.phase, delay=2.0)

        if self._track_failure(self.phase):
            return TickResult("recovery_failed", SailPhase.FAILED, ok=False)
        return TickResult("recovering", self.phase, delay=5.0)

    # ── Individual actions ───────────────────────────────────────────────
    # Each action does ONE thing and returns.  Phase transitions happen
    # at the next tick when perceive() confirms the state change.

    def _action_exit_building(self) -> TickResult:
        """Exit the current building back to port_overworld.

        Preference order:
          1. Home button (top-right) — exits directly to port_overworld
          2. In-game back arrow (top-left) — exits one level (sub_menu →
             building → overworld); tapping the on-screen icon is safe
          3. System Back (ADB KEYCODE_BACK) — fallback; UWO intercepts
             this as "Exit Game?" confirmation on many building screens,
             so it must be a LAST resort.  See 2026-05-22 18:26-18:47
             log: the bot looped 17 times pressing system Back on the
             shipyard, each cycle triggering Exit Game → Cancel →
             still-in-shipyard, costing 22 minutes.
        """
        from actions.adb_actions import tap, press_back
        from vision.chrome_detector import get_chrome_detector
        from capture.adb_capture import capture_screen

        frame = capture_screen()
        chrome = get_chrome_detector().detect(frame)
        # Home (2300,45) exits to overworld ONLY on chromed screens; on the overworlds
        # that slot is the ☰ hamburger and OPENS Company Overview (never tap it there).
        # has_hamburger flags an overworld. See project_home_button_is_chromed_only_escape.
        if chrome.has_home and not chrome.has_hamburger:
            tap(2300, 45)
            logger.info("[sail_to] Tapping Home button (2300, 45) to exit building")
        elif chrome.has_back_arrow:
            # CHROME_BACK_ARROW_REGION is (0, 0, 220, 80) — centre ~ (110, 40).
            tap(110, 40)
            logger.info(
                "[sail_to] Tapping in-game back arrow (110, 40) to exit building "
                "— avoids the 'Exit Game?' loop that system Back triggers"
            )
        else:
            press_back()
            logger.warning(
                "[sail_to] No back-arrow or home detected — falling back to "
                "system Back.  This may trigger the 'Exit Game?' dialog."
            )
        return TickResult("exit_building", SailPhase.EXIT_BUILDING, delay=2.0)

    def _action_navigate_to_harbor(self) -> TickResult:
        """Navigate from port overworld to the harbor building."""
        from actions.sail_actions import navigate_to_building

        ok = navigate_to_building("harbor")
        if ok:
            self._reset_failures()
            self.phase = SailPhase.FLEET_CHECK
            return TickResult("entered_harbor", SailPhase.GO_TO_HARBOR)

        if self._track_failure(SailPhase.GO_TO_HARBOR):
            return TickResult("harbor_nav_failed", SailPhase.FAILED, ok=False)
        return TickResult(
            "harbor_nav_retry", SailPhase.GO_TO_HARBOR, ok=False,
            note="could not enter harbor — will retry next tick",
        )

    def _action_fleet_check(self) -> TickResult:
        """Check fleet readiness (crew, supplies) before departure."""
        from actions.sail_actions import _ensure_fleet_ready

        ok = _ensure_fleet_ready()
        if ok:
            self._fleet_checked = True
            self._reset_failures()
            self.phase = SailPhase.DEPART
            return TickResult("fleet_ready", SailPhase.FLEET_CHECK)

        # Fleet not ready — don't set _fleet_checked so we re-check next tick.
        # Use failure tracking: 3 consecutive failures → give up.
        if self._track_failure(SailPhase.FLEET_CHECK):
            return TickResult("fleet_not_ready", SailPhase.FAILED, ok=False)
        return TickResult(
            "fleet_check_retry", SailPhase.FLEET_CHECK, ok=False,
            note="fleet not ready — will re-check next tick",
        )

    def _action_depart(self) -> TickResult:
        """Tap Depart and wait for sea confirmation."""
        from actions.sail_actions import _depart_from_harbour

        ok = _depart_from_harbour()
        if ok:
            self._reset_failures()
            self._destination_selected = False
            self.phase = SailPhase.SEA_NAVIGATE
            return TickResult("departed", SailPhase.DEPART)

        # Departure failed — reset fleet check so it re-runs next tick.
        # The failure likely means the button was disabled (blocker missed).
        self._fleet_checked = False
        if self._track_failure(SailPhase.DEPART):
            return TickResult("depart_failed", SailPhase.FAILED, ok=False)
        return TickResult(
            "depart_retry", SailPhase.DEPART, ok=False,
            note="departure failed — will re-check fleet next tick",
        )

    def _action_open_world_map(self) -> TickResult:
        """Open the world map from sea view."""
        from actions.sail_actions import _open_world_map_from_sea
        from capture.adb_capture import capture_screen

        frame = capture_screen()
        ok = _open_world_map_from_sea(frame)
        if ok:
            self._reset_failures()
            self.phase = SailPhase.WORLD_MAP
            return TickResult("world_map_opened", SailPhase.SEA_NAVIGATE)

        if self._track_failure(SailPhase.SEA_NAVIGATE):
            return TickResult("world_map_failed", SailPhase.FAILED, ok=False)
        return TickResult(
            "world_map_retry", SailPhase.SEA_NAVIGATE, ok=False,
            note="could not open world map — will retry next tick",
        )

    def _action_select_destination(self) -> TickResult:
        """Find and tap destination on the world map.

        Uses smart-dispatch: ports go through the existing port flow
        (Port tab + list-scroll fallback); villages go through the
        village flow (Explore tab + village catalogue).  Classification
        is by catalogue lookup on the destination name.
        """
        from actions.sail_actions import _navigate_world_map_to_destination

        # Fall back to the observation singleton when self.from_port was
        # never captured.  Happens when this goal starts while already on
        # world_map/sea — _handle_overworld (the sole writer of
        # self.from_port) never ran, so pan_to_port would get None and
        # bail without an origin to dead-reckon from.
        from_port = self.from_port
        if not from_port:
            from brain import observation as _obs
            cur = _obs.current()
            settlement = cur.last_known_settlement if cur is not None else None
            if settlement:
                from_port = settlement
                logger.info(
                    f"[sail_to] from_port unset — using "
                    f"last_known_settlement={from_port!r} "
                    f"(is_departed={cur.is_departed}, "
                    f"age={cur.last_known_settlement_age_ticks} ticks)"
                )
            else:
                logger.warning(
                    f"[sail_to] from_port unset AND "
                    f"last_known_settlement is None (cur={'present' if cur else 'None'}) "
                    f"— pan_to_port will have no origin to dead-reckon from"
                )

        ok = _navigate_world_map_to_destination(
            self.destination, from_port=from_port,
        )
        if ok:
            self._reset_failures()
            self._destination_selected = True
            self.phase = SailPhase.SAILING
            return TickResult("destination_selected", SailPhase.WORLD_MAP)

        if self._track_failure(SailPhase.WORLD_MAP):
            return TickResult("destination_failed", SailPhase.FAILED, ok=False)
        # Press back to close world map; next tick from sea → retry
        from actions.adb_actions import press_back
        press_back()
        return TickResult(
            "destination_retry", SailPhase.WORLD_MAP, ok=False,
            note=f"could not find {self.destination!r}",
        )

    def _action_sailing(self, state) -> TickResult:
        """During sailing: anti-idle tap, stall detection."""
        from actions.adb_actions import tap

        loc = state.state

        # Sea cinematic — wake from idle view
        if loc == "sea_cinematic":
            from actions import ui as _ui
            _ui.tap_centre(why="wake the sea cinematic")
            return TickResult("wake_cinematic", SailPhase.SAILING, delay=3.0)

        # Normal sea — anti-idle tap
        tap(1200, 540)

        # STALL DETECTION — re-navigate ONLY when the ship is CONFIRMED not moving: speed reads 0
        # AND the lat/lon position has not advanced since the last tick.  A speed read alone is NOT
        # enough — a single OCR misread of a MOVING ship's speed as 0 used to discard the committed
        # route and re-open the world map mid-voyage (the hamburger loop, 2026-08-19).  If the
        # position can't be read we do NOT stall (can't confirm it's stopped).
        try:
            from actions.sail_actions import _read_sea_speed
            from vision.sea_hud import read_latlon
            from capture.adb_capture import capture_screen

            frame = capture_screen()
            speed = _read_sea_speed(frame)
            pos = read_latlon(frame, prev_latlon=self._last_pos)
            advanced = (pos is not None and self._last_pos is not None
                        and abs(pos[0] - self._last_pos[0])
                          + abs(pos[1] - self._last_pos[1]) > _STALL_POS_EPS)

            if advanced or (speed is not None and speed > 0.0):
                self._zero_speed_count = 0             # moving → not stalled
            elif (speed == 0.0 and pos is not None
                    and self._last_pos is not None and not advanced):
                self._zero_speed_count += 1            # speed 0 AND position held
                logger.info(f"[sail_to] Not moving (speed=0, pos held @ {pos}) "
                            f"{self._zero_speed_count}/{_STALL_TICKS} before re-navigate")
                if self._zero_speed_count >= _STALL_TICKS:
                    logger.warning("[sail_to] Ship CONFIRMED stalled (speed=0 + position held) "
                                   "— re-navigating")
                    self._zero_speed_count = 0
                    self._destination_selected = False
                    self.phase = SailPhase.SEA_NAVIGATE
                    self._last_pos = pos
                    return TickResult("stall_renavigate", SailPhase.SAILING)
            if pos is not None:
                self._last_pos = pos
        except Exception:
            pass  # speed/pos read failed — not critical; do NOT stall on uncertainty

        return TickResult("sailing", SailPhase.SAILING, delay=20.0)


def drive_sail_to(destination: str, from_port=None, min_supply_days=None) -> dict:
    """Drive a SailToGoal to completion — the ONE sail path for BOTH ports and
    villages. SailToGoal's WORLD_MAP phase dispatches Explore-tab village vs port
    selection (_navigate_world_map_to_destination), and its arrival check accepts the
    'village' state as well as 'port_overworld' — so sailing to a village is identical
    to a port except the arrival place. Returns {ok, reason}.

    Used by the mission sub-task executors (brain/barter_mission_live) so gather /
    sail_to_village / sail_to_sell all share the validated sail (harbour Back-handling
    fix included), not the older monolithic sail_to_port. The task runner's run_sail_to
    drives the same goal; supply mid-voyage watch lives there (each barter leg departs
    via Supply Departure, so legs start topped up)."""
    import random
    import time as _time
    from loguru import logger as _logger

    # Already at the destination? Skip the pointless round-trip. (The Malé bug: the
    # scheduler picked the CURRENT port, but where_am_i's port read None, so SailToGoal
    # tried to sail to where it already was.) OmniParser is non-deterministic and a
    # port_overworld ALWAYS has a name (invariant — feedback_never_act_blind_know_
    # location_and_state), so RE-READ a few times before concluding.
    from actions.sail_actions import where_am_i
    from capture.adb_capture import capture_screen
    dest5 = (destination or "").lower()[:5]
    for attempt in range(3):
        loc = where_am_i(capture_screen())
        if loc.get("location") != "port_overworld":
            break                                    # at sea/building → proceed to sail
        port = loc.get("port")
        if port and dest5 and dest5 in port.lower():
            _logger.info(f"[drive_sail_to] already at {destination!r} (read {port!r}) — skipping sail")
            return {"ok": True, "reason": f"already at {destination}"}
        if port:
            break                                    # a DIFFERENT port → proceed to sail
        _logger.warning(f"[drive_sail_to] port_overworld but port name unreadable "
                        f"(try {attempt + 1}/3) — re-reading (a port always has a name)")
        _time.sleep(0.6)

    # Mid-voyage supply watch (parity with the task runner's run_sail_to): a long or
    # stuck voyage must divert to resupply instead of sailing until supply hits 0 and
    # the game force-returns the fleet (the gather run died this way 2026-08-17).
    try:
        from actions.task_runner import _ensure_supply, _next_supply_check_s
    except Exception:
        _ensure_supply, _next_supply_check_s = None, (lambda _d: 120.0)

    def _supply_check() -> float:
        """Read supply, divert if short, and return how long to wait before looking
        again — about one game day before the tank would run dry (user 2026-08-20)."""
        if _ensure_supply is None:
            return 120.0
        try:
            days = _ensure_supply(destination, min_days=min_supply_days)
        except Exception as exc:
            _logger.debug(f"[drive_sail_to] supply check skipped: {exc}")
            return 120.0
        wait = _next_supply_check_s(days)
        _logger.debug(f"[drive_sail_to] supply {days}d → next check in {wait / 60:.1f} min")
        return wait

    goal = SailToGoal(destination=destination, from_port=from_port)
    # Look once as soon as we are under way (tops up if we started at sea under-supplied),
    # then re-look on a cadence derived from what the HUD actually showed.
    next_supply = _time.monotonic() + _supply_check()
    while not goal.is_complete and not goal.is_failed:
        result = goal.tick()
        if _time.monotonic() >= next_supply:
            next_supply = _time.monotonic() + _supply_check()
        delay = result.delay if getattr(result, "delay", 0) and result.delay > 0 else random.uniform(1.5, 2.5)
        _time.sleep(delay)

    if goal.is_complete:
        return {"ok": True, "reason": f"arrived at {destination}"}
    reason = getattr(goal, "_fail_reason", "") or f"sail to {destination!r} did not complete"
    _logger.warning(f"[drive_sail_to] {destination!r} failed: {reason}")
    return {"ok": False, "reason": reason}
