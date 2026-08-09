"""Shadow run of the reasoning loop against the LIVE game screen.

Captures + perceives the current screen, builds the world model + reasoning
context, asks Claude for the next action, and LOGS the trace — WITHOUT acting
(shadow mode). Run it at any screen (especially a stuck one) to gather evidence:

    ANTHROPIC_API_KEY=... python -m tools.reasoning_shadow_run "depart and sail to Amsterdam"

Then read memory/knowledge/reasoning_traces/traces.jsonl to see exactly what
Claude saw and chose — and which world-model / perception facts were missing (the
gaps to fill next). Needs the phone connected (adb) + the game visible + the key.
"""
import sys

from brain.world_model import WorldModel
from brain.reasoning_loop import resolve
from brain.llm_client import available


def main() -> None:
    goal = " ".join(sys.argv[1:]).strip() or "Figure out and do the next useful thing."
    if not available():
        print("ANTHROPIC_API_KEY not set — set it and relaunch. Running the "
              "perception + world-model build anyway (no Claude call).")

    wm = WorldModel()    # populated from live perception inside resolve()
    print(f"GOAL: {goal}\n\nShadow run against the current screen "
          f"(perceive → world model → Claude → log; NO action)…\n")

    out = resolve(goal, wm, shadow=True, trigger="manual_shadow")

    print("=== WORLD MODEL (built from perception) ===")
    print(wm.to_prompt())
    print("\n=== REASONING ===")
    for s in out["steps"]:
        print("perceived:", s["perceived"])
        print("Claude action:", s.get("action"))
    print(f"\nresult: {out['reason']}")
    print("(full trace appended to memory/knowledge/reasoning_traces/traces.jsonl)")


if __name__ == "__main__":
    main()
