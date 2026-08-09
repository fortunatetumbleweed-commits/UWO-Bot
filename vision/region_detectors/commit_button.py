"""Yellow commit-button detector — normalises UWO's canonical positive action.

Every transaction screen in Uncharted Waters Origin (recruit crew, market Buy /
Sell, item shop, every shop) commits via one **yellow/gold button** styled as
`<currency-icon> <cost>  <VERB>` — cost on the left, action verb on the right.
Yellow = positive action, game-wide.

OmniParser localises this button reliably but collapses it to ONE element and
OCRs only ONE of its two texts — and *which* one is unstable: across live
captures it grabbed the COST (`205,848`, `707,048`) every time and the verb
(`Recruit`) never, so the reasoning layer never saw a button it could recognise.
Its bbox is right though, and the button is trivially separable by colour
(yellow-fraction ~0.35–0.77 vs ~0.00 for menus / list rows / type toggles).

So we re-OCR OmniParser's own button crops that are yellow, and split the text
into `{verb, cost}` — recovering BOTH regardless of which one OmniParser's label
caught. The result is a stable `[COMMIT]` affordance the reasoning layer can
always act on. See memory `project_yellow_commit_button_style`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

import numpy as np
from PIL import Image

from vision.ocr import read_text

# The commit/OK button lights up ~0.32–0.77 yellow AND is a WIDE pill (verb, or
# cost-left+verb-right). A goods TILE's yellow price-bar / "Specialties" banner is
# only a partial highlight (~0.21) on a squarish tile (aspect ~1.8) — NOT a button.
# Require both a strong yellow fill and a wide aspect to reject those highlights.
YELLOW_MIN_FRAC = 0.28
MIN_ASPECT      = 2.3      # width/height; buttons ≥2.8, tiles ~1.8


@dataclass
class CommitButton:
    verb: str            # action verb, e.g. "Recruit" / "Buy" / "Sell" ("" if unread)
    cost: str            # currency amount text, e.g. "205,848" ("" if none)
    cx: int
    cy: int
    x1: int
    y1: int
    x2: int
    y2: int
    yellow_frac: float

    @property
    def label(self) -> str:
        """The affordance label the reasoning layer sees (verb, or cost fallback)."""
        return self.verb or (f"commit ({self.cost})" if self.cost else "commit")


def yellow_fraction(arr: np.ndarray, x1: int, y1: int, x2: int, y2: int) -> float:
    """Fraction of yellow/gold pixels in a bbox — the commit-button signature."""
    h, w = arr.shape[:2]
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
    c = arr[y1:y2, x1:x2].reshape(-1, 3)
    if len(c) == 0:
        return 0.0
    r, g, b = c[:, 0].astype(int), c[:, 1].astype(int), c[:, 2].astype(int)
    return float(((r > 150) & (g > 120) & (b < 110) & (abs(r - g) < 80)).mean())


def looks_like_commit_button(w: int, h: int, yellow_frac: float) -> bool:
    """True for a WIDE, mostly-yellow pill (commit/OK) — rejects a squarish,
    weakly-yellow goods-tile highlight. Calibrated on: Recruit 554×75/0.37 (yes),
    dialog OK 215×77/0.32 (yes), Whisky tile 432×240/0.21 (NO)."""
    return w >= MIN_ASPECT * max(h, 1) and yellow_frac >= YELLOW_MIN_FRAC


def _split_verb_cost(text: str) -> tuple:
    """Classify OCR tokens: numeric-ish → cost, alphabetic → verb."""
    verb_toks, cost_toks = [], []
    for t in (text or "").replace("\n", " ").split():
        core = t.replace(",", "").replace(".", "").replace("%", "")
        if core.isdigit():
            cost_toks.append(t)
        elif any(ch.isalpha() for ch in t):
            verb_toks.append(t)
    return " ".join(verb_toks).strip(), " ".join(cost_toks).strip()


def detect_commit_buttons(elements, frame: Image.Image,
                          min_yellow: float = YELLOW_MIN_FRAC) -> List[CommitButton]:
    """Find yellow OmniParser buttons and re-OCR them into {verb, cost}.

    `elements` — raw OmniParser elements (need `.element_type`, `.x1..y2`, `.cx/cy`).
    Returns the normalised commit buttons on screen (usually one; markets can have
    Buy + Sell).
    """
    arr = np.asarray(frame.convert("RGB"))
    out: List[CommitButton] = []
    for e in elements:
        if getattr(e, "element_type", "") != "button":
            continue
        x1, y1, x2, y2 = e.x1, e.y1, e.x2, e.y2
        yf = yellow_fraction(arr, x1, y1, x2, y2)
        # a real commit/OK button is a WIDE, mostly-yellow pill; a squarish weakly-
        # yellow thing is a goods-tile highlight (tap to LOAD), not the commit button.
        if yf < min_yellow or not looks_like_commit_button(x2 - x1, y2 - y1, yf):
            continue
        verb, cost = _split_verb_cost(read_text(frame.crop((x1, y1, x2, y2))))
        if not verb:
            # verb lives on the right — OCR that portion alone as a fallback
            cut = x1 + int(0.55 * (x2 - x1))
            v2, c2 = _split_verb_cost(read_text(frame.crop((cut, y1, x2, y2))))
            verb = verb or v2
            cost = cost or c2
        out.append(CommitButton(verb=verb, cost=cost,
                                cx=int(getattr(e, "cx", (x1 + x2) // 2)),
                                cy=int(getattr(e, "cy", (y1 + y2) // 2)),
                                x1=x1, y1=y1, x2=x2, y2=y2, yellow_frac=round(yf, 2)))
    return out
