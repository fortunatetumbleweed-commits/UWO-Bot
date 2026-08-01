"""Interactive CLI: run one tactical scenario and print per-tick outcomes.

Usage:
    python -m tools.run_tactical_scenario cairo_start
    python -m tools.run_tactical_scenario nubia_bend
    python -m tools.run_tactical_scenario --list

Prints per-tick summary + assertion result.  Useful for regression
debugging when a scenario fails.
"""
from __future__ import annotations
import argparse
import importlib
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.tactical_scenarios.scenario import run_scenario, assess, Scenario

SCENARIO_MODULES = [
    "cairo_start",
    "cairo_start_reflex",
    "nubia_bend",
    "y_fork",
    "y_tip_return",
    "lake",
    "ytip_pocket_hold",
    "ytip_reach",
]


def load_scenario(name: str) -> Scenario:
    if name not in SCENARIO_MODULES:
        raise ValueError(f"unknown scenario '{name}' (available: {SCENARIO_MODULES})")
    mod = importlib.import_module(f"tests.tactical_scenarios.{name}")
    return mod.SCENARIO


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name", nargs="?", help="scenario name")
    ap.add_argument("--list", action="store_true", help="list available scenarios")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    if args.list or not args.name:
        print("Available scenarios:")
        for n in SCENARIO_MODULES:
            sc = load_scenario(n)
            print(f"  {n:20s} — {sc.note[:60]}")
        return

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="  %(message)s",
    )

    sc = load_scenario(args.name)
    print(f"\n=== Scenario: {sc.name} ===")
    print(f"  session:   {sc.session}")
    print(f"  ticks:     {sc.start_tick}..{sc.end_tick}")
    print(f"  initial commit: {sc.initial_commit_deg}°")
    print(f"  hug_side:  {sc.hug_side}")
    print(f"  note: {sc.note}\n")

    outcomes = run_scenario(sc)

    print(f"{'t':>4} {'wp':>13}({'brg':>4}) {'edge':>6} {'d_bot':>5}  reason")
    for o in outcomes:
        wp_str = str(list(o.wp_offset)) if o.wp_offset else "None"
        brg_str = f"{o.wp_bearing:3.0f}" if o.wp_bearing is not None else " --"
        dbot_str = str(o.d_bot) if o.d_bot is not None else " -"
        print(f"{o.tick:>4} {wp_str:>13}({brg_str}°) {str(o.wp_edge):>6} {dbot_str:>5}  "
              f"{o.reason[:35]}")

    ok, failures = assess(sc, outcomes)
    print(f"\n{'PASS' if ok else 'FAIL'}")
    for f in failures:
        print(f"  - {f}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
