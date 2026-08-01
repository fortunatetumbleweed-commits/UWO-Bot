# vision/scene_model.py
#
# Deterministic Scene Model — explicit structured picture of the current
# screen, populated by small region detectors that each own one rectangle.
# See docs/scene_model_design.md for the full rationale.
#
# This module defines:
#   - The SceneModel dataclass + its region sub-models.
#   - The detect_scene() classifier that combines region detector outputs
#     into a finished SceneModel.
#
# The actual region detectors live in vision/region_detectors/.  Slice 1
# ships with top_left and bottom_chrome detectors; subsequent slices add
# left_menu, action_buttons, right_panel, and so on.
#
# Slice 1 ships ADDITIVE — the SceneModel is produced alongside the
# existing classify_screen output and logged for comparison.  No
# consumers (perceive, navigate_to_building, _assert_at_port, …) read
# from SceneModel yet.  Migration begins at slice 5.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal, Optional, Tuple

from loguru import logger

from vision.omniparser import DetectedElement


# ── Field-level types ─────────────────────────────────────────────────────


SceneFamily = Literal["overworld", "chromed", "world_map", "unknown"]
Confidence  = Literal["high", "medium", "low"]
BigIconKind = Literal["lighthouse", "ship", "back_arrow", "unknown"]


@dataclass(frozen=True)
class TitleField:
    """The title text shown at the top-left of any scene.

    `raw_ocr` is the string OmniParser/EasyOCR returned BEFORE any
    correction (fuzzy match against known ports/waters).  `text` is the
    post-correction canonical form when available; falls back to
    `raw_ocr` when no correction applied.
    """
    text:     str
    raw_ocr:  str
    bbox:     Tuple[int, int, int, int]   # x1, y1, x2, y2


@dataclass(frozen=True)
class BottomChromeRegion:
    """The bottom strip present on every UWO screen: wifi/battery/UID/server.

    Slice 1 only confirms presence + reads the server name when visible.
    Per-element parsing (battery percent, UID digits) is deferred — the
    detector exists primarily to confirm "this is a real game frame,
    not a blank transition".
    """
    present:      bool
    server_name:  Optional[str] = None      # "Atlantic Ocean", etc.


@dataclass(frozen=True)
class TopLeftRegion:
    """The top-left region of any UWO scene.

    OVERWORLD LAYOUT (port_overworld + sea):

    The background is an IRREGULAR L-SHAPE — TALL on the left (covers
    the full height of the big icon) and SHORTER on the right (only
    covers Row 1 height).  Row 2 sits in the open space below Row 1
    and right of the big icon, OUTSIDE the background:

      ╔══════════════════════════════════════════════╗  ← bg top edge
      ║                                              ║
      ║   ┌──────┐   flag · port-name  ◄─── ROW 1    ║
      ║   │      │                                   ║
      ║   │  Big │   ──────────────────────────────  ╝  ← bg right edge
      ║   │ Icon │                                       drops off here
      ║   │      │     shield · effect-icons  ◄── ROW 2   (Row 2 is OUTSIDE
      ║   │      │                                        the background,
      ║   └──────┘                                        right of big icon)
      ╚══════════════╝                                ← bg left edge ends
                       (extends down to bottom of big icon)

    Anatomy:
      - **Background (irregular L-shape)**: frames the big icon
        (left, full height) PLUS Row 1 (top-right portion only).
        Visually defines a single tappable area; tapping ANYWHERE on
        the big icon or Row 1 opens the city-info overlay (port) or
        the sea-region info dialog (sea).  Row 2 is NOT inside this
        tappable area.
      - **Big icon**: lighthouse (port) or ship (sea).  Positioned at
        the left of the background.  Its height equals the height of
        Row 1 + Row 2 combined; it visually spans both rows but sits
        inside the tall LEFT portion of the L-shaped background.
      - **Row 1** (inside the background's right portion): national
        flag + port name (port), or just the waters name (sea).
        Tappable as part of the same compound as the big icon —
        both are inside the same background and trigger the same
        info overlay.
      - **Row 2** (BELOW Row 1, RIGHT of the big icon, OUTSIDE the
        background — no background of its own; sits directly on the
        3D world):
          - **Shield icon** at the left of Row 2.  Individually
            tappable; toggles between active (green) and inactive
            (gray).
          - **Effect-icon group** to the right of the shield.  All
            effect icons together form ONE tappable component;
            tapping opens the effects dropdown listing active buffs.
            Individual icons within the group are NOT separately
            tappable.

      The "no background" property of Row 2 is operationally important:
      because Row 2 elements sit directly on top of the 3D world content
      (NPCs, ships, weather effects, terrain), OmniParser can confuse
      Row 2 elements with the background and either miss them entirely
      or merge them with unrelated nearby elements.  This is the most
      fragile detection surface in the top-left region.

    CHROMED LAYOUT (building, sub_menu, world_map, port_map):

      ┌──────────────────────────────────────────┐
      │ ← title              ?                   │ ← tight single-row chrome.
      └──────────────────────────────────────────┘   No Row 2 here.

      - Small back-arrow (~30-50 px) on the left.
      - Title text in the middle (building / sub-menu / "World Map").
      - '?' tutorial icon to the right of the title.

    Detection notes:
      - The detector populates the fields appropriate to whichever
        layout it identifies.  Fields irrelevant to a family stay at
        their defaults (None / False).
      - Slice 1 + 3.5 populated big_icon, flag, title for overworld
        and back_arrow + tutorial_q for chromed.
      - Slice 6+ TODO: populate shield_* and effect_icons_* for
        overworld Row 2 once dedicated detectors exist.
      - flag_nation is reserved for a future template-match slice that
        identifies which nation's flag is shown (England, Spain,
        Portugal, Netherlands, Ottoman).
    """
    family:        SceneFamily
    title:         Optional[TitleField] = None

    # ── Overworld Row 1 (inside the arrow-shaped background) ────────────
    big_icon:      Optional[BigIconKind] = None  # lighthouse | ship
    big_icon_bbox: Optional[Tuple[int, int, int, int]] = None
    flag_present:  bool = False
    flag_bbox:     Optional[Tuple[int, int, int, int]] = None
    flag_nation:   Optional[str] = None       # template-match — future slice

    # ── Overworld Row 2 (OUTSIDE the background) ────────────────────────
    # Row 2 has no visual background — its elements sit directly on the
    # 3D world content.  Detection is the most fragile here because
    # OmniParser can confuse Row 2 icons with world content behind them.
    # Future slices will add dedicated Row 2 detectors.
    shield_present:        bool = False
    shield_bbox:           Optional[Tuple[int, int, int, int]] = None
    shield_active:         Optional[bool] = None       # True=green, False=gray
    shield_count:          Optional[int] = None        # small number lower-right of shield
    effect_icons_present:  bool = False
    effect_icons_bbox:     Optional[Tuple[int, int, int, int]] = None
    effect_icons_count:    Optional[int] = None        # number of icons in the group

    # ── Chromed layout — back arrow + title + tutorial ? ────────────────
    back_arrow:    bool = False
    tutorial_q:    bool = False               # '?' next to title


# ── Region placeholders (populated by later slices) ───────────────────────
#
# Defining these as empty frozen dataclasses NOW so the SceneModel schema
# is stable; the actual fields land in their respective slices.


@dataclass(frozen=True)
class OverworldWorldRegion:
    """3D world content: NPCs, weather, characters.  Slice 6."""
    pass


@dataclass(frozen=True)
class Row2BuffsRegion:
    """Shield + dynamic buff icons.  Slice 6."""
    shield_present: bool = False
    shield_count:   Optional[int] = None


@dataclass(frozen=True)
class LevelProgressRegion:
    """LV + progress strip bottom-centre on overworld scenes.  Slice 6."""
    level_text:      Optional[str] = None      # "LV 92"
    progress_text:   Optional[str] = None      # "8.46%"


@dataclass(frozen=True)
class MainMenuOverlay:
    """Mode flag — main menu opened on top of an overworld scene.

    Detected by the presence of the left-side panel with currencies +
    fleet supplies + load capacity.  Slice 6.
    """
    open: bool = False


@dataclass(frozen=True)
class Panel:
    """One panel widget — title bar + content body.

    Three variants share this shape (see vision/region_detectors/panels.py):

      - Overworld tabbed right panel:
          title=None, tabs=[<tab labels>], active_tab=<name>, items=<list>
      - Chromed right context panel (Cart, Hire, Sales List, City Info):
          title=<name>, is_overlay=<X icon present>, items=<list>
      - Chromed center panel (sub-menu content area):
          title=<optional>, is_overlay=False, items=<list>

    `is_overlay` is True when an X close icon is detected in the panel's
    title-bar area.  Per the design discussion, X-having panels are
    optional overlays that can be dismissed (e.g. City Info appears
    only after a port is selected on World Map).  Persistent panels
    (Cart, Hire) don't have X.  Center panels never have X.
    """
    title:        Optional[str] = None
    title_bbox:   Optional[tuple] = None
    bbox:         Optional[tuple] = None
    is_overlay:   bool = False
    tabs:         List[str] = field(default_factory=list)
    active_tab:   Optional[str] = None
    items:        List[dict] = field(default_factory=list)

    def labels(self) -> List[str]:
        """Convenience: labels of items in the panel body."""
        return [it.get("label", "") for it in self.items]

    def find(self, label: str) -> Optional[dict]:
        """Case-insensitive lookup by label."""
        target = label.strip().lower()
        for it in self.items:
            if it.get("label", "").strip().lower() == target:
                return it
        return None


# Legacy aliases kept for the placeholder slots in SceneModel.
# Each scene-position panel uses the same Panel shape; only its
# semantics differ.
OverworldRightPanel = Panel


@dataclass(frozen=True)
class TopRightChromeRegion:
    """Top-right region on chromed scenes: currency strip + home/hamburger.

    Slice 2 — required to disambiguate building/sub_menu from main_menu.
    """
    currency_strip_present: bool = False
    home_button_present:    bool = False
    hamburger_present:      bool = False


@dataclass(frozen=True)
class LeftMenuRegion:
    """Vertical menu items on the left of chromed scenes.

    Each item is a dict: {label, bbox, cx, cy, is_locked, is_selected}.
    Bbox is (x1, y1, x2, y2) absolute pixel coords.

    Slice 2 — required for purchase/sell/recruit/supply flows.
    """
    items: List[dict] = field(default_factory=list)

    def labels(self) -> List[str]:
        """Convenience: list of just the label strings."""
        return [it["label"] for it in self.items]

    def unlocked_labels(self) -> List[str]:
        """Convenience: labels of items that are not locked."""
        return [it["label"] for it in self.items if not it.get("is_locked")]

    def find(self, label: str) -> Optional[dict]:
        """Case-insensitive lookup by exact label."""
        target = label.strip().lower()
        for it in self.items:
            if it["label"].strip().lower() == target:
                return it
        return None


# Center panel and right context panel are the same shape as Panel.
CenterPanelRegion = Panel
RightContextPanel = Panel


@dataclass(frozen=True)
class ActionButtonsRegion:
    """Bottom-row action buttons: Purchase, Sell, Confirm, Set Sail.

    Each button is a dict: {label, bbox, cx, cy, is_positive, is_enabled}.
    Buttons are sorted left-to-right.  The rightmost button is marked
    is_positive=True — it's the primary commit action for the flow.

    Slice 3 — the commit step of every flow.
    """
    buttons: List[dict] = field(default_factory=list)

    def labels(self) -> List[str]:
        """Convenience: list of just the label strings."""
        return [b["label"] for b in self.buttons]

    def positive(self) -> Optional[dict]:
        """Return the primary commit button (rightmost / is_positive=True),
        or None if no buttons detected."""
        for b in self.buttons:
            if b.get("is_positive"):
                return b
        return None

    def find(self, label: str) -> Optional[dict]:
        """Case-insensitive lookup by exact label."""
        target = label.strip().lower()
        for b in self.buttons:
            if b["label"].strip().lower() == target:
                return b
        return None


@dataclass(frozen=True)
class CompositeButton:
    """One composite gold-button widget — icon + cost + action verb fused
    into a single tappable.

    UWO uses this pattern extensively for any flow that commits a payment
    (Purchase / Sell / Recruit / Donate / Repair / Set Sail with cost).
    See vision/region_detectors/composite_buttons.py for the recognition
    pipeline.

    Fields:
      action_label:  the commit verb ("Purchase", "Sell", "Recruit", …)
      cost_value:    integer cost, or 0 for 'Free', or None when no cost
                     was detected (plain action button without payment).
      cost_currency: TODO — pixel-color sampling on the icon's bbox to
                     map hue to {gold, blue_gem, red_gem, stamina}.  Not
                     implemented in slice 3.5; left as None.
      icon_present:  True when an icon element was detected adjacent to
                     the cost (helpful for downstream verification).
      bbox:          union of verb + cost + icon bboxes — tap target.
      is_positive:   True for the rightmost-bottommost composite (the
                     primary commit when multiple composites coexist).
    """
    action_label:  str
    cost_value:    Optional[int]
    cost_currency: Optional[str]
    icon_present:  bool
    bbox:          tuple
    is_positive:   bool


@dataclass(frozen=True)
class CompositeButtonsRegion:
    """Container for every composite-button widget detected on the frame.

    Buttons are sorted in reading order (top-to-bottom then left-to-right)
    so consumers can iterate predictably.  Use `positive()` to get the
    primary commit composite when multiple coexist.
    """
    buttons: List[CompositeButton] = field(default_factory=list)

    def labels(self) -> List[str]:
        """Convenience: list of just the action labels."""
        return [b.action_label for b in self.buttons]

    def positive(self) -> Optional[CompositeButton]:
        """Return the primary commit composite, or None if no buttons."""
        for b in self.buttons:
            if b.is_positive:
                return b
        return None

    def find(self, action_label: str) -> Optional[CompositeButton]:
        """Case-insensitive lookup by action label."""
        target = action_label.strip().lower()
        for b in self.buttons:
            if b.action_label.strip().lower() == target:
                return b
        return None


@dataclass(frozen=True)
class WorldMapRegion:
    """World map specific UI.  Slice 4."""
    pass


# ── The SceneModel ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SceneModel:
    """Explicit structured picture of the current screen.

    Built by detect_scene() from OmniParser elements.  Each region
    detector populates its own slot; family-specific slots stay None
    when not applicable.  Consumers read from SceneModel to make
    navigation decisions (after slice 5 migration).
    """

    scene_family: SceneFamily
    scene_kind:   str                   # e.g. "port_overworld", "building:market"
    confidence:   Confidence

    # Provenance — what fired and what (if anything) disagreed
    signals_matched:   List[str] = field(default_factory=list)
    signals_disagreed: List[str] = field(default_factory=list)

    # Universal regions (populated when present)
    bottom_chrome: Optional[BottomChromeRegion] = None
    top_left:      Optional[TopLeftRegion]      = None
    obstructions:  List[dict]                   = field(default_factory=list)

    # Overworld-only
    overworld_world:   Optional[OverworldWorldRegion] = None
    row_2_buffs:       Optional[Row2BuffsRegion]      = None
    level_progress:    Optional[LevelProgressRegion]  = None
    main_menu_overlay: Optional[MainMenuOverlay]      = None
    overworld_panel:   Optional[OverworldRightPanel]  = None

    # Chromed-only
    top_right_chrome:    Optional[TopRightChromeRegion]    = None
    left_menu:           Optional[LeftMenuRegion]          = None
    center_panel:        Optional[CenterPanelRegion]       = None
    right_panel:         Optional[RightContextPanel]       = None
    action_buttons:      Optional[ActionButtonsRegion]     = None
    composite_buttons:   Optional[CompositeButtonsRegion]  = None

    # World-map-only
    world_map: Optional[WorldMapRegion] = None

    # Overlay (popup / dialog / building NPC / main_menu / city_info etc.)
    # Independent of family — overlays sit on top of a base scene.
    # When .overlay is set, scene_kind reflects the BASE scene.
    #
    # Phase 2 (2026-05-18): typed detectors are now the canonical
    # source.  Detection priority:
    #   1. DialogModel       (vision.region_detectors.dialog)
    #   2. BuildingNpcOverlay (vision.region_detectors.building_npc_overlay)
    #   3. keyword Overlay   (vision.region_detectors.overlay) — kept
    #      for main_menu detection and any future keyword variants
    # Whichever fires first wins.  Consumers use isinstance() to
    # branch on the concrete type, or read .dismiss_action() / .kind
    # from a shared protocol.
    overlay: Optional[object] = None

    def summary(self) -> str:
        """One-line human summary for logs."""
        parts = [f"family={self.scene_family}", f"kind={self.scene_kind}",
                 f"conf={self.confidence}"]
        if self.signals_matched:
            parts.append(f"matched={','.join(self.signals_matched)}")
        if self.signals_disagreed:
            parts.append(f"disagreed={','.join(self.signals_disagreed)}")
        return "[scene_model] " + " ".join(parts)

    # ── Consumer-facing helpers ───────────────────────────────────────────

    def is_at_port_overworld(self, port_name: Optional[str] = None) -> bool:
        """True when the SceneModel represents being at port_overworld.

        When *port_name* is provided, ALSO checks that the detected top-
        left title matches that port (case-insensitive, exact match
        after fuzzy correction — which already happened in the
        top_left detector).  When *port_name* is None, returns True for
        any port_overworld regardless of which port.

        This is the slice-5 replacement for the ad-hoc OCR + KB-known-
        port-name comparison in _assert_at_port.  Going through the
        SceneModel ensures we also reject false-positive overworld
        confirmations on building/sub_menu/world_map frames where the
        port name might still be readable somewhere on screen.
        """
        if self.scene_kind != "port_overworld":
            return False
        if self.confidence == "low":
            return False
        if port_name is None:
            return True
        if self.top_left is None or self.top_left.title is None:
            return False
        return (
            self.top_left.title.text.strip().lower()
            == port_name.strip().lower()
        )

    def is_at_sea(self) -> bool:
        """True when the SceneModel represents being on the open sea."""
        return self.scene_kind == "sea" and self.confidence != "low"

    def is_at_world_map(self) -> bool:
        """True when the SceneModel represents the world map screen."""
        return self.scene_kind == "world_map" and self.confidence != "low"

    def is_inside_building(self) -> bool:
        """True when the SceneModel represents being inside a building
        (scene_kind like 'building:harbor', 'building:inn', etc.)."""
        return (
            self.scene_family == "chromed"
            and self.scene_kind.startswith("building:")
            and self.confidence != "low"
        )

    def is_inside_sub_menu(self) -> bool:
        """True when the SceneModel represents being in a sub-menu
        (scene_kind like 'sub_menu:purchase', 'sub_menu:recruit crew')."""
        return (
            self.scene_family == "chromed"
            and self.scene_kind.startswith("sub_menu:")
            and self.confidence != "low"
        )


# ── Scene classifier ──────────────────────────────────────────────────────


# Known building/sub-menu titles.  Imported from the existing classifier
# so we don't fork the canonical list — slice 1 piggybacks on what's
# already there.  Later slices may move this into scene_model.py.
from vision.screen_classifier import (
    KNOWN_BUILDING_TITLES,
    KNOWN_SUB_MENU_TITLES,
)


def detect_scene(
    frame_width:  int,
    frame_height: int,
    elements:     List[DetectedElement],
) -> SceneModel:
    """Produce a SceneModel from OmniParser elements.

    This is the Stage-2 deterministic classifier.  It composes region
    facts from the detectors below; it doesn't do classification work
    itself.  Returns a SceneModel; never raises; produces
    `scene_kind="unknown"` with `confidence="low"` when nothing matches.

    TODO(slice 7) — Stage-1 family classifier.
    A standalone single-frame call to this function is fragile when
    OmniParser misses the big icon (~32% of overworld frames per
    the audit in /tmp/big_icon_audit_fast.py).  The plan is to gate
    Stage 2 with a Stage-1 Moondream binary cascade and aggregate the
    answers temporally before deciding the family.  See
    docs/temporal_scene_classifier.md.

    TODO(slice 7) — port_arrival sub-states.
    `port_loading` (Info Card), the docking animation, and
    `port_arrival_overlay` (City Overlay) are three phases of one
    event.  detect_scene currently collapses them into one generic
    `loading` family member.  Splitting them needs FSM edges first
    (see memory/knowledge/fsm/states.json).
    """
    from vision.region_detectors.bottom_chrome import detect_bottom_chrome
    from vision.region_detectors.top_left import detect_top_left
    from vision.region_detectors.left_menu import detect_left_menu
    from vision.region_detectors.action_buttons import detect_action_buttons
    from vision.region_detectors.composite_buttons import detect_composite_buttons
    from vision.region_detectors.dialog import detect_dialog
    from vision.region_detectors.overlay import detect_overlay, elements_outside
    from vision.region_detectors.building_npc_overlay import detect_building_npc_overlay
    from vision.region_detectors.panels import (
        detect_overworld_panel,
        detect_right_panel,
        detect_center_panel,
    )

    # Detect the overlay before the base scene.  When an overlay is
    # present, base-scene detectors run on the elements OUTSIDE the
    # overlay bbox so they recover the underlying scene instead of
    # being dominated by the popup's content.
    #
    # Phase 2 priority — typed detectors win over the keyword
    # fallback, structurally-specific ones go first:
    #   1. DialogModel (bounded card)
    #   2. BuildingNpcOverlay (NPC speech)
    #   3. keyword Overlay (main_menu and any future variants)
    overlay = (
        detect_dialog(elements, frame_width, frame_height)
        or detect_building_npc_overlay(elements, frame_width, frame_height)
        or detect_overlay(elements, frame_width, frame_height)
    )
    overlay_bbox = getattr(overlay, "bbox", None) if overlay else None
    base_elements = (
        elements_outside(elements, overlay_bbox) if overlay_bbox else list(elements)
    )

    bottom = detect_bottom_chrome(base_elements, frame_width, frame_height)
    top    = detect_top_left(base_elements, frame_width, frame_height)

    family, scene_kind, confidence, matched, disagreed = _classify(top)

    # Chromed scenes have a left vertical menu, a bottom action-button row,
    # and often composite gold-buttons (icon + cost + action verb).
    # World-map screens also use composite buttons for "Go to City".
    # Overworld scenes generally have none of these.
    left_menu = None
    action_buttons = None
    composite_buttons = None
    overworld_panel = None
    right_panel = None
    center_panel = None

    if family in ("chromed", "world_map"):
        composite_buttons = detect_composite_buttons(
            elements, frame_width, frame_height,
        )
        if composite_buttons is not None and composite_buttons.buttons:
            matched.append(f"composite_buttons={len(composite_buttons.buttons)}")

    if family == "chromed":
        left_menu = detect_left_menu(elements, frame_width, frame_height)
        if left_menu is not None and left_menu.items:
            matched.append(f"left_menu_items={len(left_menu.items)}")
        action_buttons = detect_action_buttons(
            elements, frame_width, frame_height,
        )
        if action_buttons is not None and action_buttons.buttons:
            matched.append(f"action_buttons={len(action_buttons.buttons)}")
        right_panel = detect_right_panel(elements, frame_width, frame_height)
        if right_panel is not None and (right_panel.title or right_panel.items):
            matched.append("right_panel")
        center_panel = detect_center_panel(elements, frame_width, frame_height)
        if center_panel is not None and (center_panel.title or center_panel.items):
            matched.append("center_panel")
    elif family == "world_map":
        right_panel = detect_right_panel(elements, frame_width, frame_height)
        if right_panel is not None and right_panel.title:
            matched.append("right_panel")
    elif family == "overworld":
        overworld_panel = detect_overworld_panel(
            elements, frame_width, frame_height,
        )
        if overworld_panel is not None and overworld_panel.items:
            matched.append(f"overworld_panel_items={len(overworld_panel.items)}")

    if overlay is not None:
        # `.kind` can be a property/attribute (keyword Overlay) OR a
        # method (DialogModel); call it lazily if callable.
        k = getattr(overlay, "kind", None)
        kind_str = (
            k()
            if callable(k)
            else (k if isinstance(k, str)
                  else type(overlay).__name__.lower())
        )
        matched.append(f"overlay={kind_str}")

    return SceneModel(
        scene_family=family,
        scene_kind=scene_kind,
        confidence=confidence,
        signals_matched=matched,
        signals_disagreed=disagreed,
        bottom_chrome=bottom,
        top_left=top,
        left_menu=left_menu,
        action_buttons=action_buttons,
        composite_buttons=composite_buttons,
        overworld_panel=overworld_panel,
        right_panel=right_panel,
        center_panel=center_panel,
        overlay=overlay,
    )


def get_scene_model(frame=None):
    """Convenience helper: capture (or accept) a frame, run OmniParser,
    and produce a SceneModel.

    Args:
        frame: an optional PIL Image.  When None, capture a fresh frame
               via capture.adb_capture.capture_screen().

    Returns:
        The SceneModel for the frame.

    Consumers that already have an OmniParser element list should call
    detect_scene() directly to avoid re-parsing the frame.  This helper
    is for the simpler case "I have a frame (or want one), give me a
    SceneModel" — exactly what _assert_at_port, exit_to_overworld, and
    similar caller need.
    """
    from vision.omniparser import parse_fast_cached
    if frame is None:
        from capture.adb_capture import capture_screen
        frame = capture_screen()
    elements = parse_fast_cached(frame)
    return detect_scene(frame.width, frame.height, elements)


def _classify(
    top: TopLeftRegion,
) -> Tuple[SceneFamily, str, Confidence, List[str], List[str]]:
    """Apply the rules table from docs/scene_model_design.md.

    Trusts `top.family` as the canonical family decision (the detector
    has already weighed multiple signals).  This function's job is to
    resolve scene_kind within that family and compute confidence.
    """
    matched:   List[str] = []
    disagreed: List[str] = []

    title_text = (top.title.text if top.title else "").lower().strip()

    # Overworld family
    if top.family == "overworld":
        if top.big_icon == "lighthouse":
            matched.append("top_left_lighthouse")
            if top.flag_present:
                matched.append("top_left_flag")
            if top.back_arrow:
                disagreed.append("back_arrow_set_on_overworld")
            return ("overworld", "port_overworld", "high", matched, disagreed)
        if top.big_icon == "ship":
            matched.append("top_left_ship")
            if top.back_arrow:
                disagreed.append("back_arrow_set_on_sea")
            return ("overworld", "sea", "high", matched, disagreed)

    # World map family
    if top.family == "world_map":
        matched.append("top_left_back_arrow")
        if top.tutorial_q:
            matched.append("top_left_tutorial_q")
        return ("world_map", "world_map", "high", matched, disagreed)

    # Chromed family.  Confidence: high when back-arrow detected, medium
    # otherwise (title-only fallback; OmniParser missed the back-arrow).
    if top.family == "chromed":
        if top.back_arrow:
            matched.append("top_left_back_arrow")
            confidence: Confidence = "high"
        else:
            matched.append("top_left_title_only")
            confidence = "medium"
        if top.tutorial_q:
            matched.append("top_left_tutorial_q")
        if title_text in KNOWN_BUILDING_TITLES:
            return ("chromed", f"building:{title_text}",
                    confidence, matched, disagreed)
        if title_text in KNOWN_SUB_MENU_TITLES:
            return ("chromed", f"sub_menu:{title_text}",
                    confidence, matched, disagreed)
        # Chromed scene with an unfamiliar title — still chromed family,
        # scene_kind is "sub_menu:<title>" by convention.  Sub-menus are
        # more common than novel buildings so default to that kind.
        if title_text:
            return ("chromed", f"sub_menu:{title_text}",
                    "medium", matched, disagreed)
        return ("chromed", "unknown", "medium", matched, disagreed)

    return ("unknown", "unknown", "low", matched, disagreed)
