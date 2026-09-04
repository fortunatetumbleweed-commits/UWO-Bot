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

import types

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

def arrival_verdict(state, phase, *, matches_destination: bool,
                    destination_is_likely_village: bool):
    """Has the fleet arrived? Returns the REASON it decided so, or None.

    Pulled out of `tick()` so the decision can be tested without executing a voyage. The
    test that guards it used to drive `tick()` itself, which — for any phase below SAILING —
    falls past this decision into the real departure action: `open_world_map()` OCRing a
    blank frame through ten retries. That cost an hour per run, could only ever fail by
    raising from the action path, and asserted something the enum ordering already
    guarantees. The decision is four conditions; this makes them checkable in microseconds.

    `matches_destination` and `destination_is_likely_village` are passed in rather than
    looked up, so this function needs no goal instance and no screen.
    """
    sailing = phase.value >= SailPhase.SAILING.value

    # The high-confidence path: the port NAME was read and it is the destination.
    if state.state in ("port_overworld", "village") and matches_destination:
        return f"state={state.state}, port={state.port!r}"

    # Once SAILING, the Go-to-City tap already committed the destination, so the next
    # port_overworld IS the destination by construction. This is the fallback for the race
    # where the tick lands on a transition frame before port-name OCR has caught up.
    # Origin: 2026-05-25 sail-to-London. The tick saw port=None, the OCR check failed, the
    # dispatcher fell through to navigate_to_building and the goal never terminated — the
    # bot re-entered harbour and re-departed in a loop.
    if state.state == "port_overworld" and sailing:
        return (f"phase-aware: state=port_overworld with phase>=SAILING; "
                f"port reading was {state.port!r}")

    # A village screen shows literally "Village" as its title, so `port` is None while the
    # state is 'village'. Having sailed TOWARD a village, landing in one is the destination.
    # The phase guard stops this firing on a pre-departure tick. Origin: 2026-05-23 Berber.
    if (state.state == "village" and state.port is None and sailing
            and destination_is_likely_village):
        return ("state=village with unknown identity; sailed-to a village destination "
                "-> treating as arrival")

    # Qwen's goal-aware task_complete, gated hard: it must be high confidence, we must
    # actually have been sailing, and the scene must be somewhere one can arrive.
    if (state.task_complete is True and state.confidence == "high" and sailing
            and state.scene_type in ("village", "port_overworld")):
        return (f"qwen task_complete=true, scene_type={state.scene_type!r}, "
                f"state.port={state.port!r}, detail={state.detail[:80]!r}")

    return None


# How many times the task runner will commit a destination before walking to the harbour.
# Each commit is one tick, with a perceive in between — so a fleet that arrives, or gets
# under way, is noticed immediately instead of after a primitive's internal retries.
_MAX_DEPARTURE_COMMITS = 3


# `HarborActivity` guards on the perceived state before it works. This phase has already
# established that the bot is in the harbour — it just read the departure panel — so it is
# told plainly rather than paying for a second perceive to learn what it already knows.
_HARBOR_STATE = types.SimpleNamespace(state="building:harbor", port=None)


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
    _departure_commits: int = field(default=0, init=False)
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

        reason = arrival_verdict(
            state, self.phase,
            matches_destination=self._matches_destination(self._settlement_now(state)),
            destination_is_likely_village=self._destination_is_likely_village(),
        )
        if reason:
            self.phase = SailPhase.ARRIVED
            logger.info(f"[sail_to] Arrived at {self.destination!r} ({reason})")
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

    def _settlement_now(self, state) -> Optional[str]:
        """Where the fleet IS, even when the screen does not say.

        Inside a building the port name is not on screen, so `state.port` is None — and a
        destination check that only reads the screen then concludes "not there" and sails to
        the port it is standing in. Live 2026-08-21: `gather:Jakarta` ran while the fleet sat
        in Jakarta's Market, the one place it needed to be, and instead of buying it tried to
        exit, open the world map and sail to Jakarta.

        The bot still KNOWS where it is — the settlement is carried across ticks and persisted
        — so a building or sub-menu falls back to that belief. This is the one place the
        repository outranks the screen, and only because the screen is silent rather than
        contradicting it.
        """
        if state.port:
            return state.port
        if state.state not in ("building", "sub_menu"):
            return None
        try:
            from brain import observation as _obs
            cur = _obs.current()
            return (cur.last_known_settlement if cur else None) \
                or _obs._ensure_persisted_loaded()
        except Exception as exc:
            logger.debug(f"[sail_to] could not resolve the settlement from inside a "
                         f"building ({type(exc).__name__}: {exc})")
            return None

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

        The two ways this misbehaves — staying ashore, and reaching the sea with speed 0 —
        are handled HERE, one tick at a time, with a perceive between each. They used to be
        handled inside the departure call, which meant this goal could not see a fleet that
        had already arrived (2026-08-25)."""
        if not self.from_port and state.port:
            self.from_port = state.port
        self._reset_failures()

        # ONE ACTION PER TICK. `commit_departure` taps and returns; whether the fleet then
        # moved, stayed, or ARRIVED is read by the next perceive and decided here — not
        # inside the primitive. The old call owned all of that internally and the goal lost
        # control for six minutes while the fleet sat at its destination (2026-08-25).
        if self._departure_commits < _MAX_DEPARTURE_COMMITS:
            self._departure_commits += 1
            from actions.sail_actions import commit_departure
            res = commit_departure(self.destination)
            if res.get("ok"):
                self._destination_selected = True
                # NOT "sailing" yet — that is a fact about the world, and the next tick will
                # perceive it. Claiming it here is how a fleet that never left, or one that
                # already arrived, both got recorded as under way.
                return TickResult("departure committed", self.phase,
                                  note=res.get("reason"), delay=3.0)
            logger.warning(f"[sail_to] could not commit the departure "
                           f"({res.get('reason')}) — attempt "
                           f"{self._departure_commits}/{_MAX_DEPARTURE_COMMITS}")
            return TickResult("departure not committed", self.phase,
                              note=res.get("reason"), delay=2.0)

        logger.warning(f"[sail_to] {self._departure_commits} departure commits did not get "
                       "the fleet under way — falling back to the harbour flow")
        self.phase = SailPhase.GO_TO_HARBOR
        return self._action_navigate_to_harbor()

    def _handle_sea(self, state) -> TickResult:
        # THE SCREEN OUTRANKS THE FLAG.
        #
        # `_destination_selected` is a CONCLUSION this goal drew earlier, and CLAUDE.md
        # forbids trusting one: it cannot be checked by looking. The sea HUD PRINTS the
        # destination at bottom-centre — `Barcelona / ETA 1 d` — so the same question has an
        # observation to answer it.
        #
        # Live 2026-08-26 is what the flag alone costs. The departure worked, a post-tap check
        # misread the cinematic as failure, the flag stayed False, and this branch re-opened
        # the world map MID-VOYAGE and re-targeted the port the fleet had just left. That
        # frame's HUD said `Barcelona ETA 1 d` the whole time.
        #
        # Safe in the other direction too: the game can show a destination while a tap had no
        # effect (user 2026-08-21), so believing it could in principle wait on a fleet that is
        # not moving. That is what SAILING's own stall detection is for — a stalled voyage is
        # caught by watching for MOVEMENT, which is the observation for THAT question.
        if not self._destination_selected and self._already_bound(state):
            logger.info(f"[sail_to] the HUD says the fleet is already bound for "
                        f"{self.destination!r} — not re-selecting it")
            self._destination_selected = True

        if self._destination_selected:
            self.phase = SailPhase.SAILING
            return self._action_sailing(state)

        # Need to open world map and select destination
        self.phase = SailPhase.SEA_NAVIGATE
        return self._action_open_world_map()

    def _already_bound(self, state) -> bool:
        """Does the sea HUD name the destination this goal wants?"""
        from vision.region_detectors.sea_destination import bound_for
        frame = getattr(state, "frame", None)
        try:
            if frame is None:
                from capture.adb_capture import capture_screen
                frame = capture_screen()
            return bool(bound_for(frame, self.destination))
        except Exception as exc:
            # A failed read is NOT "no destination" — it is no answer, and the goal proceeds
            # exactly as it did before this check existed.
            logger.debug(f"[sail_to] could not read the destination from the HUD: {exc}")
            return False

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

        # A TRANSIENT IS NOT UNEXPECTED — IT IS THIS GOAL'S OWN ACTION STILL LANDING.
        #
        # `transient` and `loading` are the departure cinematic, the arrival fade, the notice
        # the game throws mid-move. Their defining property is that the destination is not yet
        # knowable: the action has been COMMITTED and finished, and where it lands is a
        # question only the next perceive can answer — the same shape as the idle lock's
        # swipe (user, 2026-08-27). So the answer is to WAIT AND LOOK AGAIN, never to plan a
        # recovery.
        #
        # Live 2026-08-27: the fleet committed a departure to Svear Village, the cinematic
        # came up, this called it "Unexpected state 'transient'" and planned back to
        # port_overworld. `recover_to_port_overworld` saw `sea`, took its
        # `_recover_from_sea` branch, and SAILED THE FLEET BACK to home port Barcelona —
        # cancelling the voyage the mission had just started. It ran 311.5s and reported
        # SUCCESS, because reaching port_overworld is its only success test.
        if (state.state or "") in ("transient", "loading"):
            logger.info(f"[sail_to] {state.state!r} — the committed action is still landing; "
                        "waiting to perceive where it ends up rather than recovering")
            return TickResult("settling", self.phase, delay=2.0)

        # ONE ACT, THEN REPORT — NEVER NAVIGATE.
        #
        # This used to call `planner.plan_to("port_overworld", home_port=…)`, which is the
        # only live path into `recover_to_port_overworld`. That function owns a 20-attempt
        # loop, accepts one destination as success, and — given a `home_port`, which every
        # caller passes — reaches `_recover_from_sea`, whose first branch is
        # `sail_to_port(home_port)`: full navigation. So a goal that could not read a screen
        # ended up MOVING THE FLEET.
        #
        # Live 2026-08-27: the fleet committed a departure to Svear Village, the departure
        # cinematic came up, this called it unexpected, and the recovery sailed the fleet
        # back to Barcelona — cancelling the voyage. It ran 311.5s and logged SUCCESS,
        # because reaching port_overworld is its only success test.
        #
        # Getting off a screen is one tap (`exit_current_screen` picks Home / the dialog X /
        # the in-game back arrow, and refuses a system back on an overworld). Where that
        # lands is the next perceive's business. If the screen is still unreadable after
        # three ticks, that is a fact for the layer above to act on — retry the leg, re-plan,
        # or stop — and not a licence for this goal to decide where the fleet should be.
        from actions.screen_exit import exit_current_screen

        logger.warning(f"[sail_to] cannot read state {state.state!r} — taking ONE exit and "
                       "re-perceiving; this goal does not navigate to recover")
        try:
            res = exit_current_screen()
            logger.info(f"[sail_to] exit attempt: {getattr(res, 'method', res)}")
        except Exception as exc:
            logger.warning(f"[sail_to] could not exit the screen: {exc}")

        if self._track_failure(self.phase):
            return TickResult("unreadable_state", SailPhase.FAILED, ok=False)
        return TickResult("exiting", self.phase, delay=3.0)

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
        """Check fleet readiness (crew, supplies) before departure.

        The readiness check no longer walks back to the harbour after a resolution attempt —
        that is navigation, and this goal owns navigation. So when the read says the bot is
        not looking at the departure panel, step back to GO_TO_HARBOR rather than re-reading
        the same wrong screen until the failure count runs out.
        """
        from actions.sail_actions import read_fleet_readiness

        reading = read_fleet_readiness()
        if not reading["ready"] and not reading["on_departure_panel"]:
            logger.info(f"[sail_to] the fleet check is not looking at the departure panel "
                        f"({reading['detail']}) — going back to the harbour")
            self.phase = SailPhase.GO_TO_HARBOR
            return TickResult("fleet_check_needs_harbor", SailPhase.GO_TO_HARBOR, ok=False,
                              note="not on the departure panel")

        ok = reading["ready"]
        if not ok:
            logger.warning(f"[sail_to] fleet not ready: {reading['detail']}")
            self._serve_blocker(reading)
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

    def _serve_blocker(self, reading: dict) -> None:
        """A BLOCKER IS NOT A RECOVERY — IT IS THE NEXT GOAL (user, 2026-08-26).

        "Not enough crew" is not an exception raised by sailing; it is the world saying the
        fleet is not ready, and the remedy is ordinary work. So the named blocker becomes a
        goal, and the harbour activity serves it — one rung, then this phase re-checks on its
        next tick and reads the game's own answer, which outranks anything counted here.

        The old ladder is still the fallback for blockers nothing yet serves — an unnamed one,
        or "not enough supply", whose remedy is at the market. That is a HYBRID and known to
        be one: this phase machine is standing in for the dispatcher until `sail_to` is
        migrated. What it buys is that the common case — crew, eleven of the eighteen learned
        recoveries — stops going through four rungs of guessing, without any case losing its
        handling in the meantime.
        """
        from brain.activities.harbor import HarborActivity, RecruitCrew, goal_for_blocker

        blocker = reading.get("blocker") or {}
        named = blocker.get("text") if isinstance(blocker, dict) else str(blocker or "")
        goal = goal_for_blocker(named)

        if goal is None:
            from actions.sail_actions import resolve_fleet_blocker
            logger.info(f"[sail_to] no goal serves {named or 'an unnamed blocker'} — "
                        "falling back to the resolution ladder")
            res = resolve_fleet_blocker(reading)
            logger.info(f"[sail_to] resolution attempt via {res['via']}: "
                        f"resolved={res['resolved']}")
            return

        logger.info(f"[sail_to] {named!r} blocks departure — next goal: {goal}")
        result = HarborActivity().work(goal, _HARBOR_STATE)
        logger.info(f"[sail_to] {goal} -> {result.status} {dict(result.observed)}")

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
    # DEPRECATED, AND LOUD ABOUT IT — same treatment as `open_world_map`, for the same
    # reason. It drives `SailToGoal`, eight phases that walk to the harbour, open the world
    # map, pick the destination and then POLL FOR ARRIVAL in a SAILING phase of their own.
    # That poll does not perceive through the dispatcher, so it receives neither the
    # interruptor pass that dismisses a daily-news popup nor `IdleLockActivity` — the exact
    # failure `_await_route_arrival` was deleted for.
    #
    # Its production callers are gone: `brain/barter_mission_live._sail_to` states the leg as
    # work orders (Depart -> ChooseDestination -> ArriveAshore) and the dispatcher routes
    # each. Left standing as the reference for what those orders must reproduce; any caller
    # appearing in a log is a leg that went around the dispatcher.
    import inspect as _inspect
    import random
    import time as _time
    from loguru import logger as _logger

    _caller = "?"
    for _fr in _inspect.stack()[1:]:
        if _fr.filename != __file__:
            _caller = f"{_fr.filename.rsplit('/', 1)[-1]}:{_fr.lineno} in {_fr.function}()"
            break
    _logger.warning(f"[drive_sail_to] DEPRECATED — called from {_caller}. State the leg as "
                    f"work orders (see brain/sail_runner.py); report this caller.")

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
