"""The world map's destination rail — the search box and its list, as ONE element.

THE BOX AND THE LIST ARE ONE COMPONENT (user, 2026-09-16): *"the list and box are one
component, they always show together, the list can be empty if everything is filtered out,
but it is still there."*

That sentence removes a whole class of failure. The code used to ask the question twice and
require both answers — `search_box_present(...) and any village row` — so an unlucky parse of
EITHER half denied the whole rail. Both halves have now failed live, in opposite directions:

  * the ROW half, 2026-08-24: a search narrowed to ONE result read as "no list", so the
    caller went hunting for the icon again and toggled the panel shut.
  * the BOX half, 2026-09-16 at Svear: the typed query replaced the field in the parse, so
    the rail read as closed on every tick from the moment the search SUCCEEDED. The mission
    died one leg from the village with 'Svear Village' on screen the whole time.

Worse, the conjunction made an empty filtered list unrepresentable: "there is no rail" and
"the rail is open and nothing matched" both came back False, and they call for opposite
responses. Only the first justifies tapping the icon.

THE ANCHOR IS THE RAIL'S OWN CLOSE-X, not its contents. It is the rail's dismiss control, so
it exists exactly when the rail does and it does not change when you type. Measured across
the Svear, San and Hutu sessions:

    rail closed   (frames 12, 13)                       no icon in the head band at all
    rail open     (15, 16, 17, 319, 322, 323, 330)      an icon at x≈521-523, y≈141-144
    Hutu filtered (321)                                 the same icon, same place

including frames 322 and 323, where `search_box_present` had already gone blind.

WHAT IT IS NOT FOR. The saved-ROUTE list is a different component — no search box, no
close-X, no icon strip, just full-width rows with trash icons and empty '+' slots (San frame
368). This detector correctly says no there, and route goals are served by their own context.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from loguru import logger

# The head band of the rail: the search field on the left, the close-X at its right end.
# FRACTIONS OF THE FRAME, not pixels, so a different capture size does not silently move
# them — the same reason `vision/region_detectors/overworld_panel.py` carries no absolutes.
# Measured on 2400x1080: the X sits at x 521-523, y 141-144, and the field spans x 114-490.
_X_BAND = (0.19, 0.25)        # 456-600 px at 2400 wide
_HEAD_Y = (0.10, 0.18)        # 108-194 px at 1080 tall
_FIELD_X = (0.04, 0.25)       #  96-600 px — the field, left of the X


@dataclass(frozen=True)
class DestinationRail:
    """The rail, found. `field` is whatever the parse gave for the search box — the box, the
    word 'Search', or the query typed into it, all of which are the same control."""
    close_x: tuple            # (cx, cy) of the rail's dismiss control
    field: Optional[object]   # the search field element, when the parse returned one

    @property
    def typed_query(self) -> str:
        """What is in the box, or '' — a query means the list is FILTERED, never absent."""
        label = (getattr(self.field, "label", "") or "").strip()
        return "" if label.lower() in ("", "search", "icon") else label


def detect_destination_rail(frame, elements: Optional[Sequence] = None
                            ) -> Optional[DestinationRail]:
    """The rail, or None when it is not on screen. Never raises."""
    try:
        if elements is None:
            from vision.omniparser import parse_fast_cached
            elements = list(parse_fast_cached(frame))
        # THE FRAME'S OWN SIZE WHEN IT HAS ONE, the declared screen otherwise. Callers that
        # already hold parsed elements sometimes pass a bare stand-in for the frame, and
        # going blind because the picture was not handed over would be the wrong failure —
        # the windows below are fractions of a screen this project pins at one resolution.
        from config.settings import SCREEN_HEIGHT, SCREEN_WIDTH
        w = getattr(frame, "width", None) or SCREEN_WIDTH
        h = getattr(frame, "height", None) or SCREEN_HEIGHT
        x_lo, x_hi = _X_BAND[0] * w, _X_BAND[1] * w
        y_lo, y_hi = _HEAD_Y[0] * h, _HEAD_Y[1] * h

        # ONE ELEMENT WITHOUT COORDINATES MUST NOT BLIND THE WHOLE DETECTOR. Reading `e.cx`
        # straight off would raise on the first such element and the guard below would turn
        # that into "no rail" — a detector that goes blind on a malformed neighbour is worse
        # than one that skips it.
        def _at(e, xs, ys):
            cx, cy = getattr(e, "cx", None), getattr(e, "cy", None)
            return (cx is not None and cy is not None
                    and xs[0] < cx < xs[1] and ys[0] < cy < ys[1])

        close = next((e for e in elements
                      if _at(e, (x_lo, x_hi), (y_lo, y_hi))
                      and (getattr(e, "element_type", "") or "") == "icon"), None)
        f_lo, f_hi = _FIELD_X[0] * w, _FIELD_X[1] * w
        field = next((e for e in elements
                      if _at(e, (f_lo, f_hi), (y_lo, y_hi))
                      and (getattr(e, "element_type", "") or "") != "icon"), None)

        # THE HEAD IS THE FIELD AND ITS DISMISS CONTROL, SIDE BY SIDE. Neither alone will do,
        # and this is NOT the old conjunction returning: that one demanded the field be a
        # WIDE BOX and the rows be several, which is why an unlucky parse of either half
        # denied the rail. Here both halves are only asked to EXIST somewhere in the head
        # band, which is what "the box and the list always show together" means.
        #
        # The X alone is not enough because the map's port PINS are icons too and land in the
        # same band — measured 2026-09-01, when two of them classified a bare map as a list
        # and the bot typed 'Barc' into the map twice. The field alone is not enough because
        # a query in the box parses as bare text that nothing distinguishes from a label.
        if close is None or field is None:
            return None
        return DestinationRail(close_x=(close.cx, close.cy), field=field)
    except Exception as exc:                      # noqa: BLE001 — a detector must not raise
        logger.debug(f"[rail] destination-rail check failed: {exc}")
        return None
