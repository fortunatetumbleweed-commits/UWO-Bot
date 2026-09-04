"""Where the fleet is BOUND, read off the sea HUD.

The sea HUD carries the answer at bottom-centre, in the same place the world map puts its
Move / Go-to-City control (user, 2026-08-26):

        Barcelona
        ETA  1 d

THIS IS THE OBSERVATION THAT REPLACES A CONCLUSION. CLAUDE.md forbids storing "the departure
failed" because it cannot be checked by looking. "Bound for Barcelona, ETA 1 day" can — it is
printed on the screen, and it answers the same question without any inference at all.

The cost of not reading it, live 2026-08-26: the fleet left Seville for Barcelona and the
departure worked. A post-tap check misread the departure cinematic as failure, so the goal
re-opened the world map MID-VOYAGE and searched for Barcelona again — while this readout said
`Barcelona / ETA 1 d` at 19:20, and the map's own frame showed the fleet's marker already
beside the port. Two screens were saying "you are on your way" and nothing asked either.

An empty readout is meaningful too, and is NOT the same as an unreadable one:

    a NAME        the fleet is under way to that port, and no re-selection is wanted
    nothing       no destination is set — the fleet is drifting or manually steered
    unreadable    say so; do not report "no destination", which reads as the case above
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

from loguru import logger

# Bottom-centre, normalised so it rides resolution and the notch. Measured from the live
# 2400×1080 frame where the plate sat at roughly x 975–1305, y 940–1010; widened enough to
# hold a longer port name without swallowing the level bar below it.
_REGION = (0.35, 0.855, 0.62, 0.945)

# A day count is a number glued to a 'd' — '4d', '1 d', '12d'. Matched against a WHOLE
# token, so a stray digit elsewhere in the plate cannot be mistaken for it.
_DAYS_RE = re.compile(r"^(\d{1,3})\s*d$", re.I)


def _tokens(crop) -> list:
    """The plate's text as separate tokens, read at 2x. See read_sea_destination."""
    import numpy as np
    from vision.ocr import _get_reader
    big = crop.resize((crop.width * 2, crop.height * 2))
    return [txt for _box, txt, conf in _get_reader().readtext(np.array(big), detail=1)
            if conf >= 0.3]


@dataclass(frozen=True)
class SeaDestination:
    port: str
    eta_days: Optional[int]
    text: str
    bbox: Tuple[int, int, int, int]


def read_sea_destination(frame, *, ocr=None) -> Optional[SeaDestination]:
    """The port the fleet is bound for, or None when the HUD names none.

    `ocr` takes the crop and returns TOKENS (a list of strings), not one joined line — see
    the note below on why the difference matters.

    `None` means "no destination shown" — the fleet is not under way to anywhere. It does NOT
    mean the read failed; a failure raises through to the caller's own handling rather than
    being flattened into the same answer, because the two want opposite responses.
    """
    w, h = frame.width, frame.height
    box = (int(_REGION[0] * w), int(_REGION[1] * h),
           int(_REGION[2] * w), int(_REGION[3] * h))
    crop = frame.crop(box)

    # TOKENS, NOT A JOINED STRING, AND UPSCALED FIRST.
    #
    # A small ship icon sits between 'ETA' and the number. Read at native size it blurs into
    # the digits — `ETA (ship) 4 d` came back as the single string 'ETA 94d', and a regex over
    # that reported a four-day voyage as ninety-four (caught by the user, 2026-08-26). At 2x
    # the same crop tokenises cleanly as 'Lisboa' / 'ETA' / '4d', so the icon stops being a
    # digit instead of being reasoned around.
    # `ocr` is injected by tests as a crop -> list[str]; the default reads the device frame.
    tokens = list(ocr(crop)) if ocr is not None else _tokens(crop)
    if not tokens:
        return None

    text = " ".join(tokens)
    # THE 'ETA' TOKEN IS WHAT MAKES IT A DESTINATION PLATE, and without this guard the
    # detector invents destinations. Surveyed across the trace corpus 2026-08-26, this region
    # reads 'London ETA 6d', 'Port Royal ETA @1d', 'Melanesian Village ETA 2d' — and, on
    # frames where a market panel overlays the sea, 'io Sell Supplies'. Reporting the fleet
    # as bound for 'Sell Supplies' is worse than reporting nothing.
    if not any(t.lower().startswith("eta") for t in tokens):
        logger.debug(f"[sea-hud] no ETA in {text!r} — not a destination plate")
        return None

    eta_days = None
    name_parts = []
    for t in tokens:
        m = _DAYS_RE.match(t.strip())
        if m:
            eta_days = int(m.group(1))
            continue
        if t.strip().lower().startswith("eta"):
            continue
        name_parts.append(t.strip())

    name = " ".join(name_parts)
    name = re.sub(r"[^\w\s'\-áéíóúàâäãçñüö]", " ", name, flags=re.I)
    name = " ".join(name.split()).strip()
    if not name:
        return None

    dest = SeaDestination(port=name,
                          eta_days=eta_days,
                          text=text, bbox=box)
    logger.info(f"[sea-hud] bound for {dest.port!r}"
                + (f", ETA {dest.eta_days}d" if dest.eta_days is not None else ""))
    return dest


def bound_for(frame, destination: str, *, ocr=None) -> bool:
    """True when the HUD says the fleet is already under way to `destination`.

    Deliberately a NAME comparison and nothing more. The old `_bound_elsewhere` distrusted
    this readout because the game can show a destination while a tap had no effect — which is
    a reason not to treat it as proof a tap WORKED, and no reason at all to ignore it when
    asking whether to re-select. Those are different questions, and conflating them is what
    left the answer unread.
    """
    dest = read_sea_destination(frame, ocr=ocr)
    if dest is None:
        return False
    from vision.text_correction import correct_port_name
    want = (destination or "").strip().lower()
    got = dest.port.strip().lower()
    if got == want:
        return True
    try:
        fixed, ratio = correct_port_name(dest.port)
        return bool(fixed and fixed.strip().lower() == want and ratio >= 0.8)
    except Exception as exc:
        logger.debug(f"[sea-hud] port-name correction skipped: {exc}")
        return False
