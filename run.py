#!/usr/bin/env python3
# run.py
# Main entry point.  Run with no arguments to enter interactive chat mode.
#
# Chat commands (natural language accepted):
#   go to <building>        — navigate to a building via the menu
#   exit / back / leave     — leave current building, return to overworld
#   routine <name>          — run a saved routine
#   routines                — list available routines
#   help                    — show this list
#   quit / exit bot         — stop the bot
#
# Single-shot (non-interactive):
#   python run.py goto castle
#   python run.py routine daily

from __future__ import annotations

import os
import time
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import select
import sys
import threading
from pathlib import Path

from loguru import logger

from memory.logger import setup_logging
from routines.runner import run_step, run_routine
from routines.definitions import ROUTINES


# ── command parsing ───────────────────────────────────────────────────────────

# Articles and filler words to strip from building names
_STOPWORDS = {"the", "a", "an", "to"}


def _strip_stopwords(words: list[str]) -> str:
    return " ".join(w for w in words if w not in _STOPWORDS).strip()


def _split_two_ports(tokens: list[str]) -> tuple[str, str]:
    """
    Split a flat token list into two port names using known ports as anchors.
    e.g. ["bergen", "port", "royal"] → ("Bergen", "Port Royal")
    Falls back to splitting at midpoint if no known port matches.
    """
    from actions.world_map import list_known_ports
    known = {p.lower(): p for p in list_known_ports()}

    joined = " ".join(tokens)
    # Try every split point; pick the first where both halves are known ports
    for i in range(1, len(tokens)):
        a = " ".join(tokens[:i])
        b = " ".join(tokens[i:])
        if a in known and b in known:
            return known[a], known[b]

    # Fallback: title-case each half split at midpoint
    mid = len(tokens) // 2
    a = " ".join(tokens[:mid]).title()
    b = " ".join(tokens[mid:]).title()
    return (a, b) if a and b else ("", "")


def _write_trade_route_yaml(path: Path, port_a: str, port_b: str) -> None:
    """Generate a standard two-port trade loop YAML task file."""
    slug_a = port_a.lower().replace(" ", "_")
    slug_b = port_b.lower().replace(" ", "_")
    content = f"""\
name: trade_{slug_a}_{slug_b}
start_port: {port_a}
rounds: 999
loop:
  - action: sell_all
    port: {port_a}
  - action: buy_all
    port: {port_a}
  - action: sail_to
    destination: {port_b}
  - action: sell_all
    port: {port_b}
  - action: buy_all
    port: {port_b}
  - action: sail_to
    destination: {port_a}
"""
    path.write_text(content)


def _parse(line: str) -> tuple[str, str]:
    """
    Parse a natural-language line into (action, argument).

    Examples:
      "go to the castle"       → ("goto", "castle")
      "go to market"           → ("goto", "market")
      "exit"                   → ("exit", "")
      "exit building"          → ("exit", "")
      "back"                   → ("exit", "")
      "leave"                  → ("exit", "")
      "routine daily"          → ("routine", "daily")
      "run daily"              → ("routine", "daily")
      "run routine daily"      → ("routine", "daily")
      "routines"               → ("routines", "")
      "list routines"          → ("routines", "")
      "help"                   → ("help", "")
      "quit" / "q" / "bye"     → ("quit", "")
    """
    parts = line.lower().split()
    if not parts:
        return ("", "")

    first = parts[0]

    if first in ("quit", "q", "bye"):
        return ("quit", "")

    if first in ("exit", "back", "leave"):
        return ("exit", "")

    if first in ("grow", "self_grow", "selfgrow") or (
        first == "self" and len(parts) > 1 and parts[1] == "grow"
    ):
        return ("grow", " ".join(parts[1:] if first == "self" else parts[1:]))

    if first == "explore":
        return ("explore", " ".join(parts[1:]))

    if first == "agent":
        return ("agent", " ".join(parts[1:]))

    if first == "kb":
        return ("kb", " ".join(parts[1:]))

    if first in ("map", "worldmap"):
        # "map explore" — read all visible city infos on the current world map
        # "map info <port>" — show KB entry for a port
        # "map ports" — list known ports
        return ("map", " ".join(parts[1:]))

    if first == "route":
        rest = parts[1:]
        # "route create/run/list ..." → trade route commands
        if rest and rest[0] in ("create", "run", "list"):
            return ("trade_route", " ".join(rest))
        # "route Bergen Lisbon" — show planned voyage (nav planner)
        return ("route", " ".join(rest))

    # "trade X Y" / "trade between X and Y" / "trade from X to Y"
    if first == "trade":
        tokens = [t for t in parts[1:] if t not in ("between", "and", "from", "to", "the")]
        return ("trade_route", "run " + " ".join(tokens))

    if first in ("go", "goto", "navigate"):
        # "go to the castle" / "go castle" / "goto castle"
        rest = parts[1:]
        if rest and rest[0] == "to":
            rest = rest[1:]
        target = _strip_stopwords(rest)
        return ("goto", target)

    if first == "sail" and len(parts) > 1:
        # "sail Berber" / "sail to London" / "sail Port Royal" — top-
        # level destination command.  Recognises both port and village
        # names; the destination dispatcher in actions/sail_actions
        # decides which catalogue to use.  Strip filler words ("to",
        # "the") to match the natural English forms.
        rest = parts[1:]
        if rest and rest[0] == "to":
            rest = rest[1:]
        target = _strip_stopwords(rest)
        return ("sail", target)

    # "clear cloud" / "clearcloud" / "explore coast" — depart from the
    # current port without a destination, sail manually for N minutes,
    # try to enter any settlement whose Enter New ... label appears,
    # head home.  See brain/goals/clear_cloud.py.
    if first in ("clear", "clearcloud", "clear_cloud"):
        # "clear cloud" / "clear cloud 30"
        rest = parts[1:]
        if rest and rest[0] == "cloud":
            rest = rest[1:]
        arg = " ".join(rest)
        return ("clear_cloud", arg)

    if first in ("routine", "run"):
        rest = parts[1:]
        # Strip filler so "run the trade route X Y" and "run route X Y" both work
        rest_stripped = [t for t in rest if t not in ("the", "a", "an")]
        if rest_stripped and rest_stripped[0] in ("route", "trade"):
            # Advance past "route"/"trade" and any following "route"
            tokens = rest_stripped[1:]
            if tokens and tokens[0] == "route":
                tokens = tokens[1:]
            tokens = [t for t in tokens if t not in ("between", "and", "from", "to", "the")]
            return ("trade_route", "run " + " ".join(tokens))
        # "run routine daily" → skip the word "routine"
        if rest and rest[0] == "routine":
            rest = rest[1:]
        name = " ".join(rest).strip()
        return ("routine", name)

    if first in ("routines", "list") and (
        len(parts) == 1 or parts[1] == "routines"
    ):
        return ("routines", "")

    if first == "help":
        return ("help", "")

    return ("unknown", line)


# ── dispatch ──────────────────────────────────────────────────────────────────

def _handle(action: str, arg: str) -> bool:
    """Execute a parsed command. Returns False to signal the loop should stop."""
    if action == "quit":
        return False

    if action == "goto":
        if not arg:
            print("  Which building? e.g. 'go to the castle'")
            return True
        run_step(f"goto {arg}")
        return True

    if action == "grow":
        from actions.task_runner import run_task
        task_file = Path("tasks/self_grow.yaml")
        if not task_file.exists():
            print("  tasks/self_grow.yaml not found")
            return True
        run_task(task_file)
        return True

    if action == "exit":
        from brain.states.exploring_port import exit_building
        exit_building()
        return True

    if action == "explore":
        from brain.states.exploring_port import (
            explore_all_buildings,
            DEFAULT_EXPLORE_TIMEOUT_MINUTES,
        )

        # Optional timeout: "explore 30" → 30 minutes
        timeout = DEFAULT_EXPLORE_TIMEOUT_MINUTES
        if arg:
            try:
                timeout = float(arg.split()[0])
            except ValueError:
                pass

        stop_event = threading.Event()
        thread = threading.Thread(
            target=explore_all_buildings,
            kwargs={"stop_event": stop_event, "timeout_minutes": timeout},
            daemon=True,
        )
        thread.start()
        print(
            f"  Exploring... (timeout: {timeout:.0f} min). "
            "Type 'stop' to finish after the current building.\n"
        )

        # Poll stdin while the explore thread runs
        while thread.is_alive():
            ready, _, _ = select.select([sys.stdin], [], [], 1.0)
            if ready:
                line = sys.stdin.readline().strip().lower()
                if line in ("stop", "finish", "done", "quit", "q"):
                    if line in ("quit", "q"):
                        stop_event.set()
                        thread.join(timeout=5)
                        print("Bye.")
                        return False   # signal outer loop to exit
                    stop_event.set()
                    print("  Stopping after current building...")
                    break

        thread.join()
        print("  Exploration finished.\n")
        return True

    if action == "sail":
        # Top-level "sail <name>" — run the SailToGoal FSM (the proper
        # phase machine: INIT → HARBOR → DEPART → SEA → WORLD_MAP →
        # SAILING).  Routes through _navigate_world_map_to_destination
        # so ports and villages both work.
        #
        # The other Goal.sail_to path (Agent.run, used by `agent sail`)
        # is a generic LLM-driven decision loop that doesn't know the
        # harbor-then-world-map sequence and tends to misfire on the
        # in-town port map.  Don't use that for direct sail commands.
        if not arg:
            print("  Usage: sail <port name|village name>")
            return True

        from brain.goals.sail_to import SailToGoal
        destination = arg.strip()
        goal = SailToGoal(destination=destination)
        print(f"  Sailing to {destination!r} via SailToGoal FSM.  "
              "Type 'stop' to abort.\n")

        stop_event = threading.Event()

        def _run_sail():
            import random
            while not goal.is_complete and not goal.is_failed:
                if stop_event.is_set():
                    print("  Sail aborted by user.")
                    return
                result = goal.tick()
                # Inter-tick anti-cheat jitter.  Was uniform(5, 10) but
                # perception itself already adds 5-15 s of human-scale
                # delay between transactional taps; 2 s of extra jitter
                # is enough to keep tick timing non-deterministic without
                # stacking onto an already-slow loop.
                delay = result.delay if result.delay > 0 else random.uniform(1.5, 2.5)
                # Sleep in small chunks so 'stop' is responsive.
                slept = 0.0
                while slept < delay and not stop_event.is_set():
                    time.sleep(min(0.5, delay - slept))
                    slept += 0.5

        thread = threading.Thread(target=_run_sail, daemon=True)
        thread.start()

        import select
        while thread.is_alive():
            ready, _, _ = select.select([sys.stdin], [], [], 1.0)
            if ready:
                line = sys.stdin.readline().strip().lower()
                if line in ("stop", "finish", "done", "quit", "q"):
                    stop_event.set()
                    if line in ("quit", "q"):
                        thread.join(timeout=5)
                        print("Bye.")
                        return False
                    print("  Stopping after current tick…")
                    break
        thread.join()
        if goal.is_complete:
            print(f"  Arrived at {destination!r}.\n")
        elif goal.is_failed:
            print(f"  Sail to {destination!r} failed.\n")
        else:
            print(f"  Sail to {destination!r} ended.\n")
        return True

    if action == "clear_cloud":
        # Depart from current port without a destination, sail
        # manually for N minutes, tap any 'Enter New City/Village'
        # label that appears, then head home.  See
        # brain/goals/clear_cloud.py for the full FSM.
        from brain.goals.clear_cloud import ClearCloudGoal

        # Parse optional duration: "clear cloud 30" → 30 minutes.
        minutes = 20
        if arg:
            try:
                minutes = int(arg.split()[0])
            except (ValueError, IndexError):
                pass

        goal = ClearCloudGoal(duration_minutes=minutes)
        print(
            f"  Clear-cloud (coast exploration) for {minutes} min.  "
            "Type 'stop' to abort.\n"
        )

        stop_event = threading.Event()

        def _run_clear_cloud():
            import random
            while not goal.is_complete and not goal.is_failed:
                if stop_event.is_set():
                    print("  Clear-cloud aborted by user.")
                    return
                result = goal.tick()
                delay = result.delay if result.delay > 0 else random.uniform(1.5, 2.5)
                slept = 0.0
                while slept < delay and not stop_event.is_set():
                    time.sleep(min(0.5, delay - slept))
                    slept += 0.5

        thread = threading.Thread(target=_run_clear_cloud, daemon=True)
        thread.start()

        import select
        while thread.is_alive():
            ready, _, _ = select.select([sys.stdin], [], [], 1.0)
            if ready:
                line = sys.stdin.readline().strip().lower()
                if line in ("stop", "finish", "done", "quit", "q"):
                    stop_event.set()
                    if line in ("quit", "q"):
                        thread.join(timeout=5)
                        print("Bye.")
                        return False
                    print("  Stopping after current tick…")
                    break
        thread.join()
        n = len(goal._new_discoveries)
        if goal.is_complete:
            print(f"  Clear-cloud complete.  Discoveries: {n}.\n")
        elif goal.is_failed:
            print(f"  Clear-cloud failed.  Discoveries: {n}.\n")
        else:
            print(f"  Clear-cloud ended.  Discoveries: {n}.\n")
        return True

    if action == "agent":
        from brain.agent import Agent
        from brain.goal import Goal
        from vision.ocr import read_port_name
        from capture.adb_capture import capture_screen

        parts = arg.split() if arg else []
        cmd = parts[0] if parts else "learn"

        if cmd == "explore":
            frame = capture_screen()
            port = read_port_name(frame)
            if not port:
                # OCR failed or returned garbled text — ask llava to read the port name
                from vision.local_vision import get_vision as _get_vision
                from config.prompts import SEED_KNOWLEDGE as _SK
                vision_result = _get_vision().describe_screen(frame, system=_SK)
                port = vision_result.get("screen_title") or "unknown_port"
                logger.info(f"OCR port name unavailable — llava identified: {port!r}")
            goal = Goal.explore_port(port)
        elif cmd == "goto" and len(parts) > 1:
            goal = Goal.go_to_building(" ".join(parts[1:]))
        elif cmd == "sail" and len(parts) > 1:
            goal = Goal.sail_to(" ".join(parts[1:]))
        else:
            goal = Goal.idle()

        stop_event = threading.Event()
        thread = threading.Thread(
            target=Agent().run,
            kwargs={"goal": goal, "stop_event": stop_event},
            daemon=True,
        )
        thread.start()
        print(f"  Agent running — goal: {goal}. Type 'stop' to finish.\n")

        import select
        while thread.is_alive():
            ready, _, _ = select.select([sys.stdin], [], [], 1.0)
            if ready:
                line = sys.stdin.readline().strip().lower()
                if line in ("stop", "quit", "q"):
                    stop_event.set()
                    thread.join(timeout=10)
                    if line in ("quit", "q"):
                        print("Bye.")
                        return False
                    break

        thread.join()
        print("  Agent finished.\n")
        return True

    if action == "kb":
        from memory.knowledge_base import show_kb
        show_kb(arg.split() if arg else [])
        return True

    if action == "map":
        from actions.world_map import (
            explore_visible_ports_on_world_map,
            load_city_info_from_kb,
            list_known_ports,
        )
        parts = arg.split() if arg else []
        subcmd = parts[0] if parts else "explore"

        if subcmd == "explore":
            print("  Reading City Info for all visible ports on world map…")
            recorded = explore_visible_ports_on_world_map()
            if recorded:
                print(f"  Recorded {len(recorded)} ports: {', '.join(recorded)}")
            else:
                print("  No ports recorded — open the world map first")

        elif subcmd == "ports":
            ports = list_known_ports()
            if ports:
                from actions.world_map_gather import load_trade_info
                print(f"  Known ports ({len(ports)}):")
                for p in ports:
                    info  = load_city_info_from_kb(p)
                    trade = load_trade_info(p)
                    goods_n   = len((info  or {}).get("goods", []))
                    fac_n     = len((info  or {}).get("facilities", []))
                    culture   = (info  or {}).get("culture", "")
                    mkt_n     = len((trade or {}).get("market_goods", []))
                    trade_tag = f"  {mkt_n} mkt goods" if trade else ""
                    print(f"    {p:20s}  {goods_n} city goods  {fac_n} facilities  {culture}{trade_tag}")
            else:
                print("  No port info in KB yet.  Use 'map explore' or 'map gather'.")

        elif subcmd == "info":
            port_name = " ".join(parts[1:]) if len(parts) > 1 else ""
            if not port_name:
                print("  Usage: map info <port name>")
            else:
                info = load_city_info_from_kb(port_name)
                if info:
                    import json
                    print(json.dumps(info, indent=2))
                else:
                    print(f"  No KB entry for {port_name!r}.  Use 'map explore' while near that port.")

        elif subcmd == "gather":
            # "map gather"              — scan all new ports (skip existing)
            # "map gather 20"           — limit to 20 new ports
            # "map gather refresh"      — rescan all ports, overwrite existing
            # "map gather refresh 20"   — rescan, stop after 20 ports
            # "map gather port London"  — single port (panel must be open)
            from actions.world_map_gather import (
                scan_world_map_trade_info,
                gather_port_trade_info,
                save_trade_info,
            )
            if len(parts) >= 2 and parts[1] == "port":
                port_name = " ".join(parts[2:])
                data = gather_port_trade_info(port_name)
                if data:
                    save_trade_info(port_name, data)
                    import json
                    print(json.dumps(data, indent=2, ensure_ascii=False))
                else:
                    print(f"  Failed to gather trade info for {port_name!r}")
            else:
                refresh = "refresh" in parts[1:]
                remaining = [p for p in parts[1:] if p != "refresh"]
                max_p = None
                if remaining:
                    try:
                        max_p = int(remaining[0])
                    except ValueError:
                        pass
                if refresh:
                    print("  Refresh mode — will overwrite existing KB entries.")
                gathered = scan_world_map_trade_info(
                    max_ports=max_p, skip_existing=not refresh
                )
                print(f"  Gathered {len(gathered)} port(s): {', '.join(gathered)}")

        elif subcmd == "trade":
            # "map trade <port>" — show trade KB entry for a port
            from actions.world_map_gather import load_trade_info
            port_name = " ".join(parts[1:]) if len(parts) > 1 else ""
            if not port_name:
                print("  Usage: map trade <port name>")
            else:
                info = load_trade_info(port_name)
                if info:
                    import json
                    print(json.dumps(info, indent=2, ensure_ascii=False))
                else:
                    print(f"  No trade info for {port_name!r}.  Use 'map gather' to collect it.")

        else:
            print("  map commands:")
            print("    map explore          — read City Info for all visible ports (world map must be open)")
            print("    map gather             — gather Trade info for all new ports")
            print("    map gather <N>         — gather Trade info, stop after N new ports")
            print("    map gather refresh     — rescan all ports, overwrite existing KB")
            print("    map gather refresh <N> — rescan, stop after N ports")
            print("    map gather port <p>    — gather Trade info for one port (panel must be open)")
            print("    map trade <port>     — show stored trade info for a port")
            print("    map ports            — list all ports with KB entries")
            print("    map info <port>      — show KB entry for a specific port")
        return True

    if action == "route":
        from brain.route_planner import plan_route, score_leg
        from memory.port_graph import PortGraph
        parts = arg.split() if arg else []

        if not parts:
            # Show port graph summary
            g = PortGraph.build()
            print(f"  {g.summary()}")
            print("  Usage: route <origin> <destination>   — plan a voyage")
            print("         route score <A> <B>            — show leg score breakdown")
            return True

        if parts[0] == "score" and len(parts) >= 3:
            a = " ".join(parts[1 : len(parts) // 2 + 1])
            b = " ".join(parts[len(parts) // 2 + 1 :])
            # Try all splits
            for split in range(2, len(parts)):
                a = " ".join(parts[1:split])
                b = " ".join(parts[split:])
                s = score_leg(a, b)
                print(f"  {a} → {b}")
                print(f"    prior_score : {s['prior_score']}")
                print(f"    obs_dph     : {s['obs_dph'] or 'no data yet'}")
                print(f"    edge_count  : {s['edge_count']} observations")
                print(f"    sailing_s   : {s['sailing_s']:.0f}s ({s['sailing_s']/60:.1f} min)")
                print(f"    planner_wt  : {s['weight']:.1f}  (lower = better)")
                break
            return True

        # "route London Port Royal" — plan the voyage
        for split in range(1, len(parts)):
            origin = " ".join(parts[:split])
            dest   = " ".join(parts[split:])
            path   = plan_route(origin, dest)
            if len(path) >= 2:
                print(f"  Planned route: {' → '.join(p.title() for p in path)}")
                print(f"  ({len(path)-1} hop(s))")
                break
        return True

    if action == "trade_route":
        parts = arg.split() if arg else []
        sub = parts[0] if parts else "list"

        tasks_dir = Path("tasks")
        tasks_dir.mkdir(exist_ok=True)

        if sub == "list":
            files = sorted(tasks_dir.glob("trade_*.yaml"))
            if not files:
                print("  No trade routes saved yet. Use: route create <A> <B>")
            else:
                print("  Saved trade routes:")
                for f in files:
                    name = f.stem.replace("trade_", "").replace("_", " ↔ ", 1).replace("_", " ")
                    print(f"    {name}  ({f.name})")
            return True

        if sub in ("create", "run") and len(parts) >= 3:
            # Split remaining words into two port names at the midpoint
            tokens = parts[1:]
            port_a, port_b = _split_two_ports(tokens)
            if not port_a or not port_b:
                print(f"  Could not split {' '.join(tokens)!r} into two port names.")
                return True

            slug_a = port_a.lower().replace(" ", "_")
            slug_b = port_b.lower().replace(" ", "_")
            task_file = tasks_dir / f"trade_{slug_a}_{slug_b}.yaml"

            if sub == "create" or not task_file.exists():
                _write_trade_route_yaml(task_file, port_a, port_b)
                print(f"  Route saved → {task_file}")

            if sub == "run":
                from actions.task_runner import run_task
                run_task(task_file)
            return True

        print(f"  Usage: route create <A> <B>  |  route run <A> <B>  |  route list")
        return True

    if action == "routine":
        if not arg:
            print("  Which routine? Type 'routines' to see the list.")
            return True
        run_routine(arg)
        return True

    if action == "routines":
        if not ROUTINES:
            print("  No routines defined yet. Edit routines/definitions.py to add some.")
        else:
            print("  Saved routines:")
            for name, steps in ROUTINES.items():
                print(f"    {name}:")
                for s in steps:
                    print(f"      • {s}")
        return True

    if action == "help":
        print(
            "\n  Commands:\n"
            "    grow                    — autonomous growth loop (uses tasks/self_grow.yaml)\n"
            "    agent                   — observe current screen and learn\n"
            "    agent explore           — agent explores current port (learns all buildings)\n"
            "    agent goto <building>   — agent navigates to a building\n"
            "    agent sail <port>       — agent sails to another port\n"
            "    sail <port|village>     — sail to a port or village (dispatches automatically)\n"
            "    go to <building>        — rule-based navigation to a building\n"
            "    exit / back             — leave the current building\n"
            "    explore [minutes]       — rule-based port sweep (default 15 min)\n"
            "    map explore             — read City Info for all visible ports (world map open)\n"
            "    map gather              — gather Trade info for all new ports (skip existing)\n"
            "    map gather <N>          — same but stop after N new ports\n"
            "    map gather refresh      — rescan all ports, overwrite existing KB\n"
            "    map gather refresh <N>  — rescan, stop after N ports\n"
            "    map gather port <port>  — gather one port (City Info panel must be open)\n"
            "    map trade <port>        — show stored trade info for a port\n"
            "    map ports               — list all ports with KB entries\n"
            "    map info <port>         — show KB entry for a specific port\n"
            "    kb                      — show all known ports\n"
            "    kb <port>               — list buildings known for a port\n"
            "    kb <port> <building>    — details for one building\n"
            "    kb type <type>          — cross-port knowledge for a building type\n"
            "    kb market <port>        — latest market prices for a port\n"
            "    route                   — list known nav routes with resupply stops\n"
            "    route <origin> <dest>   — show planned voyage with stops\n"
            "    route list              — list saved trade routes\n"
            "    route create <A> <B>    — create a trade loop between two ports\n"
            "    route run <A> <B>       — run trade loop (creates if missing)\n"
            "    routine <name>          — run a saved routine\n"
            "    routines                — list saved routines\n"
            "    quit / q                — stop the bot\n"
        )
        return True

    if action == "unknown":
        # Fast path didn't recognise it — try the local LLM parser
        from brain.llm_parser import parse as llm_parse
        action, arg = llm_parse(arg)
        if action != "unknown":
            return _handle(action, arg)
        print(f"  I don't understand '{arg}'. Type 'help' for available commands.")
        return True

    return True


# ── chat loop ─────────────────────────────────────────────────────────────────

def chat() -> None:
    from brain.human_escalation import TeachingAbortedError
    print("UWO Bot ready. Type a command or 'help'.")
    print("Examples: 'go to the castle'  |  'explore'  |  'kb amsterdam'  |  'quit'\n")
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break
        if not line:
            continue
        action, arg = _parse(line)
        try:
            keep_going = _handle(action, arg)
        except TeachingAbortedError as exc:
            # Initial-prompt timeout in human escalation — task aborts cleanly
            # back to the chat prompt rather than crashing the bot.  Per user
            # direction: "if time out happens, the bot quits the task, and
            # go back to the main menu".
            print(f"\n  Task aborted — teaching session timed out: {exc}")
            print("  Returning to main menu.\n")
            continue
        if not keep_going:
            print("Bye.")
            break


# ── non-interactive single-shot ───────────────────────────────────────────────

def _single_shot() -> None:
    """Handle  python run.py <command> [args...]  without entering chat mode."""
    cmd = sys.argv[1].lower()
    rest = " ".join(sys.argv[2:])

    if cmd == "routines":
        action, arg = "routines", ""
    elif cmd == "routine":
        action, arg = "routine", rest
    else:
        # Treat the whole argv as a natural-language line
        action, arg = _parse(" ".join(sys.argv[1:]))

    ok = _handle(action, arg)
    sys.exit(0 if ok else 1)


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    setup_logging()
    # Lock auto-rotate off / assert canonical landscape before any tapping —
    # hardcoded UI coords are orientation-specific (see actions/orientation.py).
    from actions.orientation import ensure_canonical_orientation
    ensure_canonical_orientation()
    if len(sys.argv) > 1:
        _single_shot()
    else:
        chat()
