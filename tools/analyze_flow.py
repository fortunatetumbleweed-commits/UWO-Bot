# tools/analyze_flow.py
# Analyzes a sequence of transaction screenshots and produces a structured
# flow definition the bot can follow.
#
# For each screenshot it runs OmniParser (if available) for element detection,
# then sends the full sequence to Claude with flow-context awareness.
#
# Claude receives:
#   - All screenshots as thumbnails (so it sees the progression)
#   - Each screenshot's OmniParser element table
#   - The flow name and a description of what transaction is being performed
#
# Output: memory/knowledge/flows/<flow_name>/flow.json
#
# Usage:
#   python tools/analyze_flow.py market_sell
#   python tools/analyze_flow.py market_sell --screenshots path/to/dir

from __future__ import annotations

from typing import Optional
import base64
import io
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from PIL import Image


# ── Data structures ────────────────────────────────────────────────────────────

FLOW_DESCRIPTIONS = {
    "market_sell": (
        "Player selling goods from ship cargo at the market. "
        "Steps: Sell tab → goods tiles visible → add to basket → tap Sell button → "
        "optional negotiation dialog → confirmation."
    ),
    "market_buy": (
        "Player buying goods at the market. "
        "Steps: Purchase tab → goods tiles visible → add to basket → tap Buy button → "
        "optional negotiation dialog → confirmation."
    ),
    "market_negotiate": (
        "Trade negotiation dialog that may appear after tapping Sell or Buy. "
        "The NPC makes a counter-offer. Player can accept, counter, or decline."
    ),
}


# ── OmniParser element extraction ─────────────────────────────────────────────

def _omniparser_elements(frame: Image.Image) -> list:
    # Flow analysis uses Claude's vision directly — OmniParser's Florence-2 model
    # requires ~12GB MPS and consistently OOMs when called per-frame in a sequence.
    # Claude reading the sequence of images is the primary analysis path here.
    return []


def _element_table(elements: list) -> str:
    if not elements:
        return "(no elements detected — Claude using image only)"
    lines = ["idx |  cx  |  cy  | type   | label"]
    lines.append("----|------|------|--------|------")
    for i, el in enumerate(elements):
        lines.append(f"{i:3d} | {el.cx:4d} | {el.cy:4d} | {el.element_type:6s} | {el.label}")
    return "\n".join(lines)


def _encode(frame: Image.Image) -> str:
    buf = io.BytesIO()
    w, h = frame.size
    if w > 1000:
        frame = frame.resize((1000, int(h * 1000 / w)), Image.LANCZOS)
    frame.save(buf, format="JPEG", quality=75)
    return base64.standard_b64encode(buf.getvalue()).decode()


# ── Claude analysis ────────────────────────────────────────────────────────────

_SYSTEM = """\
You are a game UI analyst for Uncharted Waters Origin (UWO), a mobile Age of Sail trading RPG.
You will receive a sequence of screenshots taken during a specific in-game transaction.
Your job is to document the EXACT flow: what each screen shows, what action the bot must take,
and what screen comes next — so the bot can autonomously replicate this transaction.\
"""


def _build_prompt(flow_name: str, steps: list) -> tuple:
    """
    Build the Claude prompt and content blocks for a multi-step flow analysis.
    Returns (text_prompt, content_blocks_list).
    """
    flow_desc = FLOW_DESCRIPTIONS.get(flow_name, f"Transaction flow: {flow_name}")

    content: list[dict] = []

    # Add all screenshots with their element tables
    for i, step in enumerate(steps):
        label = step.get("label", f"step_{i:02d}")
        elements_str = step.get("elements_str", "(no elements)")

        content.append({
            "type": "text",
            "text": f"\n--- SCREENSHOT {i} ({label}) ---\nOmniParser elements:\n{elements_str}\n",
        })
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": step["image_b64"],
            },
        })

    # Final instruction
    prompt_text = f"""
Transaction flow: {flow_name}
Description: {flow_desc}

Above are {len(steps)} screenshots taken in sequence during this transaction.
Analyze the complete flow and return ONLY this JSON:

{{
  "flow_name": "{flow_name}",
  "description": "{flow_desc}",
  "steps": [
    {{
      "step": 0,
      "name": "short_snake_case_name",
      "screen_description": "what is shown on this screen",
      "active_tab": "purchase | sell | none",
      "key_elements": [
        {{"label": "element name", "tap_x": 0, "tap_y": 0, "purpose": "what tapping this does"}}
      ],
      "bot_action": "exact action the bot should take at this step",
      "action_target": {{"label": "what to tap", "tap_x": 0, "tap_y": 0}},
      "leads_to": ["step_name_a", "step_name_b"],
      "is_optional": false,
      "notes": "anything important the bot must know about this step"
    }}
  ],
  "entry_point": "name of first step",
  "success_endpoint": "name of final step when transaction completes",
  "optional_branches": ["list of step names that are optional, e.g. negotiation"]
}}

Rules:
- tap_x / tap_y are in the THUMBNAIL image coordinates you see (width ≤ 1000px).
  Do NOT attempt to scale them — the bot will scale them to screen space at runtime.
- Use the pixel positions as you observe them in the images provided
- bot_action must be specific: e.g. "tap Put-In-Bulk checkbox to enable bulk mode"
  NOT vague like "interact with UI"
- For optional steps (like negotiation), set is_optional: true
- leads_to can be multiple entries if the screen can branch (e.g. negotiation or skip to confirm)
- The basket/right panel shows selected goods — it is NOT tappable for trading purposes
- The Sell/Buy button turns YELLOW when the basket has items — tap it then\
"""

    content.append({"type": "text", "text": prompt_text})
    return prompt_text, content


# ── Main analyzer ──────────────────────────────────────────────────────────────

def analyze_flow(flow_name: str, screenshots_dir: Path) -> Optional[Path]:
    """
    Analyze a sequence of screenshots and write flow.json.
    Returns path to flow.json, or None on failure.
    """
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set")
        return None

    # Load screenshots in order
    png_files = sorted(screenshots_dir.glob("step_*.png"))
    if not png_files:
        logger.error(f"No step_*.png files found in {screenshots_dir}")
        return None

    # Load manifest labels if present
    manifest_path = screenshots_dir.parent / "manifest.txt"
    labels: dict[str, str] = {}
    if manifest_path.exists():
        for line in manifest_path.read_text().splitlines():
            if "\t" in line:
                fname, label = line.split("\t", 1)
                labels[fname.strip()] = label.strip()

    logger.info(f"Analyzing {len(png_files)} screenshots for flow '{flow_name}'…")

    steps = []
    for i, png in enumerate(png_files):
        label = labels.get(png.name, png.stem)
        logger.info(f"  Processing {png.name} ({label})…")

        frame = Image.open(png)
        elements = _omniparser_elements(frame)
        elements_str = _element_table(elements)
        image_b64 = _encode(frame)

        steps.append({
            "label": label,
            "filename": png.name,
            "elements_str": elements_str,
            "image_b64": image_b64,
        })

    logger.info(f"Sending {len(steps)} screenshots to Claude for flow analysis…")

    _, content_blocks = _build_prompt(flow_name, steps)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=_SYSTEM,
            messages=[{"role": "user", "content": content_blocks}],
        )
        raw = response.content[0].text
    except Exception as exc:
        logger.error(f"Claude API error: {exc}")
        return None

    # Parse JSON
    parsed = None
    try:
        parsed = json.loads(raw.strip())
    except json.JSONDecodeError:
        m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        if parsed is None:
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                try:
                    parsed = json.loads(m.group())
                except json.JSONDecodeError:
                    pass

    if not parsed:
        logger.error(f"Could not parse flow JSON:\n{raw[:500]}")
        return None

    # Save flow.json
    flow_dir = screenshots_dir.parent
    flow_path = flow_dir / "flow.json"
    flow_path.write_text(json.dumps(parsed, indent=2, ensure_ascii=False))

    logger.info(f"Flow saved → {flow_path}")
    _print_flow_summary(parsed)
    return flow_path


def _print_flow_summary(flow: dict) -> None:
    logger.info(f"\nFlow: {flow.get('flow_name')} — {len(flow.get('steps', []))} steps")
    for step in flow.get("steps", []):
        optional = " [optional]" if step.get("is_optional") else ""
        leads = " → " + ", ".join(step.get("leads_to", [])) if step.get("leads_to") else ""
        logger.info(
            f"  [{step['step']}] {step['name']}{optional}{leads}\n"
            f"        action: {step.get('bot_action', '?')}"
        )


# ── Flow loader (used by market_actions.py) ────────────────────────────────────

def load_flow(flow_name: str) -> Optional[dict]:
    """Load a previously analyzed flow by name."""
    flow_path = Path("memory/knowledge/flows") / flow_name / "flow.json"
    if not flow_path.exists():
        return None
    try:
        return json.loads(flow_path.read_text())
    except Exception as exc:
        logger.warning(f"Failed to load flow {flow_name}: {exc}")
        return None


def get_step(flow: dict, step_name: str) -> Optional[dict]:
    """Find a step by name in a flow."""
    return next(
        (s for s in flow.get("steps", []) if s.get("name") == step_name),
        None,
    )


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]

    screenshots_dir = None
    if "--screenshots" in args:
        idx = args.index("--screenshots")
        screenshots_dir = Path(args[idx + 1])
        args = args[:idx] + args[idx + 2:]

    if not args:
        print("Usage: python tools/analyze_flow.py <flow_name> [--screenshots <dir>]")
        sys.exit(1)

    flow_name = args[0]
    if screenshots_dir is None:
        screenshots_dir = Path("memory/knowledge/flows") / flow_name / "screenshots"

    from memory.logger import setup_logging
    setup_logging()

    result = analyze_flow(flow_name, screenshots_dir)
    if result:
        print(f"\nFlow written to {result}")
    else:
        print("Flow analysis failed.")
        sys.exit(1)
