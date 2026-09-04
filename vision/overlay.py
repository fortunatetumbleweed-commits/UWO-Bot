"""Is anything covering the screen, and may we tap through it?

Two things in UWO wear the SAME chrome — a brown title bar, median RGB (76,49,37):

  * a **panel** (the barter panel, the market's Purchase/Sell right panel), and
  * a **dialog** (Trade Goods Info, the quantity keypad, the sell result).

Style therefore cannot tell them apart. What separates them is the **scrim**: a dialog
dims everything behind it, a panel does not. The game's own layout rules (user,
2026-08-27):

  * dialogs are always centred, horizontally AND vertically — never on the left;
  * panels may sit right or centre — never left — and **never cover a UI control**,
    so nothing behind a panel is ever blocked;
  * while a scrim is up NOTHING outside the modal is tappable. For a dialog the bot
    itself opened, an outside tap DISMISSES it; for a game-pushed popup (daily news)
    an outside tap does nothing at all. Neither is ever useful, and the first is
    destructive.

That last rule is why this module exists. Live 2026-08-27 at Barcelona: `sell_down_to`
misread Candle as 2148 (truly 148), failed to match the real `1/148` field, and aborted.
Its cleanup then found "Put In Bulk" still perfectly legible THROUGH the scrim, read it
OFF, and tapped it — dismissing the very dialog whose `1/148` was the fact that would
have corrected the misread. OmniParser reads straight through a scrim, so "I can see it"
had been standing in for "I can press it".

Because a dialog is always centred and a panel is never on the left, the **left gutter is
outside both by construction** — a calibrated probe region grounded in a layout rule, not
a constant that merely happened to work. The scrim is a flat 50% black, so it caps white
chrome at 255→128 there; measured on the stage suite it is exactly 128 under a modal and
exactly 255 without one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

import numpy as np
from loguru import logger
from PIL import Image

# Probe region, as fractions of the frame — the left gutter, which no dialog or panel
# may occupy. `_BORDER_PX` skips the capture tool's recording border, whose bright green
# otherwise reads as un-dimmed chrome on every frame.
_BORDER_PX = 16
_GUTTER_X1_FRAC = 0.12
_GUTTER_Y0_FRAC, _GUTTER_Y1_FRAC = 0.10, 0.95

# White chrome under a 50% scrim: 255 → 128. Read three ways, not two — a screen that is
# simply DARK (a full-screen takeover like the idle lock, 41; a pushed promotion, 66) is a
# different question from a scrim over a live screen, and must not be mistaken for one.
_SCRIM_LO, _SCRIM_HI = 100, 200

# Bounding the dialog from the bright-pixel profile: a column/row counts as part of it
# when this fraction of its length is bright; runs bridge gaps of `_PROFILE_GAP` px so
# the dialog's own dark dividers do not split it. `_CENTRED_TOL` enforces the layout
# rule that a dialog is centred on both axes.
_PROFILE_FRAC = 0.02
_PROFILE_GAP = 40
_CENTRED_TOL = 0.05

CLEAR, SCRIM, DARK = "clear", "scrim", "dark"


def scrim_state(frame: Image.Image) -> str:
    """`CLEAR` (no overlay), `SCRIM` (a centred modal is up), or `DARK` (full-screen
    takeover). Judged from the left gutter's white point — see the module docstring."""
    try:
        a = np.asarray(frame.convert("L")).astype(np.float32)
        h, w = a.shape
    except (AttributeError, TypeError, ValueError):
        # A guard must never crash what it guards. Anything unreadable is reported CLEAR:
        # unknown means "do not block", which leaves behaviour exactly as it was before
        # this check existed, rather than stranding the caller.
        logger.debug("[overlay] frame is not readable as an image — reporting CLEAR")
        return CLEAR
    gutter = a[int(h * _GUTTER_Y0_FRAC):int(h * _GUTTER_Y1_FRAC),
               _BORDER_PX:int(w * _GUTTER_X1_FRAC)]
    if gutter.size == 0:
        return CLEAR
    white = float(np.percentile(gutter, 99.9))
    if white >= _SCRIM_HI:
        return CLEAR
    return SCRIM if white >= _SCRIM_LO else DARK


@dataclass(frozen=True)
class Overlay:
    """What is covering the screen right now, and what that forbids.

    `kind` is 'none', 'panel' or 'modal'. `bbox` is the modal's extent when known — used
    to SCOPE reads to the dialog as well as to guard taps, which is how the quantity field
    is told apart from the Cargo bar without having to guess a number first.
    """
    kind: str
    state: str
    bbox: Optional[Tuple[int, int, int, int]] = None
    title: Optional[str] = None

    @property
    def is_modal(self) -> bool:
        """Only a SCRIM counts. `DARK` says the gutter holds no bright chrome at all,
        which a full-screen takeover produces — and so does a genuinely dark scene. The
        128 cap is measured and unambiguous; darkness is not, and blocking every tap on an
        unproven signal would strand the bot on any dim screen. Full-screen states are
        recognised by the family classifier, which is where that question belongs."""
        return self.kind == "modal"

    def blocks(self, x: int, y: int) -> bool:
        """May this point NOT be tapped? True while a modal is up and the point lies
        outside it — a panel never blocks anything, by the game's layout rule.

        With no bbox the whole screen is blocked: knowing a modal is up but not where it
        is means no point can be shown to be safe, and guessing is what this prevents.
        """
        if not self.is_modal:
            return False
        if self.bbox is None:
            return True
        x0, y0, x1, y1 = self.bbox
        return not (x0 <= x <= x1 and y0 <= y <= y1)


def detect_overlay(frame: Image.Image, elements: Optional[Sequence] = None) -> Overlay:
    """What is on top of this frame. `elements` (an OmniParser pass) is optional and only
    sharpens the modal's bbox; the verdict itself comes from the scrim."""
    state = scrim_state(frame)
    if state == CLEAR:
        return Overlay(kind="none", state=state)
    if state == DARK:
        return Overlay(kind="dark", state=state)
    bbox, title = _modal_bbox(frame, elements)
    return Overlay(kind="modal", state=state, bbox=bbox, title=title)


def _modal_bbox(frame: Image.Image, elements: Optional[Sequence]):
    """The centred dialog's extent. Under a scrim the only undimmed pixels ARE the dialog,
    so no element list is needed to find it — but a bare min/max over bright pixels is far
    too loose: 608 stray highlights scattered across the dimmed right panel stretched the
    box to the frame edge (live frame_0011), and a slack box wrongly ALLOWS taps on the
    dimmed controls it covers. Take instead the longest bright run of columns and of rows,
    tolerating small gaps — a dialog has dark dividers through its middle, which is why
    expanding outwards from the centre finds nothing.

    Returns (bbox, title); `title` comes from `elements` when supplied."""
    a = np.asarray(frame.convert("L")).astype(np.float32)
    h, w = a.shape
    bright = a > _SCRIM_HI
    cols = _longest_run(bright.sum(0), h * _PROFILE_FRAC, _PROFILE_GAP)
    rows = _longest_run(bright.sum(1), w * _PROFILE_FRAC, _PROFILE_GAP)
    if cols is None or rows is None:
        return None, None
    bbox = (cols[0], rows[0], cols[1], rows[1])

    # A dialog is ALWAYS centred on both axes (the game's layout rule). A box that is not
    # centred is therefore not a dialog but a misread, and the honest answer is "a modal is
    # up, extent unknown" — which blocks every tap rather than licensing one.
    off_x = abs((bbox[0] + bbox[2]) / 2 - w / 2)
    off_y = abs((bbox[1] + bbox[3]) / 2 - h / 2)
    if off_x > w * _CENTRED_TOL or off_y > h * _CENTRED_TOL:
        logger.warning(f"[overlay] bright region {bbox} is not centred "
                       f"(off by {off_x:.0f},{off_y:.0f}px) — not treating it as the "
                       "dialog's extent; every tap stays blocked")
        return None, None

    title = None
    for e in elements or []:
        x1, y1 = getattr(e, "x1", None), getattr(e, "y1", None)
        lab = (getattr(e, "label", "") or "").strip()
        if lab and x1 is not None and y1 is not None and _inside(bbox, x1, y1):
            title = lab
            break
    return bbox, title


def _longest_run(profile, thresh: float, gap: int):
    """Longest stretch of `profile` at or above `thresh`, bridging gaps up to `gap`."""
    on = profile >= thresh
    n = len(on)
    best = None
    i = 0
    while i < n:
        if not on[i]:
            i += 1
            continue
        start = last = i
        k = i
        while k < n:
            if on[k]:
                last = k
            elif k - last > gap:
                break
            k += 1
        if best is None or (last - start) > (best[1] - best[0]):
            best = (int(start), int(last))
        i = k
    return best


def _inside(bbox, x, y) -> bool:
    x0, y0, x1, y1 = bbox
    return x0 <= x <= x1 and y0 <= y <= y1
