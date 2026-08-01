from __future__ import annotations

# routines/definitions.py
# Named routines — each is an ordered list of step strings.
#
# Step syntax:
#   goto <building>          — navigate to a building via the menu
#   collect rewards          — collect daily/timed rewards (when implemented)
#   buy <item>               — buy an item from the current building (when implemented)
#
# Add new routines here; run them with:
#   python run.py routine <name>

ROUTINES: dict[str, list[str]] = {
    # Visit every building in the current port and record what's inside
    "explore_port": [
        "explore",
    ],

    # Collect the daily castle reward
    "castle_rewards": [
        "goto castle",
        "collect rewards",
    ],

    # Quick market run
    "buy_icon": [
        "goto market",
        "buy icon",
    ],

    # Full daily loop: castle rewards then market
    "daily": [
        "goto castle",
        "collect rewards",
        "goto market",
        "buy icon",
    ],
}
