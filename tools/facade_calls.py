#!/usr/bin/env python
"""What went through the barter_mission_live façade during a run, and who asked.

The façade is a task module that still reaches the UI. Rather than guess whether that costs
anything, every call through it logs `[facade] <fn> <- <caller>`; this reads a run's log back
and says what was actually used.

A QUIET REPORT IS THE EVIDENCE THE DEFERRAL IS SAFE. A noisy one names the first thing to
move — and the CALLERS matter more than the counts: a façade function called only from inside
the façade is internal plumbing, while one called from `barter_command` or `barter_task` is a
clean module reaching the UI through it.

    python tools/facade_calls.py <logfile> [...]
"""
import collections
import pathlib
import re
import sys

LINE = re.compile(r"\[facade\] (\S+) <- (.+?)\s*$")


def main(paths) -> int:
    by_fn = collections.Counter()
    by_pair = collections.Counter()
    for path in paths:
        for line in pathlib.Path(path).read_text(errors="replace").splitlines():
            m = LINE.search(line)
            if m:
                fn, caller = m.group(1), m.group(2)
                by_fn[fn] += 1
                by_pair[(fn, caller.split(" in ")[0])] += 1

    if not by_fn:
        print("No façade calls in this run — nothing went through it.")
        return 0

    print(f"{sum(by_fn.values())} call(s) through the façade\n")
    print("BY FUNCTION")
    for fn, n in by_fn.most_common():
        print(f"  {n:5}  {fn}")

    print("\nBY CALLER  (a caller outside barter_mission_live.py is the second hop)")
    for (fn, caller), n in by_pair.most_common():
        outside = "  <-- outside the façade" if "barter_mission_live" not in caller else ""
        print(f"  {n:5}  {fn:34} {caller}{outside}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1:]))
