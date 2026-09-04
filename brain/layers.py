"""Who may call whom. The layering, written down so it can be enforced.

    TASK        the COMPANY'S MANAGER. Decides the mission sequence — check, plan, gather,
                sail, barter, sell — and states WORK ORDERS. It never perceives, never taps,
                never clears a popup, never navigates.

    DISPATCH    the UI MASTER. Takes a work order, perceives, clears what is in the way,
                converts the order into an INTENT with the goal in its extras, and runs the
                activity that serves it.

    UI          screens, readers and primitives. Reached only through an activity.

    task ──work order──> dispatch ──intent(goal)──> activity ──> ui

ONE CHAIN, ONE DIRECTION. The task layer's only outward call is `run_goal(goal)`; everything
else it needs arrives as the result. A task module that imports `actions.*`, `vision.*` or
`capture.*` has stepped around the dispatcher, and every time that has happened it has cost
something: on 2026-08-28 `run_barter_command` perceived, cleared blockers and ran a UI recipe
directly, so a map-opener ended up holding an OS lock screen it had no vocabulary for.

Python cannot enforce this — there are no package-private symbols. So it is enforced by a
TEST over the import graph (`tests/test_the_layering_is_enforced.py`), and the existing
violations are recorded below as explicit debt rather than pretended away.
"""
from __future__ import annotations

# The company's manager. These decide WHAT the mission does, never HOW a screen works.
TASK_MODULES = (
    "brain/barter_runner.py",
    "brain/voyage_runner.py",
    "brain/sail_runner.py",
    "brain/mission_runner.py",
    "brain/barter_command.py",
    "brain/mission.py",
    "brain/mission_progress.py",
    "brain/barter_mission_live.py",
    "brain/barter_quantity.py",
    "brain/barter_strategy.py",
    "brain/barter_task.py",
    "brain/market_ledger.py",
)

# The UI side. A task module importing any of these has stepped around the dispatcher.
#
# Stored WITHOUT the trailing dot, and matched by `is_ui_module` below. With the dot,
# `from actions import ui` (module="actions") slipped straight through — found by a probe
# that added exactly that import and watched the test pass. A guard that does not catch the
# simplest form of what it guards against is worse than none, because it is believed.
UI_PACKAGES = ("actions", "vision", "capture")


# The task runner is PASSIVE (user, 2026-08-28):
#
#   "The task runner needs to be passive. It is only when the dispatcher settles to a world
#    that it consults the task manager to get the work order, and it dispatches it. The task
#    runner only does one thing: provide the work order to the dispatcher, and update task
#    status. Not calling anything."
#
# So the task runner is not a driver that runs a mission — it is a FUNCTION the dispatcher
# calls: (result, state) -> the next work order. The dispatcher already declares exactly this
# slot, `next_goal`. Two halves, enforced separately below:
#
#   1. It does not CALL DOWN. No UI, and above all no PERCEPTION — it is handed `state`, it
#      never goes and looks. Reaching for the game itself is how a conclusion gets stored:
#      the task runner would be reading a world the dispatcher is the owner of. (Principle #0:
#      we do not control the game, we observe it — and observation is centralized, #1.)
#   2. It does not CALL UP. It never invokes the dispatcher or the goal loop. Being consulted
#      is the whole of its participation; if it could call the loop, it would be the driver
#      again under a different name.
#
# Both halves are ratcheted, because neither is clean yet — and half 2 is dirtier than a
# glance at `barter_command.py` suggests. That module has 0 driver calls, but
# `barter_mission_live.py` calls `run_goal` six times: it IS the old driver, the shape the
# passive contract replaces. `barter_command.py` is gated at ZERO for driver calls to keep the
# module being reshaped from regressing; the legacy driver is merely frozen.

# Perception entry points: the task runner asking any of these is going to look for itself.
# It should be reading `state`, which the dispatcher perceived centrally and passed in.
PERCEPTION_CALLS = (
    "read_fleet_status",          # where are we / what is in the hold -> state
    "read_village_barter_remote", # -> a RemoteCheck work order for WorldMapActivity
    "where_am_i",                 # -> state
    "capture_screen",             # -> the dispatcher's, never a task's
    "parse_fast_cached",
    "detect_left_menu",
    "perceive",                   # the whole pass. `last_seen()` reads what was already
                                  # observed and captures nothing — that is not perceiving.
)

# Calling the loop that calls you. Inverted control, and the end of the passive contract.
DRIVER_CALLS = ("run_goal", "Dispatcher", "dispatch_once", "run_to_completion")

# Per-module ceilings, all GENERATED (see the note on KNOWN_VIOLATIONS — never typed).
PERCEPTION_BUDGET = {
    "brain/barter_runner.py": 0,        # born clean, and held there
    "brain/barter_command.py": 0,   # CLEAN
    "brain/barter_mission_live.py": 6,
}
DRIVER_BUDGET = {
    "brain/barter_runner.py": 0,        # born clean, and held there
    "brain/voyage_runner.py": 0,        # born clean, and held there
    "brain/sail_runner.py": 0,          # born clean, and held there
    "brain/mission_runner.py": 0,       # born clean, and held there
    "brain/barter_command.py": 0,       # gated: the module actively being reshaped
    "brain/barter_mission_live.py": 6,  # frozen: the legacy driver
}


def reaches_ui_through(path: str, imports_of) -> set:
    """Task modules `path` imports that themselves import UI — the SECOND hop.

    A guard that looks one hop is defeated by a façade. `brain/barter_mission_live.py` held
    the whole barter panel — capture, parse, tap — and imported thirteen UI modules to do it,
    so `brain/barter_command.py` measured perfectly clean while reaching every one of them
    through it. The gate said CLEAN and the module was not.

    `imports_of` is passed in rather than computed here so the test owns the parsing and this
    module stays free of AST machinery.
    """
    mod_to_path = {p.replace("/", ".")[:-3]: p for p in TASK_MODULES}
    me = path.replace("/", ".")[:-3]
    out = set()
    for mod in imports_of(path):
        via = mod_to_path.get(mod)
        if via is None or mod == me:
            continue
        if any(is_ui_module(m) for m in imports_of(via)):
            out.add(via)
    return out


def is_ui_module(name: str) -> bool:
    """True for `actions`, `actions.ui`, `vision.omniparser` — and not for `actionsomething`."""
    return any(name == pkg or name.startswith(pkg + ".") for pkg in UI_PACKAGES)

# What the task layer IS allowed to reach for: the dispatcher's front door, the goal types it
# states orders in, and its own kind. Goals are data — naming one is not touching a screen.
ALLOWED_FOR_TASK = (
    "brain.run_goal",
    "brain.dispatcher",
    "brain.activities",        # the goal dataclasses live beside their activities
)

# ── the ratchet ──────────────────────────────────────────────────────────────
#
# 43 imports across 2 files on 2026-08-28. A hard gate would fail on contact, so this is a
# RATCHET: every pair below is known debt, anything NOT below is a new violation and fails.
# Entries come OUT as the refactor proceeds and never go back in.
#
# GENERATED, NEVER TYPED. The first version of this list was transcribed by hand and
# missed three real pairs while inventing others — the same failure as reading field names
# off a log instead of the dataclass (Guiding Principle #3). Regenerate with the scan in
# tests/test_the_layering_is_enforced.py rather than editing entries in.
#
# `brain/barter_mission_live.py` holds most of it — it is the mission's node bodies, which is
# exactly the layer that should be stating orders rather than driving screens.
KNOWN_VIOLATIONS = frozenset({
    ("brain/barter_mission_live.py", "actions.adb_actions"),
    ("brain/barter_mission_live.py", "actions.buy_materials"),
    ("brain/barter_mission_live.py", "actions.fleet_status"),
    ("brain/barter_mission_live.py", "actions.route_execution"),
    ("brain/barter_mission_live.py", "actions.sail_actions"),
    ("brain/barter_mission_live.py", "actions.sell_goods"),
    ("brain/barter_mission_live.py", "capture.adb_capture"),
    ("brain/barter_mission_live.py", "vision.world_map_parser"),
})
