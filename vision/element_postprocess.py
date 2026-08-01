"""Semantic post-processing for OmniParser detections.

OmniParser gives us bounding boxes with one of three coarse types:
`text` / `button` / `icon`.  But on UWO screens, those tags don't tell
the bot what an element *is* in terms of gameplay role:

  - A speech bubble looks like a `button` to YOLO (rounded rectangle
    with text inside).  It isn't tappable.
  - A building name plate is a `button` with the building name — IS
    tappable.
  - A port name in the top-left is `text` — not tappable.
  - An NPC speech bubble has small font + lowercase sentence content +
    position in the 3D gameplay area — NPC chatter, ignore it.

This module wraps raw OmniParser elements with **semantic role tags**
that downstream consumers (Qwen, fingerprint registry, navigators,
caching) can filter on.

The classifiers are deterministic heuristics — no model training, no API
calls.  Each role uses a small conjunction of signals (position zone,
content pattern, bbox geometry) so false positives are rare.

Origin: 2026-05-13 discussion of why NPC bubbles cause Qwen cache misses
and why OmniParser's `[button]` tag isn't a reliable "this is tappable"
signal on the world overworld.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ── Role taxonomy ────────────────────────────────────────────────────────────

# Names are short snake_case strings; consumers should compare against the
# constants below, not against raw strings.  Each role has a clear
# operational meaning — what downstream code should do when it sees one.

# Fixed UI chrome (positions are deterministic, content is stable):
ROLE_PORT_NAME        = "port_name"          # top-left port label on port_overworld ONLY
ROLE_BUILDING_TITLE   = "building_title"      # top-left title on building / sub_menu interiors
ROLE_MODE_TAB         = "mode_tab"            # world-map tabs (Explore/Port/Route/Trade)
ROLE_TITLE_BAR        = "title_bar"           # generic top-bar title (kept for back-compat)
ROLE_BACK_ARROW       = "back_arrow"          # < back button
ROLE_HAMBURGER        = "hamburger"           # ≡ main menu
ROLE_RIGHT_PANEL_ROW  = "right_panel_row"     # vertical building list entries
ROLE_RIGHT_PANEL_TAB  = "right_panel_tab"     # 4-icon tab bar above the building list
ROLE_DATE_TIME        = "date_time"           # season / month / time / weather labels
ROLE_PHONE_OS         = "phone_os"            # bottom phone status (Wi-Fi, battery, clock)
ROLE_BUILD_INFO       = "build_info"          # bottom-right version + region string
ROLE_CHROME_ICON      = "chrome_icon"         # top-right cluster (mail, mission, settings, etc.)
ROLE_CURRENCY_LABEL   = "currency_label"      # top-right numeric counters (ducats, gems, points)

# Gameplay-area variable content (positions and presence vary):
ROLE_APPELLATION      = "appellation"         # player title at fixed position above sprite
ROLE_PLAYER_NAMEPLATE = "player_nameplate"    # player name above sprite
ROLE_BUILDING_NAMEPLATE = "building_nameplate"  # building name above entrance (in-world)
ROLE_NPC_BUBBLE       = "npc_bubble"          # NPC speech bubble — small font sentence
ROLE_PROXIMITY_BUTTON = "proximity_button"    # in-world "tap me" pill (e.g. building number)
ROLE_EVENT_BANNER     = "event_banner"        # 'Maca Boom occurred in X' ticker

# Pattern-overlay roles (composite UI signals):
ROLE_NOTIFICATION_DOT = "notification_dot"    # small red dot indicating new/pending content
ROLE_PROGRESS_BAR     = "progress_bar"        # horizontal fill bar (trade points, build progress, …)
ROLE_LOCKED_INDICATOR = "locked_indicator"    # text advertising a locked/unavailable feature

# Generic / fallback:
ROLE_BUTTON           = "button"              # OmniParser-typed button, no semantic match
ROLE_TEXT             = "text"                # OmniParser-typed text, no semantic match
ROLE_ICON             = "icon"                # OmniParser-typed icon, no semantic match

# Roles that downstream consumers should typically IGNORE when summarising
# scene content (they're either ephemeral or known game-UI noise):
NOISE_ROLES = frozenset({
    ROLE_NPC_BUBBLE, ROLE_PHONE_OS, ROLE_BUILD_INFO, ROLE_EVENT_BANNER,
})


# ── Tagged element ──────────────────────────────────────────────────────────

@dataclass
class TaggedElement:
    """Wraps an OmniParser DetectedElement with a semantic role and the
    signals that produced it.

    The raw OmniParser detection is preserved so consumers that need
    precise bboxes / OmniParser's type can still access it.  The role
    field is the *interpretation* — what role this element plays in the
    game-UI vocabulary.
    """
    raw:        object              # vision.omniparser.DetectedElement
    role:       str                 # one of the ROLE_* constants
    signals:    list[str] = field(default_factory=list)  # which classifier(s) fired

    # Convenience properties forwarding to the raw element
    @property
    def label(self) -> str:           return self.raw.label
    @property
    def cx(self) -> int:               return self.raw.cx
    @property
    def cy(self) -> int:               return self.raw.cy
    @property
    def x1(self) -> int:               return self.raw.x1
    @property
    def y1(self) -> int:               return self.raw.y1
    @property
    def x2(self) -> int:               return self.raw.x2
    @property
    def y2(self) -> int:               return self.raw.y2
    @property
    def width(self) -> int:            return self.raw.width
    @property
    def height(self) -> int:           return self.raw.height
    @property
    def omni_type(self) -> str:        return self.raw.element_type
    @property
    def is_noise(self) -> bool:        return self.role in NOISE_ROLES


# ── Geometry zones (2400 × 1080) ─────────────────────────────────────────────
#
# Empirically derived from the labelled frames we've reviewed (Bergen,
# Socotra, Port Royal, the 7 world-map captures).  Bounds are inclusive
# on both ends.  Adjust if UWO updates the UI.

# Top mode-tab band (world map mode tabs + always-on chrome along the top)
_TOP_BAND_Y                = (0, 110)

# Top-left port name zone
_PORT_NAME_X               = (0, 800)
_PORT_NAME_Y               = (0, 100)

# Top-right chrome icon cluster (Pass, mail, mission, hamburger, etc.)
_TOP_RIGHT_CHROME_X        = (1700, 2400)
_TOP_RIGHT_CHROME_Y        = (0, 100)

# Top-right currency label zone — extends further left than the icon
# cluster because long gold counts (e.g. "14,001,701,263") sit just left
# of the smaller gem / point counters.
_CURRENCY_LABEL_X          = (1500, 2400)
_CURRENCY_LABEL_Y          = (0, 100)

# Right-panel column on port_overworld (building list + tab bar + date bar)
_RIGHT_PANEL_X             = (1880, 2400)
_RIGHT_PANEL_Y             = (130, 1050)

# Right-panel tab bar (the 4 icons above the building list)
_RIGHT_PANEL_TAB_Y         = (130, 230)

# Date/season/time bar (above the building list, below the tab bar)
_DATE_BAR_Y                = (350, 460)

# Phone OS bar at the bottom (Wi-Fi, battery, clock).  Spans the full
# width — clock + Wi-Fi on the left, battery percentage further right.
_PHONE_OS_Y                = (1040, 1080)
_PHONE_OS_X                = (0, 1700)   # exclude bottom-right (build info)

# Bottom-right build/version info.  OmniParser sometimes merges this
# with a wider bbox starting higher (y ~ 1010), so the y range is more
# permissive than _PHONE_OS_Y.
_BUILD_INFO_X              = (1700, 2400)
_BUILD_INFO_Y              = (1000, 1080)

# Player appellation fixed position (small zone around the title that
# appears above the player sprite when an appellation is equipped).
# From the Bergen / Socotra captures: 'Eastern Explorer' at ~(1196, 375).
_APPELLATION_CX            = (1050, 1340)
_APPELLATION_CY            = (350, 410)

# Player nameplate sits just below the appellation, on the same x range.
_PLAYER_NAMEPLATE_CX       = (1050, 1380)
_PLAYER_NAMEPLATE_CY       = (470, 560)

# Gameplay area for 3D content (where NPCs walk, name plates float,
# bubbles appear).  Excludes the right panel and the bottom phone bar.
# Bubbles can appear anywhere in this area — including very close to the
# top edge (NPCs at the upper rim of view).
_GAMEPLAY_X                = (50, 1860)
_GAMEPLAY_Y                = (0, 1040)


# ── Content patterns ─────────────────────────────────────────────────────────

# Phone-OS bar text (always appears at the bottom of the Android display).
_PHONE_OS_PATTERN_RE = re.compile(
    r"^("
    r"\d{1,2}[:.]\d{2}"             # clock 22:05 / 22.05
    r"|\d{1,3}\.?\d*\s*%"           # battery 6.11%
    r"|.{0,3}wi-?fi.{0,3}"          # Wi-Fi (possibly with leading symbol)
    r"|@?LV\s*\d+"                  # @V 92 — phone variant of company level (false positive risk)
    r")$",
    re.IGNORECASE,
)

# Mode-tab labels on the world map
_MODE_TAB_LABELS = frozenset({"explore", "port", "route", "trade"})

# Date/season terms (subset — the world has many languages; this covers the
# English UI's common tokens).
_DATE_TERMS = frozenset({
    "spring", "summer", "autumn", "fall", "winter",
    "wet season", "dry season", "wet", "dry",
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
})

# Event-banner verbs — these distinguish "X event occurred in Y" tickers
# from regular labels even when they contain a recognisable port name.
_EVENT_VERBS = frozenset({"occurred", "ongoing", "ended", "boom"})

# Lock-state text patterns.  Conservative — distinguishes a locked
# feature ("Unavailable", "Locked", "Requires X") from informational
# level displays ("LV 1", "Company LV 92" — those are NOT locked
# indicators).  Origin: 2026-05-14, user clarified that locked rows
# represent features gated by progression (Auction, Assault, Union
# slots, Bureau Manage Market Event when not Mayor, etc.).
_LOCK_WORD_RE = re.compile(
    r"^\s*(unavailable|locked|unlock(?:ed|able)?)\s*$",
    re.IGNORECASE,
)
_REQUIRES_RE = re.compile(
    r"\brequires?\b",
    re.IGNORECASE,
)

# Words common in NPC chatter and rare in UI labels.  Used as a tiebreaker
# for "sentence-like" content.
_BUBBLE_TELL_TALES = frozenset({
    "the", "is", "are", "was", "were", "has", "have", "had",
    "this", "that", "these", "those",
    "year", "years", "day", "days",
    "trade", "company", "sea", "ship",  # game-flavour words that appear in NPC speech
})


# ── Predicates ───────────────────────────────────────────────────────────────

def _in_zone(cx: int, cy: int, x_lo: int, x_hi: int, y_lo: int, y_hi: int) -> bool:
    return x_lo <= cx <= x_hi and y_lo <= cy <= y_hi


def _looks_like_sentence(text: str) -> bool:
    """True if *text* looks like NPC chatter rather than a UI label.

    Heuristics: multi-word, contains lowercase content (UI labels in this
    game are mostly Title-Cased or numeric), AND either contains a tell-
    tale article/verb OR ends with punctuation.
    """
    if not text or len(text) < 6:
        return False
    if " " not in text:
        return False
    has_lower = any(c.islower() for c in text)
    if not has_lower:
        return False
    words = [w.lower().strip(".,;:!?") for w in text.split()]
    has_tell_tale = any(w in _BUBBLE_TELL_TALES for w in words)
    ends_with_punct = text.rstrip().endswith((".", "!", "?", ";", ":"))
    return has_tell_tale or ends_with_punct


def _looks_like_proper_noun(text: str) -> bool:
    """True if *text* looks like a single proper noun (e.g. a port name).
    Conservative — a single capitalised word, no punctuation."""
    s = text.strip()
    if not s or len(s) > 30:
        return False
    if " " in s:
        # Two-word proper nouns are possible ('Port Royal', 'Las Palmas')
        # but most UI port-name slots show a single word.  Allow up to 2.
        if len(s.split()) > 3:
            return False
    return s[0].isupper() and not any(c.isdigit() for c in s)


# ── Role classifiers ─────────────────────────────────────────────────────────
#
# Each classifier returns (role, signals) when it matches, or None when it
# doesn't.  Run in priority order — first match wins.

def _classify_phone_os(el, nav_state, fw, fh):
    if not _in_zone(el.cx, el.cy, *_PHONE_OS_X, *_PHONE_OS_Y):
        return None
    if _PHONE_OS_PATTERN_RE.match(el.label.strip()):
        return ROLE_PHONE_OS, ["pos:phone_bar", "content:phone_pattern"]
    return None


def _classify_build_info(el, nav_state, fw, fh):
    if not _in_zone(el.cx, el.cy, *_BUILD_INFO_X, *_BUILD_INFO_Y):
        return None
    # Build info strings are long alphanumeric with dots, e.g.
    # '4.0401.081.285 2604141540 Atlantic Ocean'
    s = el.label.strip()
    if len(s) > 25 and "." in s:
        return ROLE_BUILD_INFO, ["pos:build_info", "content:long_dotted"]
    return None


def _classify_event_banner(el, nav_state, fw, fh):
    # Banners appear across the top of any 3D-overworld screen.
    low = el.label.lower()
    if any(v in low for v in _EVENT_VERBS):
        if el.cy < 200:    # banner zone — top of screen
            return ROLE_EVENT_BANNER, ["content:event_verb", "pos:top"]
    return None


def _classify_currency_label(el, nav_state, fw, fh):
    """Top-right numeric counters — ducats, gems, points, etc.
    Pattern: text element in the top-right zone with purely-numeric
    content (optionally comma-separated)."""
    if not _in_zone(el.cx, el.cy, *_CURRENCY_LABEL_X, *_CURRENCY_LABEL_Y):
        return None
    if el.element_type != "text":
        return None
    s = el.label.strip()
    if not s:
        return None
    # Strip commas / periods / percent suffix; what's left should be
    # purely digits for a clean numeric currency label.
    bare = s.replace(",", "").replace(".", "").rstrip("%")
    if bare.isdigit() and len(bare) >= 2:
        return ROLE_CURRENCY_LABEL, ["pos:top_right", "content:numeric"]
    return None


def _classify_chrome_top_right(el, nav_state, fw, fh):
    # The top-right cluster of UI icons/buttons (Pass / mail / mission /
    # settings / hamburger).  Applies to every screen.
    if not _in_zone(el.cx, el.cy, *_TOP_RIGHT_CHROME_X, *_TOP_RIGHT_CHROME_Y):
        return None
    if el.element_type == "icon":
        return ROLE_CHROME_ICON, ["pos:top_right", "omni:icon"]
    if el.element_type == "button":
        # Short labels like "Pass" — chrome buttons.
        if el.label and len(el.label) <= 12:
            return ROLE_CHROME_ICON, ["pos:top_right", "omni:button",
                                       "content:short_label"]
    return None


def _classify_port_name(el, nav_state, fw, fh):
    """Top-left title text.  The role we emit depends on nav_state:
      - port_overworld → `port_name` (Bergen, Socotra, London, …)
      - building / sub_menu → `building_title` (Harbor, Inn, Union, …)
      - None (unknown nav_state) → `port_name`, retained for back-compat
    The screen position is the same in both cases; only the semantic
    role differs.  Naming both `port_name` (the pre-2026-05-14 behaviour)
    was misleading: a building's title is not a port name."""
    if nav_state not in ("port_overworld", "building", "sub_menu", None):
        return None
    if not _in_zone(el.cx, el.cy, *_PORT_NAME_X, *_PORT_NAME_Y):
        return None
    if el.element_type == "icon":
        return None
    if not _looks_like_proper_noun(el.label):
        return None
    if nav_state in ("building", "sub_menu"):
        return ROLE_BUILDING_TITLE, ["pos:top_left", "content:proper_noun",
                                      f"nav:{nav_state}"]
    return ROLE_PORT_NAME, ["pos:top_left", "content:proper_noun"]


def _classify_mode_tab(el, nav_state, fw, fh):
    if nav_state != "world_map":
        return None
    if el.cy > _TOP_BAND_Y[1]:
        return None
    if el.label.lower().strip() in _MODE_TAB_LABELS:
        return ROLE_MODE_TAB, ["pos:top_band", "content:mode_label"]
    return None


def _classify_date_time(el, nav_state, fw, fh):
    # Either the right-panel date/season bar OR the top-bar date in
    # other screens.  Match by position + content.
    in_right_date_bar = _in_zone(el.cx, el.cy,
                                   *_RIGHT_PANEL_X, *_DATE_BAR_Y)
    s = el.label.strip()
    if not s:
        return None
    s_low = s.lower()
    is_date_token = (
        s_low in _DATE_TERMS
        or re.match(r"^\d{1,2}[.:]\d{2}$", s)   # 09.48 or 15:20
    )
    if in_right_date_bar and is_date_token:
        return ROLE_DATE_TIME, ["pos:date_bar", "content:date_token"]
    return None


def _classify_right_panel(el, nav_state, fw, fh):
    if nav_state != "port_overworld":
        return None
    if not _in_zone(el.cx, el.cy, *_RIGHT_PANEL_X, *_RIGHT_PANEL_Y):
        return None
    # Tab bar (top of the right column)
    if _RIGHT_PANEL_TAB_Y[0] <= el.cy <= _RIGHT_PANEL_TAB_Y[1]:
        return ROLE_RIGHT_PANEL_TAB, ["pos:right_tab_bar"]
    # Building list row
    if el.element_type in ("button", "text"):
        return ROLE_RIGHT_PANEL_ROW, ["pos:right_panel", f"omni:{el.element_type}"]
    return None


def _classify_appellation(el, nav_state, fw, fh):
    if nav_state not in ("port_overworld", "sea", "sea_cinematic", None):
        return None
    if not _in_zone(el.cx, el.cy, *_APPELLATION_CX, *_APPELLATION_CY):
        return None
    if el.element_type == "icon":
        return None
    # Appellations are short title-cased strings: 'Eastern Explorer',
    # 'Senior Captain', 'Veteran Trader' etc.
    s = el.label.strip()
    if 4 <= len(s) <= 40 and any(c.islower() for c in s):
        return ROLE_APPELLATION, ["pos:appellation_zone"]
    return None


def _classify_player_nameplate(el, nav_state, fw, fh):
    if nav_state not in ("port_overworld", "sea", "sea_cinematic", None):
        return None
    if not _in_zone(el.cx, el.cy, *_PLAYER_NAMEPLATE_CX, *_PLAYER_NAMEPLATE_CY):
        return None
    if el.element_type == "icon":
        return None
    # Nameplates are typically a single token (player name).  No spaces.
    s = el.label.strip()
    if s and " " not in s and len(s) <= 16 and any(c.isalpha() for c in s):
        return ROLE_PLAYER_NAMEPLATE, ["pos:nameplate_zone", "content:single_token"]
    return None


def _classify_npc_bubble(el, nav_state, fw, fh):
    """Three-signal conjunction (origin: 2026-05-13 Socotra frame).

    A "bubble" is a small-font, sentence-like text element in the 3D
    gameplay area.  UI labels never satisfy all three.

    Multi-line bubbles need per-line height estimation rather than
    bbox-height/2 (which assumes 2 lines).  We estimate line count by
    dividing the label length by an approximate chars-per-line based
    on bbox width and a typical bubble char-width of ~15 px.
    """
    if nav_state not in ("port_overworld", "sea", "sea_cinematic", None):
        return None
    if el.element_type == "icon":
        return None
    in_gameplay = _in_zone(el.cx, el.cy, *_GAMEPLAY_X, *_GAMEPLAY_Y)
    if not in_gameplay:
        return None

    label = el.label or ""

    # Content signal — sentence-like text.  Check first; cheap and the
    # strongest discriminator.  No bubble has < 6 chars or 1 word.
    if not _looks_like_sentence(label):
        return None

    # Font-size signal.  Estimate line count from bbox width and label
    # length, then derive per-line height.  Bubbles use ~15 px per char.
    # Single-line: line_count = 1, line_height = bbox_height (~37 px).
    # Multi-line: line_count ≥ 2, line_height much less than bbox_height.
    chars_per_line = max(1, int(el.width / 14))
    line_count = max(1, (len(label) + chars_per_line - 1) // chars_per_line)
    line_height = el.height / max(1, line_count)
    small_font = line_height < 50   # generous upper bound; bubble line ≈ 30-42 px

    if not small_font:
        return None

    signals = ["pos:gameplay", "content:sentence",
               f"font:line_h≈{line_height:.0f}px"]
    if line_count > 1:
        signals.append(f"multi_line:{line_count}")
    return ROLE_NPC_BUBBLE, signals


def _classify_building_nameplate(el, nav_state, fw, fh):
    """Building name plate floating above an entrance.  Distinct from
    NPC bubble: large font, single building-word label."""
    if nav_state != "port_overworld":
        return None
    if not _in_zone(el.cx, el.cy, *_GAMEPLAY_X, *_GAMEPLAY_Y):
        return None
    s = el.label.strip()
    # Single building-name word.  Heights observed: 65-115 px depending
    # on whether the name plate is collapsed or expanded.
    if " " in s and len(s.split()) > 3:
        return None
    if el.height < 50:
        return None
    if not _looks_like_proper_noun(s):
        return None
    return ROLE_BUILDING_NAMEPLATE, ["pos:gameplay", "font:large",
                                        "content:proper_noun"]


def _classify_notification_dot(el, nav_state, fw, fh):
    """Small red dot icon overlaid on a sub-menu / list row indicating
    new or pending content.  Distinguished from chrome icons by SIZE:
    notification dots are ~10-30 px square, chrome icons are ~60-100 px.

    The exact icon type isn't reliably named by OmniParser's Florence-2
    caption model — it just says `'icon'` — so we go by geometry.
    Earlier classifiers (chrome_top_right, currency_label) already
    claim icons in their respective zones, so by the time this runs
    only "unclassified small icons" remain."""
    if el.element_type != "icon":
        return None
    if el.width > 35 or el.height > 35:
        return None
    if el.width < 8 or el.height < 8:
        return None    # too small — likely noise / decorative pixel
    return ROLE_NOTIFICATION_DOT, [f"size:{el.width}x{el.height}",
                                    "small_overlay"]


def _classify_progress_bar(el, nav_state, fw, fh):
    """Horizontal fill bar — wide aspect ratio, short height.
    Examples: trade-points progress (357/1,000), Investment Point Goal
    (0/500), build-job progress in Shipyard.

    Detection rule: `[icon]` or `[button]` element with width ≥ 100,
    height ≤ 30, aspect ratio ≥ 4:1.  Text elements never qualify.
    False positives on visual dividers are acceptable — downstream
    consumers treat progress_bar as informational and don't act on it."""
    if el.element_type == "text":
        return None
    if el.height <= 0 or el.height > 30:
        return None
    if el.width < 100:
        return None
    aspect = el.width / el.height
    if aspect < 4:
        return None
    return ROLE_PROGRESS_BAR, [f"aspect:{aspect:.1f}",
                                f"size:{el.width}x{el.height}"]


def _classify_locked_indicator(el, nav_state, fw, fh):
    """Text advertising a locked / unavailable feature.

    Matches: exactly `"Unavailable"` / `"Locked"` / `"Unlock"` (label is
    just the word), OR any text containing `"Requires"` / `"Require"`.

    Deliberately does NOT match `"Company LV N"` or bare `"LV N"`,
    even though those CAN appear as unlock conditions — they also
    appear as the PLAYER'S current level in chrome, and the difference
    requires spatial context this single-element classifier can't see.
    Downstream consumers can detect locked rows by clustering a
    `locked_indicator` with neighbouring `button` / `submenu_item`
    elements."""
    if el.element_type == "icon":
        return None
    label = (el.label or "").strip()
    if not label:
        return None
    if _LOCK_WORD_RE.match(label):
        return ROLE_LOCKED_INDICATOR, ["content:lock_word"]
    if _REQUIRES_RE.search(label):
        return ROLE_LOCKED_INDICATOR, ["content:requires"]
    return None


# Fallback to OmniParser's coarse type.
def _classify_fallback(el, nav_state, fw, fh):
    omni_to_role = {
        "button": ROLE_BUTTON,
        "text":   ROLE_TEXT,
        "icon":   ROLE_ICON,
    }
    return omni_to_role.get(el.element_type, ROLE_TEXT), ["fallback:omni_type"]


# Classifiers run in priority order; first match wins.
_CLASSIFIERS = [
    _classify_phone_os,
    _classify_build_info,
    _classify_event_banner,
    _classify_locked_indicator,    # text-based; runs early to claim "Unavailable" etc.
    _classify_currency_label,
    _classify_chrome_top_right,
    _classify_notification_dot,    # small icons; runs after chrome zones are claimed
    _classify_port_name,
    _classify_mode_tab,
    _classify_date_time,
    _classify_right_panel,
    _classify_appellation,
    _classify_player_nameplate,
    _classify_npc_bubble,
    _classify_building_nameplate,
    _classify_progress_bar,        # wide-aspect rectangles; near the end before fallback
    # Fallback last
    _classify_fallback,
]


# ── Public entry point ───────────────────────────────────────────────────────

def tag_elements(
    elements: list,
    nav_state: Optional[str] = None,
    frame_dims: tuple[int, int] = (2400, 1080),
) -> list[TaggedElement]:
    """Wrap each OmniParser detection with a semantic role tag.

    Args:
        elements:    list of vision.omniparser.DetectedElement
        nav_state:   coarse navigation state, used by some classifiers
                     ('port_overworld', 'world_map', 'sea', 'building',
                     'sub_menu', or None).  When None, classifiers that
                     gate on nav_state behave as if the state is "any
                     overworld" — more permissive.
        frame_dims:  (width, height) of the frame.  Default is the
                     2400×1080 we observe on this device.

    Returns:
        list of TaggedElement in the same order as *elements*.
    """
    fw, fh = frame_dims
    tagged: list[TaggedElement] = []
    for el in elements:
        for clf in _CLASSIFIERS:
            result = clf(el, nav_state, fw, fh)
            if result is not None:
                role, signals = result
                tagged.append(TaggedElement(raw=el, role=role, signals=signals))
                break
    return tagged


def group_by_role(tagged: list[TaggedElement]) -> dict[str, list[TaggedElement]]:
    """Bucket tagged elements by role.  Convenient for downstream
    consumers that want to ask 'give me all building name plates' etc."""
    out: dict[str, list[TaggedElement]] = {}
    for t in tagged:
        out.setdefault(t.role, []).append(t)
    return out


def filter_non_noise(tagged: list[TaggedElement]) -> list[TaggedElement]:
    """Drop elements whose role is in NOISE_ROLES.  Used to clean Qwen
    prompt input — NPC bubbles, phone OS, event banners are all
    irrelevant for scene-state reasoning."""
    return [t for t in tagged if t.role not in NOISE_ROLES]
