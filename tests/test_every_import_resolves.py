"""Every `from <our module> import <name>` names something that exists.

`brain/barter_mission_live._sail_until_ashore` did `from brain.goals.sail_to import
ArriveAshore`. That module has no ArriveAshore and never did — the goal lives with the
activity that serves it. So the call raised ImportError on its first line, and since it is
the arrival wait for a route leg, EVERY route tail was broken: `execute_route` set sail and
the wait died immediately.

Nothing caught it. A module-level import would have failed at import and been obvious; a
function-level one fails only when the function runs, and the tests stub the executors, so
the line was never executed. Python does not check it, linters here do not run on it, and
the code read perfectly well.

This resolves names statically — by parsing the target module, not importing it — so it costs
nothing and has no side effects on a codebase that loads vision models at import.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = ("actions", "brain", "vision", "capture", "memory")

FILES = sorted(
    str(p.relative_to(ROOT))
    for pkg in PACKAGES
    for p in (ROOT / pkg).rglob("*.py")
)


def _module_path(dotted: str):
    """The file for `a.b.c`, or None if it is not one of ours."""
    if not dotted.startswith(PACKAGES):
        return None
    as_file = ROOT / (dotted.replace(".", "/") + ".py")
    if as_file.exists():
        return as_file
    as_pkg = ROOT / dotted.replace(".", "/") / "__init__.py"
    return as_pkg if as_pkg.exists() else None


def _bound_by(target) -> set:
    """Names bound by an assignment target, INCLUDING tuple unpacking.

    `HOLD, LEFT_SHORT, ... = range(7)` binds seven names, and a checker that only looks at
    `ast.Name` sees none of them — it would report every one as missing.
    """
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        out = set()
        for el in target.elts:
            out |= _bound_by(el)
        return out
    return set()


def _names_defined_in(path: pathlib.Path) -> set:
    """Every top-level name a module binds: def, class, assignment, or its own imports."""
    out = set()
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return out
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                out.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                out |= _bound_by(t)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
        elif isinstance(node, (ast.If, ast.Try)):
            # try/except ImportError and `if TYPE_CHECKING:` both bind at top level
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    out.add(sub.name)
                elif isinstance(sub, (ast.Import, ast.ImportFrom)):
                    for a in sub.names:
                        out.add(a.asname or a.name.split(".")[0])
                elif isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        out |= _bound_by(t)
    return out


@pytest.mark.parametrize("path", FILES)
def test_every_from_import_names_something_real(path):
    src = (ROOT / path).read_text()
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        pytest.fail(f"{path} does not parse: {exc}")

    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level:   # skip relative imports
            continue
        target = _module_path(node.module or "")
        if target is None:
            continue                       # third-party or stdlib — not ours to check
        defined = _names_defined_in(target)
        if not defined:
            continue                       # unparseable target; nothing to say
        if "__getattr__" in defined:
            continue                       # PEP 562: the module synthesises attributes
        for alias in node.names:
            if alias.name == "*":
                continue
            if alias.name not in defined:
                missing.append(f"{node.module}.{alias.name} (line {node.lineno})")

    assert not missing, (
        f"{path} imports names that do not exist:\n  " + "\n  ".join(missing) + "\n"
        "A function-level import fails only when the function runs, so this is not caught "
        "by importing the module or by any test that stubs the caller."
    )
