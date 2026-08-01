#!/usr/bin/env bash
# scripts/run_voyage.sh — launch a voyage task with auto-logging.
#
# Usage:  scripts/run_voyage.sh tasks/explore_nile_south.yaml
#
# Tees stdout/stderr to /tmp/voyage_<basename>_<HHMMSS>.log so the
# bash invocation itself stays simple (single-prefix allowlistable).
set -euo pipefail

TASK="${1:?usage: run_voyage.sh <task.yaml>}"

cd "$(dirname "$0")/.."

stem=$(basename "$TASK" .yaml)
log="/tmp/voyage_${stem}_$(date +%H%M%S).log"

python -c "from actions.task_runner import run_task; run_task('${TASK}')" 2>&1 | tee "$log"
