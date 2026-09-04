"""The one screen everybody reads.

`vision.perceive_repository` holds the mechanism; this holds the INSTANCE and wires it to the
action layer, because that wiring runs `actions` -> `vision` and never the other way.

    from actions.perception import screen

    obs = screen().get(why="reading the barter panel")   # may capture; blocks
    text = screen().read_region(box)                     # never captures

WHY A SINGLETON. The point is that two readers in one moment see the SAME frame. A per-caller
repository would be the 215 scattered captures again, with extra ceremony.

WHY PARTIAL MIGRATION IS SAFE. Invalidation is driven by ACTIONS, not by captures: another
module calling `capture_screen()` directly does not change the screen, so it cannot make this
repository's observation wrong. A path can move across one at a time, and until it does the
only cost is the old duplicate capture — never a disagreement.
"""

from __future__ import annotations

from typing import Optional

from vision.perceive_repository import PerceiveRepository

_repo: Optional[PerceiveRepository] = None


def screen() -> PerceiveRepository:
    """The process-wide observation. Created on first use and wired to the action layer.

    The wiring is what makes the repository safe rather than merely tidy: every tap, swipe,
    back, wake and keystroke tells it the screen moved, so no caller has to remember to say
    so. That discipline was voluntary before, and 215 capture sites are what voluntary bought.
    """
    global _repo
    if _repo is None:
        _repo = PerceiveRepository()
        from actions.adb_actions import add_action_sink
        add_action_sink(_on_action)
    return _repo


def _on_action(kind: str, settle_s: float) -> None:
    """We moved the world, so what we last observed is superseded."""
    if _repo is not None:
        _repo.invalidate(f"we did: {kind}", settle_s=settle_s)


def reset_for_tests() -> None:
    """Drop the singleton and unhook it. Tests only."""
    global _repo
    if _repo is not None:
        try:
            from actions.adb_actions import remove_action_sink
            remove_action_sink(_on_action)
        except Exception:
            pass
    _repo = None
