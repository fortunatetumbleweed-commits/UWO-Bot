#!/usr/bin/env python
"""Ask the local Qwen what the bot should do next, given the whole situation.

An experiment, not a component (user, 2026-08-29: "I wonder what a LLM will say"). It prints
the question verbatim and the answer verbatim, so the two can be judged together.

The model is the one already in the bot: mlx-community/Qwen2.5-1.5B-Instruct-4bit — 1.5B
parameters, 4-bit, chosen for reading OCR tokens off a screen. Its own system prompt says "You
are a perception assistant... Return ONLY a valid JSON object", which would frame a planning
question wrongly, so this asks with a planning system prompt instead and says so.
"""
import sys

SYSTEM = """\
You are the planner for a bot playing Uncharted Waters Origin, a sailing trade game.
You are given the bot's current situation and the actions available to it.
Answer with the single next action and one sentence of reasoning. Be concrete."""

QUESTION = """\
SITUATION
The fleet is AT SEA. The sea HUD reads:
  destination: none
  ETA: none
  speed: 0.0 knots
  supply: 11.6 days
  cargo: 639 / 4952
So the ship is stopped in open water with no course set.

THE TASK
"barter Birch Tree at Svear Village, and sail to Lisboa"
Birch Tree is bartered at Svear Village. Its recipe, read from the village today:
  515 Birch Tree per round, for 102 Iron + 51 Matchlock Gun + 116 Candle per round
The village allows 5 barter rounds today (0 used so far).

THE PLAN already computed
  7 rounds, expecting ~3605 Birch Tree
  buy 822 Iron, 411 Matchlock Gun, 934 Candle (includes a 15% cushion)
  Iron and Matchlock Gun are sold at Amsterdam; Candle at Barcelona.

THE REMAINING LEGS, in dependency order
  1. trim_before_gather  — sell non-materials to free hold space (OPTIONAL)
  2. gather:Amsterdam    — sail to Amsterdam, buy Iron and Matchlock Gun
  3. gather:Barcelona    — sail to Barcelona, buy Candle
  4. sell_surplus        — trim materials down to what the plan needs (REQUIRED)
  5. supply_verify       — confirm the fleet can be supplied
  6. sail_to_village     — sail to Svear Village
  7. barter              — barter 7 rounds
  8. sail_to_sell        — sail to Lisboa
  9. sell                — sell the Birch Tree

WHAT THE BOT CAN DO, AND WHERE
  At a port:   enter the market (buy/sell), enter the harbour (Supply Departure = leave port
               with supplies topped up), open the world map
  At sea:      open the world map (via the minimap), read the fleet panel, watch the HUD
  On the map:  choose a destination (Port tab for ports, Explore tab for villages, Route tab
               for saved routes) and commit it, or close the map
  At a village: barter
  Buying and selling happen only inside a market, which exists only at a port.

CONSTRAINT
  A ship with no destination does not move. Setting a course requires the world map.

QUESTION
What single action should the bot take next, and why?"""


def main() -> int:
    from vision.qwen_perception import _load

    if not _load():
        print("Qwen could not be loaded — is mlx-lm installed and the model downloaded?")
        return 1

    import vision.qwen_perception as q
    from mlx_lm import generate

    print("=" * 78)
    print("SYSTEM PROMPT")
    print("=" * 78)
    print(SYSTEM)
    print()
    print("=" * 78)
    print("QUESTION")
    print("=" * 78)
    print(QUESTION)
    print()

    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": QUESTION}]
    formatted = q._tokenizer.apply_chat_template(messages, tokenize=False,
                                                 add_generation_prompt=True)
    answer = generate(q._model, q._tokenizer, prompt=formatted, max_tokens=400, verbose=False)

    print("=" * 78)
    print(f"QWEN'S ANSWER  ({q._DEFAULT_MODEL})")
    print("=" * 78)
    print(answer.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
