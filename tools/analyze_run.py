"""Post-run analysis: what failed, how often, and what quietly failed nearly always.

Why this exists. On 2026-08-26 the check `title_says_world_map` returned False on 85 of 89
looks — 96% — from the very first departure of the day. Every gathering leg still sailed,
because each needed only ONE lucky read and the goal retried until it got one. The bug cost
nothing until the Lisboa leg, where the coin never came up within the attempts available, and
by then it had been visible in the log for ten hours.

Nobody counted. That is the gap this closes: one failure is noise, the same failure
eighty-five times is a defect, and the only thing between them is arithmetic.

    python -m tools.analyze_run                        # today's log
    python -m tools.analyze_run memory/logs/bot_2026-08-26.log
    python -m tools.analyze_run --since 10:30          # one run's window
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

_LINE = re.compile(r"^(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)\.\d+ \| (\w+) \| (.*)$")

# `key=value` where the value is a bare True/False — the shape of a check reporting itself.
_FLAG = re.compile(r"\b([a-z_][a-z_0-9]*)=(True|False)\b")

# Phrases meaning "this attempt did not work". WARNING/ERROR lines count regardless.
_FAILURE_WORDS = (
    "could not", "cannot", "failed", "no progress", "not found", "unreadable",
    "did not register", "gave up", "giving up", "stalled", "refusing", "refused",
    "not open", "no route", "timed out", "retry", "retrying", "attempt",
    "not transient", "still not", "no positive button", "unavailable",
)


def _signature(msg: str) -> str:
    """Collapse a message to its shape, so repeats of one failure group together."""
    s = re.sub(r"\d+", "N", msg)
    s = re.sub(r"'[^']*'", "'X'", s)
    s = re.sub(r'"[^"]*"', '"X"', s)
    return re.sub(r"\s+", " ", s).strip()[:150]


def analyse(lines, since=None, until=None):
    failures, flags, levels = Counter(), defaultdict(Counter), Counter()
    first_seen, last_seen = {}, {}
    total = 0
    for raw in lines:
        m = _LINE.match(raw.rstrip("\n"))
        if not m:
            continue
        _d, hhmmss, level, msg = m.groups()
        if (since and hhmmss < since) or (until and hhmmss > until):
            continue
        total += 1
        levels[level] += 1
        for key, val in _FLAG.findall(msg):
            flags[key][val] += 1
        low = msg.lower()
        if level in ("WARNING", "ERROR", "CRITICAL") or any(w in low for w in _FAILURE_WORDS):
            sig = _signature(msg)
            failures[sig] += 1
            first_seen.setdefault(sig, hhmmss)
            last_seen[sig] = hhmmss
    return {"total": total, "levels": levels, "failures": failures, "flags": flags,
            "first": first_seen, "last": last_seen}


def report(a, top=15, flag_threshold=0.8) -> str:
    out = [f"lines analysed: {a['total']}    "
           + "  ".join(f"{k}={v}" for k, v in sorted(a["levels"].items()))]

    out.append("\nA CHECK THAT ALMOST ALWAYS FAILS IS A DEFECT, NOT NOISE")
    out.append("    SUSPECT = the flag CAN go both ways, but almost never does. A flag that is")
    out.append("    always False may simply not apply here (has_sea_hud in port); one that is")
    out.append("    False 93% of the time, having been True 3 times, is a check that works and")
    out.append("    usually does not — which is what a broken read looks like from outside.")
    rows = []
    for key, counts in a["flags"].items():
        t, f = counts.get("True", 0), counts.get("False", 0)
        n = t + f
        if n >= 3:
            rows.append((max(t, f) / n, n, key, t, f))
    if not rows:
        out.append("    (no boolean checks seen)")
    for skew, _n, key, t, f in sorted(rows, reverse=True):
        mark = "  <-- SUSPECT" if skew >= flag_threshold and min(t, f) > 0 else ""
        out.append(f"    {key:30s} True {t:4d} / False {f:4d}   ({skew:.0%} one way){mark}")

    out.append(f"\nREPEATED FAILURES (top {top}, by count)")
    if not a["failures"]:
        out.append("    (none)")
    for sig, n in a["failures"].most_common(top):
        out.append(f"    {n:4d}x  [{a['first'][sig]}..{a['last'][sig]}]  {sig}")
    return "\n".join(out)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("log", nargs="?", default=None)
    p.add_argument("--since", help="HH:MM:SS lower bound")
    p.add_argument("--until", help="HH:MM:SS upper bound")
    p.add_argument("--top", type=int, default=15)
    args = p.parse_args(argv)

    path = Path(args.log) if args.log else Path("memory/logs") / f"bot_{date.today()}.log"
    if not path.exists():
        print(f"no such log: {path}", file=sys.stderr)
        return 2
    with path.open(errors="replace") as fh:
        a = analyse(fh, since=args.since, until=args.until)
    window = (f"  window {args.since or '..'}-{args.until or '..'}"
              if (args.since or args.until) else "")
    print(f"RUN ANALYSIS  {path}{window}")
    print(report(a, top=args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
