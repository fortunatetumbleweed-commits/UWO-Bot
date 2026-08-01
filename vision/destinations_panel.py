"""Read the right-side 'nearby destinations' panel on the sea view.

KNOWN-LIMITATION DRAFT (v1, 2026-05-24).  Parses OmniParser elements
in the right edge of the frame and clusters into rows; pairs each
row with an adjacent 'Approx. NN.Nkm' line.  Live verification on
Dover + Las Palmas frames showed it picks up chrome (Daytime,
clock, lat/lon) as spurious 'rows' and sometimes mis-pairs the
distance line with the wrong name.  Use the output as a SECONDARY
discovery signal (log + count) and not as the authoritative trigger
for entering a port.  The primary discovery trigger remains
OmniParser-text scan for 'Enter New City' / 'Enter New Village'.

The sea screen carries a vertical list of known destinations on the
right side of the frame, sorted by approximate distance.  Each row is:

    <icon>  <name>
            Approx. <NN.Nkm>

where the icon is an anchor for a port or a building for a village.
Per the user's 2026-05-24 note: when the bot's lookouts spot a new
settlement, it appears as a fresh row at the appropriate distance —
so a tick-over-tick diff of this list COULD be a discovery signal
once the reader is tightened.  For now it's informational.

Tightening TODO: filter rows whose name has < 3 letters, no spaces,
or matches lat/lon regex; require the distance line to sit within
50 px vertically of the name; reject rows above the panel's
top-tabs band.

Origin: 2026-05-24, clear-cloud Phase 2.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from vision.omniparser import parse_fast_cached


# The panel sits in the right portion of the sea frame, below the
# top chrome (the small icons at the very top) and above the bottom
# stats / clock area.  Coordinates are normalised to 0..1 of the
# frame width / height for resolution independence.
PANEL_ZONE_NORM = (0.82, 0.10, 1.00, 0.95)   # (l, t, r, b)

# Rows in this panel are roughly evenly spaced.  Tolerance for
# clustering elements into rows: half a row's vertical span.
_ROW_Y_TOLERANCE_PX = 30

# Regex for the distance line: "Approx. 14.3km", "Approx. 253.1km", etc.
# Tolerates 'Approx', 'approx.', and km / Km / KM.
_DISTANCE_RE = re.compile(
    r"approx[. ]*\s*([\d,.]+)\s*km", re.IGNORECASE,
)


@dataclass(frozen=True)
class DestinationRow:
    """One entry in the nearby-destinations panel."""
    name:        str                # e.g. 'Las Palmas' or 'Berber Village'
    distance_km: Optional[float]    # parsed from 'Approx. NN.Nkm', or None
    row_cy:      int                # centre Y of the row (frame coords)


def read_destinations(frame) -> list[DestinationRow]:
    """Parse the right-side destinations panel on a sea frame.

    Returns nearest-first.  Empty list if the panel can't be found
    or no rows are parseable.
    """
    try:
        elements = parse_fast_cached(frame)
    except Exception as e:
        logger.debug(f"[destinations_panel] OmniParser unavailable: {e}")
        return []

    fw, fh = frame.width, frame.height
    l, t, r, b = PANEL_ZONE_NORM
    zone_x1 = int(l * fw)
    zone_y1 = int(t * fh)
    zone_x2 = int(r * fw)
    zone_y2 = int(b * fh)

    # Keep text elements inside the panel zone.  Distance lines often
    # come through as 'text' element_type; row labels can be 'text' or
    # 'button' depending on highlight state.
    in_zone = [
        e for e in elements
        if e.element_type in ("text", "button")
        and zone_x1 <= e.cx <= zone_x2
        and zone_y1 <= e.cy <= zone_y2
        and e.label and e.label.strip()
    ]
    if not in_zone:
        return []

    # Cluster elements into rows by cy proximity.
    in_zone.sort(key=lambda e: e.cy)
    rows: list[list] = []
    for e in in_zone:
        if rows and abs(e.cy - rows[-1][0].cy) <= _ROW_Y_TOLERANCE_PX:
            rows[-1].append(e)
        else:
            rows.append([e])

    # For each cluster, separate the name line from the distance line.
    # When a row spans two lines vertically (name on top, distance
    # below), they end up in adjacent clusters.  Pair them by proximity.
    parsed: list[DestinationRow] = []
    i = 0
    while i < len(rows):
        this_row = rows[i]
        # Pick the longest text element as the canonical row label.
        labels = [e.label.strip() for e in this_row if e.label]
        if not labels:
            i += 1
            continue

        # Is this row itself a distance-only row?  If so, attach it to
        # the previous parsed entry.
        joined = " ".join(labels)
        dist_match = _DISTANCE_RE.search(joined)
        if dist_match and len(joined) < 30 and parsed:
            try:
                km = float(dist_match.group(1).replace(",", ""))
                last = parsed[-1]
                parsed[-1] = DestinationRow(
                    name=last.name, distance_km=km, row_cy=last.row_cy,
                )
            except ValueError:
                pass
            i += 1
            continue

        # Otherwise this row's "longest non-distance" label is the name.
        non_dist = [s for s in labels if not _DISTANCE_RE.search(s)]
        name = max(non_dist, key=len) if non_dist else labels[0]
        name = name.strip()

        # Try to find a distance in this row as well (some rows pack name + distance on one line).
        km = None
        m = _DISTANCE_RE.search(joined)
        if m:
            try:
                km = float(m.group(1).replace(",", ""))
            except ValueError:
                pass
        cy = sum(e.cy for e in this_row) // len(this_row)
        parsed.append(DestinationRow(name=name, distance_km=km, row_cy=cy))
        i += 1

    # Sort by cy (panel's natural order, nearest first since list is
    # already sorted by distance in-game).
    parsed.sort(key=lambda r: r.row_cy)
    return parsed


def names(rows: list[DestinationRow]) -> list[str]:
    """Convenience: just the names, in order.  Used for set diff."""
    return [r.name for r in rows]
