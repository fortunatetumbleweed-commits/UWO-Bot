"""Learning flywheel (#32) — realized profit + XP feed back into the KB.

Closes the loop the two-brain design hinges on (project_barter_kb_blueprint):
    more play → recorded outcomes → calibrated estimates + remembered good plays →
    better + cheaper decisions.

Each finished BarterMission (#31) appends a MissionOutcome (estimated vs REALIZED
profit, company-XP gain) to the barter outcomes log. `realized_stats` then exposes a
CALIBRATION factor (realized / estimated) the strategy (#29) can multiply its
estimates by, so the profit model self-corrects from ground truth instead of staying
a static guess.

Timestamps are injected (the caller stamps with an absolute time) so this is pure +
testable and resumable.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from loguru import logger

_OUTCOMES_PATH = Path("memory/knowledge/barter/outcomes.jsonl")


@dataclass
class MissionOutcome:
    season: str
    good: str
    village: Optional[str]
    sell_port: Optional[str]
    est_profit: Optional[float]
    realized_profit: Optional[int]
    xp_gain: Optional[float]
    ok: bool
    timestamp: str = ""


def _play_attr(play, name, default=None):
    return getattr(play, name, default) if play is not None else default


def record_mission_outcome(result, *, season: str = "", xp_before: Optional[float] = None,
                           xp_after: Optional[float] = None, timestamp: str = "",
                           save: bool = True) -> MissionOutcome:
    """Build (and optionally append) a MissionOutcome from a mission result.

    Duck-typed: anything carrying `.ok`, `.profit` and a `.play` (good / village /
    sell_port / est_net_profit) works — e.g. brain.mission.MissionResult."""
    play = getattr(result, "play", None)
    xp_gain = (xp_after - xp_before) if (xp_before is not None and xp_after is not None) else None
    outcome = MissionOutcome(
        season=season,
        good=_play_attr(play, "good", "?"),
        village=_play_attr(play, "village"),
        sell_port=_play_attr(play, "sell_port"),
        est_profit=_play_attr(play, "est_net_profit"),
        realized_profit=getattr(result, "profit", None),
        xp_gain=xp_gain,
        ok=bool(getattr(result, "ok", False)),
        timestamp=timestamp,
    )
    if save:
        _OUTCOMES_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _OUTCOMES_PATH.open("a") as f:
            f.write(json.dumps(asdict(outcome), ensure_ascii=False) + "\n")
        logger.info(f"[flywheel] recorded {outcome.good} @ {outcome.village} → "
                    f"realized={outcome.realized_profit} (est {outcome.est_profit})")
    return outcome


def load_outcomes() -> list:
    """All recorded mission outcomes (oldest→newest)."""
    if not _OUTCOMES_PATH.exists():
        return []
    out = []
    for line in _OUTCOMES_PATH.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def realized_stats(good: Optional[str] = None) -> dict:
    """Aggregate SUCCESSFUL outcomes (optionally for one good) into a calibration.

    calibration = Σ realized / Σ estimated — the factor to correct #29's estimates by
    (None until there's both realized and estimated data). Also returns the best play
    seen (highest realized profit)."""
    rows = [o for o in load_outcomes() if o.get("ok")
            and (good is None or o.get("good") == good)]
    rows = [o for o in rows if o.get("realized_profit") is not None]
    if not rows:
        return {"count": 0, "total_realized": 0, "avg_realized": None,
                "calibration": None, "best": None}
    total_real = sum(o["realized_profit"] for o in rows)
    est_rows = [o for o in rows if o.get("est_profit")]
    total_est = sum(o["est_profit"] for o in est_rows) if est_rows else 0
    calibration = (sum(o["realized_profit"] for o in est_rows) / total_est
                   if total_est else None)
    best = max(rows, key=lambda o: o["realized_profit"])
    return {"count": len(rows), "total_realized": total_real,
            "avg_realized": total_real / len(rows),
            "calibration": calibration, "best": best}
