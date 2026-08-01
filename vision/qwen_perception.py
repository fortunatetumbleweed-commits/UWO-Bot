# vision/qwen_perception.py
#
# L2.5 — Local LLM reasoning layer (Qwen).
#
# Sits between OCR (L2) and the KB cache (L3).  Runs on every perceive() call
# for every navigation state.  Combines:
#   - Full OCR token dump from the current frame
#   - Coarse nav_state from L0/L1 (sea / building / port_overworld / …)
#   - KB context relevant to that nav_state
#
# Returns a structured QwenPerception dict:
#   { "detail": str, "sub_menu": str|None, "flow_hint": str|None,
#     "overlays": list[str], "confidence": "high"|"low" }
#
# Returns None gracefully if the model is unavailable, so perceive() falls back
# to the existing keyword scan with no change in behaviour.
#
# Model backend: mlx-lm (Apple Silicon, in-process, same as brain/llm_parser.py).
# No separate server required.
# Default model: mlx-community/Qwen2.5-1.5B-Instruct-4bit.  The 0.5B variant
# (used by brain/llm_parser.py for simpler structured-output tasks) wasn't
# reliable enough at instruction following — it routinely confabulated the
# wrong building (e.g. labelling the inn as "harbor" when given OCR title
# "inn" plus a long disambiguation rules block).  1.5B fits in ~1 GB RAM
# and has noticeably better adherence to the prompt's ground-truth fields.
# Override via env var:
#   QWEN_PERCEPTION_MODEL — HuggingFace model id (default: mlx-community/Qwen2.5-1.5B-Instruct-4bit)

from __future__ import annotations

import json
import os
from typing import Optional

from loguru import logger

from brain.perceive import CONFIDENCE_HIGH, CONFIDENCE_LOW

# ── Constants ─────────────────────────────────────────────────────────────────

_DEFAULT_MODEL = "mlx-community/Qwen2.5-1.5B-Instruct-4bit"

# Shared model cache — same process as llm_parser.py so the model may already
# be loaded.  We keep a separate cache here because llm_parser uses a different
# system prompt and we do not want to interfere with its state.
_model     = None
_tokenizer = None

# Chrome glossary — static domain knowledge inlined into every Qwen prompt.
# Loaded lazily once and cached.  Stops Qwen from inventing narratives like
# "Wi-Fi at 8.46% battery" out of static UI furniture (gold counter, server
# name, in-game clock, player level).  See
# `memory/knowledge/control/qwen_chrome_glossary.md` for the canonical text.
_CHROME_GLOSSARY: Optional[str] = None


def _load_chrome_glossary() -> str:
    """Load and cache the chrome glossary.  Returns '' if the file is
    missing — Qwen still runs, just without the anti-hallucination KB."""
    global _CHROME_GLOSSARY
    if _CHROME_GLOSSARY is not None:
        return _CHROME_GLOSSARY
    from pathlib import Path
    path = (Path(__file__).parent.parent
            / "memory" / "knowledge" / "control"
            / "qwen_chrome_glossary.md")
    try:
        _CHROME_GLOSSARY = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning(
            f"[qwen] chrome glossary not found at {path} — running "
            "Qwen without anti-hallucination KB"
        )
        _CHROME_GLOSSARY = ""
    return _CHROME_GLOSSARY


def _serialise_elements(elements) -> str:
    """Serialise OmniParser DetectedElement list into a Qwen-friendly
    structured text block.  Each line: `[type] "label" @ (cx,cy) WxH`.

    Empty labels are kept for icons that the icon classifier saw but
    couldn't name; the bbox alone is still useful evidence that
    "something is there at this position."  Truncates at 80 elements
    so a busy screen doesn't blow the prompt budget.
    """
    if not elements:
        return ""
    lines = []
    for el in elements[:80]:
        label = (getattr(el, "label", "") or "").strip().replace('"', "'")
        etype = getattr(el, "element_type", "?")
        cx, cy = getattr(el, "cx", 0), getattr(el, "cy", 0)
        w, h = getattr(el, "width", 0), getattr(el, "height", 0)
        lines.append(f'  [{etype}] "{label}" @ ({cx},{cy}) {w}x{h}')
    suffix = ""
    if len(elements) > 80:
        suffix = f"\n  … ({len(elements) - 80} more elements omitted)"
    return "\n".join(lines) + suffix

# ── KB context loaders ────────────────────────────────────────────────────────

def _building_context(building_type: str) -> str:
    """Return KB context block for a building nav_state."""
    from pathlib import Path
    import json as _json

    kb_root = Path(__file__).parent.parent / "memory" / "knowledge" / "building_types"
    kb_path = kb_root / f"{building_type}.json"

    data: dict = {}
    if kb_path.exists():
        try:
            data = _json.loads(kb_path.read_text())
        except Exception:
            pass

    sub_menus   = data.get("sub_menus", [])
    flows       = data.get("flows", {})
    description = data.get("description", "")

    lines = [f"Building type: {building_type}"]
    if description:
        lines.append(f"Description: {description}")
    if sub_menus:
        sm_list = ", ".join(
            f"{sm['id']} (tab labels: {sm['labels']})" for sm in sub_menus
        )
        lines.append(f"Known sub-menus / tabs: {sm_list}")
    if flows:
        fl_list = ", ".join(f"{k} → {v}" for k, v in flows.items())
        lines.append(f"Sub-menu → flow map: {fl_list}")

    # ── Cross-building disambiguation (loaded from KB) ────────────────────────
    # List all known building types with their descriptions and distinguishing
    # tab labels so the LLM can identify the current building from context.
    lines.append("")
    lines.append("ALL KNOWN BUILDING TYPES IN THIS GAME:")
    try:
        from brain.kb import control as _ckb
        for btype, bdata in _ckb().known_building_types().items():
            desc = bdata.get("description", "")
            subs = bdata.get("sub_menus", [])
            tab_str = ", ".join(
                "/".join(sm["labels"]) for sm in subs
            ) if subs else "(no sub-menus)"
            variants = bdata.get("all_names", [btype])
            var_str  = f" (also: {', '.join(v for v in variants if v != btype)})" if len(variants) > 1 else ""
            lines.append(f"• {btype}{var_str}: {desc}")
            lines.append(f"  Tabs: {tab_str}")
    except Exception:
        pass

    lines += [
        "",
        "CRITICAL DISAMBIGUATION RULES:",
        "• Identify the building from TAB LABELS and SECTION HEADERS — not from payment text.",
        "• Yellow composite action buttons with ducat amounts appear in ALL payment contexts",
        "  (harbor supply, repair, recruit, item shop, market). Ducat presence alone does",
        "  NOT indicate you are at market.",
        "• HARBOR ≠ MARKET. Harbor supply deals with fleet provisions (water, food, ammo,",
        "  materials) for voyages — it does NOT buy or sell trade goods. Ducat amounts in",
        "  harbor are resupply costs, not trade transactions. 'Sell Over Supply' in harbor",
        "  sells excess provisions after a fleet configuration change — this is NOT market selling.",
        "• 'Recruit Crew' appears at both harbor and inn — use other tab labels to decide.",
        "• If tab labels are ambiguous or absent, set confidence=low.",
    ]
    return "\n".join(lines)


def _port_overworld_context() -> str:
    """Return KB context for port_overworld state."""
    from pathlib import Path
    import json as _json

    signals_path = Path(__file__).parent.parent / "memory" / "knowledge" / "control" / "ui_signals.json"
    try:
        data = _json.loads(signals_path.read_text())
    except Exception:
        return "Nav state: port_overworld"

    buildings  = list(data.get("building_name_variants", {}).keys())
    overlays   = data.get("port_overworld_overlay_keywords", [])
    lines = [
        "Nav state: port_overworld",
        f"Known building types: {', '.join(buildings)}",
        f"Overlay notice keywords: {', '.join(overlays[:10])}",
    ]
    return "\n".join(lines)


def _sea_context() -> str:
    """Return KB context for sea state."""
    from pathlib import Path
    import json as _json

    growth_path  = Path(__file__).parent.parent / "memory" / "knowledge" / "strategy" / "growth.json"
    signals_path = Path(__file__).parent.parent / "memory" / "knowledge" / "control" / "ui_signals.json"

    region_names: list[str] = []
    sea_kw: dict = {}

    try:
        region_names = _json.loads(growth_path.read_text()).get("sea_region_hud_names", [])
    except Exception:
        pass
    try:
        sea_kw = _json.loads(signals_path.read_text()).get("sea_state_keywords", {})
    except Exception:
        pass

    active_kw  = sea_kw.get("active_sailing",  [])
    encounter_kw = sea_kw.get("encounter",      [])
    lines = [
        "Nav state: sea",
        f"Known sea regions: {', '.join(region_names)}",
        f"Active sailing keywords: {', '.join(active_kw)}",
        f"Encounter keywords: {', '.join(encounter_kw)}",
    ]
    return "\n".join(lines)


def _world_map_context() -> str:
    """Return KB context for world_map state."""
    from pathlib import Path
    import json as _json

    signals_path = Path(__file__).parent.parent / "memory" / "knowledge" / "control" / "ui_signals.json"
    try:
        data = _json.loads(signals_path.read_text())
    except Exception:
        return "Nav state: world_map"

    wm_kw = data.get("world_map_ui_keywords", {})
    lines = [
        "Nav state: world_map",
        f"City-selected keywords: {', '.join(wm_kw.get('city_selected', []))}",
        f"Route-active keywords: {', '.join(wm_kw.get('route_active', []))}",
    ]
    return "\n".join(lines)


def _main_menu_context() -> str:
    """Return KB context for main_menu state."""
    from pathlib import Path
    import json as _json

    signals_path = Path(__file__).parent.parent / "memory" / "knowledge" / "control" / "ui_signals.json"
    try:
        data = _json.loads(signals_path.read_text())
    except Exception:
        return "Nav state: main_menu"

    items = data.get("main_menu_item_ids", [])
    id_list = ", ".join(item["id"] for item in items)
    lines = [
        "Nav state: main_menu",
        f"Known menu items: {id_list}",
    ]
    return "\n".join(lines)


def _loading_context() -> str:
    return "Nav state: loading\n(Detect destination name if visible)"


def _build_kb_context(nav_state: str, nav_detail: str) -> str:
    """Build the KB context block for the given nav_state."""
    if nav_state == "building":
        # Extract building type from detail, e.g. "building: harbor — departure"
        building_type = "unknown"
        if nav_detail:
            part = nav_detail.lower().replace("building:", "").strip()
            # Take first word before space or em-dash
            building_type = part.split()[0].rstrip("—").strip() if part else "unknown"
        return _building_context(building_type)
    elif nav_state == "port_overworld":
        return _port_overworld_context()
    elif nav_state in ("sea", "sea_cinematic"):
        return _sea_context()
    elif nav_state == "world_map":
        return _world_map_context()
    elif nav_state == "main_menu":
        return _main_menu_context()
    elif nav_state == "loading":
        return _loading_context()
    else:
        return f"Nav state: {nav_state}"


# ── Prompt construction ───────────────────────────────────────────────────────

_SYSTEM = """\
You are a perception assistant for a bot playing Uncharted Waters Origin (UWO).
You receive OCR tokens extracted from the game screen and KB context describing the current state.
Your job: determine the precise current state by reasoning over the OCR tokens and context.

Output rules — apply strictly:
- Return ONLY a valid JSON object. No explanation, no markdown fences, no prose before/after.
- "detail" must be ONE short sentence, ≤120 characters. State only what you observe; do NOT
  speculate, do NOT enumerate UI chrome (battery, Wi-Fi, server name, date/time), and do NOT
  invent metrics that aren't visible.
- Use null (not strings like "none") for fields with no value.
- Prefer short overlay ids over descriptive phrases."""

_QUESTIONS_BY_STATE = {
    "building":       "What sub-menu is active? What action button label is visible? Are any dialogs open?",
    "port_overworld": "Are any overlay notices visible (discovery, achievement, level-up, login reward)? Is the bot near a building entrance? Which building?",
    "sea":            "Is this active sailing or idle/cinematic? Which sea region HUD text is visible, if any? Is there an encounter or event banner?",
    "sea_cinematic":  "Is this idle/cinematic sea view? Is a sea region name or arrival notice visible?",
    "world_map":      "Is a city selected? Which one? Is a route highlighted? Is a City Info panel open?",
    "main_menu":      "Which menu item is focused or highlighted?",
    "loading":        "What is the loading destination? (city name, 'Preparing for Voyage', etc.)",
}

_OUTPUT_SCHEMA = """\
{
  "detail": "<ONE short sentence, ≤120 chars, no UI chrome or invented metrics>",
  "sub_menu": "<active sub-menu id or null>",
  "flow_hint": "<suggested flow id if a transaction appears in progress, or null>",
  "overlays": ["<overlay id>", ...],
  "confidence": "high or low",
  "scene_type": "<one of: village, harbor, market, inn, port_overworld, sea, world_map, building_other, dialog, unknown — leave null if unsure>",
  "task_complete": "true or false or null — only set when a 'Current task' line is given in the prompt; null otherwise"
}"""


# Scene-action cheatsheet — domain knowledge for Qwen.  When the bot
# sees these label sets, the scene_type is strongly determined.  Keep
# this small and high-signal; long lists dilute attention.
_SCENE_ACTION_CHEATSHEET = """\
Screen-type cheatsheet (use as STRONG evidence — when ≥2 labels from a
group appear in element labels or OCR tokens, the scene IS that type
regardless of structural classifier hints):

  • village interior — title is literally "Village" (no village name in
    the title; the name comes from the destination context).
    Actions:   Barter, Explore, Recruit, Gifting, Loot,
               Achievement Reward, Weekly Reward
  • harbor   — Depart Now, Supply Departure, Supply, Repair,
               Recruit Crew, Ready to sail
  • market   — Purchase, Sell, Recommended Purchase, Recommended Sell
  • inn      — Hire, Party, Rest, Mate
  • bureau   — Invest, Tax, Trade Permit, Market Event
  • castle   — Daily Reward, Quest, Audience
  • bank     — Deposit, Withdrawal, Savings Account, Insurance
  • shipyard — Build, Modify, Dismantle, Parts, Assemble
  • shop     — Black Market, Gear, Tool
  • temple/cathedral — Donate, Pray, Fortune"""


def _build_prompt(
    nav_state: str,
    nav_detail: str,
    ocr_tokens: list,
    parent_building: Optional[str] = None,
    elements: Optional[list] = None,
    task_hint: Optional[str] = None,
) -> str:
    """Build the Qwen L2.5 prompt.

    Sections (in order they appear in the prompt):
      1. Chrome glossary — static UI furniture catalogue (always present)
         to suppress hallucinations like "Wi-Fi 8.46% battery."
      2. Scene layout reference — markdown describing this screen's
         structure, loaded from `memory/knowledge/scene_layouts/`.
      3. KB context — building-type or sea/world-map KB excerpts.
      4. OmniParser elements — structured detections with bboxes/types
         (when *elements* is provided).  This anchors Qwen's reasoning
         to ground-truth pixel positions and prevents it from inventing
         coordinates.  See vision/omniparser.py for the producer.
      5. OCR tokens — kept as a fallback / cross-check (legacy input).
      6. Question + JSON schema.

    The layout content is logged at INFO so live runs make it clear
    whether scene-context was actually applied.
    """
    kb_ctx   = _build_kb_context(nav_state, nav_detail)
    question = _QUESTIONS_BY_STATE.get(nav_state, "What is the current state?")
    ocr_text = "\n".join(f"  {t}" for t, *_ in ocr_tokens) if ocr_tokens else "  (no OCR tokens)"

    # ── Chrome glossary (Phase 2) ──────────────────────────────────────────
    glossary = _load_chrome_glossary()
    glossary_section = (
        f"{glossary}\n\n" if glossary else ""
    )

    # ── OmniParser elements (Phase 1) ──────────────────────────────────────
    elements_section = ""
    if elements:
        serialised = _serialise_elements(elements)
        if serialised:
            elements_section = (
                "Detected UI elements on this screen "
                "(format: [type] \"label\" @ (cx,cy) WxH):\n"
                f"{serialised}\n\n"
            )

    # ── Scene layout context ────────────────────────────────────────────────
    layout_section = ""
    try:
        from vision.scene_layouts import load_layout
        layout_md = load_layout(nav_state, nav_detail, parent_building)
        if layout_md:
            logger.info(
                f"[qwen] layout: loaded {len(layout_md)} chars for "
                f"nav_state={nav_state!r} detail={nav_detail!r} "
                f"parent={parent_building!r}"
            )
            layout_section = (
                "Scene layout reference (markdown describing this screen's "
                "structure; treat as ground truth):\n"
                f"{layout_md}\n\n"
            )
        else:
            logger.info(
                f"[qwen] layout: no .md matched for nav_state={nav_state!r} "
                f"detail={nav_detail!r} parent={parent_building!r} — "
                "running Qwen WITHOUT layout context"
            )
    except Exception as e:
        logger.warning(
            f"[qwen] layout: load failed ({type(e).__name__}: {e}) — "
            "running Qwen without layout context"
        )

    # Static scene-action cheatsheet — short, high-signal.
    cheatsheet_section = f"{_SCENE_ACTION_CHEATSHEET}\n\n"

    # Task hint — when the caller knows the bot's current intent, hand
    # it to Qwen so it can cross-check "did we arrive?" against the
    # observed scene.  When set, Qwen is asked to populate
    # `task_complete`; when None, Qwen returns null for that field.
    task_section = ""
    if task_hint:
        task_section = (
            f"Current task: {task_hint}\n"
            "If the visible scene clearly matches the task's target "
            "destination (e.g. the bot is sailing to a village and the "
            "scene shows village action labels), set `task_complete: true`. "
            "Otherwise set `task_complete: false`.  Do NOT set "
            "task_complete=true just because the destination name appears "
            "in OCR — the scene must structurally match.\n\n"
        )

    return (
        f"{glossary_section}"
        f"{cheatsheet_section}"
        f"{layout_section}"
        f"{kb_ctx}\n\n"
        f"{elements_section}"
        f"OCR tokens from current screen:\n{ocr_text}\n\n"
        f"{task_section}"
        f"{question}\n\n"
        f"Return ONLY this JSON (no markdown):\n{_OUTPUT_SCHEMA}"
    )


# ── mlx-lm backend ────────────────────────────────────────────────────────────

def _load() -> bool:
    """Load the model on first use. Returns False if mlx-lm is not installed."""
    global _model, _tokenizer
    if _model is not None:
        return True
    model_id = os.environ.get("QWEN_PERCEPTION_MODEL", _DEFAULT_MODEL)
    try:
        from mlx_lm import load
        logger.info(f"[qwen] Loading {model_id} for L2.5 perception…")
        _model, _tokenizer = load(model_id)
        logger.info("[qwen] L2.5 model ready.")
        return True
    except ImportError:
        logger.debug("[qwen] mlx-lm not installed — L2.5 unavailable")
        return False
    except Exception as e:
        logger.debug(f"[qwen] Model load failed: {e}")
        return False


def _call_mlx(prompt: str) -> Optional[str]:
    """Run inference via mlx-lm. Returns raw text or None on failure."""
    if not _load():
        return None
    try:
        from mlx_lm import generate
        messages = [
            {"role": "system", "content": _SYSTEM},
            {"role": "user",   "content": prompt},
        ]
        formatted = _tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        raw = generate(_model, _tokenizer, prompt=formatted,
                       max_tokens=512, verbose=False)
        return raw.strip()
    except Exception as e:
        logger.debug(f"[qwen] Inference failed: {e}")
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def qwen_perceive(
    nav_state: str,
    nav_detail: str,
    ocr_tokens: list,
    parent_building: Optional[str] = None,
    elements: Optional[list] = None,
    task_hint: Optional[str] = None,
) -> Optional[dict]:
    """
    L2.5 reasoning over OCR tokens + OmniParser elements + KB context.

    Args:
        nav_state:       coarse state id from L0/L1 (e.g. "building", "sea")
        nav_detail:      detail string from where_am_i() (e.g. "building: harbor")
        ocr_tokens:      list of (text, conf, cx, cy) from OCR
        parent_building: optional parent building slug when nav_state is
                         "sub_menu" — enables sub-menu-specific layout
                         lookup (e.g. parent_building="harbor" +
                         detail="sub_menu: recruit crew" loads
                         scene_layouts/buildings/harbor/recruit_crew.md).
                         When None, sub_menu lookup falls back to the
                         default building chrome only.
        elements:        optional list of OmniParser DetectedElement.  When
                         provided, the prompt includes a structured
                         element list (label + type + bbox) so Qwen
                         reasons about real screen content instead of
                         guessing from a bag of OCR strings.  Pass the
                         cached `parse_fast_cached(frame)` result.

    Returns:
        dict with keys: detail, sub_menu, flow_hint, overlays, confidence
        None if model is unavailable (caller falls back to keyword scan)
    """
    import time as _time
    t_start = _time.monotonic()

    prompt = _build_prompt(
        nav_state, nav_detail, ocr_tokens,
        parent_building=parent_building, elements=elements,
        task_hint=task_hint,
    )
    logger.info(
        f"[qwen] call: nav_state={nav_state!r} detail={nav_detail!r} "
        f"parent={parent_building!r} prompt={len(prompt)} chars "
        f"ocr={len(ocr_tokens) if ocr_tokens else 0} tokens"
    )

    raw = _call_mlx(prompt)

    elapsed = _time.monotonic() - t_start
    if raw is None:
        logger.warning(
            f"[qwen] call returned None after {elapsed:.1f}s "
            f"(model unavailable or inference failed; nav_state={nav_state!r})"
        )
        return None

    # Strip markdown fences if model added them anyway
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    if not raw:
        logger.warning(
            f"[qwen] empty response after {elapsed:.1f}s "
            f"(nav_state={nav_state!r})"
        )
        return None

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(
            f"[qwen] JSON parse error after {elapsed:.1f}s: {e} — "
            f"raw[:200]={raw[:200]!r}"
        )
        return None

    # Normalise fields
    confidence = result.get("confidence", CONFIDENCE_HIGH)
    if confidence not in (CONFIDENCE_HIGH, CONFIDENCE_LOW):
        confidence = CONFIDENCE_HIGH

    detail = result.get("detail", nav_detail)

    # ── OCR-conflict guard ──────────────────────────────────────────────────
    # If the OCR-derived nav_detail names a specific building (e.g.
    # "building: inn") and Qwen's freeform detail mentions a DIFFERENT known
    # building name (e.g. "the harbor building"), Qwen has hallucinated.
    # The OCR title is ground truth; drop the conflicting Qwen detail back
    # to the OCR-derived nav_detail and downgrade confidence so the rest of
    # the pipeline knows to trust local signals or escalate.
    if nav_state == "building" and isinstance(detail, str) and detail:
        detail, confidence = _enforce_ocr_consistency(
            nav_detail, detail, confidence
        )

    # Normalise task_complete — only honoured when caller passed
    # task_hint; otherwise None regardless of what the model returned.
    raw_tc = result.get("task_complete")
    if task_hint and isinstance(raw_tc, bool):
        task_complete: Optional[bool] = raw_tc
    elif task_hint and isinstance(raw_tc, str):
        task_complete = raw_tc.strip().lower() == "true"
    else:
        task_complete = None

    output = {
        "detail":        detail,
        "sub_menu":      result.get("sub_menu") or None,
        "flow_hint":     result.get("flow_hint") or None,
        "overlays":      result.get("overlays") or [],
        "confidence":    confidence,
        "scene_type":    result.get("scene_type") or None,
        "task_complete": task_complete,
    }

    logger.info(
        f"[qwen] result ({elapsed:.1f}s): detail={detail[:60]!r} "
        f"sub_menu={output['sub_menu']!r} "
        f"flow_hint={output['flow_hint']!r} "
        f"overlays={output['overlays']} "
        f"confidence={output['confidence']}"
    )
    return output


# ── OCR consistency enforcement ───────────────────────────────────────────────

def _enforce_ocr_consistency(
    nav_detail:    str,
    qwen_detail:   str,
    confidence:    str,
) -> tuple[str, str]:
    """
    Reconcile Qwen's freeform `detail` with the OCR-derived `nav_detail` for
    the building nav_state.

    Two cases handled:

    1. STRICT — nav_detail's OCR title is itself a known building name
       (e.g. "building: inn" or "building: inn — ...").  If qwen_detail
       names a DIFFERENT known building type, the qwen_detail is
       hallucinating.  Replace the detail with nav_detail and downgrade
       confidence to low.

    2. SOFT — nav_detail's OCR title is NOT a canonical building name
       (e.g. "building: recruit crew" — a sub-screen / sub-menu title).
       In that case we can't tell which building owns the screen from the
       title alone.  But we can still detect Qwen NAMING a specific
       building in its description; if it does, we drop the Qwen
       free-text suffix back to the bare nav_detail and downgrade
       confidence — without claiming Qwen is necessarily wrong, since we
       don't know the parent building.  This catches cases like
       "building: recruit crew — The player is recruiting crew at the
       harbor" (when the bot was actually in the inn) while remaining
       conservative about cases where Qwen is right.

    Returns (sanitised_detail, sanitised_confidence).
    """
    # Extract the OCR title — first word after "building:" before any " — "
    nd = nav_detail.lower().replace("building:", "").split(" — ", 1)[0].strip()
    if not nd:
        return qwen_detail, confidence

    # Pull all known building names + variants from KB.
    try:
        from brain.kb import control as _ckb
        known: set[str] = set()
        for btype, bdata in _ckb().known_building_types().items():
            known.add(btype.lower())
            for v in bdata.get("all_names", []):
                known.add(v.lower())
    except Exception:
        return qwen_detail, confidence

    qd_lower = qwen_detail.lower()
    import re

    # Mentions of canonical building names in qwen_detail (whole-word match).
    qwen_mentions = sorted({
        b for b in known
        if re.search(rf"\b{re.escape(b)}\b", qd_lower)
    })

    # ── Case 1: OCR title IS a canonical building name (strict) ──────────────
    ocr_building = nd.split()[0]
    if ocr_building in known:
        conflicts = [b for b in qwen_mentions if b != ocr_building]
        if not conflicts:
            return qwen_detail, confidence
        logger.warning(
            f"[qwen] OCR title is {ocr_building!r} but detail names {conflicts!r} — "
            "dropping conflicting Qwen detail and downgrading confidence (strict)"
        )
        # Return empty detail so perceive.py's `if l25_detail:` skips the
        # append step.  Returning nav_detail itself caused a duplicated
        # "building: X — building: X" prefix because perceive.py prepends
        # the OCR title before appending the qwen detail.
        return "", CONFIDENCE_LOW

    # ── Case 2: OCR title is a sub-screen / unknown title (soft) ─────────────
    # We don't know which building owns this sub-screen, so we can't claim
    # Qwen is wrong — just that it's NAMING a specific building when we
    # can't verify.  Drop the Qwen suffix to avoid propagating misleading
    # text and downgrade confidence so downstream code re-classifies via Claude.
    if qwen_mentions:
        logger.warning(
            f"[qwen] OCR title is sub-screen {nd!r} (not a canonical building) "
            f"but detail names {qwen_mentions!r} — dropping Qwen suffix and "
            "downgrading confidence (soft)"
        )
        # Same return-empty rationale as the strict case above — avoids the
        # duplicated 'building: recruit crew — building: recruit crew' prefix
        # that surfaced in the most recent live run.
        return "", CONFIDENCE_LOW

    return qwen_detail, confidence
