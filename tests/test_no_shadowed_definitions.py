"""No module may define the same top-level name twice.

`actions/sail_actions.py` defined `_label_matches` TWICE: a whole-word boolean matcher for
dialog buttons at line 203, and a fuzzy float matcher for occluded map labels at 4333. The
later one won at import time, so `_find_button` — the bot's core button finder — was silently
running map semantics with its two arguments REVERSED. It went unnoticed because two separate
test files each tested a different function under the one name, and the one testing the
shadow passed.

Python does not warn about this. Nothing does. So this test does.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = ("actions", "brain", "vision", "capture")

FILES = sorted(
    str(p.relative_to(ROOT))
    for pkg in PACKAGES
    for p in (ROOT / pkg).rglob("*.py")
)


def _top_level_definitions(path):
    """Every name bound by a top-level def/class, with the lines that bind it."""
    tree = ast.parse((ROOT / path).read_text())
    seen = {}
    for node in tree.body:                       # top level ONLY — a nested def is not a clash
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            seen.setdefault(node.name, []).append(node.lineno)
    return seen


@pytest.mark.parametrize("path", FILES)
def test_no_top_level_name_is_defined_twice(path):
    dupes = {n: ls for n, ls in _top_level_definitions(path).items() if len(ls) > 1}
    assert not dupes, (
        f"{path} defines the same top-level name more than once: {dupes}.\n"
        "The later definition silently replaces the earlier one, and every caller of the "
        "earlier gets the later — with whatever signature and semantics it happens to have."
    )
