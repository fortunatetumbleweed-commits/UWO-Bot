"""Live (acting) run of the reasoning loop — bounded, safety-gated, step-by-step.

Runs the reasoning loop with shadow OFF (it EXECUTES Claude's actions), one step
at a time, printing each step so it can be watched. Whitelisted ops only; the
executor REFUSES any commit/spend tap (Confirm/Pay/…) by default — so it can
navigate and recruit-screen its way toward a fix without risking a spend.

    ANTHROPIC_API_KEY=... python -m tools.reasoning_live_run "<goal>" [max_steps]

Use it to see whether the reasoning layer can handle a real situation (e.g.
not-enough-crew) end to end. Traces land in memory/knowledge/reasoning_traces/.
"""
import sys

from brain.world_model import WorldModel
from brain.reasoning_loop import resolve


def main() -> None:
    args = sys.argv[1:]
    goal = args[0] if args else "Make the fleet ready and depart to sail."
    max_steps = int(args[1]) if len(args) > 1 else 5

    wm = WorldModel()
    print(f"GOAL: {goal}\n(LIVE — will ACT; commit/spend taps are refused; "
          f"max_steps={max_steps})\n")

    for i in range(1, max_steps + 1):
        out = resolve(goal, wm, shadow=False, max_steps=1, trigger="live_run")
        s = out["steps"][-1] if out["steps"] else {}
        print(f"[step {i}] {s.get('perceived')}")
        print(f"         action: {s.get('action')}")
        if "exec" in s:
            print(f"         exec:   {s['exec']}")
        print(f"         -> {out['reason']}")
        # stop on a clear terminal signal
        r = out["reason"]
        if r.startswith("aborted") or "refused" in r or "no usable action" in r:
            print(f"\nSTOP: {r}")
            break
        sys.stdout.flush()

    print("\n=== FINAL WORLD MODEL ===")
    print(wm.to_prompt())


if __name__ == "__main__":
    main()
