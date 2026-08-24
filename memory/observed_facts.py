"""Timestamped facts the bot has observed, so a thing read once need not be re-derived.

Three failures in one session (live 2026-08-22) had the same shape: a value was perceived
correctly, thrown away, and then re-derived from a screen that could not supply it.

  • the Textiles count, read on the Sell grid, asked for again after navigating off it
  • the cargo hold, read twice at 21:24:51 and 21:25:08, then re-read via a tap that left
    the market
  • the port name 'Kolkata', confirmed twice on the overworld at 21:29:38 and 21:31:45,
    then asked for from the MAIN MENU, which does not paint a port name at all — the
    mission aborted with "current port unreadable"

Every fact here carries WHEN it was seen, because the right question is never "do we know
this?" but "do we know this recently enough to act on it?". Different facts go stale at
different rates: a port name cannot change while the fleet sits in a menu, while market
prices re-roll every few hours.

Deliberately not a cache of anything expensive — it is a record of what the bot has SEEN.
Refresh by calling `remember` again; the timestamp moves with it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional, Tuple

from loguru import logger

_PATH = Path("memory/knowledge/state/observed_facts.json")

_facts: Optional[dict] = None


def _load() -> dict:
    global _facts
    if _facts is None:
        try:
            _facts = json.loads(_PATH.read_text()) if _PATH.exists() else {}
        except Exception as exc:
            logger.debug(f"[facts] could not read {_PATH}: {exc}")
            _facts = {}
    return _facts


def _save() -> None:
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        _PATH.write_text(json.dumps(_load(), indent=2, sort_keys=True))
    except Exception as exc:
        logger.debug(f"[facts] could not write {_PATH}: {exc}")


def remember(key: str, value: Any, *, now: Optional[float] = None) -> None:
    """Record `value` for `key`, stamped with the time it was observed."""
    _load()[key] = {"value": value, "at": float(now if now is not None else time.time())}
    _save()


def recall(key: str, *, max_age_s: Optional[float] = None,
           now: Optional[float] = None) -> Optional[Tuple[Any, float]]:
    """The remembered `(value, age_in_seconds)`, or None.

    None means "nothing recent enough", which callers must treat as "unknown" — never as a
    value in its own right. Pass `max_age_s` whenever the fact can change behind the bot's
    back; omit it only for facts that cannot (see `forget` for the invalidation side).
    """
    rec = _load().get(key)
    if not isinstance(rec, dict) or "at" not in rec:
        return None
    age = float(now if now is not None else time.time()) - float(rec["at"])
    if max_age_s is not None and age > max_age_s:
        return None
    return rec.get("value"), age


def forget(key: str) -> None:
    """Drop a fact that has just been invalidated — the fleet sailed, the market re-rolled.

    Cheaper and safer than guessing an expiry: the moment the bot DOES something that makes
    a fact untrue is the moment it knows for certain.
    """
    if _load().pop(key, None) is not None:
        _save()


def _reset_for_tests() -> None:
    global _facts
    _facts = {}
