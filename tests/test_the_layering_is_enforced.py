"""The task layer may not reach for the UI. Enforced, because Python will not enforce it.

    task ──work order──> dispatch ──intent(goal)──> activity ──> ui

The task runner is the COMPANY'S MANAGER: it decides the mission sequence and states work
orders. The dispatcher is the UI MASTER: it perceives, clears what is in the way, turns an
order into an intent, and runs the activity that serves it.

Python has no package-private symbols, so this is a test over the import graph. It is a
RATCHET, not a gate: 43 UI imports across 2 files existed on 2026-08-28, they are recorded in
`brain.layers.KNOWN_VIOLATIONS` as explicit debt, and anything NOT recorded fails.

Why it matters, concretely: `run_barter_command` perceived, cleared blockers, reoriented and
ran a UI recipe directly — 13 calls — and the result was `open_world_map` holding an OS lock
screen, a daily-news popup and an Investment Season banner it had no vocabulary for. It read
"Season" out of the banner, called it a port name, and reported "Overworld confirmed".
"""
import ast
import pathlib

import pytest

from brain.layers import (ALLOWED_FOR_TASK, KNOWN_VIOLATIONS, TASK_MODULES,
                          UI_PACKAGES, is_ui_module, PERCEPTION_CALLS,
                          DRIVER_CALLS, PERCEPTION_BUDGET, DRIVER_BUDGET)


def _ui_imports(path: str):
    """(module, lineno) for every UI package imported by `path`."""
    p = pathlib.Path(path)
    if not p.exists():
        return []
    out = []
    for node in ast.walk(ast.parse(p.read_text())):
        mod = None
        if isinstance(node, ast.ImportFrom) and node.module:
            mod = node.module
        elif isinstance(node, ast.Import):
            mod = node.names[0].name
        if mod and is_ui_module(mod):
            out.append((mod, node.lineno))
    return out


@pytest.mark.parametrize("path", TASK_MODULES)
def test_no_new_ui_import_from_the_task_layer(path):
    """A task module reaching into actions/ vision/ capture/ has stepped around the
    dispatcher. New ones fail here; the existing ones are named debt."""
    new = [(mod, line) for mod, line in _ui_imports(path)
           if (path, mod) not in KNOWN_VIOLATIONS]
    assert not new, (
        f"{path} imports UI directly: {new}.\n"
        "The task layer states WORK ORDERS and calls run_goal(goal); the dispatcher\n"
        "perceives, routes and runs the activity. If this import is genuinely needed,\n"
        "the work belongs in an activity — see docs/activity_as_context.md §14.3."
    )


def test_the_ratchet_only_tightens():
    """An entry that no longer describes reality must come OUT, so the debt cannot quietly
    grow behind a stale allowlist."""
    stale = [pair for pair in KNOWN_VIOLATIONS
             if pair[1] not in {m for m, _l in _ui_imports(pair[0])}]
    assert not stale, (
        f"these are recorded as debt but no longer exist: {stale}.\n"
        "Delete them from brain.layers.KNOWN_VIOLATIONS — the ratchet only tightens."
    )


def test_the_debt_is_shrinking_or_at_least_known():
    """A number to watch. It should fall as nodes move to work orders; it must never rise.

    21 import statements across 8 distinct (module, package) pairs — 46/19 before the recipe
    step moved to `brain/barter_runner.py`, and one pair lower again once the hold read went
    with it. It only ever goes down. The pair count was
    first recorded as 18 because the original matcher required a trailing dot and was blind
    to `from actions import ui` — three real violations hiding behind a guard that was
    believed."""
    total = sum(len([1 for mod, _l in _ui_imports(p) if (p, mod) in KNOWN_VIOLATIONS])
                for p in TASK_MODULES)
    assert total <= 21, f"UI imports from the task layer rose to {total} (was 21)"


def test_what_the_task_layer_MAY_reach_for():
    """Goals are data. Naming `Barter('Birch Tree', 'Svear Village')` is not touching a
    screen, which is why the goal types are importable and the readers are not."""
    assert "brain.run_goal" in ALLOWED_FOR_TASK
    assert not any(is_ui_module(a) for a in ALLOWED_FOR_TASK)


def test_the_guard_catches_a_BARE_package_import():
    """`from actions import ui` has module="actions" — no dot. The first version of this
    guard matched on the prefix "actions." and let it straight through; a probe that added
    exactly that import watched the test pass. A guard that misses the simplest form of what
    it guards against is worse than none, because it is believed."""
    assert is_ui_module("actions")
    assert is_ui_module("actions.ui")
    assert is_ui_module("vision.omniparser")
    assert not is_ui_module("actionsomething")
    assert not is_ui_module("brain.run_goal")


def test_activities_may_reach_the_ui_freely():
    """The rule is directional. An activity's whole job is to work one screen — localised
    interpretation — so it reaching for readers and primitives is the design, not a breach."""
    market = _ui_imports("brain/activities/market.py")
    village = _ui_imports("brain/activities/village.py")
    assert market or village, "activities are expected to use the UI layer"


# ---------------------------------------------------------------------------
# The task runner is PASSIVE (user, 2026-08-28):
#   "It is only when the dispatcher settles to a world that it consults the task manager to
#    get the work order... The task runner only does one thing: provide the work order to the
#    dispatcher, and update task status, not calling anything."
# ---------------------------------------------------------------------------

def _calls(path, names):
    """Every call to one of `names` in `path`, as (name, lineno)."""
    found = []
    for node in ast.walk(ast.parse(pathlib.Path(path).read_text())):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        nm = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
        if nm in names:
            found.append((nm, node.lineno))
    return found


@pytest.mark.parametrize("path", sorted(PERCEPTION_BUDGET))
def test_the_task_runner_does_not_go_and_look(path):
    """Half 1: it does not CALL DOWN to perceive.

    The task runner is HANDED `state`; it never fetches it. Reaching for the game itself is
    how a conclusion gets stored — it would be reading a world the dispatcher owns. Ratcheted,
    because it is not clean yet; every one of these is a node that still has to move."""
    found = _calls(path, PERCEPTION_CALLS)
    assert len(found) <= PERCEPTION_BUDGET[path], (
        f"{path} perceives for itself {len(found)}x (budget {PERCEPTION_BUDGET[path]}): {found}.\n"
        "The task runner is passive — read `state`, or return a work order that asks an "
        "activity to look."
    )


@pytest.mark.parametrize("path", sorted(DRIVER_BUDGET))
def test_the_task_runner_does_not_drive_the_loop(path):
    """Half 2: it does not CALL UP.

    Being consulted is the whole of its participation. If it can call the loop that calls it,
    it is the driver again under a different name. `barter_command.py` is gated at 0;
    `barter_mission_live.py` still calls run_goal 6x and is merely frozen."""
    found = _calls(path, DRIVER_CALLS)
    assert len(found) <= DRIVER_BUDGET[path], (
        f"{path} drives the loop {len(found)}x (budget {DRIVER_BUDGET[path]}): {found}.\n"
        "The dispatcher consults the task runner, never the other way round."
    )


def test_the_budgets_are_not_stale():
    """A ceiling above the real count is a guard that is believed and does nothing — the same
    failure as the missing trailing dot. If a count drops, lower the budget."""
    for path, budget in {**PERCEPTION_BUDGET}.items():
        actual = len(_calls(path, PERCEPTION_CALLS))
        assert actual == budget, f"{path}: {actual} perception calls, budget says {budget} — retighten"


# ---------------------------------------------------------------------------
# Modules that have finished migrating. These are GATES, not ratchets.
# ---------------------------------------------------------------------------

GRADUATED = ("brain/barter_command.py", "brain/barter_runner.py",
              "brain/voyage_runner.py", "brain/sail_runner.py",
              "brain/mission_runner.py")


@pytest.mark.parametrize("path", GRADUATED)
def test_a_graduated_module_stays_clean(path):
    """No UI import, no perception, no driving — and no way back.

    `barter_command.py` started at 43 UI imports and 12 perception calls, including a
    `perceive()` for a log line and a sixty-line ladder that read the fleet three times. It
    now reaches the game only by returning work orders. A ratchet was right while the number
    was falling; once it is zero the guard should be a gate, because the next violation is not
    a regression in degree but a return to the old shape.
    """
    ui = [m for m, _ in _ui_imports(path)]
    perception = [n for n, _ in _calls(path, PERCEPTION_CALLS)]
    driving = [n for n, _ in _calls(path, DRIVER_CALLS)]
    assert not ui, f"{path} imports UI again: {ui}"
    assert not perception, f"{path} perceives again: {perception}"
    assert not driving, f"{path} drives the loop again: {driving}"


# ---------------------------------------------------------------------------
# The SECOND hop. A guard that looks one hop is defeated by a façade.
# ---------------------------------------------------------------------------

FACADES = {
    # A task module that still fronts for the UI, and who reaches through it. Every entry is
    # a module whose UI has not yet been moved to `actions/`; the fix is always the same —
    # move the UI code to the UI layer, not to hide the hop better.
    "brain/barter_mission_live.py": 8,
}


def test_a_task_module_fronting_for_the_ui_is_declared():
    """`brain/barter_command.py` measured 0 UI imports while reaching the whole barter panel
    through `barter_mission_live`, which imported thirteen UI modules on its behalf. The gate
    said CLEAN and the module was not — so the second hop is counted, and every façade has to
    be named here rather than discovered."""
    from brain.layers import is_ui_module

    actual = {}
    for path in TASK_MODULES:
        if not pathlib.Path(path).exists():
            continue
        ui = {m for m, _ in _ui_imports(path)}
        if ui:
            actual[path] = len(ui)
    assert actual == FACADES, (
        f"the task modules importing UI are {actual}, declared {FACADES}.\n"
        "A new one means UI code was added to the task layer; a smaller number means a "
        "façade shrank and this figure should come down with it."
    )


# Who still reaches the UI through a façade. A ratchet, not a gate: emptying it means moving
# the rest of `barter_mission_live`'s market and sailing UI into `actions/`, which is the next
# extraction. Declared so it cannot grow, and so nobody reads GRADUATED as "reaches no UI".
KNOWN_SECOND_HOP = {
    "brain/barter_command.py": {"brain/barter_mission_live.py"},
    "brain/barter_runner.py": set(),
    "brain/voyage_runner.py": set(),
    "brain/sail_runner.py": set(),
    "brain/mission_runner.py": set(),
}


@pytest.mark.parametrize("path", GRADUATED)
def test_a_graduated_module_does_not_reach_ui_through_a_facade(path):
    """Clean directly is not clean if it imports something that is not."""
    import ast

    from brain.layers import reaches_ui_through

    def imports_of(p):
        out = set()
        for node in ast.walk(ast.parse(pathlib.Path(p).read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                out.add(node.module)
            elif isinstance(node, ast.Import):
                out.add(node.names[0].name)
        return out

    via = reaches_ui_through(path, imports_of)
    assert via == KNOWN_SECOND_HOP[path], (
        f"{path} reaches UI through {sorted(via)}, declared {sorted(KNOWN_SECOND_HOP[path])}.\n"
        "A new entry means UI arrived by the back door; a smaller set means a façade was "
        "cleared and this declaration should shrink with it."
    )


# ---------------------------------------------------------------------------
# An activity that WORKS must say which orders it serves.
# ---------------------------------------------------------------------------

# The activities that legitimately declare no GOALS. Each is in the way of EVERY goal and
# must run whatever the order is — that is what makes it a clearing activity, and it is why
# declaring none is the right answer for these and the wrong one for everybody else.
CLEARING = {"idle_lock", "transient", "unrecognized_chromed_screen"}


def test_every_activity_declares_where_it_serves():
    """No activity's state name may live in the registry instead of on the activity.

    `idle_lock` and `transient` used to be assigned in `default_activities()` as
    `registry["idle_lock"] = [...]` — a second place that knew the state's name, and an
    ASSIGNMENT where every other entry appends. An activity declaring SERVES = ("idle_lock",)
    would have been silently dropped: the exact overwrite bug the setdefault/append beside it
    exists to prevent, reintroduced two lines below the comment explaining it.
    """
    from brain.run_goal import default_activities

    for where, activities in default_activities().items():
        for activity in activities:
            serves = getattr(activity, "SERVES", None)
            assert serves, f"{activity.name} declares no SERVES but is registered at {where!r}"
            assert where in serves, (
                f"{activity.name} is registered at {where!r} but declares SERVES={serves}"
            )


def test_a_working_activity_declares_its_goals():
    """`VillageActivity` and `HarborActivity` both declared none, so the dispatcher treated
    them as clearing activities: they absorbed every goal in their states and answered BLOCKED
    to orders they cannot fill. The village swallowed `ClearOfTheVillage` instead of letting
    it route to a Back, and the harbour would have swallowed anything at all. Nothing catches
    this by reading the code — an omitted attribute looks exactly like a considered choice."""
    from brain.run_goal import default_activities

    undeclared = set()
    for activities in default_activities().values():
        for activity in activities:
            if not getattr(activity, "GOALS", None) and activity.name not in CLEARING:
                undeclared.add(activity.name)
    assert not undeclared, (
        f"these activities declare no GOALS and are not clearing activities: "
        f"{sorted(undeclared)}.\n"
        "The dispatcher will hand them every goal in the states they serve."
    )
