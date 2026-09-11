# run_barter.py — run a barter mission from one typed command
#
# Usage:
#   python run_barter.py "barter Box of Nutmeg at Melanesian Village, then take the route jakarta to london"
#   python run_barter.py "barter Camas at Apache Village, and sail to Edinburgh"
#   python run_barter.py "barter Pulque at Apache Village" --dry-run
#
# --dry-run stops after the plan.  It STILL performs the remote village check (that is
# where this window's live quantities and remaining rounds come from) — it just doesn't
# gather, sail, or barter.
#
# --capacity / --cargo override the live fleet read when the main-menu panel can't be
# read (the plan needs capacity − cargo − a 7-day supply reserve).
#
# --clear-surplus sells every non-material, non-supply good at the CURRENT port before
# planning, to free hold space (sell_goods goal="clear").  Opt-in: a clear-sell ignores
# profit, so it can dump cargo you were carrying to sell at a better market.
#
# --ignore-low-stock sails even when EVERY known source of a material is recorded scarce
# this season.  Off by default: a season where nothing restocks properly is a season to sit
# out (user, 2026-08-30).  Turn it on for a mission worth running regardless — the stock is
# thin, not absent, so the gather simply takes longer and returns less.
#
# --cushion=0.15 (default) hedges the village check going stale under the mission: the
# quantities re-roll every ~6h, and bartering moves amity — crossing a tier changes the
# ratio.  --cushion=0 plans on the snapshot exactly (more rounds, no headroom).

import os
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import sys
from memory.logger import setup_logging
setup_logging()

args = sys.argv[1:]

# ── saved missions ────────────────────────────────────────────────────────────
# A barter mission IS one line of natural language, so a saved one is just that line
# under a name. No second format, no execution path of its own — `--task` looks the
# line up and everything downstream runs exactly as if it had been typed.
from pathlib import Path as _Path
_MISSIONS = _Path(__file__).resolve().parent / "tasks" / "barter"


def _saved(name: str) -> str:
    f = _MISSIONS / f"{name}.txt"
    if not f.exists():
        have = sorted(p.stem for p in _MISSIONS.glob("*.txt")) if _MISSIONS.exists() else []
        print(f"no saved mission {name!r}." + (f" saved: {', '.join(have)}" if have else ""))
        sys.exit(1)
    return f.read_text().strip()


def _opt(flag: str):
    for a in args:
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


if "--list" in args:
    for f in sorted(_MISSIONS.glob("*.txt")) if _MISSIONS.exists() else []:
        print(f"  {f.stem:24} {f.read_text().strip()}")
    sys.exit(0)

command = next((a for a in args if not a.startswith("--")), None)

_save_as = _opt("--save")
if _save_as:
    if not command:
        print('--save=<name> needs the command too: '
              'python run_barter.py --save=hutu_groundnut "barter Groundnut at ..."')
        sys.exit(1)
    _MISSIONS.mkdir(parents=True, exist_ok=True)
    (_MISSIONS / f"{_save_as}.txt").write_text(command.strip() + "\n")
    print(f"saved {_save_as!r}: {command.strip()}")
    sys.exit(0)

_task = _opt("--task")
if _task:
    command = _saved(_task)
    print(f"[{_task}] {command}")

if not command:
    print('Usage: python run_barter.py "barter <good> at <village>'
          '[, then take the route <name> | and sail to <port>]"'
          ' [--dry-run] [--no-trace] [--clear-surplus] [--ignore-low-stock] [--from=<port>]'
          ' [--capacity=N --cargo=N] [--cushion=0.15]')
    print('   or: python run_barter.py --task=<name>          # run a saved mission')
    print('       python run_barter.py --save=<name> "<cmd>"  # save one')
    print('       python run_barter.py --list                 # list them')
    sys.exit(1)

dry_run = "--dry-run" in args
clear_surplus = "--clear-surplus" in args
allow_low_stock = "--ignore-low-stock" in args
# Tracing is ON by default: every run should be reviewable afterwards. The per-tap cost
# is one screencap — action_trace REUSES the perception the bot just computed (cache
# hits, no fresh OmniParser), so this is cheap. Skipping it on 2026-08-21 is what left the
# one interesting failure with no frames to inspect. --no-trace opts out.
trace = "--no-trace" not in args


def _flag(name, cast):
    for a in args:
        if a.startswith(f"--{name}="):
            return cast(a.split("=", 1)[1])
    return None


def _int_flag(name):
    return _flag(name, int)


def _str_flag(name):
    return _flag(name, str)


def _other_bot_processes():
    """Other processes that could be driving the phone right now.

    Two of them tapping at once interleaves input and confuses both — a whole gather run
    was lost to it (memory: ALWAYS pkill + verify before relaunching) — and a pytest run
    that reaches ADB pans the world map out from under a mission. Checked here rather than
    left to whoever remembers, because the symptom (a mission "failing to find" things)
    looks nothing like the cause."""
    import subprocess as _sp
    mine = str(os.getpid())
    try:
        out = _sp.run(["pgrep", "-fl", "run_barter.py|pytest|probe_|trace_viewer"],
                      capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    hits = []
    for line in out.splitlines():
        pid = line.split(None, 1)[0] if line.split() else ""
        if pid and pid != mine and "pgrep" not in line:
            hits.append(line.strip()[:110])
    return hits


_others = _other_bot_processes()
if _others:
    print("Refusing to start — something else is already driving the phone:")
    for h in _others:
        print(f"    {h}")
    print("\nStop them first (pkill -f run_barter.py / pkill -f pytest), then re-run.")
    sys.exit(2)

# ALWAYS lock orientation — --dry-run still TAPS (the remote village check drives the
# world map, the Village Info tabs and the material pins), and every one of those coords
# is orientation-specific.  See actions/orientation.py.
from actions.orientation import ensure_canonical_orientation
ensure_canonical_orientation()

session_dir = None
if trace:
    from actions import action_trace
    session_dir = action_trace.start("barter_cmd")
    # Keep the LOG with the frames — a session should be self-contained, so a report can
    # be built from it later without hunting for the console output.
    try:
        from loguru import logger as _logger
        _logger.add(str(session_dir / "run.log"), level="INFO", enqueue=True,
                    backtrace=False, diagnose=False)
        _logger.info(f"[run_barter] command: {command!r}")
    except Exception as _exc:            # logging must never break the run
        print(f"(could not attach a session log: {_exc})")

from brain.barter_command import run_barter_command

try:
    result = run_barter_command(command,
                                cargo_capacity=_int_flag("capacity"),
                                cargo_used=_int_flag("cargo"),
                                cushion=_flag("cushion", float),
                                clear_surplus=clear_surplus,
                                allow_low_stock=allow_low_stock,
                                from_port=_str_flag("from"),
                                dry_run=dry_run)
finally:
    if trace:
        from actions import action_trace
        done = action_trace.stop()
        if done is not None:
            print(f"\nSession: {done}")
            print(f"  log    : {done}/run.log")
            print(f"  report : python -m tools.trace_viewer {done}")

plan = result.get("plan")
if plan is not None:
    print(f"\nPlan: {plan.rounds} round(s) (limited by {plan.limited_by}) — "
          f"buy {plan.total_needs} → expect ~{plan.output_qty} units "
          f"[{plan.reserved_per_round}/round reserved of {plan.free_space} free, "
          f"incl. +{plan.cushion:.0%} cushion]")
print(f"\n{'OK' if result.get('ok') else 'FAILED'} at step "
      f"{result.get('step')}: {result.get('reason', '')}")
sys.exit(0 if result.get("ok") else 1)
