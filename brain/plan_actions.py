# brain/plan_actions.py
#
# Action dispatcher — maps PlanStep.action dicts to live actuation helpers.
#
# Layer 4a of the migration.  Bridges the architecture-level Plan
# representation (which uses untyped action dicts so the planner can
# generate / amend plans without referencing implementation details)
# to the existing tap / navigate / press_back primitives that actually
# move the bot.
#
# Action shape examples:
#   {"kind": "tap", "x": 100, "y": 200}
#   {"kind": "press_back"}
#   {"kind": "tap_button", "labels": ["recruit"], "region": {...}}
#   {"kind": "navigate_to", "target": "inn"}
#   {"kind": "exit_to_port_overworld"}
#   {"kind": "tap_primary_action", "building": "inn", "sub_menu": "recruit_crew"}
#   {"kind": "wait", "wait_seconds": 2}
#
# The dispatcher is plug-replaceable: callers of achieve_goal can pass any
# callable as `execute_step_fn`.  The default `execute_step` here uses the
# existing live helpers; tests inject stubs to exercise the dispatcher
# without ADB / OCR.

from __future__ import annotations

from typing import Any, Callable, Optional

from loguru import logger


class StepExecutionError(Exception):
    """Raised when a PlanStep.action cannot be executed (button not found,
    navigation failed, unknown action kind, etc.).  achieve_goal catches
    this and records a step failure with REASON_EXECUTE_RAISED."""


# ── Region-pct → pixel conversion (mirror of recovery.py's logic) ────────────


def _region_pct_to_kwargs(region_pct: Optional[dict], frame_w: int, frame_h: int) -> dict:
    """
    Convert a normalised region descriptor into pixel
    {"x_min", "x_max", "y_min", "y_max"} kwargs that _find_button accepts.

    Two formats are supported.  The original explicit-bounds format:
        {"x_min_pct": 0.8, "x_max_pct": 0.95, "y_min_pct": 0.7, "y_max_pct": 0.85}
    And the rectangle format Claude replan generates naturally:
        {"x": 0.82, "y": 0.75, "w": 0.16, "h": 0.10}
    where (x, y) is the top-left corner and (w, h) is the rectangle's
    width and height in normalised units.

    Without rectangle support, replan-inserted spatial constraints were
    silently dropped (key mismatch → empty kwargs → unconstrained
    search), causing 'Recruit' to match the title-bar 'Recruit Crew' at
    the top of the screen instead of the gold action button on the
    right side.  The rectangle format is converted to explicit bounds
    on the way through.
    """
    if not region_pct:
        return {}

    # Rectangle format: {x, y, w, h} → explicit bounds.
    if "x" in region_pct and "y" in region_pct and "w" in region_pct and "h" in region_pct:
        x = float(region_pct["x"]); y = float(region_pct["y"])
        w = float(region_pct["w"]); h = float(region_pct["h"])
        return {
            "x_min": int(frame_w * x),
            "x_max": int(frame_w * (x + w)),
            "y_min": int(frame_h * y),
            "y_max": int(frame_h * (y + h)),
        }

    # Explicit-bounds format.
    out: dict = {}
    if "y_min_pct" in region_pct: out["y_min"] = int(frame_h * region_pct["y_min_pct"])
    if "y_max_pct" in region_pct: out["y_max"] = int(frame_h * region_pct["y_max_pct"])
    if "x_min_pct" in region_pct: out["x_min"] = int(frame_w * region_pct["x_min_pct"])
    if "x_max_pct" in region_pct: out["x_max"] = int(frame_w * region_pct["x_max_pct"])
    return out


# ── Per-action handlers — each is a small function so they can be ────────────
# replaced individually for testing.  They are looked up via a registry
# that the default dispatcher consults; tests can register stubs at the
# registry level to swap behaviour cleanly.

ActionHandler = Callable[[dict], None]
_REGISTRY: dict[str, ActionHandler] = {}


def register_action(kind: str) -> Callable[[ActionHandler], ActionHandler]:
    """Decorator to register a handler for an action kind."""
    def deco(fn: ActionHandler) -> ActionHandler:
        _REGISTRY[kind] = fn
        return fn
    return deco


# ── Default handlers (production behaviour) ──────────────────────────────────


@register_action("tap")
def _handle_tap(action: dict) -> None:
    from actions.adb_actions import tap
    x = int(action["x"])
    y = int(action["y"])
    tap(x, y)


@register_action("press_back")
def _handle_press_back(action: dict) -> None:
    from actions.adb_actions import press_back
    press_back()


@register_action("wait")
def _handle_wait(action: dict) -> None:
    import time
    time.sleep(float(action.get("wait_seconds", 2)))


@register_action("tap_button")
def _handle_tap_button(action: dict) -> None:
    """
    Find a button by OCR label and tap.  Optional region constraint
    narrows the search rectangle (regions specified as percent of frame
    dims to be resolution-independent).
    """
    from actions.adb_actions import tap
    from actions.sail_actions import _find_button
    from capture.adb_capture import capture_screen

    frame = capture_screen()
    labels = list(action.get("labels") or [])
    if not labels and action.get("label"):
        labels = [action["label"]]
    labels = [l for l in labels if l]   # drop None/empty
    if not labels:
        raise StepExecutionError(f"tap_button: no labels supplied in action {action!r}")

    kwargs = _region_pct_to_kwargs(action.get("region"), frame.width, frame.height)
    btn = _find_button(frame, *labels, **kwargs)
    if not btn:
        raise StepExecutionError(
            f"tap_button: no button matching {labels!r} found "
            f"(region={action.get('region')})"
        )
    logger.info(f"[plan_actions] tap_button {labels[0]!r} @ {btn}")
    tap(*btn)


@register_action("navigate_to")
def _handle_navigate_to(action: dict) -> None:
    from actions.sail_actions import navigate_to_building
    target = action["target"]
    if not navigate_to_building(target):
        raise StepExecutionError(f"navigate_to: failed to enter {target!r}")


@register_action("exit_to_port_overworld")
def _handle_exit_to_port_overworld(action: dict) -> None:
    from brain.recovery import recover_to_port_overworld
    result = recover_to_port_overworld(home_port=action.get("home_port"))
    if result.state != "port_overworld":
        raise StepExecutionError(
            f"exit_to_port_overworld: did not reach port_overworld "
            f"(ended at {result.state!r})"
        )


@register_action("tap_primary_action")
def _handle_tap_primary_action(action: dict) -> None:
    """
    Open a building's primary sub-menu (e.g. inn → recruit_crew).
    Mirrors brain.recovery._tap_building_action.
    """
    from brain.recovery import _tap_building_action
    building = action["building"]
    sub_menu = action.get("sub_menu") or action.get("primary_action")
    if not sub_menu:
        raise StepExecutionError(
            f"tap_primary_action: no sub_menu/primary_action in {action!r}"
        )
    if not _tap_building_action(building, sub_menu):
        raise StepExecutionError(
            f"tap_primary_action: failed to tap {sub_menu!r} in {building!r}"
        )


@register_action("commit_via_positive_taps")
def _handle_commit_via_positive_taps(action: dict) -> None:
    """Tap the screen's positive button repeatedly until the screen
    cycles back to a previously-seen state, no positive button is found,
    or *max_taps* iterations.

    Generic transaction-commit primitive — the plan author spells out
    'commit at this point' without specifying which buttons to tap or
    where they are; the handler discovers them dynamically via
    OmniParser.  See brain/commit_actions.py for selection rules.
    """
    # Metric-gated recruit commit: if the crew HUD is readable, we're on a
    # recruit/crew screen — verify the commit actually INCREASED crew instead
    # of scoring success on "a button was tapped".  This is what stops the blind
    # re-tap loop (2026-08-13); the generic path only knew "I tapped something".
    # Opt out with action["verify"] == "off".
    if action.get("verify", "auto") != "off":
        try:
            from capture.adb_capture import capture_screen
            from vision.hud_readers import read_crew
            if read_crew(capture_screen()) is not None:
                from brain.verified_recruit import recruit_crew_verified
                res = recruit_crew_verified()
                if not res["ok"]:
                    logger.warning(
                        f"[commit] recruit verify — crew {res['crew_before']}→"
                        f"{res['crew_after']} did NOT increase; commit ineffective "
                        f"(tapped {res['tapped']}) — plan verify should escalate, not re-tap"
                    )
                return
        except Exception as exc:
            logger.debug(f"[commit] crew-verify probe failed: {exc}")

    from brain.commit_actions import commit_via_positive_taps
    commit_via_positive_taps(
        max_taps=action.get("max_taps", 6),
        settle_secs=action.get("settle_secs", 3.0),
        goal_keywords=action.get("goal_keywords"),
    )


@register_action("retry")
def _handle_retry(action: dict) -> None:
    """
    No-op.  The 'retry' action is semantic at the resolution-sequence
    level (re-attempt the original blocking signal); achieve_goal's
    heavy_check at the final checkpoint detects whether the retry
    succeeded.
    """
    return


@register_action("noop")
def _handle_noop(action: dict) -> None:
    return


# ── The dispatcher ───────────────────────────────────────────────────────────


def execute_step(action: dict) -> None:
    """
    Dispatch a single PlanStep.action dict to the registered handler for
    its `kind`.  Default execute_step_fn for achieve_goal in production.

    Raises StepExecutionError if the action kind is unknown or the
    handler reports failure.  achieve_goal catches the exception and
    records a step failure (REASON_EXECUTE_RAISED).
    """
    kind = action.get("kind")
    if not kind:
        raise StepExecutionError(f"action has no 'kind': {action!r}")
    handler = _REGISTRY.get(kind)
    if handler is None:
        raise StepExecutionError(f"unknown action kind {kind!r} (action={action!r})")
    logger.debug(f"[plan_actions] executing {kind!r}: {action!r}")
    handler(action)


def registered_kinds() -> list[str]:
    """Discoverability helper for callers and tests."""
    return sorted(_REGISTRY.keys())
