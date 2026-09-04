"""One small per-frame cache, so the id-reuse bug is fixed once instead of five times.

Vision work is expensive and several consumers see the same frame within one perceive tick,
so each reader memoises its answer. PIL Images are not hashable, so every one of them reached
for `id(frame)` — and an id is only unique while the object is ALIVE. CPython hands the same
address to the next allocation once a frame is freed, so the next image silently inherits an
unrelated frame's answer.

THIS HAS BEEN FOUND TWICE AND FIXED ONCE:

  * 2026-08-21, `omniparser.parse_fast_cached`: "200 sequentially-created 2400x1080 PIL images
    occupied just THREE distinct ids, i.e. 197 collisions" — a world-map frame came back
    carrying the port overworld's buildings. Fixed there by holding a reference beside the
    entry. The fix never left that function.
  * 2026-08-26, `family_classifier`: 109 collisions in 120 opened trace frames. A survey
    reported `chromed` and `transient` screens as `sea`, and a claim about the sea HUD was
    made about frames that had never been at sea.

Both are the same defect, and it is silent by construction: every wrong answer is a plausible
answer of the right shape. So the guard belongs in ONE place, and that is here.

The entry holds a WEAK reference. A strong one works too — omniparser uses one — but it keeps
frames alive, which is only acceptable because that cache is bounded at four. A weak reference
lets the frame go and turns a recycled id into a detectable miss rather than a wrong hit.
"""

from __future__ import annotations

import weakref
from collections import OrderedDict
from typing import Any, Callable, Optional


class FrameCache:
    """A bounded LRU keyed on a frame, safe against id reuse.

    `extra` distinguishes several answers about the same frame — a threshold, a question —
    the way the readers' own keys used to.
    """

    def __init__(self, name: str, max_entries: int = 8) -> None:
        self._name = name
        self._max = max_entries
        self._entries: "OrderedDict[tuple, tuple]" = OrderedDict()   # key -> (ref, value)

    def get(self, frame: Any, extra: Any = None) -> Optional[Any]:
        """The cached value for THIS frame, or None if there is none to trust."""
        key = (id(frame), extra)
        entry = self._entries.get(key)
        if entry is None:
            return None
        ref, value = entry
        if ref() is frame:                       # the same LIVE object, not a recycled id
            self._entries.move_to_end(key)
            return value
        del self._entries[key]                   # dead or different — never trust it
        return None

    def put(self, frame: Any, value: Any, extra: Any = None) -> Any:
        key = (id(frame), extra)
        try:
            self._entries[key] = (weakref.ref(frame), value)
        except TypeError:
            return value                         # not weakref-able: correct, just uncached
        if len(self._entries) > self._max:
            self._entries.popitem(last=False)
        return value

    def memoize(self, frame: Any, compute: Callable[[], Any], extra: Any = None) -> Any:
        """`compute()` unless this exact frame's answer is already held."""
        got = self.get(frame, extra)
        if got is not None:
            return got
        return self.put(frame, compute(), extra)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
