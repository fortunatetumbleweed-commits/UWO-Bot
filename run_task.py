# run_task.py — execute a bot task from a YAML task file
#
# Usage:
#   python run_task.py tasks/trade_ceylon_aceh.yaml
#   python run_task.py tasks/trade_ceylon_aceh.yaml --dry-run

import os
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import sys
from memory.logger import setup_logging
setup_logging()

if len(sys.argv) < 2:
    print("Usage: python run_task.py <task_file> [--dry-run]")
    sys.exit(1)

task_file = sys.argv[1]
dry_run   = "--dry-run" in sys.argv

if not dry_run:
    # Lock auto-rotate off / assert canonical landscape before any tapping —
    # hardcoded UI coords are orientation-specific (see actions/orientation.py).
    from actions.orientation import ensure_canonical_orientation
    ensure_canonical_orientation()

from actions.task_runner import run_task

report = run_task(task_file, dry_run=dry_run)
print(f"\nTask complete. Rounds: {report.rounds_completed}  "
      f"Profit: {report.total_profit:+,} ducats")
