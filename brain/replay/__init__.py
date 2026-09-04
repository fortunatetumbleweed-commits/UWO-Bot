"""Replay a captured frame and ask what the bot would DECIDE — without touching the device.

Every run leaves a trace: `data/sessions/trace_*/frame_NNNN.png` plus an `actions.jsonl` of
what was tapped and when. Six thousand frames of real barter runs are already on disk. This
turns them into something answerable before a live run rather than after it.

WHY THIS IS POSSIBLE NOW. Before the dispatcher refactor, checking a stage meant driving the
goal loop, which meant executing the real action code — navigation, OCR, retries, sleeps —
against whatever frame the fixture handed it. docs/simulation_tests.md is the post-mortem of
that: one test class took 1h05m, and its assertion could not fail on its merits. The lesson it
ends on is the design this module rests on — TEST THE DECISION, NOT THE VOYAGE.

An activity is now a decision function. `work(goal, state)` reads a screen and returns what it
would do. So a stage check is:

    frame on disk  ->  perceive(frame)  ->  state  ->  activity.work(goal, state)  ->  a decision

and nothing between those arrows moves a ship.

NOTHING HERE MAY TAP. `no_device()` replaces the tap primitives with raisers, because the
difference between a simulation and a live run is exactly that, and a harness that can quietly
become the second is worse than no harness. Injected fakes are how an activity's own actions
are observed — see `decide()`.
"""

from __future__ import annotations

import hashlib
import json
import types
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from loguru import logger

_ROOT = Path(__file__).resolve().parents[2]
_CACHE = _ROOT / "data" / "sim" / "perceive_cache"


class DeviceTouched(AssertionError):
    """Raised when replayed code tries to reach the phone. Always a bug in the caller."""


@contextmanager
def no_device():
    """Make every route to the device raise. The one guarantee this harness offers."""
    from unittest.mock import patch

    def refuse(*a, **k):
        raise DeviceTouched(
            "replay tried to touch the device. A stage check reads a stored frame and asks "
            "what the bot would DECIDE; if it needs to tap, inject a fake for that step "
            "instead (see brain.replay.decide)."
        )

    targets = ["actions.adb_actions.tap", "actions.adb_actions.swipe",
               "capture.adb_capture.capture_screen", "actions.ui.tap_at"]
    patches = []
    for t in targets:
        try:
            p = patch(t, side_effect=refuse)
            p.start()
            patches.append(p)
        except (AttributeError, ModuleNotFoundError) as exc:
            logger.debug(f"[replay] no such target to block: {t} ({exc})")
    try:
        yield
    finally:
        for p in reversed(patches):
            p.stop()


def load(frame_path: str | Path):
    """The frame as a PIL image."""
    from PIL import Image
    return Image.open(_ROOT / frame_path if not Path(frame_path).is_absolute() else frame_path)


def state_of(frame_path: str | Path, *, use_cache: bool = True) -> Any:
    """Perceive the frame, keyed the way the dispatcher keys states (`building:market`).

    Perception costs ~7s a frame, so the answer is CACHED BY FILE CONTENT — not by path, so a
    re-captured frame under the same name cannot serve a stale reading.
    """
    path = Path(frame_path)
    full = path if path.is_absolute() else _ROOT / path
    key = hashlib.sha1(full.read_bytes()).hexdigest()[:16]
    cached = _CACHE / f"{key}.json"

    if use_cache and cached.exists():
        loc = json.loads(cached.read_text())
    else:
        from brain.perceive import perceive
        loc = perceive(load(full)).to_location_dict()
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps(loc, default=str))

    where, detail = loc.get("location"), (loc.get("detail") or "")
    if where in ("building", "sub_menu") and ":" in detail:
        name = detail.split(":", 1)[1].strip().split(" — ", 1)[0].strip().lower()
        if name:
            where = f"{where}:{name}"
    return types.SimpleNamespace(state=where, port=loc.get("port"), detail=detail,
                                 frame=load(full), raw=loc)


def decide(frame_path: str | Path, goal: Any, activity: Any) -> Any:
    """What would this activity do, on this frame, with this goal?

    `activity` is passed in already built with whatever fakes the stage needs — that is
    deliberate. An activity's device-touching steps are its constructor arguments, so a
    caller says what it wants to observe by choosing which ones to fake, and everything it
    does NOT fake is blocked by `no_device` rather than silently reaching the phone.
    """
    state = state_of(frame_path)
    with no_device():
        return activity.work(goal, state)
