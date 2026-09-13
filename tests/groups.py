"""Which half of the bot a test file belongs to, read off its imports.

THE SPLIT (user, 2026-09-13): *"we can generally separate the tests into navigation and
business, most of them do not overlap, and changing one does not affect the other. The only
difference is upstream, at the dispatcher level, and the sea gauges, those are shared. If
those are not touched, generally we only need to run the tests for one group."*

Measured 2026-09-13, all three green, on a quiet machine:

    pytest -m "not business"     2,336 tests    7:57    the navigation half
    pytest -m "not navigation"   5,188 tests   11:12    the business half
    pytest                       5,622 tests   16:15    everything

So a navigation change verifies in half the time and a business change in about two thirds.
The halves overlap by some 1,900 tests and that is why neither is anywhere near half the
clock: the shared middle — the vision primitives and the hygiene checks — belongs to neither
side and runs in both. The navigation half is few tests and slow anyway, because it loads the
mini-map and shoreline models.

DERIVED, NOT DECLARED. Nothing is marked in the test files themselves. A file is classified
by the modules it names — imports and `mock.patch` targets alike — so a test that starts
touching the market becomes a business test the moment it does, with nobody remembering to
say so. A file that names BOTH sides, or NEITHER, is shared and runs in every group: that is
the safe direction to be wrong in.
"""
from __future__ import annotations

import re

# The manual-navigation loop and the readers it runs on. `brain/goals/` is listed module by
# module rather than as a package: every goal in it is sea control EXCEPT `sail_to`, which is
# the sailing leg of a business mission and is named by hygiene tests besides. It is in
# neither list, so a file that reaches only it stays shared and runs in both groups.
NAVIGATION = (
    "brain.ai_nav",
    "brain.goals.backtrack_planner",
    "brain.goals.clear_cloud",
    "brain.goals.coverage_tracker",
    "brain.goals.destination_generator",
    "brain.goals.frontier_picker",
    "brain.goals.hug_shore",
    "brain.goals.junction_detector",
    "brain.goals.junction_graph",
    "brain.goals.m_line_monitor",
    "brain.goals.shore_segment",
    "brain.goals.uturn_recovery",
    "brain.steering",
    "tools.run_ai_nav_live",
    "tools.bank_tracer",
    "vision.frame_shift",
    "vision.minimap_navigation_view",
    "vision.minimap_reader",
    "vision.navigation_view",
    "vision.shoreline_reader",
    "vision.water_skeleton",
    # the replay harness: real pipeline over recorded frames, and the only navigation import
    # some of its tests make.
    "tests.tactical_scenarios",
)

# The dispatcher path: ports, markets, villages, the world map, missions.
BUSINESS = (
    "brain.activities",
    "brain.barter_command",
    "brain.barter_mission",
    "brain.barter_mission_live",
    "brain.barter_quantity",
    "brain.barter_runner",
    "brain.barter_strategy",
    "brain.barter_task",
    "brain.dispatcher",
    "brain.event_selling",
    "brain.gathering_solver",
    "brain.goal_context",
    "brain.intents",
    "brain.jettison_planner",
    "brain.market_context",
    "brain.market_ledger",
    "brain.market_state",
    "brain.mission",
    "brain.mission_progress",
    "brain.mission_runner",
    "brain.plan",
    "brain.plan_actions",
    "brain.plan_loop",
    "brain.plan_runtime",
    "brain.port_context",
    "brain.replan",
    "brain.run_goal",
    "brain.sell_port",
    "brain.supply_guard",
    "brain.supply_planner",
    "brain.task_conditions",
    "brain.task_loop",
    "brain.trade_policy",
    "brain.village_context",
    "brain.world_map_context",
    "actions.barter_executor",
    "actions.barter_panel",
    "actions.barter_reader",
    "actions.buy_materials",
    "actions.market_actions",
    "actions.overflow_dialog",
    "actions.port_panel",
    "actions.route_execution",
    "actions.sail_actions",
    "actions.sell_goods",
    "actions.task_runner",
    "actions.trade_events",
    "actions.trade_list_stitch",
    "actions.village_check",
    "actions.village_remote_reader",
    "actions.world_map",
    "actions.world_map_gather",
    "actions.world_map_nav",
    "run_barter",
    "run_task",
)

# Touching one of these is touching BOTH groups, whatever the diff looks like. This is the
# list today's regression came out of: `read_speed` was made more careful for the business
# run and doubled the navigation tick, and `vision/sea_hud.py` is the file it happened in.
SHARED_MODULES = (
    "brain/perceive.py",
    "brain/dispatcher.py",
    "vision/sea_hud.py",
    "vision/omniparser.py",
)

_MODULE = re.compile(r"\b(?:brain|actions|vision|tools|tests|run_barter|run_task)(?:\.[a-z_][a-z_0-9]*)*")


def _names(source: str) -> set[str]:
    return set(_MODULE.findall(source))


def _touches(names: set[str], prefixes: tuple[str, ...]) -> bool:
    return any(n == p or n.startswith(p + ".") for n in names for p in prefixes)


def classify(source: str) -> str | None:
    """`"navigation"`, `"business"`, or None for shared — which runs in both groups."""
    names = _names(source)
    nav, bus = _touches(names, NAVIGATION), _touches(names, BUSINESS)
    if nav and not bus:
        return "navigation"
    if bus and not nav:
        return "business"
    return None
