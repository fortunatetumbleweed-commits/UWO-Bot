# memory/voyage_log.py
#
# Append-only log of completed sail legs and trade rounds.
#
# Two record types are written to memory/knowledge/voyage_log.jsonl:
#
#   leg   — one sail_to step: from/to port, sailing time, success flag
#   round — one complete trade round: ports visited, profit, elapsed time
#
# Records are intentionally minimal and additive — no existing behaviour changes.
# The port_graph and route_planner read this file to derive statistics.

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

_LOG_PATH = Path(__file__).parent.parent / "memory" / "knowledge" / "voyage_log.jsonl"


# ── Writers ────────────────────────────────────────────────────────────────────

def log_leg(
    from_port: str,
    to_port: str,
    departed_at: datetime,
    arrived_at: datetime,
    ok: bool,
) -> None:
    """
    Record one completed sail_to step.

    Parameters
    ----------
    from_port    : port we departed from
    to_port      : port we arrived at (or tried to reach if ok=False)
    departed_at  : wall-clock time when sail_to started
    arrived_at   : wall-clock time when sail_to ended
    ok           : True if arrival was confirmed
    """
    sailing_time_s = (arrived_at - departed_at).total_seconds()
    record = {
        "type":          "leg",
        "from":          from_port,
        "to":            to_port,
        "departed_at":   departed_at.isoformat(),
        "arrived_at":    arrived_at.isoformat(),
        "sailing_time_s": round(sailing_time_s, 1),
        "ok":            ok,
    }
    _append(record)
    if ok:
        mins = int(sailing_time_s // 60)
        secs = int(sailing_time_s % 60)
        logger.debug(f"  [voyage_log] leg {from_port!r}→{to_port!r}  {mins}m{secs}s")


def log_round(
    ports_visited: list[str],
    profit: int,
    elapsed_s: float,
    ducats_per_hour: Optional[float],
) -> None:
    """
    Record one completed trade round (full circuit of ports).

    Parameters
    ----------
    ports_visited  : ordered list of ports (including start and end)
    profit         : net ducats earned this round (sell revenue − buy cost − resupply)
    elapsed_s      : wall-clock seconds for the full round
    ducats_per_hour: profit / elapsed_s * 3600 (None if elapsed unknown)
    """
    record = {
        "type":             "round",
        "ports":            ports_visited,
        "profit":           profit,
        "elapsed_s":        round(elapsed_s, 1),
        "ducats_per_hour":  round(ducats_per_hour, 1) if ducats_per_hour is not None else None,
        "recorded_at":      datetime.now(timezone.utc).isoformat(),
    }
    _append(record)
    dph = f"  {ducats_per_hour:,.0f} duc/hr" if ducats_per_hour else ""
    logger.info(f"  [voyage_log] round {profit:+,}d  {elapsed_s/60:.1f}min{dph}")


# ── Reader ─────────────────────────────────────────────────────────────────────

def load_legs(limit: int = 0) -> list[dict]:
    """
    Return all (or the last `limit`) leg records, oldest first.
    limit=0 means return all.
    """
    return _load_type("leg", limit)


def load_rounds(limit: int = 0) -> list[dict]:
    """
    Return all (or the last `limit`) round records, oldest first.
    limit=0 means return all.
    """
    return _load_type("round", limit)


def _load_type(record_type: str, limit: int) -> list[dict]:
    if not _LOG_PATH.exists():
        return []
    lines = [l.strip() for l in _LOG_PATH.read_text().splitlines() if l.strip()]
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") == record_type:
            out.append(rec)
    if limit > 0:
        out = out[-limit:]
    return out


def _append(record: dict) -> None:
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_LOG_PATH, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
