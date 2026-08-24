"""AI KB-ingestion compiler (#28) — the "AI WRITES the KB" side of the two-brain
design (project_two_ai_brains_architecture_2026-08-14 / project_barter_kb_blueprint).

Claude turns the fuzzy, seasonal game surface — barter-panel / preference reads AND
pasted human text (an article, a season announcement) — into STRUCTURED
barter_kb records (BarterRecipe, PortPreference). This is what keeps the KB current
as the investment season rotates (project_investment_season_vs_weather); fixed rules
can't. Both AI and the deterministic solvers then READ those records.

The LLM call is injectable, so the JSON parse + validation + save is unit-testable
without a live model. Records are stamped with the INVESTMENT season number.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Optional

from loguru import logger

from memory.barter_kb import BarterRecipe, PortPreference


_SCHEMA_HINT = """Output ONE JSON object, no prose, of this shape:
{
  "recipes": [
    {
      "good": "<barter output good>",
      "inputs": [{"material": "<name>", "ratio": <int per round>,
                  "source_ports": ["<port>", ...]}],
      "villages": ["<village offering it>", ...],
      "output_per_round": {"Neutral": <int>, "Favorable": <int>,
                           "Trusting": <int>, "Friendly": <int>},
      "preconditions": {"amity_min": "<Neutral|Favorable|Trusting|Friendly|null>",
                        "trade_level_min": <int|null>, "guild": "<str|null>",
                        "event": "<str|null>"},
      "notes": "<optional>"
    }
  ],
  "preferences": [
    {"port": "<port>", "preferences": {"<Category>": <signed int markup %>, ...}}
  ]
}
Include only fields you can determine; omit or null the rest. Numbers are integers."""


def build_prompt(text: str) -> str:
    """The extraction prompt: raw game/article text → structured barter knowledge."""
    return (
        "You are compiling Uncharted Waters Origin BARTER knowledge into a database.\n"
        "From the source below, extract the season's barter recipes (output good, input\n"
        "materials + per-round ratios, source ports, villages, per-amity yields,\n"
        "preconditions) and any port sell-PREFERENCES (per-category markup %).\n\n"
        f"{_SCHEMA_HINT}\n\nSOURCE:\n{text}\n"
    )


def _extract_json(raw: str) -> dict:
    """Pull the JSON object out of an LLM reply (tolerates ```json fences / prose)."""
    if not raw:
        return {}
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception as exc:
        logger.warning(f"[kb_ingest] JSON parse failed: {exc}")
        return {}


def parse_ingest_json(raw: str, season: str = "") -> dict:
    """Parse an LLM reply into {'recipes': [BarterRecipe], 'preferences': [PortPreference]},
    stamping each record with the investment `season`. Malformed entries are skipped."""
    data = _extract_json(raw)
    recipes, prefs = [], []
    for rd in data.get("recipes", []) or []:
        try:
            rd = dict(rd)
            rd.setdefault("season", season)
            recipes.append(BarterRecipe.from_dict(rd))
        except Exception as exc:
            logger.warning(f"[kb_ingest] bad recipe entry skipped: {exc}")
    for pd in data.get("preferences", []) or []:
        try:
            pd = dict(pd)
            pd.setdefault("season", season)
            prefs.append(PortPreference.from_dict(pd))
        except Exception as exc:
            logger.warning(f"[kb_ingest] bad preference entry skipped: {exc}")
    return {"recipes": recipes, "preferences": prefs}


def ingest_barter_knowledge(text: str, season: str = "", *,
                            llm_fn: Optional[Callable[[str], str]] = None,
                            save: bool = True) -> dict:
    """Compile `text` into KB records via the LLM and (optionally) persist them.

    Returns {recipes, preferences, saved}. Records are stamped with the investment
    `season`; `save` writes them through memory.barter_kb (upsert by key)."""
    if llm_fn is None:
        from brain.llm_client import claude_llm_fn
        llm_fn = claude_llm_fn

    raw = llm_fn(build_prompt(text))
    out = parse_ingest_json(raw, season)

    saved = 0
    if save:
        from memory.barter_kb import save_recipe, save_preference
        for r in out["recipes"]:
            save_recipe(r)
            saved += 1
        for p in out["preferences"]:
            save_preference(p)
            saved += 1
    out["saved"] = saved
    logger.info(f"[kb_ingest] season={season!r}: "
                f"{len(out['recipes'])} recipes, {len(out['preferences'])} prefs, saved={saved}")
    return out
