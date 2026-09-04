"""Read CONTENT from a region, and merge it with the whole-frame parse.

A whole-frame parse answers coarse questions and loses small text — a quantity drawn on a
thumbnail, light over artwork. Measured 2026-08-26: the full 2400x1080 frame never proposed
the `44` on a trade-list thumbnail as text at all (`parse_raw` at conf 0.01 shows only an
`icon` box there), while Iron's `102` came back at conf 0.9999. See
`docs/downscale_content_audit.md`.

THREE THINGS HAVE TO BE RIGHT, and the first two attempts at this were wrong:

1. THE REGION COMES FROM THE SCREEN'S OWN LANDMARKS, never from constants (CLAUDE.md: "if
   you are about to write a number that means where on the screen, find the element
   instead"). Callers pass a box they DERIVED; this module does not guess one.

2. THE REGION IS UPSCALED. Cropping alone is not the fix: a correctly-derived 548x582 crop
   does NOT read the `44`, and the same crop at x2 does. A hand-picked crop that happens to
   work is luck, not method.

3. MERGE BY ASSOCIATION, NEVER BY COORDINATE. The two parses disagree on box geometry — the
   crop's `358` maps back to y~505 where the frame has it at 462 — so any absolute-y pairing
   shifts rows. A row's value is drawn BELOW its name, so each value binds to the nearest
   name ABOVE it, using only the ordering WITHIN one parse. That is
   `memory/identify-by-association-not-dimension` applied inside a row, and it is what makes
   merging two disagreeing parses possible at all: reading order is the only thing they share.

The merge keeps the WHOLE-FRAME value where it has one and lets the region fill the gaps,
because the two fail on different items — the region read `44` correctly and misread Candle
as `402`, while the whole frame had Candle right and lost the `44`. Together they are correct;
neither is alone.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, Optional, Sequence, Tuple

from loguru import logger

# The smallest upscale measured to recover a thumbnail quantity. Larger also works and costs
# more; x2 is the floor, not a tuning knob to raise casually.
_MIN_SCALE = 2


def read_region(frame, box: Tuple[int, int, int, int], *, scale: int = _MIN_SCALE,
                parse: Optional[Callable] = None) -> list:
    """Parse `box` of `frame` at `scale`, returning elements in REGION coordinates.

    Coordinates are deliberately NOT mapped back to the frame. Callers must not pair region
    elements with whole-frame ones positionally — see the module docstring — so handing back
    frame coordinates would only invite the bug this module exists to avoid.
    """
    if parse is None:
        from vision.omniparser import parse_fast_cached as parse
    crop = frame.crop(box)
    if scale > 1:
        crop = crop.resize((crop.width * scale, crop.height * scale))
    return list(parse(crop))


def rows_by_association(elements: Iterable, row_labels: Sequence[str], *,
                        value_of: Optional[Callable] = None) -> Dict[str, Any]:
    """{row label -> the value drawn under it}, from ONE parse.

    `row_labels` must name EVERY row on the panel, not just the ones wanted. With only the
    interesting rows eligible, a neighbouring row's value binds to the nearest wanted name
    and displaces the real one — live 2026-08-26, Birch Tree's output `358` was recorded as
    Iron's quantity because only materials were candidates.
    """
    if value_of is None:
        value_of = _as_int

    wanted = {(r or "").strip().lower() for r in row_labels}
    names = sorted(((e.y1 + e.y2) / 2, (e.label or "").strip())
                   for e in elements
                   if (e.label or "").strip().lower() in wanted)
    if not names:
        return {}

    # BY POSITION, NOT BY PARSE ORDER. `setdefault` keeps the first value seen for a row, so
    # iterating the element list as it happens to come back would let a lower value win over
    # the row's own. Sorting by y makes "first" mean "topmost", which is what the layout means.
    values = sorted(((e.y1 + e.y2) / 2, value_of(e)) for e in elements
                    if value_of(e) is not None)

    out: Dict[str, Any] = {}
    for mid, value in values:
        # The value sits BELOW its name; the small tolerance covers a value box that starts
        # level with the label it belongs to. A value with NO name above it belongs to a row
        # this caller did not name, and is dropped rather than attached to something else.
        above = [label for y, label in names if y <= mid + 25]
        if above:
            out.setdefault(above[-1], value)
    return out


def merged_rows(frame, box, row_labels: Sequence[str], *, scale: int = _MIN_SCALE,
                whole_elements: Optional[Iterable] = None,
                parse: Optional[Callable] = None,
                value_of: Optional[Callable] = None) -> Dict[str, Any]:
    """{row label -> value}, taking the whole-frame read and letting the region fill its gaps.

    The whole frame WINS where it has a value: the region is the less reliable read (it
    misread Candle as `402` on the frame this was built from) and exists to supply what the
    whole frame could not see at all.
    """
    if whole_elements is None:
        from vision.omniparser import parse_fast_cached
        whole_elements = parse_fast_cached(frame)

    whole = rows_by_association(whole_elements, row_labels, value_of=value_of)
    region = rows_by_association(read_region(frame, box, scale=scale, parse=parse),
                                 row_labels, value_of=value_of)

    merged = dict(region)
    merged.update({k: v for k, v in whole.items() if v is not None})

    filled = sorted(set(region) - set(whole))
    if filled:
        logger.info(f"[region-read] the whole-frame parse missed {filled}; "
                    f"the region supplied {[region[k] for k in filled]}")
    return merged


def _as_int(element) -> Optional[int]:
    label = (getattr(element, "label", "") or "").strip().replace(",", "")
    return int(label) if label.isdigit() else None
