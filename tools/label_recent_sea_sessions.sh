#!/usr/bin/env bash
# Label every capture session in data/sessions/ — sea and non-sea.
#
# Auto-discovers sessions by scanning data/labels.jsonl for any frame
# with a label record.  That covers every session previously labelled
# by hand or by any auto-labeller.
#
# For a NEW capture that hasn't been labelled yet (no entries in
# labels.jsonl), pass the session id as a positional argument and it
# will be appended to the discovered list.
#
# Usage:
#   ANTHROPIC_API_KEY=sk-ant-... bash tools/label_recent_sea_sessions.sh
#   ANTHROPIC_API_KEY=sk-ant-... bash tools/label_recent_sea_sessions.sh 2026-05-27_my-new-session
#
# Three passes run per session:
#   0. tools/auto_classify_screen_type.py — screen_type per frame
#      (covers every screen — sea, sub_menu, dialog_*, world_map, …)
#   1. tools/auto_label_sea_capture.py    — sea-view tags (S/B/R/I + conditions)
#      (only processes frames whose screen_type is "sea")
#   2. tools/label_minimap.py              — mini-map content tags (radar classes)
#      (only processes frames whose screen_type is "sea")
#
# All three passes are idempotent — already-labelled frames are
# skipped, so re-running this script is safe and only spends API on
# new frames.
set -e

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "ERROR: ANTHROPIC_API_KEY is not set.  Prefix the command:" >&2
  echo "  ANTHROPIC_API_KEY=sk-ant-... bash tools/label_recent_sea_sessions.sh" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABELS_PATH="${REPO_ROOT}/data/labels.jsonl"

# ── discover sessions that already have any labelled frame ───────────────
SESSIONS=()
while IFS= read -r s; do
  [[ -n "$s" ]] && SESSIONS+=("$s")
done < <(
  python3 - "$LABELS_PATH" <<'PY'
import json, sys, os
path = sys.argv[1]
if not os.path.exists(path):
    sys.exit(0)
seen = set()
with open(path) as f:
    for line in f:
        try:
            r = json.loads(line)
        except Exception:
            continue
        sid = r.get("session_id")
        if sid:
            seen.add(sid)
for s in sorted(seen):
    print(s)
PY
)

# ── append any extra sessions passed as positional args (for brand-new
#    captures that don't have any labels yet) ─────────────────────────────
for extra in "$@"; do
  SESSIONS+=("$extra")
done

# ── dedup while preserving order (portable — bash 3.2 on macOS) ───────────
# `awk '!seen[$0]++'` keeps the first occurrence of each line and drops dups.
DEDUPED_LIST=$(printf '%s\n' "${SESSIONS[@]}" | awk 'NF && !seen[$0]++')
SESSIONS=()
while IFS= read -r s; do
  [[ -n "$s" ]] && SESSIONS+=("$s")
done <<< "$DEDUPED_LIST"

if [[ ${#SESSIONS[@]} -eq 0 ]]; then
  echo "No sea-bearing sessions found in $LABELS_PATH and no extras passed." >&2
  echo "Pass new session ids as positional arguments:" >&2
  echo "  bash $0 2026-05-27_my-new-session" >&2
  exit 0
fi

echo "Discovered ${#SESSIONS[@]} session(s):"
printf '  %s\n' "${SESSIONS[@]}"
echo

# ── pass 0 — screen-type classification (every frame) ────────────────────
for s in "${SESSIONS[@]}"; do
  echo
  echo "=== screen-type classification: $s ==="
  python tools/auto_classify_screen_type.py --session "$s"
done

# ── pass 1 — sea-view tags (only frames with screen_type=sea) ─────────────
echo
echo "=== Sea-view tag pass — yolo/time/weather/sail/right_panel tags ==="
for s in "${SESSIONS[@]}"; do
  echo
  echo "  -- sea-view pass: $s --"
  python tools/auto_label_sea_capture.py --session "$s"
done

# ── pass 2 — mini-map content tags (only frames with screen_type=sea) ─────
echo
echo "=== Mini-map content pass — adds minimap:* tags to existing labels ==="
for s in "${SESSIONS[@]}"; do
  echo
  echo "  -- mini-map pass: $s --"
  python tools/label_minimap.py --session "$s"
done

echo
echo "All sessions processed.  Review in the supervisor /training page."
