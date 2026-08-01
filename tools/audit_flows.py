#!/usr/bin/env python
"""
Audit flows.json for completeness per the project guideline (CLAUDE.md →
"Flow Completeness & Self-Correction").

A flow is COMPLETE only when:
  1. Its terminal_state is a recognized state (foundational or learned), AND
  2. At least one of its steps is categorised as a positive transaction.

Otherwise the flow is INCOMPLETE.  Cancellations are runtime events, not an
authoring outcome, so this audit does not produce STATUS_CANCELLED.

Usage:
    python -m tools.audit_flows               # report only (no changes)
    python -m tools.audit_flows --apply       # write status back to flows.json

The tool is idempotent — re-running with --apply on an already-tagged file
produces the same result.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make sure the repo root is on the path when invoked as a script.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from brain.flow_completeness import (
    STATUS_COMPLETE, STATUS_INCOMPLETE,
    categorize_step, has_positive_transaction,
)

_FLOWS_PATH  = _REPO / "memory" / "knowledge" / "fsm" / "flows.json"
_STATES_PATH = _REPO / "memory" / "knowledge" / "fsm" / "states.json"


def _load_known_state_ids() -> set[str]:
    if not _STATES_PATH.exists():
        return set()
    return {s["id"] for s in json.loads(_STATES_PATH.read_text(encoding="utf-8"))}


def _classify_flow(flow: dict, known_states: set[str]) -> tuple[str, dict]:
    """
    Return (status, summary) for a single flow record.
    summary contains the diagnostic fields used by the report.
    """
    steps = flow.get("steps", []) or []
    categories = [categorize_step(s) for s in steps]
    has_txn = has_positive_transaction(steps)
    terminal = flow.get("terminal_state") or ""
    terminal_recognized = bool(terminal) and terminal in known_states

    status = STATUS_COMPLETE if (has_txn and terminal_recognized) else STATUS_INCOMPLETE
    return status, {
        "step_count":          len(steps),
        "categories":          categories,
        "has_transaction":     has_txn,
        "terminal_state":      terminal,
        "terminal_recognized": terminal_recognized,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument(
        "--apply", action="store_true",
        help="Write status / categorisation back to flows.json (default: report only)",
    )
    parser.add_argument(
        "--flows-path", default=str(_FLOWS_PATH),
        help="Path to flows.json (default: %(default)s)",
    )
    args = parser.parse_args()

    flows_path = Path(args.flows_path)
    if not flows_path.exists():
        print(f"error: flows file not found: {flows_path}", file=sys.stderr)
        return 2

    flows = json.loads(flows_path.read_text(encoding="utf-8"))
    known_states = _load_known_state_ids()

    # Strip any pre-existing `category` tags so this audit is the authority
    # on derivation.  Without this, a previous run that mis-categorised a
    # step would persist forever (categorize_step honours explicit tags).
    for flow in flows:
        for step in flow.get("steps", []) or []:
            step.pop("category", None)

    incomplete = []
    complete   = []
    for flow in flows:
        fid = flow.get("id", "<no id>")
        status, summary = _classify_flow(flow, known_states)
        record = (fid, status, summary)
        if status == STATUS_COMPLETE:
            complete.append(record)
        else:
            incomplete.append(record)

        if args.apply:
            flow["status"] = status
            flow["positive_transaction_count"] = max(
                int(flow.get("positive_transaction_count", 0)),
                1 if summary["has_transaction"] else 0,
            )
            flow["terminal_state_recognized"] = summary["terminal_recognized"]
            # Tag each step with its derived category so the file is
            # self-describing.  Existing categories are preserved.
            for step, cat in zip(flow.get("steps", []), summary["categories"]):
                step.setdefault("category", cat)

    print(f"flows.json: {len(flows)} flows  ({len(complete)} complete, {len(incomplete)} incomplete)")
    print()
    print("=== COMPLETE ===")
    for fid, _status, s in complete:
        print(f"  ✓  {fid}  steps={s['step_count']}  terminal={s['terminal_state']!r}")

    print()
    print("=== INCOMPLETE ===")
    for fid, _status, s in incomplete:
        reasons = []
        if not s["has_transaction"]:
            reasons.append("no positive transaction")
        if not s["terminal_recognized"]:
            reasons.append(f"terminal_state={s['terminal_state']!r} not recognized")
        joined = "; ".join(reasons) if reasons else "?"
        cats = ", ".join(s["categories"]) if s["categories"] else "<no steps>"
        print(f"  ✗  {fid}")
        print(f"       reason: {joined}")
        print(f"       step categories: [{cats}]")

    if args.apply:
        flows_path.write_text(json.dumps(flows, indent=2, ensure_ascii=False), encoding="utf-8")
        print()
        print(f"wrote status / categories back to {flows_path}")
    else:
        print()
        print("(report only — re-run with --apply to write status back to flows.json)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
