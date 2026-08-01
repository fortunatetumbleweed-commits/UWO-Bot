# brain/llm_parser.py
#
# Natural-language command parser using a local Qwen2.5-0.5B model via mlx-lm.
# Runs entirely in-process on Apple Silicon — no separate server needed.
#
# Strategy: translate the user's free-form text into the bot's canonical command
# syntax (e.g. "what does Bergen sell" → "map trade Bergen"), then pass the
# result through the existing _parse() function so all dispatch logic stays
# in one place.
#
# Model is loaded lazily on the first call and cached for the session.

from __future__ import annotations

import re
from typing import Optional
from loguru import logger

_model = None
_tokenizer = None

MODEL_ID = "mlx-community/Qwen2.5-0.5B-Instruct-4bit"

_SYSTEM_PROMPT = """\
You are a command parser for a maritime trading game bot.
Translate the user's message into EXACTLY ONE command from the examples below.
Output ONLY the command — no explanation, no extra words, no angle brackets.
Fix typos in port names, building names, and numbers.

Examples (user message → command to output):
  "what does Bergen sell"              → map trade Bergen
  "show trade info for London"         → map trade London
  "list all known ports"               → map ports
  "show all ports"                     → map ports
  "gather 5 new ports"                 → map gather 5
  "scan 10 ports"                      → map gather 10
  "scan new ports"                     → map gather
  "refresh all port data"              → map gather refresh
  "rescan 5 ports"                     → map gather refresh 5
  "get trade info for London"          → map gather port London
  "read world map ports"               → map explore
  "go to the castle"                   → goto castle
  "navigate to the market"             → goto market
  "explore the port"                   → explore
  "explore for 30 minutes"             → explore 30
  "leave the building"                 → exit
  "go back"                            → exit
  "show knowledge base"                → kb
  "what do we know about Bergen"       → kb Bergen
  "show route from London to Lisbon"         → route London Lisbon
  "create a trade route Bergen to Lisbon"    → route create Bergen Lisbon
  "run the trade route London to Port Royal" → route run London Port Royal
  "start the London Port Royal route"        → route run London Port Royal
  "list my trade routes"                     → route list
  "run the daily routine"                    → routine daily
  "list routines"                            → routines
  "show help"                                → help
  "stop the bot"                             → quit
"""


def _load() -> bool:
    """Load the model on first use. Returns False if mlx-lm is not installed."""
    global _model, _tokenizer
    if _model is not None:
        return True
    try:
        from mlx_lm import load
        logger.info(f"Loading {MODEL_ID} for natural-language parsing…")
        _model, _tokenizer = load(MODEL_ID)
        logger.info("LLM parser ready.")
        return True
    except ImportError:
        logger.warning(
            "mlx-lm not installed — natural language parsing unavailable. "
            "Run: pip install mlx-lm"
        )
        return False
    except Exception as e:
        logger.warning(f"Failed to load LLM parser: {e}")
        return False


def _known_ports() -> list[str]:
    try:
        from actions.world_map import list_known_ports
        return list_known_ports()
    except Exception:
        return []


def parse(user_input: str) -> tuple[str, str]:
    """
    Translate *user_input* to a canonical command via the local LLM, then
    run it through _parse() and return (action, arg).

    Falls back to ("unknown", user_input) if the model is unavailable or
    produces output that _parse() cannot map.
    """
    from run import _parse   # import here to avoid circular import at module load

    if not _load():
        return ("unknown", user_input)

    # Build system prompt — include known ports so the model can correct typos
    ports = _known_ports()
    system = _SYSTEM_PROMPT
    if ports:
        system += f"\nKnown ports: {', '.join(ports)}\n"

    messages = [
        {"role": "system",  "content": system},
        {"role": "user",    "content": user_input},
    ]

    try:
        from mlx_lm import generate
        prompt = _tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        raw = generate(_model, _tokenizer, prompt=prompt,
                       max_tokens=32, verbose=False)

        # The model sometimes wraps output in quotes or adds trailing punctuation
        cmd = raw.strip().strip('"\'').rstrip(".")
        # Take only the first line in case the model adds commentary
        cmd = cmd.splitlines()[0].strip()

        logger.info(f"LLM parsed {user_input!r} → {cmd!r}")

        action, arg = _parse(cmd)
        if action != "unknown":
            return action, arg

        # If _parse still can't handle it, log and return unknown
        logger.warning(f"LLM output not recognised by parser: {cmd!r}")

    except Exception as e:
        logger.warning(f"LLM parse error: {e}")

    return ("unknown", user_input)
