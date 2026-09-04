"""Ports and villages are on DIFFERENT world-map tabs, so their icons cannot be mixed.

Live 2026-08-27: the map was left on EXPLORE by the village navigation, `_try_port_search`
selected no tab, and typing "Barc" into that tab's rail returned `carved horn` — a trade
good — and a scatter of island names, never Barcelona. It then saved (70,290) as
`port_list_icon`, which is the position `_try_village_search` uses for the VILLAGE list, so
the wrong icon was persisted to disk and biased the next run's candidate ranking.
"""
import inspect

from actions import sail_actions


# NOTE: "does each rail search CHECK its tab?" lives in
# tests/test_the_world_map_tab_is_checked_not_assumed.py, which owns that rule for both
# sides. Asserting it here too meant two files pinning one behaviour through different
# literals — and when the call sites moved to `require_world_map_tab`, this file broke while
# the other passed. That is the drift these tests exist to prevent, so this file keeps to the
# DOMAIN FACT (ports and villages are different tabs) and leaves the rule to the other.


def test_the_village_search_uses_the_explore_tab():
    """The other half of the pair — pinned so the two cannot drift back together."""
    src = inspect.getsource(sail_actions.pan_to_village)
    assert 'select_world_map_tab("explore")' in src


def test_no_stale_port_icon_is_carried_in_the_config():
    """The learned position was the VILLAGE icon. It is cleared; if it reappears it must
    have been learned on a confirmed Port tab."""
    import json
    from pathlib import Path
    cfg = Path("memory/knowledge/config/world_map_ui.json")
    if not cfg.exists():
        return
    saved = json.loads(cfg.read_text()).get("port_list_icon")
    assert saved != [70, 290], "that is the village-list icon, not the port-list icon"
