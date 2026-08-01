# training/claude_fallback.py
# Claude Vision calls used as teacher fallbacks when local models fail.
#
# Each function:
#   1. Encodes the image and calls Claude
#   2. Parses the structured response
#   3. Saves the example via training.collector
#   4. Returns the result for immediate use
#
# New task types can be added by following the market_tile pattern:
#   - define a prompt
#   - define a parse function
#   - expose a public function that calls _call_claude + save_example
#
# All functions return None if Claude is unavailable (no API key, network error).

from __future__ import annotations

import base64
import io
import json
import os
import re
from typing import Any

from loguru import logger
from PIL import Image


# ── shared Claude client ───────────────────────────────────────────────────────

def _get_client():
    """Return an anthropic.Anthropic client, or None if unavailable."""
    try:
        import anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            logger.debug("ANTHROPIC_API_KEY not set — Claude fallback disabled")
            return None
        return anthropic.Anthropic(api_key=api_key)
    except ImportError:
        logger.debug("anthropic package not installed — Claude fallback disabled")
        return None


def _encode(image: Image.Image, max_width: int = 600) -> str:
    """Encode a PIL image as base64 JPEG (small crop — keep tokens low)."""
    w, h = image.size
    if w > max_width:
        image = image.resize((max_width, int(h * max_width / w)), Image.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=85)
    return base64.standard_b64encode(buf.getvalue()).decode()


def _extract_json(text: str) -> dict | None:
    """Extract a JSON object from a text response that may contain prose."""
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    fence = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return None


def _call_claude(image: Image.Image, prompt: str, system: str) -> dict | None:
    """Send one image + prompt to Claude and return parsed JSON, or None."""
    client = _get_client()
    if client is None:
        return None
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            system=system,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": _encode(image),
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        raw = resp.content[0].text
        parsed = _extract_json(raw)
        if parsed is None:
            logger.warning(f"Claude fallback: could not parse JSON: {raw[:200]}")
        return parsed
    except Exception as exc:
        logger.warning(f"Claude fallback API error: {exc}")
        return None


# ── Market tile ────────────────────────────────────────────────────────────────

_MARKET_TILE_SYSTEM = (
    "You are reading UI tiles from the mobile game 'Uncharted Waters Origin' (UWO). "
    "The game is an Age of Sail trading RPG. Return only valid JSON, no prose."
)

_MARKET_TILE_PROMPT = """\
This is a single trade-good tile from the UWO market screen (tab: {tab}).
The tile shows: item icon on the left, item name + category text on the upper right,
a price index % badge, and a numeric price (coins) on the lower right.
{sell_hint}
Return ONLY this JSON:
{{
  "name":     "<item name, e.g. 'Gold Dust'>",
  "category": "<category, e.g. 'Metals'>",
  "price":    <integer price in coins, or null if not visible>,
  "index_pct": <integer index %, or null if not visible>
}}
If you cannot read the name, set "name" to null."""

_SELL_HINT = (
    "For the sell tab, the price format is 'N(±M)' where N is the base price "
    "and M is the gain/loss; return only N as the price."
)


def read_market_tile(
    crop: Image.Image,
    tab: str = "purchase",
    local_name: str | None = None,
    local_confidence: float | None = None,
    trigger: str = "no_name",
) -> dict[str, Any] | None:
    """
    Ask Claude to read one market tile crop.

    Parameters
    ----------
    crop             : PIL image of the tile region
    tab              : "purchase" or "sell"
    local_name       : what OCR read (may be wrong or None)
    local_confidence : OCR confidence (or None)
    trigger          : why Claude was called ("no_name", "no_price", "low_conf")

    Returns {"name", "category", "price", "index_pct"} or None on failure.
    The result is also saved to data/training/market_tile/.
    """
    prompt = _MARKET_TILE_PROMPT.format(
        tab=tab,
        sell_hint=_SELL_HINT if tab == "sell" else "",
    )
    result = _call_claude(crop, prompt, _MARKET_TILE_SYSTEM)
    if result is None:
        return None

    logger.info(
        f"  Claude fallback [market_tile/{tab}]: "
        f"OCR={local_name!r} → Claude={result.get('name')!r}  "
        f"price={result.get('price')}  idx={result.get('index_pct')}%"
    )

    # If Claude corrected a name misread, save it to the correction cache so
    # the same Claude call is never needed again for this OCR error.
    from training.collector import save_example, save_name_correction
    claude_name = result.get("name")
    if local_name and claude_name and local_name != claude_name:
        save_name_correction(local_name, claude_name)

    save_example(
        category="market_tile",
        image=crop,
        claude_label=result,
        local_prediction=local_name,
        local_confidence=local_confidence,
        trigger=trigger,
    )

    return result


# ── Scene classifier fallback ─────────────────────────────────────────────────
# Future home for: when MobileNetV3 confidence < 0.70, ask Claude.

_SCENE_SYSTEM = (
    "You are classifying screenshots from 'Uncharted Waters Origin' (UWO). "
    "Return only valid JSON."
)

_SCENE_PROMPT = """\
Classify this UWO screenshot. Valid scene types:
  port_overworld, port_map, building_interior, sub_menu, sea, market,
  world_map, loading, dialog_gameplay, dialog_system, unknown

Return ONLY:
{"scene_type": "<type>", "confidence": "<high|medium|low>", "notes": "<one line>"}"""


def classify_scene(
    frame: Image.Image,
    local_scene_type: str | None = None,
    local_confidence: float | None = None,
) -> dict[str, Any] | None:
    """
    Ask Claude to classify a full screenshot when the local classifier is uncertain.
    Saves the example to data/training/scene_type/.
    """
    result = _call_claude(frame, _SCENE_PROMPT, _SCENE_SYSTEM)
    if result is None:
        return None

    logger.info(
        f"  Claude fallback [scene_type]: "
        f"local={local_scene_type!r} ({local_confidence}) "
        f"→ Claude={result.get('scene_type')!r}"
    )

    from training.collector import save_example
    save_example(
        category="scene_type",
        image=frame,
        claude_label=result,
        local_prediction=local_scene_type,
        local_confidence=local_confidence,
        trigger="low_conf",
    )

    return result
