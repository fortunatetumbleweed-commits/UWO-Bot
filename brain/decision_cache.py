"""Decision learn-once cache — screen SIGNATURE -> the action that made progress.

Makes "control" cheap (docs/next_phase_architecture_2026-08-09.md §5): once the LLM
(or Qwen) has figured out what to do on a screen, a repeat of that exact screen
replays the cached action WITHOUT an LLM call. Claude usage trends toward "only
when the world surprises us."

Self-correcting: the task executor records a decision only AFTER it made progress,
and INVALIDATES a cached decision that stops making progress — so a stale entry
falls back to the LLM rather than looping. The signature is the coarse structural
`_screen_sig` (screen identity + button/commit affordances), so a cache hit means
"same screen, same options" — the situation really is the same.
"""
from __future__ import annotations

import json
import pathlib
from typing import Optional

from loguru import logger

_DEFAULT_PATH = pathlib.Path("memory/knowledge/decision_cache.json")


class DecisionCache:
    def __init__(self, path=_DEFAULT_PATH, persist: bool = True):
        self.path = pathlib.Path(path)
        self.persist = persist
        self._d: dict = {}
        self._load()

    @staticmethod
    def _key(sig) -> str:
        # sig is nested tuples of strings -> stable JSON string (tuples->arrays)
        return json.dumps(sig, default=list, sort_keys=False)

    def get(self, sig) -> Optional[dict]:
        e = self._d.get(self._key(sig))
        return {"op": e["op"], "arg": e["arg"]} if e else None

    def record(self, sig, action: Optional[dict]) -> None:
        """Remember that `action` made progress at screen `sig`."""
        if not action or not action.get("op"):
            return
        k = self._key(sig)
        hits = self._d.get(k, {}).get("hits", 0)
        self._d[k] = {"op": action.get("op"), "arg": action.get("arg"), "hits": hits + 1}
        self._save()

    def invalidate(self, sig, action: Optional[dict]) -> None:
        """Drop the cached decision at `sig` if it matches `action` (it stopped
        making progress) so the next visit re-asks the LLM."""
        k = self._key(sig)
        e = self._d.get(k)
        a = action or {}
        if e and e.get("op") == a.get("op") and e.get("arg") == a.get("arg"):
            del self._d[k]
            self._save()

    def __len__(self) -> int:
        return len(self._d)

    def _load(self) -> None:
        try:
            if self.path.exists():
                self._d = json.loads(self.path.read_text())
        except Exception as exc:
            logger.debug(f"[decision_cache] load failed: {exc}")

    def _save(self) -> None:
        if not self.persist:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._d))
        except Exception as exc:
            logger.debug(f"[decision_cache] save failed: {exc}")
