"""Game integers, as OCR actually returns them.

THE THOUSANDS SEPARATOR IS NOT ALWAYS A COMMA ON SCREEN — it is whatever the reader saw.
The game draws `3,129`; EasyOCR returns `3.129` often enough to matter, and it does so per
glyph, so one number in a pair can be right while the other is wrong:

    frame 236, the cargo bar:   '3.129/4,952'      (user, 2026-09-05)

Every reader in this codebase spelled the separator `[\\d,]`, which does not admit a period.
A regex scanning `3.129/4,952` therefore cannot match at the `3` — the next character is not
a slash — and settles on `129/4,952`. The hold read 129 instead of 3,129, the cargo
cross-check found the tiles summing to 3,129 against a hold of 129, correctly refused to buy
against an impossible number, and the leg stopped 249 Candle short: two barter rounds, about
1,200 units of product.

Nothing here is a decimal. These are counts of goods, ducats and crew, so a separator is a
separator whichever glyph came back, and `3.129` can only mean 3129.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

# A digit run that may carry group separators: comma, period, apostrophe, thin space.
# `\d` then any mix, so a bare `129` still matches and `,129` does not.
GROUPED = r"\d[\d,.'  ]*\d|\d"

SEPARATORS = str.maketrans("", "", ",.'  ")
_SEPARATORS = SEPARATORS


def to_int(text: str) -> Optional[int]:
    """`'3.129'` / `'3,129'` / `'3129'` -> 3129. None when there is no number in `text`."""
    m = re.search(GROUPED, str(text or ""))
    if m is None:
        return None
    try:
        return int(m.group(0).translate(_SEPARATORS))
    except ValueError:
        return None


def parse_pair(text: str) -> Optional[Tuple[int, int]]:
    """The `N/M` counters — cargo load, crew, quantity dialogs. None if `text` has no pair."""
    m = re.search(rf"({GROUPED})\s*/\s*({GROUPED})", str(text or ""))
    if m is None:
        return None
    a, b = to_int(m.group(1)), to_int(m.group(2))
    return None if a is None or b is None else (a, b)
