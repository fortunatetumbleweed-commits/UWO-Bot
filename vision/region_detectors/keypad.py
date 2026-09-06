"""The numeric keypad — the game's amount prompt, wherever it appears.

It is not the market's. The same card opens for a market quantity, a sell amount, a discard
amount and anywhere else the game wants a number, so it lives here and every activity that
needs an amount shares one answer (user, 2026-09-06: *"keypad dialog is used through out this
game"*).

RECOGNISED BY THE KEYS THEMSELVES. OmniParser emits the keypad's buttons as unlabelled
`icon` elements of a very consistent size, and a keypad has TWELVE of them — 0-9, `Max` and
enter. Measured on `data/sessions/trace_barter_cmd_2026-09-05T21-38-09`:

    frame 66, 68 (keypad up)        12 key-sized icons
    frame 214, 255 (no keypad)       0

That is a clean separation with nothing in between, and it is the good kind of test: the
thing itself, not a caption for it.

WHY NOT THE TITLE. `"Enter Number"` is readable and works today, but it is wording — the
weakest evidence this codebase has (`docs/market_as_contexts.md`, FC-3: three guessed
overflow phrases that matched nothing the game ever drew). The keys are language-independent
and cannot be re-worded by a patch.

WHY NOT A MODEL, AND WHY NOT PIXELS. Both were considered. The keypad is pixel-identical
between appearances (mean abs diff 0.03-1.30 across six frames, against 43-144 for any other
screen), so a template would also work — but it needs a stored crop, a threshold, and a
region, and this needs none of those. A model over the same parse would inherit whatever the
parse can see, which is exactly this.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence, Tuple

# A key, measured across two staging sequences: 116-121 wide, 105-110 tall. The bounds are
# loose enough to ride a re-layout and tight enough that no other icon in the game's cards
# has been seen inside them.
_KEY_W = (100, 140)
_KEY_H = (88, 126)

# 0-9, Max, enter. Twelve is the full pad; the floor is lower so a key lost to a poor parse
# does not hide the whole keypad — nothing else on these screens produces even eight.
_MIN_KEYS = 8


def keypad_keys(elements: Sequence[Any]) -> Tuple[Any, ...]:
    """The key-sized icons among `elements`. Empty when no keypad is up."""
    keys = []
    for e in elements or []:
        if (getattr(e, "label", "") or "").strip().lower() != "icon":
            continue
        try:
            w = abs(float(e.x2) - float(e.x1))
            h = abs(float(e.y2) - float(e.y1))
        except (AttributeError, TypeError, ValueError):
            continue
        if _KEY_W[0] <= w <= _KEY_W[1] and _KEY_H[0] <= h <= _KEY_H[1]:
            keys.append(e)
    return tuple(keys)


def keypad_is_up(elements: Sequence[Any]) -> bool:
    """Is the numeric keypad on screen?"""
    return len(keypad_keys(elements)) >= _MIN_KEYS


def keypad_bbox(elements: Sequence[Any]) -> Optional[Tuple[int, int, int, int]]:
    """The pad's extent, for a caller that wants to aim inside it. None when it is not up."""
    keys = keypad_keys(elements)
    if len(keys) < _MIN_KEYS:
        return None
    return (int(min(k.x1 for k in keys)), int(min(k.y1 for k in keys)),
            int(max(k.x2 for k in keys)), int(max(k.y2 for k in keys)))
