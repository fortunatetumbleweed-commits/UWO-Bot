# run_step.py — run a single trade step manually for testing
#
# Usage:
#   python run_step.py sell              # sell all cargo at current port
#   python run_step.py buy               # buy recommended goods at current port
#   python run_step.py sail <port>       # sail to named port
#   python run_step.py sell --dry-run    # dry run (no ADB)
#
# Each command reports what it did and whether it succeeded.

import os
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import sys
from memory.logger import setup_logging
setup_logging()

from loguru import logger

dry_run = "--dry-run" in sys.argv
args = [a for a in sys.argv[1:] if not a.startswith("--")]

if not args:
    print("Usage:")
    print("  python run_step.py sell")
    print("  python run_step.py buy")
    print("  python run_step.py sail <destination>")
    print("  (add --dry-run to skip ADB)")
    sys.exit(1)

action = args[0].lower()

from actions.task_runner import run_sell_all, run_buy_all, run_sail_to, TaskReport, _current_port
from actions.sail_actions import where_am_i

report = TaskReport(task_name=f"step:{action}")

if not dry_run:
    loc = where_am_i()
    port = loc.get("port") or _current_port() or "?"
    logger.info(f"Current location: {loc['location']!r}  port: {port!r}")
else:
    port = "?"

if action == "sell":
    result = run_sell_all(port, report, dry_run)
    logger.info(f"sell_all → ok={result.ok}  notes={result.notes!r}")

elif action == "buy":
    result = run_buy_all(port, report, dry_run)
    logger.info(f"buy_all  → ok={result.ok}  notes={result.notes!r}")

elif action == "sail":
    if len(args) < 2:
        print("Usage: python run_step.py sail <destination>")
        sys.exit(1)
    destination = args[1]
    result = run_sail_to(destination, report, dry_run)
    logger.info(f"sail_to {destination!r} → ok={result.ok}  notes={result.notes!r}")

else:
    print(f"Unknown action {action!r}. Choose: sell, buy, sail <port>")
    sys.exit(1)

status = "OK" if result.ok else "FAILED"
print(f"\n[{status}] {action}  —  {result.notes}")
