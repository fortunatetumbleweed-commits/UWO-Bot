# vision/state_fingerprints_data.py
#
# Phase 6 L3 — fingerprint definitions populated from data.
#
# Each fingerprint here is data-driven: signals were extracted by
# vision/fingerprint_survey.py from frames in data/labels.jsonl.
# The cross-frame stability of each label is noted in comments
# alongside the relevant signal.
#
# Generic principles:
#   - Use ElementCountSignal for STRUCTURAL density signals (button-
#     dense panel, icon cluster, etc.).  Resolution-invariant via
#     normalised regions.
#   - Use LabelSetSignal for KB-driven label match (KNOWN_BUILDING_TITLES,
#     KNOWN_SUB_MENU_TITLES) or for stable cross-frame labels (main_menu's
#     'Friend / Rank / Guild' bottom-right bar).
#   - Use TextContainsSignal when ALL of a small set of strings must be
#     present (main_menu's 'Ducat' AND 'Total Load Capacity' header).
#
# Importing this module registers all fingerprints into the registry.

from __future__ import annotations

from vision.state_fingerprints import (
    ElementCountSignal,
    Fingerprint,
    LabelSetSignal,
    TextContainsSignal,
    register_fingerprint,
)


# ── Region constants (normalised) ────────────────────────────────────────────

# Top-left region (port name, building title, sub-menu title, world map title).
TOP_LEFT_TITLE   = (0.00, 0.00, 0.40, 0.10)
# Wider top-left for multi-row company panel content.
TOP_LEFT_PANEL   = (0.00, 0.00, 0.30, 0.55)
# Top-center mode-tab row (world map's Port/Explore/Route/Trade).
TOP_CENTER       = (0.33, 0.00, 0.67, 0.10)
# Top-right corner (chrome icons: home / hamburger / X-close).
TOP_RIGHT_CHROME = (0.65, 0.00, 1.00, 0.10)
# Right edge panel — port_overworld's tab+minimap cluster sits here.
RIGHT_EDGE       = (0.85, 0.05, 1.00, 0.45)
# Right side broad — main_menu's icon grid spans cx>0.65.
RIGHT_SIDE_GRID  = (0.65, 0.05, 1.00, 0.85)
# Bottom-right tile bar (main_menu's Auction/Rank/Guild/Friend row).
BOTTOM_RIGHT_BAR = (0.65, 0.85, 1.00, 1.00)
# Bottom-left utility column (main_menu's Settings/Exit/Screenshot;
# port_map's 'World map' button).
BOTTOM_LEFT_UTIL = (0.00, 0.80, 0.20, 1.00)
# Mid-right column for sailing-condition loading variant.
MID_RIGHT        = (0.65, 0.33, 1.00, 0.67)
# Back-arrow region (very top-left corner).
BACK_ARROW       = (0.00, 0.00, 0.10, 0.10)
# Bottom-center action button (world map's 'Go to City').
BOTTOM_CENTER    = (0.30, 0.80, 0.70, 1.00)


# ── Known label sets (KB-driven) ─────────────────────────────────────────────

# Buildings the bot has visited; titles match top-left text inside a
# building interior.  Mirrored from vision/chrome_detector.py.
KNOWN_BUILDING_TITLES = frozenset({
    "harbor", "harbour", "market", "castle", "inn", "bank",
    "cathedral", "church", "fortune teller", "item shop", "shop",
    "union", "bureau", "mercator estate", "estate", "tavern",
    "blacksmith", "guild", "warehouse", "exchange", "office",
    "shipyard", "palace",
})

KNOWN_SUB_MENU_TITLES = frozenset({
    # Market sub-menus
    "purchase", "sell", "auto-buy", "auto-sell", "negotiation",
    # Inn sub-menus
    "recruit crew", "hire", "hire crew", "redistribute crew",
    "manage mate", "manage mates", "employee",
    "tavern story", "drink", "party",
    # Bank sub-menus (and Mercator Estate equivalents)
    "deposit", "withdraw", "deposit/withdrawal",
    "savings account", "insurance",
    # Harbor sub-menus
    "supply", "supply departure", "depart", "depart now",
    "fleet management", "repair", "ready to sail",
    # Shipyard sub-menus
    "build", "blueprint", "gear",
    "parts shop", "dismantle", "modify", "assemble",
    # Bureau / nation / merchant office sub-menus
    "invest", "tool", "contract",
    # Cathedral sub-menus
    "pray", "donate",
    # Union sub-menus
    "limited",
    # Quest / discovery sub-menus
    "requests", "report discoveries", "discovery rank",
    "report resource", "cartography",
    # Misc
    "fortune", "black market",
})

# main_menu's right-side icon-grid labels (5/5 stable across surveyed
# frames; LV-prefixed labels are matched as substring so 'lv 40
# lighthouse' still matches 'lighthouse').
MAIN_MENU_GRID_LABELS = frozenset({
    "ship", "fleet", "assign", "build", "storage",
    "mates", "admiral", "chronicles", "mission", "nation",
    "journal", "collection", "enhance",
    "lighthouse", "combat", "assault", "dispatch", "production",
})

# main_menu's bottom-right tile bar — perfect 5/5 consistency,
# strong enough to single-handedly identify main_menu.
MAIN_MENU_TILE_BAR_LABELS = frozenset({
    "auction", "rank", "guild", "friend",
})

# main_menu's bottom-left utility column.
MAIN_MENU_UTIL_LABELS = frozenset({
    "help", "settings", "exit", "screenshot",
    "save energy", "sailing log",
})

# world_map mode tabs at top center — 5/5 stable across surveyed frames.
WORLD_MAP_MODE_TABS = frozenset({
    "port", "explore", "route", "trade",
})

# world_map bottom-left controls — 4-5/5 stable.
WORLD_MAP_BOTTOM_LEFT_LABELS = frozenset({
    "trade event schedule", "my location", "filter",
    "enemy company lv",
})

# Sea HUD substring tokens — fuzzy match against any element label.
SEA_HUD_TOKENS = frozenset({
    "day", "sailing", "eta", "destination",
    "supply", "remaining",
})


# ── Fingerprint definitions ──────────────────────────────────────────────────

# main_menu — single discriminating signal: the bottom-right tile bar.
# User-articulated invariant: 'Auction/Rank/Guild/Friend' as a 4-button
# row in the bottom-right corner is unique to main_menu — no other
# state has that label cluster.  Other regions (top-left company panel,
# right-side icon grid, bottom-left utility column) are present too
# but are confirmatory rather than required, so we leave them out
# rather than complicate the confidence ratio.
register_fingerprint(Fingerprint(
    state_id="main_menu",
    description=(
        "Hamburger side panel.  Identified by the bottom-right tile "
        "bar (Auction/Rank/Guild/Friend) — that label cluster is "
        "unique to this state."
    ),
    positive_signals=(
        LabelSetSignal(
            name="tile_bar",
            region=BOTTOM_RIGHT_BAR,
            labels=MAIN_MENU_TILE_BAR_LABELS,
            min_matches=3,
            element_types=frozenset({"button"}),
        ),
    ),
    min_positive_to_match=1,
))

# world_map — top-center mode tabs are 5/5 stable.
register_fingerprint(Fingerprint(
    state_id="world_map",
    description=(
        "World map view.  Identified by the Port/Explore/Route/Trade "
        "mode tabs at top center plus the World Map title in top-left."
    ),
    positive_signals=(
        # Mode tabs — 5/5 across all sampled frames.
        LabelSetSignal(
            name="mode_tabs",
            region=TOP_CENTER,
            labels=WORLD_MAP_MODE_TABS,
            min_matches=2,
        ),
        # 'World Map' title in top-left — 4/5 stable.
        LabelSetSignal(
            name="title",
            region=TOP_LEFT_TITLE,
            labels=frozenset({"world map"}),
            min_matches=1,
        ),
        # Bottom-left controls — 4-5/5.
        LabelSetSignal(
            name="bottom_left",
            region=BOTTOM_LEFT_UTIL,
            labels=WORLD_MAP_BOTTOM_LEFT_LABELS,
            min_matches=1,
        ),
        # 'Go to City' button bottom-center — present whenever a city
        # is selected on the map.
        LabelSetSignal(
            name="go_to_city",
            region=BOTTOM_CENTER,
            labels=frozenset({"go to city"}),
            min_matches=1,
        ),
    ),
    min_positive_to_match=1,
))

# port_map — bottom-left has 'world' + 'map' as separate text labels
# alongside the back arrow.  The world_map button is unique to port_map.
#
# Requires BOTH signals (min_positive_to_match=2): a lone back_arrow
# is far too generic — village interior, building interior, world
# map, and many sub_menus all carry one.  When only back_arrow
# matched (1/2), port_map fired at medium confidence and shadowed
# the legacy chain's village/building/etc. checks.  Observed live
# on 2026-05-23 Berber sail: the village arrival frame matched
# port_map @ medium via back_arrow=1/1, the sail_to FSM saw an
# unexpected state, planned recovery, and bounced the bot back to
# port_overworld instead of marking ARRIVED.  The 'world map'
# text in the bottom-left is the actual discriminator; without it,
# the frame is NOT a port_map.
register_fingerprint(Fingerprint(
    state_id="port_map",
    description=(
        "Port map (in-port grid view).  Identified by the 'World map' "
        "button bottom-left plus a back arrow top-left."
    ),
    positive_signals=(
        # 'world' + 'map' both appearing in bottom-left — 5/5 stable.
        TextContainsSignal(
            name="world_map_button",
            region=BOTTOM_LEFT_UTIL,
            required=("world", "map"),
        ),
        # Back arrow icon in top-left.
        ElementCountSignal(
            name="back_arrow",
            region=BACK_ARROW,
            min_count=1,
            element_types=frozenset({"icon", "button"}),
        ),
    ),
    min_positive_to_match=2,
))

# village — the bare word "Village" in the top-left title region.
# In-game village interiors show literally "Village" as the title
# (no specific village name); no port_overworld, building, sub_menu,
# port_map, world_map or main_menu uses that exact label as its
# top-left title, so the single-signal match is specific enough.
#
# Originally this fingerprint also required a back-arrow icon in
# BACK_ARROW region (2-of-2), but live inspection of the labelled
# `village` frame (data/sessions/2026-05-21_15-12-02/0001) showed
# OmniParser tags the 'Village' button starting at x=140 — close
# enough to the corner that the back-arrow visual is absorbed into
# the title button's bbox.  Zero icons appeared in BACK_ARROW.
# Requiring back_arrow gated out the very frame this fingerprint
# was meant to catch — the 2026-05-23 Berber sail succeeded only
# because the cascade fell through to the legacy chain's generic-
# title branch.  Dropping the back_arrow requirement makes this
# the primary path.
#
# Identity (which village) still cannot be read from the title —
# it must come from the caller's goal context (e.g. SailToGoal's
# destination, checked via _destination_is_likely_village).
register_fingerprint(Fingerprint(
    state_id="village",
    description=(
        "Village interior.  Top-left title is literally 'Village'."
    ),
    positive_signals=(
        LabelSetSignal(
            name="village_title",
            region=TOP_LEFT_TITLE,
            labels=frozenset({"village"}),
            min_matches=1,
        ),
    ),
    min_positive_to_match=1,
))

# building_interior — matches via a known building name in the
# top-left title region.
#
# History (2026-05-22):
#   Morning: bot was on port_overworld with "shipyard" tagged near
#     top-left by OmniParser; the title-only fingerprint matched →
#     classified as building → sail_to fired press_back → 22-min loop.
#     Patched by requiring title + back-arrow icon (min 2-of-2).
#   Evening: that 2-of-2 fix backfired — when actually INSIDE the
#     harbor, OmniParser sometimes didn't tag the back-arrow icon, so
#     building couldn't match and a learned fingerprint
#     (learned_repair_supply) won by default, sending sail_to into
#     another stuck loop.
#
# Resolution: revert to title-only (1-of-1).  The original Palma
# shipyard case is now defended structurally by the Phase 4a family
# classifier (commit d7c49f7) — port_overworld frames short-circuit at
# the top of _classify_nav_state, before this OmniParser fingerprint
# cascade ever runs.  When actually inside a building, the title is
# the canonical signal; we trust it.
register_fingerprint(Fingerprint(
    state_id="building",
    description=(
        "Inside any building.  Identified by a known building name in "
        "the top-left title region.  Defence against port_overworld "
        "false positives (Palma shipyard case) is provided upstream by "
        "the Phase 4a family classifier, which short-circuits "
        "port_overworld frames before this fingerprint is evaluated."
    ),
    positive_signals=(
        LabelSetSignal(
            name="building_title",
            region=TOP_LEFT_TITLE,
            labels=KNOWN_BUILDING_TITLES,
            min_matches=1,
        ),
    ),
    min_positive_to_match=1,
))

# sub_menu — matches ONLY via known sub-menu title in top-left.
register_fingerprint(Fingerprint(
    state_id="sub_menu",
    description=(
        "A building sub-menu (Recruit Crew, Purchase, Sell, ...).  "
        "Identified solely by a known sub-menu name in the top-left "
        "title.  Back arrow alone is not sufficient — it appears on "
        "many screens (port_map, building, world_map…)."
    ),
    positive_signals=(
        LabelSetSignal(
            name="sub_menu_title",
            region=TOP_LEFT_TITLE,
            labels=KNOWN_SUB_MENU_TITLES,
            min_matches=1,
        ),
    ),
    min_positive_to_match=1,
))

# port_overworld — port name top-left + right-edge tab cluster.
# Port name is run-time variable so we can't hard-code labels; instead
# the fingerprint accepts ANY screen with a right-edge cluster, BUT
# rejects screens that match a more specific state (building, sub_menu,
# main_menu, world_map, sea).  This 'most-general state' shape is the
# correct semantic — port_overworld is the default at-port screen.
register_fingerprint(Fingerprint(
    state_id="port_overworld",
    description=(
        "Port town overworld.  Default at-port screen — matches when a "
        "right-edge cluster is present and none of the more specific "
        "negative signals fire (building, sub_menu, main_menu, "
        "world_map, sea)."
    ),
    positive_signals=(
        # Right-edge cluster of buttons/icons — port_overworld's tab
        # bar (4 tabs) + minimap.
        ElementCountSignal(
            name="right_edge_panel",
            region=RIGHT_EDGE,
            min_count=2,
            element_types=frozenset({"icon", "button"}),
        ),
    ),
    negative_signals=(
        # If a known building title is in top-left, this is building.
        LabelSetSignal(
            name="not_building_title",
            region=TOP_LEFT_TITLE,
            labels=KNOWN_BUILDING_TITLES,
            min_matches=1,
        ),
        # If a known sub-menu title is in top-left, this is sub_menu.
        LabelSetSignal(
            name="not_sub_menu_title",
            region=TOP_LEFT_TITLE,
            labels=KNOWN_SUB_MENU_TITLES,
            min_matches=1,
        ),
        # If 'World Map' title is in top-left, this is world_map.
        LabelSetSignal(
            name="not_world_map_title",
            region=TOP_LEFT_TITLE,
            labels=frozenset({"world map"}),
            min_matches=1,
        ),
        # If main_menu's bottom-right tile bar is present, this is main_menu.
        LabelSetSignal(
            name="not_main_menu_tile_bar",
            region=BOTTOM_RIGHT_BAR,
            labels=MAIN_MENU_TILE_BAR_LABELS,
            min_matches=3,
            element_types=frozenset({"button"}),
        ),
        # If world_map mode tabs are present, this is world_map.
        LabelSetSignal(
            name="not_world_map_mode_tabs",
            region=TOP_CENTER,
            labels=WORLD_MAP_MODE_TABS,
            min_matches=2,
        ),
        # If sea HUD tokens are present anywhere, this is sea, not port.
        LabelSetSignal(
            name="not_sea_hud",
            region=(0.0, 0.0, 1.0, 1.0),
            labels=SEA_HUD_TOKENS,
            min_matches=2,
        ),
    ),
    min_positive_to_match=1,
))

# sea — sailing HUD tokens.  Same negative signals as port_overworld.
register_fingerprint(Fingerprint(
    state_id="sea",
    description=(
        "Open sea with sailing HUD visible.  Identified by sea HUD "
        "tokens (day, sailing, eta, ...) appearing as substrings."
    ),
    positive_signals=(
        # Sea HUD tokens — substring match on any element in any region.
        # We use a wide region (whole frame) since HUD tokens can appear
        # in either top-left supply panel or bottom-center destination.
        LabelSetSignal(
            name="sea_hud",
            region=(0.0, 0.0, 1.0, 1.0),
            labels=SEA_HUD_TOKENS,
            min_matches=2,
        ),
    ),
    negative_signals=(
        # Reject if main_menu or world_map signals present.
        LabelSetSignal(
            name="not_main_menu_tile_bar",
            region=BOTTOM_RIGHT_BAR,
            labels=MAIN_MENU_TILE_BAR_LABELS,
            min_matches=3,
            element_types=frozenset({"button"}),
        ),
        LabelSetSignal(
            name="not_world_map_mode_tabs",
            region=TOP_CENTER,
            labels=WORLD_MAP_MODE_TABS,
            min_matches=2,
        ),
        # Reject if a known building/sub-menu title is in top-left.
        LabelSetSignal(
            name="not_building_title",
            region=TOP_LEFT_TITLE,
            labels=KNOWN_BUILDING_TITLES,
            min_matches=1,
        ),
        LabelSetSignal(
            name="not_sub_menu_title",
            region=TOP_LEFT_TITLE,
            labels=KNOWN_SUB_MENU_TITLES,
            min_matches=1,
        ),
    ),
    min_positive_to_match=1,
))

# port_loading — pre-port-arrival ranking display.  Strong 5/5 signals.
register_fingerprint(Fingerprint(
    state_id="port_loading",
    description=(
        "Loading screen shown when arriving at a port — displays the "
        "port's nation/private investment ranking before transition."
    ),
    positive_signals=(
        LabelSetSignal(
            name="rank_panel",
            region=MID_RIGHT,
            labels=frozenset({"private rank", "nation rank"}),
            min_matches=1,
        ),
    ),
    min_positive_to_match=1,
))

# NOTE: states beyond this point are LEARNED, not hand-authored.  When
# the bot encounters an unknown screen and Claude analyses it, the
# learning hook in vision/claude_vision.py persists a candidate
# Fingerprint to memory/knowledge/learned_fingerprints/, and the
# registry auto-loads those at startup (see _load_learned_fingerprints
# in vision/state_fingerprints.py).  This file holds only the
# foundational hand-authored fingerprints — everything else is
# discovered.


# Auto-load learned fingerprints discovered by the Claude scene-analyser
# in prior runs (memory/knowledge/learned_fingerprints/).  Registered
# AFTER the foundational entries above, so any state_id collision with
# a foundation entry is resolved in favour of the foundation.
from vision.state_fingerprints import load_learned_fingerprints as _load_learned
try:
    _learned = _load_learned()
    if _learned:
        from loguru import logger as _logger
        _logger.info(
            f"[state_fingerprints] auto-loaded {len(_learned)} learned "
            f"fingerprint(s): {[fp.state_id for fp in _learned]}"
        )
except Exception as _exc:
    pass


# loading (generic transition) — only one stable signal.
register_fingerprint(Fingerprint(
    state_id="loading",
    description=(
        "Generic transition loading screen.  Some loading variants "
        "show 'sailing condition' / 'sailing conditions' messaging."
    ),
    positive_signals=(
        LabelSetSignal(
            name="sailing_condition",
            region=MID_RIGHT,
            labels=frozenset({"sailing condition", "sailing conditions"}),
            min_matches=1,
        ),
    ),
    min_positive_to_match=1,
))
