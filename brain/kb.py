# brain/kb.py
#
# Unified accessor for both Game Control Intelligence and Game Play Intelligence KBs.
#
# Two namespaces:
#   control()  — how to operate the game UI (navigation, dialogs, buttons)
#   strategy() — how to play the game well (trade rules, trends, growth)
#
# Both are loaded lazily on first access and cached for the process lifetime.
# Call reload() to force a fresh load (e.g. after editing a KB file mid-run).
#
# Usage:
#   from brain.kb import control, strategy
#   tokens = control().sea_hud_tokens()
#   rules  = strategy().trade_rules()

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from loguru import logger

_KB_ROOT = Path("memory/knowledge")

# ── Recovery action type identifiers ─────────────────────────────────────────
# These values must match the "recovery_action" field in flows.json steps.
# Import these constants wherever action strings are compared or set.
RECOVERY_TAP_BUTTON  = "tap_button"
RECOVERY_WAIT        = "wait"
RECOVERY_TAP_OK_OR_X = "tap_ok_or_x"

# ── Blocking signal resolution step action identifiers ────────────────────────
# These values must match the "action" field in flows.json blocking_signals[].resolution[].
# Import these constants wherever resolution steps are executed or defined.
RES_EXIT_TO_OVERWORLD    = "exit_to_port_overworld"
RES_NAVIGATE_TO_BUILDING = "navigate_to_building"
RES_TAP_PRIMARY_ACTION   = "tap_primary_action"   # tap the main action button in current building/sub-menu
RES_RETRY                = "retry"                # sequence done — caller retries the original action

# ── Region constraint dict keys ───────────────────────────────────────────────
# These keys are used in recovery_region dicts in flows.json and in Python
# code that reads them.  Import from here to keep spellings in sync.
RGN_Y_MIN = "y_min_pct"
RGN_Y_MAX = "y_max_pct"
RGN_X_MIN = "x_min_pct"
RGN_X_MAX = "x_max_pct"


# ── Low-level loader ──────────────────────────────────────────────────────────

def _load(rel_path: str) -> Any:
    """Load a JSON KB file relative to memory/knowledge/. Returns {} on error."""
    path = _KB_ROOT / rel_path
    if not path.exists():
        logger.warning(f"[kb] KB file not found: {path}")
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"[kb] Failed to load {path}: {e}")
        return {}


# ── Control KB ────────────────────────────────────────────────────────────────

class ControlKB:
    """
    Game Control Intelligence — how to navigate and operate the game UI.
    Loaded once from disk; all accessors are fast dict lookups.
    """

    def __init__(self):
        self._ui      = _load("control/ui_signals.json")
        self._market  = _load("control/dialogs/market.json")
        self._worldmap = _load("control/dialogs/world_map.json")

    # ── Navigation signals ────────────────────────────────────────────────────

    def sea_hud_tokens(self) -> tuple[str, ...]:
        return tuple(self._ui.get("sea_hud_tokens", []))

    def loading_keywords(self) -> tuple[str, ...]:
        return tuple(self._ui.get("loading_screen_keywords", ["entering", "preparing", "loading"]))

    def sea_notice_keywords(self) -> tuple[str, ...]:
        return tuple(self._ui.get("sea_notice_keywords", []))

    def zone_words(self) -> tuple[str, ...]:
        return tuple(self._ui.get("zone_words", []))

    def main_menu_keywords(self) -> set[str]:
        return set(self._ui.get("main_menu_keywords", []))

    def harbor_panel_keywords(self) -> set[str]:
        return set(self._ui.get("harbor_panel_keywords", []))

    def world_map_title_keywords(self) -> tuple[str, ...]:
        return tuple(self._ui.get("world_map_title_keywords", ["world map"]))

    def exit_game_keywords(self) -> tuple[str, ...]:
        return tuple(self._ui.get("exit_game_keywords", []))

    def building_name_variants(self, building: str) -> list[str]:
        """Return all known label variants for a building type (e.g. 'harbor' → ['harbor','harbour'])."""
        variants = self._ui.get("building_name_variants", {})
        return variants.get(building.lower(), [building.lower()])

    def detail_mentions_building(self, detail: str, building: str) -> bool:
        """
        True if `detail` (case-insensitively) contains any known label
        variant of the given canonical `building` name.

        Use this anywhere code needs to branch on which building a screen
        belongs to.  Avoids hardcoded substrings like
        `"harbor" in detail or "harbour" in detail` — variants are read
        from the KB so adding a new spelling, plural, or translation
        requires only a config change.
        """
        if not detail:
            return False
        d = detail.lower()
        return any(v in d for v in self.building_name_variants(building))

    def known_building_types(self) -> dict[str, dict]:
        """
        Return all known building types loaded from building_types/*.json.
        Keys are canonical building type names (e.g. 'harbor', 'market').
        Values are the full KB dict (description, sub_menus, flows, notes, …).

        Includes all name variants so callers can check substrings of nav_detail
        without needing a separate variant lookup.  Each entry gains an extra
        'all_names' key: [canonical_name, …variants…].
        """
        from pathlib import Path
        import json as _json

        bld_dir = _KB_ROOT / "building_types"
        result: dict[str, dict] = {}
        if not bld_dir.exists():
            return result

        for f in sorted(bld_dir.glob("*.json")):
            try:
                data = _json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            btype = data.get("building_type", f.stem)
            variants = self.building_name_variants(btype)
            result[btype] = {**data, "all_names": variants}

        return result

    # ── Market dialogs ────────────────────────────────────────────────────────

    def dialog_confirmation(self, dialog_id: str) -> tuple[str, ...]:
        """
        Keywords that confirm a specific market dialog is on screen.
        Used as *args to _wait_for_screen / _screen_contains.
        dialog_id: one of 'trade_goods_info', 'basket_loaded', 'confirm_sales',
                   'negotiation', 'sell_result', 'number_input',
                   'confirm_purchase', 'buy_result'
        """
        confs = self._market.get("dialog_confirmations", {})
        return tuple(confs.get(dialog_id, [dialog_id]))

    def sell_result_label_map(self) -> dict[str, tuple[str, int]]:
        """Returns {label_string: (field_name, max_value)} for sell result parsing."""
        fields = self._market.get("sell_result_fields", [])
        return {f["label"]: (f["field"], f["max_value"]) for f in fields}

    def buy_result_label_map(self) -> dict[str, tuple[str, int]]:
        """Returns {label_string: (field_name, max_value)} for buy result parsing."""
        fields = self._market.get("buy_result_fields", [])
        return {f["label"]: (f["field"], f["max_value"]) for f in fields}

    def market_action_button_labels(self, action: str) -> list[str]:
        """
        Label variants for primary action buttons in market flows.
        action: 'purchase' or 'sell'
        e.g. market_action_button_labels("purchase") → ["purchase", "Purchase"]
        """
        return self._market.get("action_buttons", {}).get(action, [])

    def negotiation_button_labels(self, action: str) -> list[str]:
        """
        Labels to look for on the negotiation dialog.
        action: 'use_one', 'use_all', or 'skip'
        """
        btns = self._market.get("negotiation_buttons", {})
        return btns.get(action, [])

    # ── World map / city info ─────────────────────────────────────────────────

    def city_info_title_keywords(self) -> tuple[str, ...]:
        return tuple(self._worldmap.get("city_info_title_keywords", ["city info"]))

    def city_info_skip_labels(self) -> set[str]:
        return set(self._worldmap.get("city_info_skip_labels", []))

    def culture_keywords(self) -> tuple[str, ...]:
        return tuple(self._worldmap.get("culture_keywords", []))

    def facility_keywords(self) -> tuple[str, ...]:
        return tuple(self._worldmap.get("facility_keywords", []))

    def tax_keywords(self) -> tuple[str, ...]:
        return tuple(self._worldmap.get("tax_keywords", ["tax"]))

    # ── FSM flow step detection ───────────────────────────────────────────────

    def flow_detection_order(self) -> list[tuple[str, str]]:
        """
        Ordered list of (flow_id, step_id) pairs to check during flow detection.
        Each entry is checked in order; first match wins.
        Loaded from ui_signals.json → flow_detection_order.
        """
        return [tuple(pair) for pair in self._ui.get("flow_detection_order", [])]

    def flow_step_recovery(self, flow_id: str, step_id: str) -> tuple[str, list[str], dict]:
        """
        Returns (recovery_action, button_labels, region_pct) for a specific flow step.
        recovery_action: 'tap_button' | 'wait' | 'tap_ok_or_x'
        button_labels: list of label variants to search for.
        region_pct: optional {y_min_pct, y_max_pct, x_min_pct, x_max_pct} fractions of
                    frame dimensions — used to constrain _find_button to the right area.
                    Empty dict means no constraint.

        Button labels can be specified two ways on a step:
          1. INLINE: `recovery_button_labels: [...]`            — list of literal labels
          2. REFERENCE: `recovery_button_labels_ref: "<path>"`  — path into a loaded KB
             file (e.g. "market.negotiation_buttons.skip" → self._market[
             "negotiation_buttons"]["skip"]).  Used to avoid duplicating
             labels that already live in dialogs/*.json.

        If both are present, the REFERENCE wins.  Falls back to ('tap_ok_or_x', [], {})
        if the step is not found.
        """
        from brain.fsm_registry import get_fsm_registry
        flow = get_fsm_registry().flows.get(flow_id)
        if not flow:
            return RECOVERY_TAP_OK_OR_X, [], {}
        for step in flow._raw.get("steps", []):
            if step.get("id") == step_id:
                action = step.get("recovery_action", RECOVERY_TAP_OK_OR_X)
                ref    = step.get("recovery_button_labels_ref")
                if ref:
                    labels = self._resolve_button_labels_ref(ref)
                else:
                    labels = step.get("recovery_button_labels", [])
                region = step.get("recovery_region", {})
                return action, labels, region
        return RECOVERY_TAP_OK_OR_X, [], {}

    def _resolve_button_labels_ref(self, ref: str) -> list[str]:
        """
        Resolve a button-labels reference like 'market.negotiation_buttons.skip'
        to the underlying list, by traversing the matching loaded KB dict.

        First segment selects the KB file:
          'market'   → self._market   (control/dialogs/market.json)
          'worldmap' → self._worldmap (control/dialogs/world_map.json)
          'ui'       → self._ui       (control/ui_signals.json)

        Remaining segments are dict keys.  If any segment is missing or the
        resolved value is not a list, returns [] and logs a warning.
        """
        parts = ref.split(".")
        if len(parts) < 2:
            logger.warning(f"[kb] malformed button-labels ref {ref!r} — expected '<kb>.<key>...'")
            return []
        kb_map = {
            "market":   self._market,
            "worldmap": self._worldmap,
            "ui":       self._ui,
        }
        cursor = kb_map.get(parts[0])
        if cursor is None:
            logger.warning(f"[kb] unknown KB root {parts[0]!r} in label ref {ref!r}")
            return []
        for p in parts[1:]:
            if isinstance(cursor, dict):
                cursor = cursor.get(p)
            else:
                cursor = None
                break
        if isinstance(cursor, list):
            return [str(x) for x in cursor]
        logger.warning(f"[kb] label ref {ref!r} did not resolve to a list (got {cursor!r})")
        return []

    def market_flow_ids(self) -> set[str]:
        """
        Flow IDs that require purchase/sell tab disambiguation via nav_detail.
        Reads disambiguate_by_nav_detail flag from each flow in flows.json.
        """
        from brain.fsm_registry import get_fsm_registry
        return {
            fid for fid, flow in get_fsm_registry().flows.items()
            if flow._raw.get("disambiguate_by_nav_detail", False)
        }

    def flow_step_keywords(self, flow_id: str, step_id: str) -> list[str]:
        """
        Detection keywords for a specific step within a flow.
        Reads from FSM registry (flows.json step_detection_keywords).
        Returns [] if not defined.
        """
        from brain.fsm_registry import get_fsm_registry
        flow = get_fsm_registry().flows.get(flow_id)
        if not flow:
            return []
        return flow.step_detection_keywords.get(step_id, [])

    def flow_step_button_labels(self, flow_id: str, step_id: str) -> list[str]:
        """
        Label variants for the primary action button on a flow step.
        e.g. flow_step_button_labels("market_purchase", "confirm") → ["purchase", "Purchase"]
        Returns [] if not defined (caller should use fallback coords).
        """
        from brain.fsm_registry import get_fsm_registry
        flow = get_fsm_registry().flows.get(flow_id)
        if not flow:
            return []
        for step in flow._raw.get("steps", []):
            if step.get("id") == step_id:
                return step.get("button_labels", [])
        return []

    # ── Interruptor detection keywords ────────────────────────────────────────

    def interruptor_keywords(self) -> list[tuple[str, list[str], str]]:
        """
        Returns list of (interruptor_id, detection_keywords, dismissal_method)
        ordered as defined in interruptors.json.
        Only includes interruptors that have detection_keywords defined.
        """
        from brain.fsm_registry import get_fsm_registry
        result = []
        for iid, interruptor in get_fsm_registry().interruptors.items():
            kws = interruptor.detection_keywords
            if kws:
                result.append((iid, kws, interruptor.dismissal))
        return result


# ── Strategy KB ───────────────────────────────────────────────────────────────

class StrategyKB:
    """
    Game Play Intelligence — what to do in the game to grow the company.
    Seeded from CLAUDE.md documentation; refined by bot-observed data over time.
    """

    def __init__(self):
        self._trade      = _load("strategy/trade_rules.json")
        self._trends     = _load("strategy/market_trends.json")
        self._growth     = _load("strategy/growth.json")
        self._investment = _load("strategy/investment.json")

    def trade_rules(self) -> dict:
        return self._trade

    def negotiation_default(self, side: str) -> str:
        """side: 'sell' or 'buy'. Returns default negotiation strategy."""
        return self._trade.get("negotiation_defaults", {}).get(side, "no")

    def culture_restrictions(self, culture: str) -> dict:
        """Return trade restrictions for a cultural context (e.g. 'islamic')."""
        return self._trade.get("culture_restrictions", {}).get(culture.lower(), {})

    def market_trends(self) -> list:
        return self._trends.get("trends", [])

    def trend_goods(self, trend_type: str) -> list[str]:
        """Return the goods affected by a specific trend type."""
        for t in self.market_trends():
            if t["type"] == trend_type:
                return t.get("affected_goods", [])
        return []

    def sea_region_progression(self) -> list:
        return self._growth.get("sea_region_progression", [])

    def growth_tracks(self) -> dict:
        return self._growth.get("growth_tracks", {})

    def investment_strategy(self) -> dict:
        return self._investment


# ── Singletons ────────────────────────────────────────────────────────────────

_control_instance:  ControlKB  | None = None
_strategy_instance: StrategyKB | None = None


def control() -> ControlKB:
    global _control_instance
    if _control_instance is None:
        _control_instance = ControlKB()
        logger.debug("[kb] Control KB loaded")
    return _control_instance


def strategy() -> StrategyKB:
    global _strategy_instance
    if _strategy_instance is None:
        _strategy_instance = StrategyKB()
        logger.debug("[kb] Strategy KB loaded")
    return _strategy_instance


def reload() -> None:
    """Force reload of all KB files. Call after editing KB files mid-run."""
    global _control_instance, _strategy_instance
    _control_instance  = None
    _strategy_instance = None
    logger.info("[kb] All KB caches cleared — will reload on next access")
