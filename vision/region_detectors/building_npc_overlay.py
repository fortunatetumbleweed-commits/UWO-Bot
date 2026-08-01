"""BuildingNpcOverlay — structural detector for in-building NPC overlays.

See docs/dialog_and_event_models.md → 'Building NPC overlay'.

A Building NPC overlay is the transient overlay that appears AFTER a
transaction inside a building (e.g. "The crew is ready" at the
harbor; "Hmm; never mind." at the market sell screen).  It is NOT a
dialog — it has no dark-brown title bar and no X close button.  Its
visual signature is:

  - A large NPC art icon (often ≥ 20% of screen area), biased toward
    the centre or right.
  - A speaker-name button or label (suffix-matched, colon-greeting,
    or short capitalised name near the icon).
  - Quoted-speech text or short capitalised label nearby.

DialogModel covers the bounded-card variants (title bar + content +
optional buttons).  Building NPC overlays are a sibling overlay
type with their own model so the Dialog definition stays strict.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from vision.omniparser import DetectedElement


_SPEAKER_NAME_SUFFIXES = (
    # Common UWO building-NPC role suffixes.  Extended 2026-05-18 after
    # frame 0031/0034 (Bank Clerk) and other building-specific roles.
    "owner", "official", "captain", "innkeeper", "master",
    "merchant", "trader", "blacksmith", "priest", "guard",
    "officer", "harbormaster",
    "clerk", "mayor", "tradesman", "shipwright", "quartermaster",
    "warden", "sergeant", "teller", "manager", "broker",
    "messenger", "mate", "scholar", "tutor",
)


@dataclass(frozen=True)
class BuildingNpcOverlay:
    bbox:           Tuple[int, int, int, int]
    speaker:        Optional[str] = None
    speech:         Tuple[str, ...] = ()
    npc_art_bbox:   Optional[Tuple[int, int, int, int]] = None
    bubble_bbox:    Optional[Tuple[int, int, int, int]] = None  # the speech bubble itself
    anchors_fired:  Tuple[str, ...] = ()    # which structural anchors matched

    def dismiss_action(self) -> str:
        """Building NPC overlays auto-dismiss on tap-anywhere or after
        a brief delay.  Returns the recommended runtime action."""
        return "tap_continue"


def detect_building_npc_overlay(
    elements:     Sequence[DetectedElement],
    frame_width:  int,
    frame_height: int,
) -> Optional[BuildingNpcOverlay]:
    """Return a BuildingNpcOverlay when the NPC-overlay structural
    pattern is recognised; None otherwise.

    Trigger combos (any one suffices):
      - big NPC art + speech text                       (quoted dialogue case)
      - big NPC art + nearby short-cap text label       (bare-name speaker case)
      - strong speaker label + speech text              (no big icon variant)
    """
    fw, fh = frame_width, frame_height
    frame_area = max(1, fw * fh)

    # Threshold 0.17 (was 0.20) — frame 0031_1610160824 has the NPC art
    # at 19.0% of frame area; lower threshold catches that without
    # admitting false-positives (only NPC art is that large in central
    # screen real estate during transactions).
    big_icons = [
        e for e in elements
        if e.element_type == "icon"
        and (e.width * e.height) / frame_area >= 0.17
        and e.cx >= 0.30 * fw
    ]
    strong_speakers = [
        e for e in elements
        if _looks_like_speaker_label(e, allow_bare_cap=False)
    ]
    speech_texts = [
        e for e in elements
        if e.element_type == "text"
        and _looks_like_speech(e.label)
    ]

    fired: List[str] = []
    npc_art_bbox:   Optional[Tuple[int, int, int, int]] = None
    bubble_bbox:    Optional[Tuple[int, int, int, int]] = None
    bubble_element = None
    speaker_element = None

    # Pick a speaker if we have one (used for bubble proximity).
    if strong_speakers:
        speaker_element = strong_speakers[0]

    # Find the speech bubble as a single OmniParser button element.
    # The bubble has a distinctive shape: large button (~800×256 px),
    # below the speaker, horizontally near the speaker.  When found,
    # use its bbox directly — no need to synthesise one.
    if speaker_element is not None:
        bubble_element = _find_speech_bubble(elements, speaker_element)

    if big_icons:
        bi = max(big_icons, key=lambda e: e.width * e.height)
        npc_art_bbox = (bi.x1, bi.y1, bi.x2, bi.y2)
        # When we don't have a labelled speaker yet, see if there's a
        # bubble somewhere below the NPC art and treat the topmost
        # short-cap text near the art as the speaker.
        if bubble_element is None:
            bubble_element = _find_speech_bubble_below_icon(elements, bi)
        if speaker_element is None:
            nearby = _short_cap_text_near(elements, bi)
            if nearby:
                speaker_element = nearby[0]

    # Detection rule — fire when we have AT LEAST ONE strong structural
    # anchor (bubble or big_icon) combined with one supporting signal
    # (speaker, speech text, or the other anchor).
    if bubble_element is not None:
        bubble_bbox = (bubble_element.x1, bubble_element.y1,
                       bubble_element.x2, bubble_element.y2)
        if speaker_element is not None:
            fired = ["speaker", "bubble"]
        elif big_icons:
            fired = ["big_icon", "bubble"]
        else:
            fired = ["bubble"]
    elif big_icons and speech_texts:
        fired = ["big_icon", "speech"]
    elif big_icons and speaker_element is not None:
        fired = ["big_icon", "speaker"]
    elif strong_speakers and speech_texts:
        fired = ["speaker", "speech"]

    if not fired:
        return None

    # Overall overlay bbox — union of whichever real elements anchor it.
    pieces = []
    if npc_art_bbox:  pieces.append(npc_art_bbox)
    if bubble_bbox:   pieces.append(bubble_bbox)
    if speaker_element is not None:
        pieces.append((speaker_element.x1, speaker_element.y1,
                       speaker_element.x2, speaker_element.y2))
    if not pieces:
        # speech-text-only path: union of speech text boxes
        pieces = [(t.x1, t.y1, t.x2, t.y2) for t in speech_texts]
    bbox = (
        min(b[0] for b in pieces),
        min(b[1] for b in pieces),
        max(b[2] for b in pieces),
        max(b[3] for b in pieces),
    )

    speaker_text = speaker_element.label.strip() if speaker_element else None
    bubble_speech: Tuple[str, ...] = (
        (bubble_element.label.strip(),) if bubble_element else ()
    )
    strict_speech = tuple(e.label.strip() for e in speech_texts)

    return BuildingNpcOverlay(
        bbox=bbox,
        speaker=speaker_text,
        speech=bubble_speech or strict_speech,
        npc_art_bbox=npc_art_bbox,
        bubble_bbox=bubble_bbox,
        anchors_fired=tuple(fired),
    )


def _find_speech_bubble(elements, speaker) -> Optional[object]:
    """Detect the speech bubble as a single OmniParser button element.

    The bubble's structural signature in UWO:
      - element type = "button"
      - large (width ≥ 500, height ≥ 100) — typically ~800×256
      - below the speaker (y1 within speaker.y2 ± 100)
      - horizontally near the speaker (|cx − speaker.cx| ≤ 600)
      - multi-word label (the bubble's caption text)

    Returns the bubble element when found, else None.  Caller reads
    `.x1/.y1/.x2/.y2` directly — no synthetic bbox needed.
    """
    candidates = []
    for e in elements:
        if e.element_type != "button":
            continue
        if e.width < 500 or e.height < 100:
            continue
        if e.y1 < speaker.y2 - 100:
            continue
        if abs(e.cx - speaker.cx) > 600:
            continue
        label = (e.label or "").strip()
        if len(label) < 5 or " " not in label:
            continue
        candidates.append(e)
    if not candidates:
        return None
    return max(candidates, key=lambda e: e.width * e.height)


def _find_speech_bubble_below_icon(elements, big_icon) -> Optional[object]:
    """Speaker-less variant: find a bubble button anywhere below the
    NPC art icon.  Used when the speaker label is not detected.
    """
    candidates = []
    for e in elements:
        if e.element_type != "button":
            continue
        if e.width < 500 or e.height < 100:
            continue
        if e.cy < big_icon.cy:
            continue
        if abs(e.cx - big_icon.cx) > 800:
            continue
        label = (e.label or "").strip()
        if len(label) < 5 or " " not in label:
            continue
        candidates.append(e)
    if not candidates:
        return None
    return max(candidates, key=lambda e: e.width * e.height)


# ── Helpers (shared structure used to live in dialog.py) ──────────


def _looks_like_speech(label: str) -> bool:
    s = (label or "").strip().rstrip('"\'')
    if len(s) < 5 or " " not in s:
        return False
    if not s[0].isalpha() or not s[0].isupper():
        return False
    return s.rstrip().endswith((".", "?", "!"))


def _looks_like_speaker_label(el, allow_bare_cap: bool = True) -> bool:
    if getattr(el, "element_type", None) not in ("button", "text"):
        return False
    raw = (el.label or "").strip()
    if not (3 <= len(raw) <= 25):
        return False
    if not raw[0].isalpha() or not raw[0].isupper():
        return False
    if raw.rstrip().endswith((".", "?", "!")):
        return False
    low = raw.lower()
    if low.endswith(":"):
        return True
    if any(low.endswith(s) for s in _SPEAKER_NAME_SUFFIXES):
        return True
    if allow_bare_cap:
        words = raw.split()
        if len(words) <= 2 and all(w[0].isupper() for w in words if w):
            return True
    return False


def _nearby_conversational_text(elements, speaker) -> List[DetectedElement]:
    """Return text/button elements that are part of the speech bubble
    next to *speaker*.

    The speech bubble in UWO is:
      - immediately BELOW the speaker label (y1 ≳ speaker.y2)
      - horizontally NEAR the speaker (centred under or just beside
        the NPC art — typically within ~600 px of speaker.cx)
      - made of multi-word conversational text

    Why both axes matter: with a vertical-only check, the detector
    picks up text from the underlying sub_menu's right panel that
    bleeds through the translucent overlay (frame 0067: 'many
    different ways, the easiest of' at cx=2067 is request-screen
    body text, NOT what the NPC says).  The real bubble for that
    frame is 'look forward to your success:' at cx=1154 — within
    a tight horizontal window of the speaker at cx=891.

    Constraints (all required):
      - element type is text OR button
      - cy in [speaker.y2 - 50, speaker.y2 + 350]  (just below speaker)
      - |cx - speaker.cx| <= 700                   (horizontally near)
      - label is multi-word and ≥ 10 chars
    """
    # Vertical band: just-below-speaker through ~speech-bubble bottom.
    # Bubble height in real frames is ~256 px; pad slightly more to allow
    # for sub-line OCR variation but stop short of the screen's bottom
    # chrome (which would let underlying sub_menu action buttons like
    # 'Language Effect' bleed in).
    band_top = speaker.y2 - 50
    band_bot = speaker.y2 + 250
    speaker_cx = speaker.cx
    horiz_pad = 600
    out = []
    for e in elements:
        if e is speaker:
            continue
        if e.element_type not in ("text", "button"):
            continue
        if not (band_top <= e.cy <= band_bot):
            continue
        if abs(e.cx - speaker_cx) > horiz_pad:
            continue
        raw = (e.label or "").strip()
        if len(raw) < 10 or " " not in raw:
            continue
        out.append(e)
    return out


def _short_cap_text_near(elements, anchor) -> List[DetectedElement]:
    ay1, ay2 = anchor.y1, anchor.y2
    pad_y = 100
    band_top = ay1 - pad_y
    band_bot = ay2 + pad_y
    out = []
    for e in elements:
        if e is anchor:
            continue
        if e.element_type not in ("text", "button"):
            continue
        if not (band_top <= e.cy <= band_bot):
            continue
        raw = (e.label or "").strip()
        if not (2 <= len(raw) <= 20):
            continue
        if not raw[0].isalpha() or not raw[0].isupper():
            continue
        if raw.rstrip().endswith((".", "?", "!")):
            continue
        out.append(e)
    return out
