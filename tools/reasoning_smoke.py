"""Live smoke test of the reasoning layer against Claude.

Builds the actual crew-shortage scenario, sends it through the reasoning layer to
Claude, and prints the prompt + Claude's chosen action + where the trace was
logged. Run with your key:

    ANTHROPIC_API_KEY=sk-... python -m tools.reasoning_smoke

No phone needed — this exercises context assembly → Claude → parsed action → trace
log end to end, so you can see whether the context is enough for Claude to infer
the fix (and read the logged trace to spot any missing knowledge/perception).
"""
from brain.llm_client import claude_llm_fn, available
from brain.reasoning import ReasoningContext, reason, _TRACE_PATH
from brain.world_model import WorldModel, Fleet, Ship


def main() -> None:
    if not available():
        print("ANTHROPIC_API_KEY not set / anthropic unavailable — cannot run live.")
        return

    wm = WorldModel(
        currencies={"ducat": 14_001_701_263, "blue_gem": 340, "red_gem": 12},
        fleets=[Fleet(location="London", current_building="Inn",
                      supply_days=6, crew_current=42, crew_capacity=60,
                      ships=[Ship("Barque", 88)])],
        discovered_ports=["London", "Amsterdam", "Seville", "Port Royal"],
    )
    ctx = ReasoningContext(
        goal="Depart from London and sail to Amsterdam — the fleet cannot depart.",
        world_model=wm.to_prompt(),
        perception=("base=panel context=Inn "
                    "menu=[Recruit, Hire, Party, Employee, Manage Mate] "
                    "selected=Hire title='Inn'"),
        game_knowledge=(
            "Crew (sailors) can be recruited at the Harbor, the Inn, or a Village "
            "(Village gives limited numbers; Harbor is fastest). A fleet needs full "
            "crew AND every ship's life >= 20 to depart. Recruit crew via the "
            "'Recruit' menu item. 'Hire' hires mates (different from crew)."
        ),
    )

    print("=== PROMPT SENT TO CLAUDE ===")
    print(ctx.to_prompt())
    print("=== CLAUDE'S ACTION ===")
    action = reason(ctx, llm_fn=claude_llm_fn, trigger="precondition_mismatch",
                    shadow=False)
    print(action)
    print(f"\n(trace appended to {_TRACE_PATH})")


if __name__ == "__main__":
    main()
