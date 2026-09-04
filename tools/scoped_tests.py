#!/usr/bin/env python
"""Which tests does a change actually need? Derived from what it touched.

CLAUDE.md's rule is to run the tests for the modules you edited, and escalate to the full
suite only for a major refactor. That rule is only usable if "the tests for these modules" can
be answered without guessing — and guessing is how two whole test files covering the departure
were missed on 2026-08-28, one of them holding a regression.

Usage:
    python tools/scoped_tests.py                 # tests for the uncommitted change
    python tools/scoped_tests.py HEAD~1          # tests for the last commit
    python tools/scoped_tests.py 5be908c..HEAD   # tests for a range
    python tools/scoped_tests.py --run [rev]     # ...and run them

A test is in scope when it names a changed module, imports it, or patches something in it.
That is deliberately generous: the cost of one extra test file is seconds, and the cost of a
missed one was a 55-minute suite and a regression that reached a commit.
"""
import pathlib
import subprocess
import sys


def changed_sources(rev: str) -> list:
    if ".." in rev:
        cmd = ["git", "diff", "--name-only", rev]
    elif rev:
        cmd = ["git", "diff", "--name-only", f"{rev}..HEAD"]
    else:
        cmd = ["git", "status", "--porcelain"]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout.splitlines()
    names = [ln[3:].strip() if not rev else ln.strip() for ln in out]
    return [n for n in names if n.endswith(".py") and not n.startswith("tests/")]


def scope(sources) -> list:
    """Every test file that names one of these modules, by dotted path or by basename."""
    dotted = {s[:-3].replace("/", ".") for s in sources}
    bases = {s.rsplit("/", 1)[-1][:-3] for s in sources}
    hits = []
    for t in sorted(pathlib.Path("tests").glob("test_*.py")):
        text = t.read_text(errors="replace")
        if any(d in text for d in dotted) or any(f"import {b}" in text for b in bases):
            hits.append(str(t))
    return hits


def main(argv) -> int:
    run = "--run" in argv
    argv = [a for a in argv if a != "--run"]
    rev = argv[0] if argv else ""

    sources = changed_sources(rev)
    if not sources:
        print("No source files changed — nothing to scope.")
        return 0
    tests = scope(sources)

    print(f"{len(sources)} source file(s) changed:")
    for s in sources:
        print(f"    {s}")
    print(f"\n{len(tests)} test file(s) in scope "
          f"(of {len(list(pathlib.Path('tests').glob('test_*.py')))}):")
    for t in tests:
        print(f"    {t}")

    if not run:
        print("\nRun them with:\n    python -m pytest " + " ".join(tests) + " -q")
        return 0
    return subprocess.run([sys.executable, "-m", "pytest", *tests, "-q"]).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
