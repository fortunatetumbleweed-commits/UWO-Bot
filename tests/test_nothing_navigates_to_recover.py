"""A goal that cannot read a screen must not decide where the fleet should be.

`recover_to_port_overworld` owns a 20-attempt loop, treats one destination as its only
success, and with a `home_port` — which all nine call sites pass — reaches
`_recover_from_sea`, whose first branch is `sail_to_port(home_port)`: full navigation.

Live 2026-08-27: the fleet committed a departure to Svear Village, the departure cinematic
came up, `sail_to` called it "Unexpected state 'transient'" and planned back to
port_overworld. The recovery sailed the fleet back to Barcelona, cancelling the voyage — and
logged SUCCESS, because reaching port_overworld is its only success test.
"""
import inspect
import re

from brain.goals import sail_to as sail_to_mod


def _code(obj) -> str:
    """Source with comments and docstrings stripped — assert on CODE, not on prose.

    Without this the tests matched the comment that EXPLAINS the removed call and reported
    the very thing being forbidden as still present.
    """
    lines = []
    for line in inspect.getsource(obj).splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append(line.split("  #")[0])
    body = "\n".join(lines)
    return re.sub(r'("""|\'\'\')(?:.|\n)*?\1', "", body)


def test_sail_to_does_not_plan_or_navigate_to_recover():
    src = _code(sail_to_mod.SailToGoal._handle_unknown)
    assert "plan_to(" not in src, "planning to a destination is how the fleet got sailed"
    assert "sail_to_port(" not in src
    assert "exit_current_screen" in src, "one exit, then re-perceive"


def test_a_transient_is_not_unexpected():
    """It is this goal's own committed action still landing — the idle lock's shape."""
    src = _code(sail_to_mod.SailToGoal._handle_unknown)
    assert '"transient"' in src and '"loading"' in src


def test_plan_to_has_no_callers_left():
    """The only live route into the recovery loop.

    Parsed with the AST, not grepped: source text matches prose too, and the first version
    of this test failed on the very COMMENT that explains the removal.
    """
    import ast
    import pathlib

    hits = []
    for f in list(pathlib.Path("brain").rglob("*.py")) + list(pathlib.Path("actions").rglob("*.py")):
        try:
            tree = ast.parse(f.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name == "plan_to":
                    hits.append(f"{f}:{node.lineno}")
    assert hits == [], f"plan_to is called again from {hits}"


def test_the_recovery_announces_itself_as_deprecated():
    """The remaining call sites have never been observed firing. They are left in place and
    made LOUD rather than deleted blind, so any that fire name themselves and can be retired
    on evidence — `python tools/recovery_calls.py` after each run."""
    src = inspect.getsource(__import__("brain.recovery", fromlist=["x"]).recover_to_port_overworld)
    assert "DEPRECATED" in src
