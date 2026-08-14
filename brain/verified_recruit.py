"""Verified recruit commit — the recruit instance of the metric-gated
verify pattern (`resolve()`'s design, applied deterministically).

The stuck-recruit loop (2026-08-13) had two causes: the wrong button (fixed in
`commit_via_positive_taps` — it now prefers the yellow commit) AND no progress
check — the deterministic path scored success as "tapped a button," not "crew
went up," so it re-tapped forever.

This closes the second half WITHOUT turning on the never-run-live LLM `resolve()`
loop and WITHOUT a blind retry subloop: it does ONE verified commit — read crew,
tap the (yellow) Recruit commit, re-read crew — and REPORTS whether crew actually
increased. The caller decides what to do with a no-progress result (escalate with
the tried-list, or fall back), per the "escalate, don't absorb" rule. Repetition,
if any, comes from the caller/tick re-invoking — not a loop in here.

Metric: `vision.hud_readers.read_crew(frame) -> (current, capacity)`.
Done semantics mirror `brain.task_conditions.crew_increased`: crew went UP, or
reached capacity (already full).
"""
from __future__ import annotations

import time
from typing import Callable, Optional, Tuple

from loguru import logger

Crew = Optional[Tuple[int, int]]   # (current, capacity)


def _crew_progressed(before: Crew, after: Crew) -> Tuple[bool, str]:
    """True if crew went up, or the fleet is already at capacity (full)."""
    if after is not None and after[1] and after[0] >= after[1]:
        return True, "crew at capacity (full)"
    if before is not None and after is not None and after[0] > before[0]:
        return True, f"crew increased {before[0]}→{after[0]}"
    return False, "crew unchanged"


def recruit_crew_verified(
    *,
    settle_secs: float = 2.5,
    capture_fn: Optional[Callable] = None,
    commit_fn: Optional[Callable] = None,
    read_crew_fn: Optional[Callable] = None,
) -> dict:
    """Do ONE verified recruit commit on the (already-open) Recruit Crew screen.

    Returns:
      {ok, reason, crew_before, crew_after, tapped}

    ok=True  -> crew rose or is at capacity (recruit succeeded / nothing to do).
    ok=False -> the commit tap did NOT increase crew; `reason` + `tapped` describe
                what was tried so the caller can escalate with a real tried-list
                instead of blindly re-tapping.
    """
    if capture_fn is None:
        from capture.adb_capture import capture_screen
        capture_fn = capture_screen
    if read_crew_fn is None:
        from vision.hud_readers import read_crew
        read_crew_fn = read_crew
    if commit_fn is None:
        from brain.commit_actions import commit_via_positive_taps
        commit_fn = lambda: commit_via_positive_taps(goal_keywords=["recruit"])

    before = read_crew_fn(capture_fn())
    # Already full? Nothing to recruit — report success so the caller stops.
    if before is not None and before[1] and before[0] >= before[1]:
        logger.info(f"[recruit_verified] crew already at capacity {before} — nothing to do")
        return {"ok": True, "reason": "crew already at capacity",
                "crew_before": before, "crew_after": before, "tapped": []}

    tapped = commit_fn() or []       # commit_via_positive_taps taps the yellow Recruit (edit #1)
    time.sleep(settle_secs)
    after = read_crew_fn(capture_fn())

    progressed, why = _crew_progressed(before, after)
    if progressed:
        logger.info(f"[recruit_verified] OK: {why} (before={before} after={after})")
        return {"ok": True, "reason": why,
                "crew_before": before, "crew_after": after, "tapped": tapped}

    logger.warning(
        f"[recruit_verified] NO PROGRESS: commit tapped {tapped} but crew "
        f"{before}→{after} did not increase — caller should escalate, not re-tap"
    )
    return {"ok": False, "reason": "recruit commit did not increase crew",
            "crew_before": before, "crew_after": after, "tapped": tapped}
