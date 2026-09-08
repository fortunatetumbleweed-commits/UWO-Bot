"""Phase B2a — LLM consult for novel obstructions with goal context.

When the structural classifier (Layer A) reports an obstruction
(`kind=dialog|overlay|popup`) but no known interruptor matched its
keywords, fall back to Claude.  Claude gets:

  - A thumbnail of the frame
  - The OCR text inside the obstruction's bbox (whole sentences,
    spatial positions preserved)
  - The current `GoalContext` chain — what the bot was trying to do

Claude returns a structured `ObstructionAnalysis` describing the
obstruction's purpose, dismissal method, and whether it advances or
blocks the current goal.

This module DOES NOT dismiss anything.  Phase B2a is data collection:
results are cached forever (so the same obstruction never costs a
second API call), and every (input, output) pair is appended to
`data/training/obstructions/log.jsonl` for future Qwen distillation.

Origin: 2026-05-15.  See `feedback_goal_aware_perception_design.md`
for the full layered architecture and Qwen distillation plan.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from loguru import logger
from PIL import Image


# ── Storage paths ──────────────────────────────────────────────────────────


_CACHE_DIR = Path("memory/knowledge/obstruction_analyses")
_TRAINING_LOG = Path("data/training/obstructions/log.jsonl")


# ── Result type ───────────────────────────────────────────────────────────


@dataclass
class ObstructionAnalysis:
    """Claude's structured interpretation of an obstruction.

    Fields mirror the JSON schema Claude is asked to fill.  All
    fields are JSON-safe so the record can round-trip through the
    cache + training log without custom serialization.
    """

    purpose:            str         # one-sentence explanation
    full_text:          str         # the entire visible message (NPC line, dialog body, banner text…)
    dismissal:          str         # 'tap_anywhere', 'tap_close_x', 'tap_accept', 'tap_decline', 'unknown', etc.
    relates_to_goal:    bool        # True if the obstruction is connected to the current goal
    outcome_for_goal:   str         # 'likely_resolved' | 'still_blocking' | 'irrelevant' | 'unknown'
    confidence:         str         # 'high' | 'medium' | 'low'
    # Provenance — populated by the consult helper, not Claude
    obstruction_kind:   str = ""
    structural_hash:    str = ""
    goal_intent:        Optional[str] = None
    cached_at:          str = ""
    visit_count:        int = 1
    raw_api_response:   dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ObstructionAnalysis":
        # Permissive load — extra keys are ignored, missing keys
        # fall back to dataclass defaults.
        known = {k: v for k, v in d.items()
                 if k in cls.__dataclass_fields__}
        return cls(**known)


# ── Cache + training-data IO ──────────────────────────────────────────────


def _structural_hash(tokens_in_bbox: List[Tuple[str, int, int]]) -> str:
    """Build a stable hash from the OCR tokens inside the obstruction.

    Sorted by text content (spatial positions are not part of the
    hash so minor pixel drift between visits still cache-hits).
    """
    payload = "|".join(sorted(
        (t.strip().lower() for t, _cx, _cy in tokens_in_bbox if t.strip()),
        key=str,
    ))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _cache_path(kind: str, structural_hash: str, goal_intent: Optional[str]) -> Path:
    goal = (goal_intent or "no_goal").replace(" ", "_").replace("/", "_")
    return _CACHE_DIR / f"{kind}__{structural_hash}__{goal}.json"


def _load_cached(path: Path) -> Optional[ObstructionAnalysis]:
    if not path.exists():
        return None
    try:
        return ObstructionAnalysis.from_dict(json.loads(path.read_text()))
    except Exception as e:
        logger.warning(f"[obstruction_consult] cache read failed for {path}: {e}")
        return None


def _save_cached(path: Path, analysis: ObstructionAnalysis) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(analysis.to_dict(), indent=2, ensure_ascii=False))


def _append_training_record(record: dict) -> None:
    """Append one JSONL line.  Always-on data-flywheel collection."""
    try:
        _TRAINING_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _TRAINING_LOG.open("a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"[obstruction_consult] training-log write failed: {e}")


# ── Claude prompt ─────────────────────────────────────────────────────────


_CONSULT_PROMPT = """\
You are analysing a UI obstruction in the mobile game "Uncharted Waters Origin" (UWO),
an Age of Sail trading/exploration RPG.

The bot's structural classifier has detected a likely obstruction on screen:
  - kind: {kind}
  - confidence: {classifier_confidence}
  - bbox: {bbox}

The OCR text inside the obstruction's bounding box reads (left-to-right, top-to-bottom):
{ocr_text}

Bot's current goal (most-recent first):
{goal_chain}

Return ONLY a JSON object with this exact shape:
{{
  "purpose":          "one-sentence explanation of what this obstruction is about in game context",
  "full_text":        "the entire visible message — concatenate the OCR lines into a readable sentence",
  "dismissal":        "tap_anywhere | tap_close_x | tap_accept | tap_decline | tap_ok | press_back | unknown",
  "relates_to_goal":  true | false,
  "outcome_for_goal": "likely_resolved | still_blocking | irrelevant | unknown",
  "confidence":       "high | medium | low"
}}

Rules:
- `purpose` should explain the obstruction in game-domain terms, not UI terms.
  Good: "Harbor Official confirms crew was recruited"
  Bad:  "A speech bubble at the centre of the screen"
- `full_text` should reconstruct the message even if OCR fragmented it.
- `dismissal`: pick the SAFEST action that clears the obstruction without
  ejecting the bot from a useful state.  `tap_anywhere` for translucent
  overlays.  `tap_close_x` when there's an explicit X to close.
  `press_back` is the LAST resort — it exits the underlying screen.
  CRITICAL: never recommend `press_back` for popups that appear on a
  village screen (top-left shows a village name like "Berber Village").
  In a village, back is the system gesture to LEAVE the village
  entirely (back to sea), so a back-press to dismiss a popup also
  exits the fleet from the village.  Always prefer `tap_close_x` or
  `tap_anywhere` for village popups.  Same rule for popups on
  port_overworld (top-left shows a port name, no back arrow) — back
  there opens the main menu rather than dismissing the popup.
- `relates_to_goal` = true when the obstruction is about (or caused by) the
  current goal.  e.g. crew-hired overlay relates to a recruit_crew goal;
  daily login reward does NOT relate to a sell_all_cargo goal.
- `outcome_for_goal`:
    likely_resolved   — the obstruction indicates the goal's success state
    still_blocking    — the obstruction must be dealt with before the goal advances
    irrelevant        — the obstruction is unrelated (e.g. login reward during trade)
    unknown           — cannot tell from the visible text alone
"""


# ── Claude call wrapper ───────────────────────────────────────────────────


def _call_claude(
    prompt: str,
    frame: Image.Image,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 1024,
) -> Optional[dict]:
    """Call Claude with the prompt + a thumbnail.  Returns the parsed
    JSON dict or None on failure.  Silently disabled when
    ANTHROPIC_API_KEY is missing.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.debug(
            "[obstruction_consult] ANTHROPIC_API_KEY not set — skipping consult"
        )
        return None
    try:
        import anthropic
    except ImportError:
        logger.warning(
            "[obstruction_consult] anthropic package not installed"
        )
        return None

    # Resize to keep tokens / cost down
    thumb = frame.copy()
    if thumb.width > 1200:
        ratio = 1200 / thumb.width
        thumb = thumb.resize(
            (1200, int(thumb.height * ratio)), Image.LANCZOS,
        )
    buf = io.BytesIO()
    thumb.save(buf, format="JPEG", quality=85)
    img_b64 = base64.standard_b64encode(buf.getvalue()).decode()

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": img_b64,
                    }},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
    except Exception as e:
        logger.warning(f"[obstruction_consult] Claude API call failed: {e}")
        return None

    raw_text = response.content[0].text if response.content else ""
    return {"raw_text": raw_text, "parsed": _extract_json(raw_text)}


def _extract_json(text: str) -> Optional[dict]:
    """Find a JSON object in a chunk of Claude's response text."""
    if not text:
        return None
    # Strip code fences if present.
    s = text.strip()
    for fence in ("```json", "```"):
        if s.startswith(fence):
            s = s[len(fence):].lstrip()
            if s.endswith("```"):
                s = s[:-3].rstrip()
    # Find the outermost {...}
    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(s[start:end + 1])
    except json.JSONDecodeError as e:
        logger.warning(
            f"[obstruction_consult] JSON parse error: {e} — raw[:200]={s[:200]!r}"
        )
        return None


# ── Public entry point ─────────────────────────────────────────────────────


def consult_obstruction(
    frame: Image.Image,
    obstruction,                # vision.obstruction_classifier.ObstructionResult
    ocr_tokens: list,           # list of (text, conf, cx, cy)
    goal_context=None,          # brain.goal_context.GoalContext | None
    force: bool = False,
) -> Optional[ObstructionAnalysis]:
    """Analyse an obstruction with Claude (cache-first).  Returns None
    when Claude is unavailable AND no cache hit.

    Cache key: (obstruction.kind, structural_hash, goal_intent).
    The same obstruction text under different goals records different
    analyses — e.g. an Innkeeper overlay during a recruit_crew goal vs
    a sell_all_cargo goal may have different outcome_for_goal verdicts.
    """
    if obstruction.bbox is None:
        return None
    x1, y1, x2, y2 = obstruction.bbox
    tokens_in_bbox = [
        (t, cx, cy)
        for t, _conf, cx, cy in ocr_tokens
        if x1 <= cx <= x2 and y1 <= cy <= y2 and (t or "").strip()
    ]
    if not tokens_in_bbox:
        return None

    structural_hash = _structural_hash(tokens_in_bbox)
    goal_intent = goal_context.intent if goal_context is not None else None
    cache_path = _cache_path(obstruction.kind, structural_hash, goal_intent)

    # Cache hit
    if not force:
        cached = _load_cached(cache_path)
        if cached is not None:
            cached.visit_count = (cached.visit_count or 0) + 1
            cached.cached_at = cached.cached_at or datetime.now(timezone.utc).isoformat()
            _save_cached(cache_path, cached)
            logger.info(
                f"[obstruction_consult] cache hit: "
                f"{obstruction.kind!r}/{structural_hash[:8]}/{goal_intent or 'no_goal'} "
                f"(visit #{cached.visit_count})"
            )
            return cached

    # Build the prompt
    goal_chain_str = "(no active goal)"
    if goal_context is not None:
        chain = goal_context.chain()
        goal_chain_str = "\n".join(
            f"  {i + 1}. {g.intent}  target={g.target}"
            for i, g in enumerate(chain)
        )
    ocr_text_str = "\n".join(
        f"  ({cx},{cy})  {t!r}"
        for t, cx, cy in sorted(tokens_in_bbox, key=lambda r: (r[2], r[1]))
    )
    prompt = _CONSULT_PROMPT.format(
        kind=obstruction.kind,
        classifier_confidence=obstruction.confidence,
        bbox=obstruction.bbox,
        ocr_text=ocr_text_str,
        goal_chain=goal_chain_str,
    )

    logger.info(
        f"[obstruction_consult] consulting Claude: "
        f"kind={obstruction.kind!r} hash={structural_hash[:8]} "
        f"goal={goal_intent!r} bbox={obstruction.bbox} "
        f"({len(tokens_in_bbox)} tokens in bbox)"
    )
    result = _call_claude(prompt, frame)
    # The LLM tab of the trace viewer reads every consult from one place; this is the other
    # model we ask. See `vision/llm_trace.py`.
    try:
        from vision.llm_trace import record as _record
        _record("claude (obstruction consult)",
                f"What is this {obstruction.kind} and how do we get past it?",
                prompt, (result or {}).get("raw_text") or result,
                goal_intent=goal_intent, kind=obstruction.kind,
                structural_hash=structural_hash)
    except Exception:                    # noqa: BLE001 — a trace is never load-bearing
        pass
    if result is None or result.get("parsed") is None:
        # Save the failed attempt to the training log so we can review
        # what went wrong even if no analysis was produced.
        _append_training_record({
            "timestamp":         datetime.now(timezone.utc).isoformat(),
            "obstruction_kind":  obstruction.kind,
            "structural_hash":   structural_hash,
            "goal_intent":       goal_intent,
            "ocr_tokens":        [list(r) for r in tokens_in_bbox],
            "claude_status":     "no_parse",
            "claude_raw":        (result or {}).get("raw_text", ""),
        })
        return None

    parsed = result["parsed"]
    analysis = ObstructionAnalysis(
        purpose          = parsed.get("purpose", ""),
        full_text        = parsed.get("full_text", ""),
        dismissal        = parsed.get("dismissal", "unknown"),
        relates_to_goal  = bool(parsed.get("relates_to_goal", False)),
        outcome_for_goal = parsed.get("outcome_for_goal", "unknown"),
        confidence       = parsed.get("confidence", "low"),
        obstruction_kind = obstruction.kind,
        structural_hash  = structural_hash,
        goal_intent      = goal_intent,
        cached_at        = datetime.now(timezone.utc).isoformat(),
        visit_count      = 1,
        raw_api_response = parsed,
    )
    _save_cached(cache_path, analysis)
    _append_training_record({
        "timestamp":         analysis.cached_at,
        "obstruction_kind":  obstruction.kind,
        "structural_hash":   structural_hash,
        "goal_intent":       goal_intent,
        "ocr_tokens":        [list(r) for r in tokens_in_bbox],
        "claude_status":     "ok",
        "analysis":          analysis.to_dict(),
    })
    logger.info(
        f"[obstruction_consult] saved analysis: "
        f"purpose={analysis.purpose!r} "
        f"dismissal={analysis.dismissal!r} "
        f"relates_to_goal={analysis.relates_to_goal} "
        f"outcome={analysis.outcome_for_goal!r}"
    )
    return analysis
