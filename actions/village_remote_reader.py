"""Remote village readers (barter-nav gaps A + B) — read a village's barter recipe
and a material's source ports from the WORLD MAP, without sailing there.

Validated live 2026-08-15 from Malé (project_village_info_readable_remotely):

  A. read_barter_ratios — the Village Info → Barter → Trade List (with the
     "View by Min. Exchange Unit" toggle OFF) shows each good with its per-round
     number: the output good's yield + each material's amount needed for one round
     (e.g. Box of Nutmeg 591 ← 136 Ebony + 204 Coral + 180 Textiles).

  B. read_material_sources — tapping a material's location 📍 icon opens a "Source"
     panel listing the Market ports that sell it (Coral → Malé, Atuona, Guam, …),
     which we record into RecipeInput.source_ports so next time it's a KB lookup.

Both readers consume OMNIPARSER elements (never raw crop+OCR) — these are chromed
panels, and OmniParser gives clean, layout-robust elements (it even keeps
"Santo Domingo" as one element where raw OCR split it). This follows the standing
rule: chromed screen ⇒ OmniParser. The pure parsers are unit-testable on element
data; the *_frame wrappers run OmniParser on a live frame.
"""
from __future__ import annotations

import re
from typing import Optional, Sequence
from utils.digits import SEPARATORS as _SEP

_INT_RE = re.compile(r"^\d[\d,.\']*$")
# Words that appear in the Trade List / Source panels but are not a good or a port.
_BARTER_CHROME = {"spices", "wares", "jewelry", "fabrics", "food", "textile", "metal",
                  "trade list", "closeout", "barter", "explore", "base", "village info",
                  "view by min. exchange unit", "market", "current location", "source"}


def _to_int(s: str) -> Optional[int]:
    s = (s or "").strip().translate(_SEP)
    return int(s) if s.isdigit() else None


def _pos(e):
    if isinstance(e, (tuple, list)):
        return (e[-2], e[-1]) if len(e) >= 2 else (None, None)
    return getattr(e, "cx", None), getattr(e, "cy", None)


def _label(e):
    if isinstance(e, (tuple, list)):
        return str(e[0])
    return getattr(e, "label", "") or ""


def read_barter_ratios(elements, num_x_max: int = 1830, row_tol: int = 45) -> dict:
    """From the Village Info Barter Trade List: {good_name: per-round number}.

    Each row has a NUMBER in the left column (cx < num_x_max) and the good NAME to
    its right on the same row. Returns {good: number} for every row (the output good
    + each material) — the caller knows which name is the recipe's output good."""
    numbers, names = [], []
    for e in elements:
        cx, cy = _pos(e)
        lab = _label(e).strip()
        if cx is None or cy is None or not lab:
            continue
        if _INT_RE.match(lab) and cx < num_x_max:
            numbers.append((cx, cy, _to_int(lab)))
        elif lab.lower() not in _BARTER_CHROME and any(c.isalpha() for c in lab) and cx >= num_x_max:
            names.append((cx, cy, lab))
    out = {}
    for _ncx, ncy, val in numbers:
        row = [(nm, abs(cy - ncy)) for _cx, cy, nm in names if abs(cy - ncy) <= row_tol]
        if row and val is not None:
            name = min(row, key=lambda z: z[1])[0]
            out[name] = val
    return out


# Resolution lives in `memory.places` — a port name is KB data, and the task layer must be
# able to ask about one without importing this module. Re-exported so existing callers here
# and their tests are unchanged.
from memory.places import (_catalogues, _strip_name,            # noqa: F401
                           resolve_source_port, resolve_source_village)


def _list_panel(frame, card):
    """The Source dialog's LEFT PANEL — the cream card the places are listed on.

    MEASURED, NOT A FRACTION OF THE DIALOG (user, 2026-09-08: "it should only read the left
    panel of the dialog"). The right two-thirds of the Source modal is a world map, and the
    map is where every phantom came from: villages showing past the left edge, port labels
    painted above the title bar. A fraction of the dialog's width is the same fixed-window
    thinking that caused this, one level in.

    The panel is chrome with a fixed palette and the map is not. Measured on the four
    captured panels: the cream columns read 226-238, the map beside them 117-163, with ~60
    points of clear air. The cut is taken between the two rather than baked, so a differently
    sized dialog still splits at its own boundary.
    """
    try:
        import numpy as _np
        x1, y1, x2, y2 = card
        band = _np.asarray(frame.convert("L")).astype(float)[y1 + 250:y2 - 60, x1:x2]
        if band.size == 0:
            return None
        cols = band.mean(axis=0)
        lo, hi = _np.percentile(cols, 10), _np.percentile(cols, 90)
        if hi - lo < 40:                     # no two-tone split — do not guess at an edge
            return None
        cut = (lo + hi) / 2.0
        # THE FIRST COLUMN IS THE DIALOG'S OWN BORDER, darker than the cream it frames, so
        # the run is found rather than assumed to start at the edge.
        bright = [i for i, v in enumerate(cols) if v >= cut]
        if not bright:
            return None
        start = bright[0]
        end = start
        for i in bright:
            if i - end > 12:                 # a gap wider than the border ends the panel
                break
            end = i
        return (x1 + start, y1, x1 + end, y2) if end > start else None
    except Exception:                        # noqa: BLE001 — no panel is a refusal
        return None


def source_card_box(frame):
    """The Source dialog's own box, or None. A centred modal under a brown title bar."""
    try:
        from vision.region_detectors.dialog import find_title_bars, card_from_bar
        best = None
        for bar in find_title_bars(frame) or []:
            card = card_from_bar(frame, bar)
            if card is None:
                continue
            x1, y1, x2, y2 = card
            if best is None or (x2 - x1) * (y2 - y1) < (best[2] - best[0]) * (best[3] - best[1]):
                best = card                 # the innermost card is the Source modal
        return best
    except Exception:                        # noqa: BLE001 — no box is a refusal, not a crash
        return None


def read_material_sources_by_kind(frame, elements, *, card=None) -> dict:
    """{'market': [ports], 'village': [villages]} from a material's Source panel.

    READ INSIDE THE DIALOG, NOT A RECTANGLE. This used to take a fixed window — `cx < 700`,
    `430 <= cy <= 860` — which spans the dialog's left column AND THE WORLD MAP'S VILLAGE
    LIST BEHIND IT, then sorted by y so the two interleaved. Read live 2026-08-25 for
    Matchlock Gun, whose panel says in full "Market: Barcelona, Seville":

        ['Barcelona', 'Chinook', 'Village', 'Seville', 'Ciguayo Village', 'Oriya Village',
         'Nubia', 'Village', 'Sioux Village', 'Bari Village', 'Lusitanian Villag', ...]

    Every "village" there is a row of the map list showing past the dialog's left edge —
    Barcelona (cx 426) beside Chinook Village (cx 228), Seville (cx 404) beside Ciguayo
    Village (cx 223). `Lusitanian Villag` is truncated because the dialog clips it, and
    `Nubia Village` split in two because it wrapped. No village sells Matchlock Gun and the
    panel never said one did; the window invented them. `a-box-bigger-than-its-thing`, and
    `dialogs-are-centered-panels-are-not-left` names the gutter it leaked from.

    THE CATALOGUE SAYS WHICH KIND EACH LINE IS, so the section headings need not be found —
    which matters, because OmniParser does not reliably detect them and the material's own
    category line repeats the same words ("Market" at cx 849 is the header, not the list).
    A line naming a port is a market source, a line naming a village is a village source,
    and a line naming neither is a heading, a chrome word, or a production recipe.

    PRODUCTION IS DROPPED (user, 2026-09-08: "lets filter out production for now"). It is a
    crafting feature, not a place — `Smelting Handbook: Uncut Ore - Iron` names no place and
    falls out here for free.

    A VILLAGE IS A REAL SOURCE (user: "some materials are available at both villages and
    ports, like diamond. And some only at villages or ports").
    """
    card = card or source_card_box(frame)
    if card is None:
        return {}
    panel = _list_panel(frame, card) if frame is not None else None
    x1, y1, x2, y2 = panel or card
    list_x_max = x2 if panel else x1 + (x2 - x1) * 0.34

    rows = []
    for e in elements:
        cx, cy = _pos(e)
        lab = _label(e).strip()
        if cx is None or cy is None or not lab or not any(c.isalpha() for c in lab):
            continue
        # BOTH BOUNDS. Bounding x alone still read the world map ABOVE the dialog: live
        # 2026-09-08 on Damascus Steel, whose Market list is the single port Beirut, the
        # reader also took 'Istanbul' (cy 32) and 'Thessaloniki' (cy 68) — port labels
        # painted on the map behind, sitting in the same x band as the dialog's list and
        # far above its title bar at y 167.
        if not (x1 <= cx <= list_x_max and y1 <= cy <= y2):
            continue                        # outside the dialog, or over on its map
        low = lab.lower()
        if low in _BARTER_CHROME or low == "icon" or "current" in low or "locati" in low:
            continue
        rows.append((cy, lab))
    rows.sort()

    def _keep(kind: str, name: str) -> None:
        out.setdefault(kind, [])
        if name not in out[kind]:
            out[kind].append(name)

    out: dict = {}
    labels = [lab for _cy, lab in rows]
    i = 0
    while i < len(labels):
        lab, step = labels[i], 1
        port, village = resolve_source_port(lab), resolve_source_village(lab)
        if port is None and village is None and i + 1 < len(labels):
            # A WRAPPED NAME IS TWO LINES OF ONE PLACE. 'Prey Nokor' comes back as 'Nokor'
            # and 'Prey' — two entries, neither a place, and a gather leg aimed at whichever
            # one it took first. Try the pair, both ways round, before giving up on either.
            # NOTE it can also join a HEADING to a wrapped name: on the Damascus Steel
            # panel OmniParser returns 'Village' (the section heading, cy 522), then 'Turk'
            # and 'Village' (cy 571, the name split in two). The pair 'Village' + 'Turk'
            # resolves to Turk Village and is right — and stays right with several villages
            # listed, because the heading and the wrapped suffix are the same word. That is
            # a coincidence, not a design; the catalogue is what keeps it honest.
            for pair in (f"{lab} {labels[i + 1]}", f"{labels[i + 1]} {lab}"):
                port, village = resolve_source_port(pair), resolve_source_village(pair)
                if port is not None or village is not None:
                    step = 2
                    break
        if port is not None:
            _keep("market", port)
        elif village is not None:
            _keep("village", village)
        i += step
    return out


def read_material_sources(elements, *, card) -> list:
    """The MARKET ports named inside `card`. The box is required — see the by-kind reader.

    It used to take a fixed window and no box, which is the whole defect: the window fell
    outside the dialog and read the world map behind it.
    """
    return list(read_material_sources_by_kind(None, elements, card=card).get("market") or [])


# ── OmniParser frame wrappers (chromed screen ⇒ OmniParser, never raw OCR) ──────

def _omni(frame):
    from vision.omniparser import parse_fast_cached
    return parse_fast_cached(frame)


def read_barter_ratios_frame(frame) -> dict:
    """Read the village barter recipe {good: per-round number} from a live Village
    Info → Barter Trade List frame (OmniParser)."""
    return read_barter_ratios(_omni(frame))


def read_material_sources_frame(frame) -> list:
    """A material's Source-panel MARKET ports, from a live frame."""
    return list(read_material_sources_by_kind(frame, _omni(frame)).get("market") or [])


def read_material_sources_by_kind_frame(frame) -> dict:
    """{'market': [...], 'village': [...]} for a material, from a live frame."""
    return read_material_sources_by_kind(frame, _omni(frame))
