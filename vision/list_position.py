"""Where a scrollable list IS, measured from the screen rather than from the parse.

Two questions the reader must answer without trusting the parser, because the parser is the
layer that fails:

  * **Did the list move?** `content_shift` aligns two frames and returns the offset. Zero
    means the gesture did nothing, whatever it asked for.
  * **Which end is it at?** `scrollbar_thumb` finds the thumb and `bar_position` says whether
    it is against the top or the bottom of its track.

Both used to live in the diagnostic report, while the reader itself decided it had finished
by asking whether the last screen PARSED anything new. That conflates "there is nothing more
to read" with "I could not read this screen": on 2026-08-25 a parser bug blanked two Cheyenne
screens and the sweep declared the end of the list with two goods still below the fold.
"""

from __future__ import annotations

from loguru import logger


def scrollbar_thumb(img, vp):
    """(thumb_y1, thumb_y2) of the list's scrollbar, or None.

    THE SCROLLBAR IS THE ONLY THING ON SCREEN THAT KNOWS WHERE THE LIST IS. Row tiles say
    what is visible; they cannot say whether anything lies above or below it. Twelve
    identical frames were read as "the scroll is broken" when the thumb was sitting flush
    against the bottom of its track in the very first frame — the list was at its end before
    a single gesture was sent, and no scroll could have moved it.

    The thumb is found by SHAPE AND PLACE, not by being the biggest white thing: it is a
    narrow band of columns that all carry the same vertical run, and it lies to the right of
    every row card. Taking the longest run instead picks up the white edge of whichever card
    happens to be tallest, which put the thumb 150px off and called an exhausted list
    "mid-list".
    """
    import numpy as np
    if not vp:
        return None
    a = np.asarray(img.convert("RGB"), dtype=int)
    top, bot = max(0, vp[0] - 12), min(a.shape[0], vp[1] + 12)
    track = bot - top

    runs_at = {}
    for x in range(2200, 2250):
        col = a[top:bot, x]
        white = (col.min(axis=1) > 205) & (col.max(axis=1) - col.min(axis=1) < 22)
        ys = np.where(white)[0]
        if len(ys) < 40:
            continue
        runs, st = [], ys[0]
        for i in range(1, len(ys)):
            if ys[i] != ys[i - 1] + 1:
                runs.append((st, ys[i - 1]))
                st = ys[i]
        runs.append((st, ys[-1]))
        y1, y2 = max(runs, key=lambda r: r[1] - r[0])
        if 30 <= (y2 - y1 + 1) <= 0.95 * track:
            runs_at[x] = (y1 + top, y2 + top)

    # Right to left: the first band of columns sharing one run is the scrollbar.
    xs = sorted(runs_at, reverse=True)
    band = []
    for x in xs:
        if not band:
            band = [x]
            continue
        y1, y2 = runs_at[x]
        b1, b2 = runs_at[band[-1]]
        if band[-1] - x == 1 and abs(y1 - b1) <= 5 and abs(y2 - b2) <= 5:
            band.append(x)
        elif len(band) >= 3:
            break
        else:
            band = [x]
    if len(band) < 3:
        return None
    y1 = min(runs_at[x][0] for x in band)
    y2 = max(runs_at[x][1] for x in band)
    return (y1, y2)


def bar_position(bar, vp):
    """(at_top, at_end) from the scrollbar thumb's place in its track.

    THE TRACK IS NOT SYMMETRIC around the list viewport. Measured on Cheyenne: at the very
    top the thumb sits 23px inside the estimated track, at the very bottom only 9px. A fixed
    tolerance between those two answers "am I at the top?" with a permanent NO, which is how
    a rewind loop swiped to its 12-swipe limit against a list already at the top (live
    2026-08-25) — the swipes were real, the arrival was never recognised.

    The tolerance therefore scales with how far the thumb can actually travel, so it stays
    meaningful on a long list and forgiving on a short one.
    """
    if not bar or not vp:
        return (False, False)
    top, bot = vp[0] - 12, vp[1] + 12
    travel = (bot - top) - (bar[1] - bar[0] + 1)
    tol = max(25.0, 0.08 * travel)
    return ((bar[0] - top) <= tol, (bot - bar[1]) <= tol)


def content_shift(prev, cur, max_px: int = 400):
    """How far the list ACTUALLY moved between two frames, in pixels.

    The report asked for the requested distance and never for the delivered one, so twelve
    scrolls against the bottom stop looked exactly like twelve successful ones. This aligns
    the two crops by brute-force vertical shift and returns the offset with the lowest mean
    absolute difference — 0 means the list did not move, whatever the gesture asked for.
    """
    import numpy as np
    a = np.asarray(prev.convert("L").crop((1690, 382, 2260, 967)), dtype=float)
    b = np.asarray(cur.convert("L").crop((1690, 382, 2260, 967)), dtype=float)
    # THE SEARCH CANNOT EXCEED THE OVERLAP. The comparison crop is one viewport tall (~585px),
    # so a shift larger than that leaves no shared content to align and the slice goes empty.
    # This is a real limit, not a tuning knob: a scroll that advances more than a full screen
    # skips rows outright, and no frame alignment can measure what is on neither frame. An
    # unconvincing match therefore means EITHER the list did not move OR it jumped more than a
    # screen — the scrollbar is what tells those apart.
    max_px = max(1, min(max_px, a.shape[0] - 40))
    best, zero = None, None
    # BOTH DIRECTIONS. Searching only positive offsets cannot represent content moving the
    # other way, so an up-scroll came back as "47px, residual 12" — a mismatch dressed up as
    # a measurement. Negative means the content moved DOWN, i.e. the list scrolled up.
    for dy in range(-max_px, max_px + 1):
        if dy > 0:
            x, y = a[dy:], b[:len(b) - dy]
        elif dy < 0:
            x, y = a[:len(a) + dy], b[-dy:]
        else:
            x, y = a, b
        err = float(np.abs(x - y).mean())
        if dy == 0:
            zero = err
        if best is None or err < best[1]:
            best = (dy, err)
    # A MATCH IS RELATIVE, NOT ABSOLUTE. A real scroll pulls unseen rows into the frame, so
    # even a perfect alignment carries a few units of residual; a fixed cut-off called a
    # genuine 324px scroll "unmeasurable". What marks a true match is being decisively
    # better than not shifting at all.
    return (best[0], best[1], zero)
