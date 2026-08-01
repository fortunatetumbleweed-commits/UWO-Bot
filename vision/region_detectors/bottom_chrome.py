# vision/region_detectors/bottom_chrome.py
#
# Bottom-strip chrome detector — the wifi/battery/UID/server row that
# appears on EVERY UWO screen.  Slice 1.
#
# This detector exists primarily to confirm "the game is showing a real
# frame, not a blank transition / loading screen".  Per-element parsing
# (battery percent, UID digits) is deferred — the field is binary
# `present` for now plus optional server name when it parses cleanly.
#
# The server-name set is the small list of UWO servers we know about.
# Used as an additional sanity check that this is actually the bottom
# chrome row and not some other text that happens to be at y > 0.95.

from __future__ import annotations

from typing import List, Optional

from vision.omniparser import DetectedElement
from vision.scene_model import BottomChromeRegion


# Normalised region: bottom strip, full width, last 4% of frame height.
# On a 1080-tall frame that's y > 1036.  Conservative; the actual chrome
# starts ~y=1040 and goes to the very bottom.
_BOTTOM_STRIP_Y_NORM = 0.96

# Known UWO server names.  When we see one of these in the bottom strip,
# we're confident this is a real game frame.  Extend as new servers are
# observed.
_KNOWN_SERVERS = (
    "atlantic ocean",
    "pacific",
    "indian ocean",
    "arctic",
    "mediterranean",
)


def detect_bottom_chrome(
    elements:     List[DetectedElement],
    frame_width:  int,
    frame_height: int,
) -> Optional[BottomChromeRegion]:
    """Return a BottomChromeRegion describing the bottom-strip chrome.

    Returns None when no chrome elements are detected at the bottom of
    the frame — usually means a loading screen or blank transition.
    """
    y_threshold = int(_BOTTOM_STRIP_Y_NORM * frame_height)
    bottom_elements = [
        el for el in elements
        if el.cy >= y_threshold and (el.label or "").strip()
    ]
    if not bottom_elements:
        return None

    server_name = _find_server_name(bottom_elements)
    return BottomChromeRegion(
        present=True,
        server_name=server_name,
    )


def _find_server_name(elements: List[DetectedElement]) -> Optional[str]:
    """Find the UWO server name in the bottom-strip elements.

    OCR for the server name in this row tends to be reliable because
    the text is on a clean dark background.  We match against known
    server names case-insensitively and return the canonical form when
    we find one.
    """
    for el in elements:
        text = (el.label or "").lower()
        for server in _KNOWN_SERVERS:
            if server in text:
                # Return the canonical title-case version
                return server.title() if server != "atlantic ocean" else "Atlantic Ocean"
    return None
