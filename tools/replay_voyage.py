"""Offline replay of a captured reference voyage through the bot's planner.

Re-runs the planner deterministically against the perception state
recorded during the capture (sectors + lat/lon + heading from
`trace.jsonl`), without touching ADB or the live game.  The bot's
recomputed per-tick decision is written to `bot_replay.jsonl`
alongside the original capture's `trace.jsonl`.

Why this matters: every commit that changes the planner can now be
validated by replaying against the canonical Nile reference voyage
and comparing the recomputed decisions against the captured ones (and
against the user-supplied verdict labels in `labels.jsonl`).  No live
game.  No taps.  ~1 second per voyage instead of ~25 minutes.

Architecture:
- Stub `ReplayNav` reconstructs the planner-facing slice of
  `MinimapNavigationView` from the saved trace data.  No re-perception
  of the minimap crop — we trust the captured sectors and heading.
  This is "planner replay," not "perception replay."  Full-perception
  replay is a future capture-format change (save full frames + re-run
  vision/minimap_navigation_view.py).
- `actions.sea_actions` is monkey-patched to no-ops (same pattern as
  `tools/capture_voyage.py`).
- The bot's `BotObservation` singleton is updated per tick with the
  replay nav; `HugShoreGoal.tick()` then runs as if it were live.

Per-replay-tick output to `bot_replay.jsonl`:
  {tick, wall_iso, lat, lon, heading_deg, action, phase, note,
   commanded_deg_this, picker_target, lyapunov.*}
Same shape as the capture's `trace.jsonl` so the diff tool can join
on `tick`.

See `docs/exploration_navigation_layers.md` for the
perception/mapping/planning split this harness depends on.

Usage:
  python -m tools.replay_voyage data/sessions/reference_<name>_<ts>/

Produces:
  data/sessions/<same dir>/bot_replay.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple


# ── Monkey-patch sea_actions BEFORE any bot import ──────────────────────────

import actions.sea_actions as _sea_actions  # noqa: E402


_REAL_FNS = {}
_INTENT_LOG: List[dict] = []


def _make_stub(name: str):
    def stub(*args, **kwargs):
        ts = datetime.now().isoformat()
        _INTENT_LOG.append({
            "wall_iso": ts, "fn": name,
            "args": list(args), "kwargs": dict(kwargs),
        })
        try:
            from actions.sea_actions import SeaActionResult
            return SeaActionResult(
                ok=True, action=name,
                detail="replay_stub: tap suppressed",
            )
        except Exception:
            return None
    stub.__name__ = name
    return stub


_STUBBED = ("sail_start", "sail_stop",
            "turn_left", "turn_right",
            "hold_left", "hold_right",
            "is_ship_moving")
for _name in _STUBBED:
    if hasattr(_sea_actions, _name):
        _REAL_FNS[_name] = getattr(_sea_actions, _name)
        setattr(_sea_actions, _name, _make_stub(_name))

# is_ship_moving needs to return True so the live-loop bootstrap
# (which calls sail_start() when ship is stopped) doesn't fire a
# stub-recorded sail_start at replay tick 1.
_sea_actions.is_ship_moving = lambda: True   # type: ignore


# ── Replay stubs for the perception surface ─────────────────────────────────

@dataclass
class ReplaySector:
    """Mirrors `vision.minimap_navigation_view.SectorReading` minimally."""
    i:              int
    land_fraction:  float
    nearest_dist:   Optional[float]
    is_observed:    bool
    bearing_deg:    Optional[float] = None


@dataclass
class ReplayNav:
    """Stand-in for `MinimapNavigationView` driven by captured trace data.

    Only the planner-facing surface is implemented.  Anything the
    planner consults that wasn't captured returns a sensible empty
    default (e.g. `targets = ()`, `water_mask = None`).
    """
    sectors:           Tuple[ReplaySector, ...]
    ship_heading_deg:  Optional[float]
    heading_strategy:  Optional[str] = None
    heading_confidence: Optional[float] = None
    water_mask:        Any = None      # not captured — disables skel hybrid
    targets:           Tuple = ()      # not captured — empty for replay

    def is_reachable(self, target) -> bool:
        return False


_SECTOR_BEARINGS_DEG = tuple(i * 22.5 for i in range(16))


def nav_from_trace_record(rec: dict) -> Optional[ReplayNav]:
    """Build a ReplayNav from one `trace.jsonl` record.

    Returns None when the record doesn't carry sector data (early
    pre-sea ticks during capture set-up).
    """
    nav_rec = rec.get("nav")
    if not nav_rec:
        return None
    sectors_raw = nav_rec.get("sectors") or ()
    if not sectors_raw:
        return None
    sectors = tuple(
        ReplaySector(
            i=int(s.get("i", idx)),
            land_fraction=float(s.get("frac", 0.0)),
            nearest_dist=(
                float(s["dist"]) if s.get("dist") is not None else None),
            is_observed=bool(s.get("obs", False)),
            bearing_deg=_SECTOR_BEARINGS_DEG[int(s.get("i", idx))],
        )
        for idx, s in enumerate(sectors_raw)
    )
    return ReplayNav(
        sectors=sectors,
        ship_heading_deg=nav_rec.get("heading_deg"),
        heading_strategy=rec.get("heading_strategy"),
        heading_confidence=rec.get("heading_confidence"),
    )


# ── Replay loop ─────────────────────────────────────────────────────────────

def replay(
    session_dir: Path,
    side: str = "port",
    junction_picker: str = "tremaux",
    max_ticks_override: Optional[int] = None,
) -> Path:
    """Replay the captured voyage at `session_dir` through the planner.

    Returns the path to the written `bot_replay.jsonl`.
    """
    trace_path = session_dir / "trace.jsonl"
    if not trace_path.exists():
        raise SystemExit(f"missing {trace_path}")

    # Deferred import so monkey-patch is installed first.
    from brain.goals.hug_shore import HugShoreGoal
    from brain import observation as _obs

    _obs.reset()
    trace: List[dict] = [
        json.loads(line) for line in trace_path.read_text().splitlines()
        if line.strip()
    ]
    if not trace:
        raise SystemExit(f"{trace_path}: empty")

    max_ticks = max_ticks_override or len(trace) + 10
    goal = HugShoreGoal(
        side=side,
        max_ticks=max_ticks,
        junction_picker=junction_picker,
    )

    out_path = session_dir / "bot_replay.jsonl"
    out_fp = out_path.open("w")
    n_replayed = 0
    n_skipped_no_nav = 0
    for rec in trace:
        nav = nav_from_trace_record(rec)
        if nav is None:
            n_skipped_no_nav += 1
            continue

        # Feed the goal the same HUD readings the capture saw.
        try:
            goal.set_hud(
                lat=rec.get("lat"),
                lon=rec.get("lon"),
                speed_kt=rec.get("speed_kt"),
                raw_heading=rec.get("heading_deg_raw"),
                rejected=bool(rec.get("heading_rejected", False)),
            )
        except TypeError:
            # Older set_hud signature — pass only lat/lon as a fallback.
            goal.set_hud(lat=rec.get("lat"), lon=rec.get("lon"))

        # Install the replay nav as the current observation so the
        # goal's tick() reads it via `_obs.current()`.
        _obs.update(
            tick=rec.get("tick"),
            timestamp=datetime.fromisoformat(rec["wall_iso"])
                       if rec.get("wall_iso") else None,
            nav=nav,
        )

        # Mirror the live loop's §13.28 heading-rejection short-circuit.
        # When the captured trace records heading_rejected=True, the
        # live loop in `run_hug_shore_loop` (hug_shore.py:4730) returns
        # a synthetic `stopped_for_rejection` action WITHOUT calling
        # goal.tick() — the planner's state machine does not advance
        # on rejected ticks.  Replay has to follow suit, otherwise the
        # goal's state diverges from the captured trace at the first
        # rejection and cascades forever.  Confirmed via first-divergence
        # bisect against reference_nile_full_20260606_141824 (t28 was
        # the first rejected tick, t48 was the first direction flip —
        # 20 ticks of state drift, exactly the kind of cascade this
        # block now prevents).
        from brain.goals.hug_shore import HugTickResult
        if rec.get("heading_rejected"):
            goal.tick_count += 1
            result = HugTickResult(
                action="stopped_for_rejection",
                phase=goal.phase,
                note="replay: heading_rejected per trace",
            )
        else:
            try:
                result = goal.tick()
            except Exception as e:
                # Don't let one bad tick kill the whole replay — log
                # it and keep going.  These are the diagnostic events
                # the user actually wants to see.
                print(f"[replay] tick {rec.get('tick')} planner error: "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
                continue

        # Pull through the same lyapunov diagnostic dict the live loop
        # logs, so the diff tool sees apples-to-apples columns.
        lyap = getattr(goal, "_last_lyap", {}) or {}

        out_fp.write(json.dumps({
            "tick":               rec.get("tick"),
            "wall_iso":           rec.get("wall_iso"),
            "lat":                rec.get("lat"),
            "lon":                rec.get("lon"),
            "heading_deg":        nav.ship_heading_deg,
            "action":             result.action,
            "phase":              result.phase.name
                                  if hasattr(result.phase, "name")
                                  else str(result.phase),
            "note":               result.note,
            "lyapunov":           lyap,
        }) + "\n")
        n_replayed += 1

    out_fp.close()
    print(f"[replay] {session_dir.name}: replayed {n_replayed} ticks "
          f"({n_skipped_no_nav} skipped — no nav data)")
    print(f"[replay]   → {out_path}")
    return out_path


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("session_dir", type=Path,
                    help="data/sessions/reference_<name>_<ts>/")
    ap.add_argument("--side", default="port",
                    choices=("port", "starboard"),
                    help="Hug side. Default: port (Nile-hug-left).")
    ap.add_argument("--junction-picker", default="tremaux",
                    choices=("none", "yamauchi", "tremaux"),
                    help="Default: tremaux.")
    ap.add_argument("--max-ticks", type=int, default=None,
                    help="Override goal max_ticks; default = trace length + 10.")
    args = ap.parse_args(argv)
    if not args.session_dir.is_dir():
        raise SystemExit(f"not a directory: {args.session_dir}")
    replay(
        session_dir=args.session_dir,
        side=args.side,
        junction_picker=args.junction_picker,
        max_ticks_override=args.max_ticks,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
