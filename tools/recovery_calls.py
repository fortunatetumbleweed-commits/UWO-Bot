#!/usr/bin/env python
"""Summarise every recover_to_port_overworld call in a run — who asked, and how it ended.

Run after each session. The point is to retire the ~40 call sites on evidence: which
callers actually need a recovery, which get one they never use, and which end somewhere
that was fine all along (`sea` after a departure is the case that killed the run on
2026-08-27 — the fleet was sailing to Tripoli exactly as ordered).

    python tools/recovery_calls.py [session_dir]      # default: the newest session
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

ENTER = re.compile(r"\[recovery-call\] (?:ENTER|DEPRECATED) .*? ← (?P<caller>.*?) \(home_port=(?P<hp>.*?), "
                   r"timeout=(?P<to>[\d.]+)\)")
EXIT = re.compile(r"\[recovery-call\] EXIT\s+(?P<verdict>reached|DID NOT reach|raised \w+).*?"
                  r"(?:ended at (?P<where>\S+?) )?after (?P<secs>[\d.]+)s ← (?P<caller>.*)$")
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        session = Path(argv[1])
    else:
        sessions = sorted(Path("data/sessions").glob("trace_*"), key=lambda p: p.stat().st_mtime)
        if not sessions:
            print("no sessions found under data/sessions/")
            return 1
        session = sessions[-1]
    log = session / "run.log"
    if not log.exists():
        print(f"no run.log in {session}")
        return 1

    calls, by_caller = [], defaultdict(lambda: {"n": 0, "failed": 0, "secs": 0.0, "ends": set()})
    pending = None
    for line in ANSI.sub("", log.read_text(errors="replace")).splitlines():
        if m := ENTER.search(line):
            pending = m.groupdict()
        elif m := EXIT.search(line):
            e = m.groupdict()
            caller = e["caller"]
            row = {"caller": caller, "verdict": e["verdict"],
                   "where": (e.get("where") or "?").strip("'\""),
                   "secs": float(e["secs"]),
                   "timeout": (pending or {}).get("to", "?")}
            calls.append(row)
            agg = by_caller[caller]
            agg["n"] += 1
            agg["secs"] += row["secs"]
            agg["ends"].add(row["where"])
            if e["verdict"] != "reached":
                agg["failed"] += 1
            pending = None

    print(f"session: {session.name}")
    if not calls:
        print("  no [recovery-call] lines — either the run predates the call logging, "
              "or recover_to_port_overworld was never called (which is the goal).")
        return 0

    print(f"  {len(calls)} call(s), {sum(1 for c in calls if c['verdict'] != 'reached')} "
          f"that did not reach port_overworld, "
          f"{sum(c['secs'] for c in calls):.0f}s total\n")
    print(f"  {'caller':46} {'n':>3} {'fail':>5} {'secs':>7}  ended at")
    for caller, a in sorted(by_caller.items(), key=lambda kv: -kv[1]["secs"]):
        print(f"  {caller[:46]:46} {a['n']:>3} {a['failed']:>5} {a['secs']:>7.1f}  "
              f"{', '.join(sorted(a['ends']))}")

    # `sea` is the one worth naming: it is a legitimate overworld, and treating it as a
    # failure is what aborted the 2026-08-27 run mid-voyage.
    at_sea = [c for c in calls if c["where"] in ("sea", "sea_cinematic")]
    if at_sea:
        print(f"\n  ⚠ {len(at_sea)} call(s) ended at sea and were counted as FAILURES. "
              "Sea is a valid overworld;\n    after a departure it is the destination, "
              "not a place to be recovered from.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
