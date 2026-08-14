# brain/recovery.py
#
# FSM-driven recovery layer.
#
# Primary entry points:
#   assert_at_port(context, home_port)  — call at the start of any port action.
#       Ensures the bot is at port_overworld, recovering if needed.
#       If recovery fails, raises RecoveryError.
#
#   recover_to_port_overworld(home_port, timeout)  — raw recovery primitive.
#       Uses perceive() + FSM BFS paths to navigate back to port_overworld.
#       Sea state is handled by sailing to home_port (or waiting for natural arrival).
#
# Recovery strategy per state:
#   atomic flow active  → advance flow steps until complete, then navigate out
#   building (no flow)  → tap Home or press Back
#   port_map            → tap Home
#   world_map           → press Back
#   main_menu           → press Back
#   sea/sea_cinematic   → if home_port given: sail there; else wait for natural arrival
#   loading             → wait
#   unknown             → blind: dialog advance + back + home region tap
#
# Backward compat:
#   recover_to_known_state()  — kept; delegates to recover_to_port_overworld()
#   assert_stable()           — kept; delegates to assert_at_port()

from __future__ import annotations

import time
from typing import Optional

from loguru import logger

from brain.kb import RECOVERY_TAP_BUTTON, RECOVERY_WAIT, RECOVERY_TAP_OK_OR_X, RGN_Y_MIN, RGN_Y_MAX, RGN_X_MIN, RGN_X_MAX
from brain.perceive import CONFIDENCE_LOW


class RecoveryError(Exception):
    """Raised when recovery could not reach port_overworld within timeout."""


# ── Flow-completeness attempt tracker ────────────────────────────────────────
#
# CLAUDE.md → "Flow Completeness & Self-Correction" requires the runner to
# count positive transactions executed during a flow attempt and report
# whether the terminal state was a recognized state.
#
# The tracker is a MODULE-LEVEL singleton so every caller of
# _advance_active_flow (sail_to, recover_to_port_overworld, …) participates
# in the same attempt without each callsite carrying its own state.  Flow
# exit is detected by notify_perceive_result: when the perceived flow_id
# differs from the active tracker's flow_id, the tracker records the
# outcome and clears.

class _FlowAttemptTracker:
    __slots__ = ("flow_id", "start_time", "positive_count", "executed_step_ids", "cancelled")

    def __init__(self, flow_id: str, start_time: float):
        self.flow_id           = flow_id
        self.start_time        = start_time
        self.positive_count    = 0
        self.executed_step_ids = set()
        self.cancelled         = False

    def note_step_executed(self, flow_id: str, step_id: str) -> None:
        """Increment the transaction counter if this step is a transaction."""
        if flow_id != self.flow_id:
            return
        if step_id in self.executed_step_ids:
            return  # Don't double-count the same step on retries
        self.executed_step_ids.add(step_id)
        from brain.flow_completeness import categorize_step, CATEGORY_TRANSACTION
        from brain.fsm_registry import get_fsm_registry
        flow = get_fsm_registry().flows.get(flow_id)
        if flow is None:
            return
        step_def = next((s for s in flow.steps if s.get("id") == step_id), None)
        if step_def is None:
            return
        if categorize_step(step_def) == CATEGORY_TRANSACTION:
            self.positive_count += 1
            logger.debug(
                f"  [recovery] flow {flow_id!r} step {step_id!r} = transaction "
                f"(running count: {self.positive_count})"
            )

    def note_cancelled(self) -> None:
        """Mark this attempt as cancelled (Back/Home pressed during the flow)."""
        self.cancelled = True


# Module-level singleton — shared across sail_to, recover_to_port_overworld,
# and any other caller of _advance_active_flow.
_active_tracker: Optional[_FlowAttemptTracker] = None


def _record_tracker_outcome(perceived_state: str) -> None:
    """Record the outcome for the active tracker and clear it."""
    global _active_tracker
    if _active_tracker is None:
        return
    from brain.fsm_registry import get_fsm_registry
    registry = get_fsm_registry()
    tracked_flow = registry.flows.get(_active_tracker.flow_id)
    if tracked_flow is not None:
        success  = (perceived_state == tracked_flow.terminal_state)
        duration = time.time() - _active_tracker.start_time
        terminal_recognized = perceived_state in registry.states
        logger.info(
            f"  [flow-tracker] flow {_active_tracker.flow_id!r} exited — "
            f"expected terminal={tracked_flow.terminal_state!r}  "
            f"actual={perceived_state!r}  → {'SUCCESS' if success else 'FAILURE'}  "
            f"transactions={_active_tracker.positive_count}  "
            f"terminal_recognized={terminal_recognized}  "
            f"cancelled={_active_tracker.cancelled}"
        )
        registry.record_flow_outcome(
            _active_tracker.flow_id,
            success=success,
            duration_secs=duration,
            positive_transaction_count=_active_tracker.positive_count,
            terminal_state_recognized=terminal_recognized,
            cancelled=_active_tracker.cancelled,
        )
        # Surface the learning re-trigger marker (CLAUDE.md guideline) when
        # this attempt left the flow in INCOMPLETE state.
        from brain.flow_completeness import STATUS_INCOMPLETE as _STATUS_INC
        tracked_after = registry.flows.get(_active_tracker.flow_id)
        if tracked_after is not None and tracked_after.status == _STATUS_INC:
            logger.warning(
                f"  [flow-tracker] flow {_active_tracker.flow_id!r} marked INCOMPLETE — "
                f"perceive will skip it on next encounter; learning hook will "
                f"re-fire to extend the flow with the missing step."
            )
    _active_tracker = None


def notify_perceive_result(perceive_result) -> None:
    """
    Notify the flow tracker of a fresh perceive() result.

    Called from goal-loop ticks (e.g. sail_to.tick) so flow-exit is detected
    even when a goal advances the flow directly via _advance_active_flow
    without going through recover_to_port_overworld.
    """
    global _active_tracker
    new_flow_id = perceive_result.flow if perceive_result is not None else None

    # Flow exit: tracker exists but the perceived flow has changed (or gone away).
    if _active_tracker is not None and new_flow_id != _active_tracker.flow_id:
        _record_tracker_outcome(perceive_result.state if perceive_result is not None else "unknown")

    # Flow start: a flow is now active and tracker is empty (or for a different flow).
    if new_flow_id and (_active_tracker is None or _active_tracker.flow_id != new_flow_id):
        _active_tracker = _FlowAttemptTracker(new_flow_id, time.time())


def get_active_tracker() -> Optional[_FlowAttemptTracker]:
    """Expose the active tracker so callers can pass it to _advance_active_flow."""
    return _active_tracker


def reset_active_tracker() -> None:
    """Clear the tracker without recording outcome — used by tests."""
    global _active_tracker
    _active_tracker = None


# ── Transition action executor ────────────────────────────────────────────────

def _execute_transition(action: str, frame) -> None:
    """
    Execute a single FSM transition action.
    Maps action names from states.json exits to actual Python calls.
    """
    from actions.sail_actions import press_back
    from actions.adb_actions import tap
    from vision.chrome_detector import get_chrome_detector

    if action in ("press_back", "tap_x_button"):
        press_back()
        time.sleep(2.0)

    elif action == "tap_home":
        chrome = get_chrome_detector().detect(frame)
        if chrome.has_home:
            # Home button is at a known region — tap centre of that region
            tap(2300, 45)
        else:
            tap(2300, 45)  # fallback to known position
        time.sleep(2.0)

    elif action == "tap_centre":
        tap(1200, 600)
        time.sleep(1.5)

    elif action == RECOVERY_WAIT:
        time.sleep(3.0)

    elif action == "open_world_map_from_sea":
        # Delegate to existing world map open logic (_frame param is unused internally)
        from actions.sail_actions import _open_world_map_from_sea
        _open_world_map_from_sea(None)

    else:
        logger.warning(f"  [recovery] Unknown transition action {action!r} — pressing Back")
        press_back()
        time.sleep(2.0)


# ── Atomic flow advancement ───────────────────────────────────────────────────

def advance_mandatory_flow(
    frame,
    prefer_complete: bool = False,
) -> Optional[tuple]:
    """
    Advance one step of a mandatory dialog flow that is blocking navigation.

    Some dialogs (negotiation, purchase result, confirmation) cannot be
    back-dismissed — the bot must respond to them before Home/Back work.

    prefer_complete=False (default): decline first (No/Cancel), then OK.
      Safe for negotiation: declines the offer, purchase still completes.
    prefer_complete=True: complete first (OK/Confirm), then No/Cancel.
      Used when decline made no progress — the flow has moved to a result
      step that only has an OK button.

    Returns the (x, y) tapped, or None if nothing found.
    """
    from actions.sail_actions import _find_button
    from actions.adb_actions import tap

    if prefer_complete:
        order = [("ok", "confirm", "done"), ("no", "cancel", "close", "x")]
    else:
        order = [("no", "cancel", "close", "x"), ("ok", "confirm", "done")]

    for labels in order:
        btn = _find_button(frame, *labels)
        if btn:
            label_str = "/".join(labels[:2])
            logger.info(
                f"  [flow] {'Completing' if prefer_complete else 'Declining'} "
                f"dialog step ({label_str}) — tapping {btn}"
            )
            tap(*btn)
            time.sleep(2.0)
            return btn

    logger.debug("  [flow] No dialog button found — tapping centre")
    tap(1200, 600)
    time.sleep(1.0)
    return None


# ── Autonomous flow-step learning ────────────────────────────────────────────

def _ask_claude_for_flow_step(frame, flow_id: str) -> Optional[dict]:
    """
    Send a screenshot to Claude and ask it to classify the current (unknown)
    step within flow_id.  Returns a step-definition dict ready to write to
    flows.json, or None if Claude is unavailable.
    """
    import base64, io, json as _json, os
    try:
        import anthropic
    except ImportError:
        logger.warning("  [recovery] anthropic not installed — cannot learn flow step")
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("  [recovery] ANTHROPIC_API_KEY not set — cannot learn flow step")
        return None

    from brain.fsm_registry import get_fsm_registry
    flow = get_fsm_registry().flows.get(flow_id)
    known_steps = [s.get("id") for s in (flow._raw.get("steps", []) if flow else [])]

    thumb = frame.copy()
    thumb.thumbnail((1200, 540))
    buf = io.BytesIO()
    thumb.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    prompt = f"""\
The bot is inside the '{flow_id}' flow in Uncharted Waters Origin but cannot identify
the current step from its KB.  Known steps: {known_steps}.

Look at the screenshot and return a JSON step definition so the bot can recognise
and advance this step automatically from now on.

Screen resolution: 2400×1080 (landscape). Image sent at half resolution — report
all coordinates in the ORIGINAL 2400×1080 space.

Respond ONLY with valid JSON:
{{
  "step_id": "short_snake_case_id",
  "description": "one-line description of this step",
  "detection_keywords": ["unique short phrase ONLY visible on this specific dialog/screen"],
  "recovery_action": "tap_button",
  "recovery_button_labels": ["label1", "label2"],
  "recovery_region": {{"y_min_pct": 0.5, "x_min_pct": 0.5}}
}}

Rules:
- detection_keywords: 1–2 short phrases that uniquely identify THIS step and will NOT
  appear on other steps in the same flow.  Prefer column headers, dialog titles, or
  unique button labels over generic words.
- recovery_region: omit or use {{}} if the action button is centred and unambiguous.
  Add y_min_pct / x_min_pct when the same label appears elsewhere on screen.
- recovery_action: almost always "tap_button"; use "wait" only for loading screens.
"""

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        raw = response.content[0].text.strip()
        logger.debug(f"  [recovery] Claude flow-step response: {raw[:200]}")
        if not raw:
            return None
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return _json.loads(raw)
    except Exception as e:
        logger.warning(f"  [recovery] _ask_claude_for_flow_step failed: {e}")
        return None


def _save_flow_step_to_kb(flow_id: str, step_def: dict) -> None:
    """
    Write a newly learned step into flows.json and insert it into the
    flow_detection_order in ui_signals.json, then reload the KB.
    New steps are inserted BEFORE existing entries for the same flow so that
    more specific dialogs are checked first.
    """
    import json as _json
    from pathlib import Path

    step_id = step_def.get("step_id")
    if not step_id:
        logger.warning("  [recovery] Learned step has no step_id — skipping KB write")
        return

    # ── 1. Update flows.json ──────────────────────────────────────────────────
    flows_path = Path("memory/knowledge/fsm/flows.json")
    try:
        flows = _json.loads(flows_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"  [recovery] Cannot read flows.json: {e}")
        return

    saved = False
    for flow in flows:
        if flow.get("id") != flow_id:
            continue

        # Update step_detection_keywords
        kws = step_def.get("detection_keywords", [])
        if kws:
            flow.setdefault("step_detection_keywords", {})[step_id] = kws

        # Add step entry if not already present
        existing_ids = [s.get("id") for s in flow.get("steps", [])]
        if step_id not in existing_ids:
            new_step: dict = {
                "id":          step_id,
                "description": step_def.get("description", ""),
                "recovery_action": step_def.get("recovery_action", RECOVERY_TAP_OK_OR_X),
                "recovery_button_labels": step_def.get("recovery_button_labels", []),
            }
            region = step_def.get("recovery_region") or {}
            if region:
                new_step["recovery_region"] = region
            flow.setdefault("steps", []).append(new_step)
            logger.info(f"  [recovery] Added new step '{flow_id}/{step_id}' to flows.json")
        else:
            logger.info(f"  [recovery] Step '{flow_id}/{step_id}' already in flows.json — updated keywords")

        flows_path.write_text(_json.dumps(flows, indent=2, ensure_ascii=False))
        saved = True
        break

    if not saved:
        logger.warning(f"  [recovery] Flow {flow_id!r} not found in flows.json")
        return

    # ── 2. Insert into flow_detection_order in ui_signals.json ───────────────
    signals_path = Path("memory/knowledge/control/ui_signals.json")
    try:
        signals = _json.loads(signals_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"  [recovery] Cannot read ui_signals.json: {e}")
        return

    order: list = signals.get("flow_detection_order", [])
    pair = [flow_id, step_id]
    if pair not in order:
        # Insert before the first existing entry for this flow so the new (more
        # specific) step is checked first within the flow's detection group.
        insert_pos = next(
            (i for i, p in enumerate(order) if p[0] == flow_id),
            len(order),
        )
        order.insert(insert_pos, pair)
        signals["flow_detection_order"] = order
        signals_path.write_text(_json.dumps(signals, indent=2, ensure_ascii=False))
        logger.info(
            f"  [recovery] Inserted [{flow_id!r}, {step_id!r}] into flow_detection_order "
            f"at position {insert_pos}"
        )

    # ── 3. Reload KB ──────────────────────────────────────────────────────────
    from brain.kb import reload as reload_kb
    reload_kb()
    logger.info(f"  [recovery] KB reloaded after learning step '{flow_id}/{step_id}'")


def _advance_active_flow(frame, result, tracker: Optional["_FlowAttemptTracker"] = None) -> None:
    """
    Drive an active atomic flow one step forward using the perceive result.
    Fully KB-driven: button labels and recovery actions are read from
    flows.json step definitions via ControlKB.flow_step_recovery().
    No game strings are hardcoded here.

    Flow-completeness bookkeeping (CLAUDE.md guideline): if `tracker` is
    supplied it is used; otherwise we fall back to the module-level
    singleton so every callsite participates in the same attempt without
    each one having to plumb the tracker through.
    """
    from actions.adb_actions import tap
    from actions.sail_actions import _find_button
    from brain.kb import control as _ckb

    if tracker is None:
        # Ensure the singleton is in sync with the perceived flow.  This
        # initialises the tracker the first time _advance_active_flow runs
        # for a given flow, even when no goal-loop called notify_perceive_result.
        notify_perceive_result(result)
        tracker = get_active_tracker()

    flow = result.flow
    step = result.flow_step or "unknown"
    logger.info(f"  [recovery] Active flow {flow!r} at step {step!r} — advancing")

    # ── Unknown step: ask Claude to classify and teach the bot ────────────────
    if step == "unknown":
        logger.info(
            f"  [recovery] Step unknown in flow {flow!r} — asking Claude to classify"
        )
        step_def = _ask_claude_for_flow_step(frame, flow)
        if step_def:
            _save_flow_step_to_kb(flow, step_def)
            # Execute the action Claude prescribed directly from the learned def
            action  = step_def.get("recovery_action", RECOVERY_TAP_OK_OR_X)
            labels  = step_def.get("recovery_button_labels", [])
            region_pct = step_def.get("recovery_region") or {}
        else:
            logger.warning(
                f"  [recovery] Claude could not classify step — falling back"
            )
            advance_mandatory_flow(frame, prefer_complete=True)
            time.sleep(2.0)
            return
    else:
        action, labels, region_pct = _ckb().flow_step_recovery(flow, step)

    if action == RECOVERY_TAP_BUTTON and labels:
        # Convert fractional region to pixel constraints for this frame
        fh, fw = frame.height, frame.width
        kwargs: dict = {}
        if region_pct.get(RGN_Y_MIN) is not None:
            kwargs["y_min"] = int(fh * region_pct[RGN_Y_MIN])
        if region_pct.get(RGN_Y_MAX) is not None:
            kwargs["y_max"] = int(fh * region_pct[RGN_Y_MAX])
        if region_pct.get(RGN_X_MIN) is not None:
            kwargs["x_min"] = int(fw * region_pct[RGN_X_MIN])
        if region_pct.get(RGN_X_MAX) is not None:
            kwargs["x_max"] = int(fw * region_pct[RGN_X_MAX])

        btn = _find_button(frame, *labels, **kwargs)
        if btn:
            logger.info(f"  [recovery] Tapping {step!r} button {labels[0]!r} @ {btn} (region={region_pct or 'none'})")
            tap(*btn)
            # Flow-completeness bookkeeping: if the step we just executed is
            # categorised as a positive transaction, bump the counter so the
            # eventual record_flow_outcome call can mark the flow complete.
            if tracker is not None:
                tracker.note_step_executed(flow, step)
        else:
            logger.warning(f"  [recovery] No button found for labels {labels} region={region_pct} — using advance_mandatory_flow")
            advance_mandatory_flow(frame, prefer_complete=True)
    elif action == RECOVERY_WAIT:
        time.sleep(3.0)
    else:
        # Fallback for unknown action or missing step definition
        advance_mandatory_flow(frame, prefer_complete=False)

    time.sleep(2.0)


# ── Sea recovery ──────────────────────────────────────────────────────────────

def _dismiss_fleet_defeat_dialog(frame) -> bool:
    """Handle the Quick-Revive-or-Port-Return dialog (fleet defeat).

    Dispatches to the existing interruptor entry `fleet_death_recovery_screen`
    in memory/knowledge/fsm/interruptors.json, which has the proper
    chained dismissal recipe (tap Port Return card → wait → tap Port
    Return text → wait → tap OK).  Without this dispatch the recovery
    layer's blind Back/Home is a no-op on this modal and the bot loops
    indefinitely on a dead fleet.

    Returns True when the dismissal was dispatched, False if the
    interruptor record is missing (in which case the caller falls back
    to blind Back/Home).  The actual tap success isn't measured here —
    perceive() will re-classify on the next iteration and recovery
    will either move on (typically to `sea`) or retry.
    """
    iid = "fleet_death_recovery_screen"
    try:
        from brain.fsm_registry import get_fsm_registry
        from brain.perceive import _dismiss_interruptor
        interruptor = get_fsm_registry().interruptors.get(iid)
    except Exception as e:
        logger.warning(f"  [recovery] fleet-defeat dispatch lookup failed: {e}")
        return False

    if interruptor is None:
        logger.warning(
            f"  [recovery] interruptor {iid!r} not in registry — "
            "fleet-defeat dialog cannot be dismissed via interruptor path"
        )
        return False

    logger.info(
        f"  [recovery] fleet-defeat dialog detected — dispatching "
        f"interruptor {iid!r} dismissal "
        f"({interruptor._raw.get('dismissal', '?')!r})"
    )
    try:
        _dismiss_interruptor(iid, frame)
    except Exception as e:
        logger.warning(f"  [recovery] fleet-defeat dispatch raised: {e}")
        return False
    return True


def _recover_from_sea(home_port: Optional[str], timeout: float) -> bool:
    """
    From sea state, return to port_overworld.
    - If home_port given: open world map and sail there.
    - If not: wait for natural arrival (ship was already sailing somewhere).
    Returns True if port_overworld reached, False on timeout.
    """
    from brain.perceive import perceive

    if home_port:
        logger.info(f"  [recovery] At sea — sailing to home port {home_port!r}")
        try:
            from actions.sail_actions import sail_to_port
            ok = sail_to_port(home_port, from_building=False)
            if ok:
                return True
        except Exception as e:
            logger.warning(f"  [recovery] sail_to_port failed: {e}")

    # Wait for natural arrival
    logger.info("  [recovery] At sea — waiting for natural port arrival")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(10.0)
        r = perceive()
        if r.state == "port_overworld":
            return True
        if r.state not in ("sea", "sea_cinematic", "loading"):
            logger.warning(f"  [recovery] Unexpected state while waiting at sea: {r.state!r}")
            break

    return False


# ── Blocking signal resolution ────────────────────────────────────────────────

# Mapping of blocker text → goal_id.  Each entry routes a specific
# blocking signal to the planner architecture.  When a blocker is added
# to flows.json:*.blocking_signals with action='resolve', register its
# goal here and seed a Plan + Goal + cue catalog in
# memory/knowledge/{plans,verification}/.
_BLOCKER_TO_GOAL: dict[str, str] = {
    "not enough crew": "has_enough_crew",
    # Future:
    #   "not enough supply": "has_enough_supply",
}


def execute_resolution(signal: dict, context: str = "") -> bool:
    """
    Resolve a blocking signal via the planner architecture.

    The blocker text (e.g. "not enough crew") is mapped to a Goal id
    (e.g. has_enough_crew); the planner is asked to drive the bot to
    that goal.  Each goal has its own seeded Plan(s) under
    memory/knowledge/plans/<goal_id>/, its own cue catalog under
    memory/knowledge/verification/<goal_id>.json, and its own wired
    entry point in brain/known_goals.py.

    See docs/planner_architecture.md for the full architecture.

    Parameters:
      signal:  a blocking_signals entry from flows.json with
               action="resolve" — only the 'text' field is consulted
               here; the 'resolution' array (the legacy 6-step recipe)
               is loaded by PlanRuntime.lookup() as a starting Plan
               candidate, not walked directly.
      context: human-readable description of where the block was detected

    Returns True if the goal was achieved (caller can retry the
    original action), False if the planner gave up.

    Migration note (2026-05-02): replaced the legacy recipe-walk
    machinery (RES_EXIT_TO_OVERWORLD / RES_NAVIGATE_TO_BUILDING /
    RES_TAP_PRIMARY_ACTION / RES_RETRY) with planner dispatch.  The
    legacy machinery walked steps blindly without per-step verification
    and only invoked Claude reasoning at failure boundaries; the
    planner runs light_check after every step and heavy_check at
    checkpoints, with replan-on-uncertainty filling in missing
    sub-steps automatically.  See git history for the pre-migration
    implementation.
    """
    blocker_text = (signal.get("text") or "").lower().strip()
    goal_id = _BLOCKER_TO_GOAL.get(blocker_text)

    logger.info(
        f"  [resolution] blocker={blocker_text!r}  goal={goal_id!r}"
        + (f"  context={context!r}" if context else "")
    )

    if goal_id is None:
        logger.warning(
            f"  [resolution] no goal mapping for blocker {blocker_text!r} — "
            "cannot resolve via planner; caller should escalate"
        )
        return False

    if goal_id == "has_enough_crew":
        from brain.known_goals import achieve_has_enough_crew
        result = achieve_has_enough_crew()
        if result.success:
            logger.info(
                f"  [resolution] planner achieved {goal_id!r}  "
                f"plan={result.plan.plan_id if result.plan else '?'!r}  "
                f"steps_executed={len(result.history)}  "
                f"duration={result.duration_secs:.1f}s"
            )
            return True
        logger.warning(
            f"  [resolution] planner did not achieve {goal_id!r}  "
            f"reason={result.reason!r}  "
            f"steps_executed={len(result.history)}  "
            f"duration={result.duration_secs:.1f}s"
        )
        return False

    # New goal mapped but no entry-point wired yet — fail loudly so the
    # mapping is completed before the next live run.
    logger.error(
        f"  [resolution] goal {goal_id!r} mapped but no entry-point function "
        "registered in execute_resolution; add a branch above"
    )
    return False

    return True


def _tap_building_action(building_type: str, sub_menu_id: str) -> bool:
    """
    Tap the primary action button for a specific sub-menu in a building.
    Button labels are read from building_types/<building_type>.json KB —
    no game strings are hardcoded here.

    Each sub_menu may include an optional `region` field with the same
    pct-based shape used by flow recovery_region (y_min_pct, y_max_pct,
    x_min_pct, x_max_pct).  When present it constrains the OmniParser/OCR
    label search so substring matches on incidental top-chrome elements
    (back arrow tooltips, NPC dialogue text containing "recruit") are
    rejected.

    After tapping, the function verifies the screen title changed.  If the
    screen is identical to before the tap, the action did not register and
    the function returns False so the caller can escalate.

    Returns True if a button was found, tapped, and the screen advanced.
    """
    import json as _json
    import time as _time
    from pathlib import Path
    from actions.adb_actions import tap
    from actions.sail_actions import _find_button
    from capture.adb_capture import capture_screen
    from vision.ocr import read_screen_title
    from brain.kb import RGN_Y_MIN, RGN_Y_MAX, RGN_X_MIN, RGN_X_MAX

    kb_path = Path("memory/knowledge/building_types") / f"{building_type}.json"
    if not kb_path.exists():
        logger.warning(f"  [resolution] No KB entry for building_type {building_type!r}")
        return False

    try:
        data = _json.loads(kb_path.read_text())
    except Exception as e:
        logger.warning(f"  [resolution] Cannot read {kb_path}: {e}")
        return False

    # Find the sub-menu entry and read its labels
    sub_menus = data.get("sub_menus", [])
    sm = next((s for s in sub_menus if s.get("id") == sub_menu_id), None)
    if sm is None:
        logger.warning(
            f"  [resolution] Sub-menu {sub_menu_id!r} not found in {building_type!r} KB"
        )
        return False

    labels = sm.get("labels", [])
    if not labels:
        logger.warning(f"  [resolution] Sub-menu {sub_menu_id!r} has no labels in KB")
        return False

    frame = capture_screen()

    # Default region for any building's sub-menu items: the LEFT-side
    # vertical list that sits below the title bar.  This is a universal
    # UWO building convention — back-arrow + screen title at the top,
    # then a vertical list of selectable menu items down the left.
    # Restricting the search to that quadrant avoids substring matches on
    # mid-screen NPC mate descriptions, sign text, or right-panel content
    # that happen to contain the same word as the menu item ("Recruit
    # Crew" can appear as a heading on a mate's profile etc.).
    # Per-sub_menu region in KB overrides this default when set.
    DEFAULT_SUB_MENU_REGION = {"y_min_pct": 0.10, "x_max_pct": 0.30}
    region_pct = sm.get("region") or DEFAULT_SUB_MENU_REGION
    fw, fh = frame.width, frame.height
    kwargs: dict = {}
    if region_pct.get(RGN_Y_MIN) is not None:
        kwargs["y_min"] = int(fh * region_pct[RGN_Y_MIN])
    if region_pct.get(RGN_Y_MAX) is not None:
        kwargs["y_max"] = int(fh * region_pct[RGN_Y_MAX])
    if region_pct.get(RGN_X_MIN) is not None:
        kwargs["x_min"] = int(fw * region_pct[RGN_X_MIN])
    if region_pct.get(RGN_X_MAX) is not None:
        kwargs["x_max"] = int(fw * region_pct[RGN_X_MAX])

    # Snapshot the screen title pre-tap so we can verify the action took effect.
    title_before = read_screen_title(frame)

    btn = _find_button(frame, *labels, **kwargs)
    if not btn:
        logger.warning(
            f"  [resolution] Action button not found for {building_type!r}/{sub_menu_id!r} "
            f"(labels: {labels}, region={region_pct or 'none'})"
        )
        # Diagnostic dump (Fix B): list every OmniParser element AND OCR
        # token whose centre falls inside the search region so the log
        # alone is enough to diagnose why the labelled button wasn't
        # matched.  Without this, a single "not found" line is opaque —
        # was the label rendered differently? was the region wrong? was
        # the menu absent because of an overlay?  The dump answers all
        # three from the log without needing to re-capture the screen.
        try:
            from vision.omniparser import parse_fast_cached
            from actions.sail_actions import _ocr_frame
            x_lo = kwargs.get("x_min", 0)
            y_lo = kwargs.get("y_min", 0)
            x_hi = kwargs.get("x_max", frame.width)
            y_hi = kwargs.get("y_max", frame.height)
            try:
                elements = parse_fast_cached(frame)
            except Exception:
                elements = []
            in_region_om = [
                e for e in elements
                if x_lo <= e.cx <= x_hi and y_lo <= e.cy <= y_hi
            ]
            tokens = _ocr_frame(frame, min_conf=0.30)
            in_region_ocr = [
                (text, conf, cx, cy) for (text, conf, cx, cy) in tokens
                if x_lo <= cx <= x_hi and y_lo <= cy <= y_hi
            ]
            logger.warning(
                f"  [resolution]   region pixels: x=[{x_lo},{x_hi}] y=[{y_lo},{y_hi}] "
                f"(frame {frame.width}×{frame.height})"
            )
            logger.warning(
                f"  [resolution]   OmniParser saw {len(in_region_om)} element(s) in region:"
            )
            for e in in_region_om[:20]:
                logger.warning(
                    f"  [resolution]     [{e.element_type}] {e.label!r} @ ({e.cx},{e.cy})"
                )
            if len(in_region_om) > 20:
                logger.warning(f"  [resolution]     … and {len(in_region_om) - 20} more")
            logger.warning(
                f"  [resolution]   OCR saw {len(in_region_ocr)} token(s) in region:"
            )
            for (text, conf, cx, cy) in in_region_ocr[:20]:
                logger.warning(
                    f"  [resolution]     {text!r} conf={conf:.2f} @ ({cx},{cy})"
                )
            if len(in_region_ocr) > 20:
                logger.warning(f"  [resolution]     … and {len(in_region_ocr) - 20} more")
        except Exception as e:
            logger.debug(f"  [resolution]   diagnostic dump failed: {e}")
        return False

    logger.info(
        f"  [resolution] Tapping {building_type!r}/{sub_menu_id!r} action @ {btn} "
        f"(region={region_pct or 'none'})"
    )
    tap(*btn)
    _time.sleep(2.0)

    # Dismiss any result/confirm dialog that may appear
    frame = capture_screen()
    from actions.sail_actions import _find_button as _fb
    ok_btn = _fb(frame, "ok", "confirm", "close", "collect")
    if ok_btn:
        logger.info(f"  [resolution] Dismissing post-action dialog @ {ok_btn}")
        tap(*ok_btn)
        _time.sleep(1.5)
        frame = capture_screen()

    # Verify the screen actually advanced.  Title DOES change when a sub-menu
    # is tapped successfully (e.g. "Inn" → "Recruit Crew") — title-unchanged
    # means the tap missed (substring matched chrome text or a mid-screen NPC
    # description instead of the real menu item).  Returning False here lets
    # the failure path in execute_resolution invoke the Claude-guided loop
    # to find and tap the real button.
    title_after = read_screen_title(frame)
    if not ok_btn and title_after == title_before:
        logger.warning(
            f"  [resolution] Screen title unchanged after tap "
            f"({title_before!r}) — {sub_menu_id!r} action likely did not register"
        )
        return False

    return True


# Confirmation words on a NOTICE/confirm dialog whose positive button clears it.
# Deliberately excludes commit/spend verbs (Purchase/Buy/Pay) — this is a safe
# "acknowledge a blocking dialog" tap, not a transaction.
_DIALOG_OK_WORDS = frozenset({"ok", "confirm", "continue", "yes"})


def _tap_dialog_ok(frame) -> bool:
    """Tap a blocking confirmation dialog's OK/Continue/Yes button, if present.

    The 'different action' recovery tries when press_back keeps bouncing off a
    confirmation Notice (e.g. "Moving to another menu will empty the cart.
    Continue?" — OK empties the cart and lets us leave). Returns True if a
    positive confirmation button was found and tapped. Never taps a commit/spend
    button (those words aren't in _DIALOG_OK_WORDS).
    """
    from actions.adb_actions import tap
    try:
        from actions.sail_actions import _ocr_frame
        tokens = _ocr_frame(frame, min_conf=0.4)
    except Exception as exc:
        logger.debug(f"  [recovery] _tap_dialog_ok OCR failed: {exc}")
        return False
    for text, _conf, cx, cy in tokens:
        if text.strip().lower() in _DIALOG_OK_WORDS:
            logger.info(f"  [recovery] variation: tapping dialog '{text}' @ ({cx},{cy}) "
                        "(attempt-memory: press_back wasn't working)")
            tap(cx, cy)
            return True
    return False


# ── Primary recovery function ─────────────────────────────────────────────────

def recover_to_port_overworld(
    home_port: Optional[str] = None,
    timeout: float = 300.0,
    _after_escalation: bool = False,
) -> "brain.perceive.PerceiveResult":
    """
    From any state, navigate back to port_overworld using FSM BFS paths.

    - Atomic flows are advanced until complete before navigation.
    - Interruptors are handled by perceive() automatically.
    - Sea state: sails to home_port or waits for natural arrival.
    - Other states: follows FSM BFS path (precomputed at registry load time).

    Returns the final PerceiveResult. Check result.state == "port_overworld".
    Never raises.
    """
    from capture.adb_capture import capture_screen
    from brain.perceive import perceive
    from brain.fsm_registry import get_fsm_registry
    from actions.sail_actions import press_back
    from actions.adb_actions import tap

    from brain.perceive import reclassify_with_claude

    registry = get_fsm_registry()
    deadline = time.time() + timeout
    MAX_ATTEMPTS = 20
    attempt = 0
    last_signature = None       # (state, flow, flow_step) — for blind-back threshold
    last_flow_signature = None  # (state, flow) — for reclassification trigger
    stall_count = 0

    # Phase 4 + flow-completeness bookkeeping (CLAUDE.md guideline) is now
    # shared across the whole goal-loop via the module-level singleton in
    # this file.  Calling notify_perceive_result(result) on every iteration
    # detects flow exit (and start) and records the outcome automatically.

    while time.time() < deadline and attempt < MAX_ATTEMPTS:
        attempt += 1
        frame  = capture_screen()
        # Dismiss non-game blockers (lock/screensaver 'Slide up to unlock',
        # promo/store popups) first — the FSM has no path out of them, so without
        # this recovery stalls and escalates to the (headless) teaching loop.
        # See brain/unexpected_dialog.clear_blockers.
        try:
            from brain.unexpected_dialog import clear_blockers
            if clear_blockers(frame).get("cleared"):
                time.sleep(1.0)
                frame = capture_screen()
        except Exception as exc:
            logger.debug(f"  [recovery] clear_blockers failed: {exc}")
        result = perceive(frame)
        state  = result.state

        logger.info(
            f"  [recovery] attempt {attempt}: state={state!r}  "
            f"flow={result.flow!r}  step={result.flow_step!r}  "
            f"confidence={result.confidence!r}  detail={result.detail!r}"
        )

        # ── Flow-completion verification + completeness tracking ─────────
        # notify_perceive_result transitions the module-level tracker:
        # detects flow EXIT (perceived flow != active tracker) and records
        # the outcome; detects flow START (perceived flow active, tracker
        # absent) and initialises a new attempt.  All callsites — sail_to,
        # recover_to_port_overworld, future goals — share the same state.
        notify_perceive_result(result)

        # ── Perception reset: correct wrong state before acting ────────────
        # Trigger when confidence is low OR we've been stuck at the same
        # (state, flow) for 2 iterations — local perception is wrong.
        # Use (state, flow) not (state, flow, step) so that step oscillation
        # (e.g. basket→negotiate→basket) is treated as a stall, not progress.
        signature = (state, result.flow, result.flow_step)
        flow_signature = (state, result.flow)
        if result.confidence == CONFIDENCE_LOW or (flow_signature == last_flow_signature and stall_count >= 2):
            logger.warning(
                f"  [recovery] Perception uncertain (confidence={result.confidence!r}, "
                f"stall={stall_count}) — re-classifying with Claude"
            )
            result = reclassify_with_claude(frame, result)
            state  = result.state
            logger.info(
                f"  [recovery] After reclassification: state={state!r}  "
                f"flow={result.flow!r}  step={result.flow_step!r}"
            )
            # ── Stall-triggered step learning ─────────────────────────────────
            # If Claude identified a flow step that has no local detection
            # keywords, the bot has never seen this step before.  Learn it now
            # so future runs detect it without needing reclassification.
            if result.corrected and result.in_flow and result.flow_step:
                from brain.kb import control as _ckb
                known_kws = _ckb().flow_step_keywords(result.flow, result.flow_step)
                if not known_kws:
                    logger.info(
                        f"  [recovery] Step {result.flow_step!r} in flow {result.flow!r} "
                        "has no local detection keywords — learning from screenshot"
                    )
                    step_def = _ask_claude_for_flow_step(frame, result.flow)
                    if step_def:
                        _save_flow_step_to_kb(result.flow, step_def)
            # Reset stall counter — we now have a fresh, authoritative perception
            stall_count = 0

        # ── Already at target ──────────────────────────────────────────────
        if state == "port_overworld":
            logger.info("  [recovery] Reached port_overworld")
            return result

        # ── Stall tracking ────────────────────────────────────────────────
        # flow_signature used for reclassification (catches step oscillation).
        # full signature used for blind-back threshold (truly stuck same step).
        if flow_signature == last_flow_signature:
            stall_count += 1
            if stall_count >= 6:
                logger.warning(f"  [recovery] Hard stall at flow={result.flow!r} — blind back+home")
                # If a flow was active when we resorted to blind back+home,
                # this is a Cancel for the flow-completeness rule (CLAUDE.md):
                # leaving the current state via Back/Home does not count as
                # flow progress.
                tracker = get_active_tracker()
                if tracker is not None:
                    tracker.note_cancelled()
                press_back()
                time.sleep(2.0)
                tap(2300, 45)  # home region
                time.sleep(2.0)
                stall_count = 0
        else:
            stall_count = 0
        last_signature = signature
        last_flow_signature = flow_signature

        # ── Active atomic flow — must advance before navigating ────────────
        if result.in_flow:
            # The module-level tracker was already initialised by
            # notify_perceive_result(result) at the top of this iteration.
            _advance_active_flow(frame, result)
            continue

        # ── Sea / sea_cinematic — special path ─────────────────────────────
        if state in ("sea", "sea_cinematic"):
            # Wake cinematic first
            if state == "sea_cinematic":
                tap(1200, 600)
                time.sleep(2.0)
            success = _recover_from_sea(home_port, timeout=min(timeout, deadline - time.time()))
            # Re-enter loop to re-perceive after sailing
            continue

        # ── Loading — just wait ────────────────────────────────────────────
        if state == "loading":
            time.sleep(3.0)
            continue

        # ── Fleet-defeat / Quick-Revive-or-Port-Return dialog ─────────────
        # The learned fingerprint `learned_quick_revive_port_return`
        # classifies this screen as a navigation state, but recovery via
        # Back/Home is a no-op (the dialog requires choosing one of the
        # two cards).  Dispatch to the interruptor dismissal that knows
        # to pick Port Return (the safe, free option).  Without this,
        # the bot loops on a dead fleet indefinitely.
        if state == "learned_quick_revive_port_return":
            success = _dismiss_fleet_defeat_dialog(frame)
            if success:
                time.sleep(2.0)
                continue
            # Fall through to blind Back/Home only if dismissal failed.

        # ── Clear a blocking confirmation dialog BEFORE the FSM path ──────────
        # press_back bounces off confirmation dialogs whose positive button is OK
        # (e.g. "Moving to another menu will empty the cart. Continue?"), so the
        # FSM's press_back loops forever. Do something DIFFERENT: if a
        # confirmation dialog is on screen, tap its OK instead of pressing Back.
        # Checked EVERY iteration (not gated on a stuck counter) because the
        # reclassification above resets any per-state counter, which previously
        # let this be preempted and never fire. Safe: _tap_dialog_ok only taps
        # OK/Continue/Yes, never a commit/spend button. (User 2026-08-12: "if
        # seeing the same thing and can't get where it wants, do something else.")
        if state != "port_overworld" and _tap_dialog_ok(frame):
            time.sleep(1.5)
            continue

        # ── All other states: follow FSM BFS recovery path ────────────────
        path = registry.recovery_path(state)

        if not path:
            # Unknown state or no path — blind recovery
            logger.warning(
                f"  [recovery] No FSM path from {state!r} — "
                "trying Back then Home"
            )
            press_back()
            time.sleep(2.0)
            tap(2300, 45)
            time.sleep(2.0)
            continue

        # Execute the first transition in the BFS path and re-perceive.
        # (Don't batch: each step may open a different state needing re-check.)
        transition = path[0]
        logger.info(
            f"  [recovery] FSM path from {state!r}: "
            f"action={transition.action!r} → {transition.to!r}"
        )
        _execute_transition(transition.action, frame)

    # Timeout / max attempts — escalate to human operator
    final = perceive()
    if final.state != "port_overworld":
        if _after_escalation:
            # Already escalated once — don't loop again; let caller handle it
            logger.error("  [recovery] Still stuck after post-escalation recovery — giving up")
            return final
        logger.error(
            f"  [recovery] Failed to reach port_overworld after {attempt} attempts "
            f"(timeout={timeout}s) — escalating to human operator"
        )
        from brain.human_escalation import escalate
        final = escalate(context=f"recover_to_port_overworld (home={home_port})", perceive_result=final)
        if final.state == "port_overworld":
            return final
        # Human resolved something (e.g. tapped OK on a dialog) but we may not be
        # at port_overworld yet — give the bot one more recovery pass with a fresh
        # short timeout rather than immediately failing.
        logger.info("  [recovery] Escalation applied — retrying recovery (120s)")
        final = recover_to_port_overworld(home_port=home_port, timeout=120.0, _after_escalation=True)
    return final


# ── Entry guards ──────────────────────────────────────────────────────────────

def assert_at_port(context: str = "", home_port: Optional[str] = None) -> "brain.perceive.PerceiveResult":
    """
    Call at the start of any action that requires being at port_overworld.

    If already there, returns immediately. Otherwise calls recover_to_port_overworld().
    Raises RecoveryError if recovery fails, so the caller knows not to proceed.
    """
    from brain.perceive import perceive

    result = perceive()
    if result.state == "port_overworld":
        return result

    logger.warning(
        f"  [recovery] Not at port_overworld before {context!r}: "
        f"state={result.state!r} — recovering"
    )
    result = recover_to_port_overworld(home_port=home_port)
    if result.state != "port_overworld":
        raise RecoveryError(
            f"Could not reach port_overworld before {context!r}; "
            f"stuck at {result.state!r}"
        )
    return result


# ── Backward-compatible wrappers ──────────────────────────────────────────────

_STABLE = {"port_overworld", "sea", "sea_cinematic", "world_map"}


def recover_to_known_state(timeout: float = 60.0) -> dict:
    """
    Backward-compatible wrapper.  Delegates to recover_to_port_overworld().
    Returns a where_am_i()-style dict.
    """
    result = recover_to_port_overworld(home_port=None, timeout=timeout)
    return result.to_location_dict()


def assert_stable(context: str = "", home_port: Optional[str] = None) -> dict:
    """
    Backward-compatible wrapper for assert_at_port().
    Unlike the old version, this now targets port_overworld specifically
    rather than any "stable" state.  Returns a where_am_i()-style dict.
    """
    from brain.perceive import perceive

    result = perceive()
    if result.state in _STABLE:
        return result.to_location_dict()

    logger.warning(
        f"  [recovery] Unstable state before {context!r}: "
        f"{result.state!r} — recovering"
    )
    result = recover_to_port_overworld(home_port=home_port, timeout=60.0)
    return result.to_location_dict()
