"""LLM client for the reasoning layer — a text-only Claude `llm_fn`.

Reuses the Anthropic setup already used across the codebase (ANTHROPIC_API_KEY,
model `claude-sonnet-4-6`). `claude_llm_fn(prompt)` returns the raw text reply, or
`""` when the LLM is unavailable — so callers / shadow mode degrade gracefully.
Plug it into `reasoning.reason(ctx, llm_fn=claude_llm_fn, shadow=False)`.
"""
from __future__ import annotations

import os
from loguru import logger

_MODEL = "claude-sonnet-4-6"

# The reasoning operator's system prompt. The red-gem guardrail is stated here as
# well as enforced downstream (whitelisted ops + confirm-before-spend).
_SYSTEM = (
    "You are the reasoning operator for a maritime-trading game bot. Given the "
    "GOAL, the company/fleet WORLD MODEL, the current OBSERVED screen, and GAME "
    "KNOWLEDGE, choose EXACTLY ONE next action from the ALLOWED ACTIONS. Reply "
    'with ONLY a JSON object: {"op": <allowed op>, "arg": <string or null>, '
    '"why": <short reason>}. Never pick an action that spends RED GEMS (real '
    'money) or confirms an irreversible purchase — if that is the only way '
    'forward, reply with op "abort".'
)

_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    try:
        import anthropic
    except ImportError:
        logger.warning("[reasoning] anthropic package not installed")
        return None
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        logger.warning("[reasoning] ANTHROPIC_API_KEY not set — reasoning LLM disabled")
        return None
    _client = anthropic.Anthropic(api_key=key)
    return _client


def available() -> bool:
    return _get_client() is not None


def claude_llm_fn(prompt: str, *, model: str = _MODEL, max_tokens: int = 512) -> str:
    """Send the reasoning prompt to Claude; return the raw reply (or "")."""
    client = _get_client()
    if client is None:
        return ""
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_SYSTEM,
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        )
        raw = (resp.content[0].text or "").strip()
        if raw.startswith("```"):                       # strip markdown fences
            raw = raw.split("```")[1]
            if raw.lower().startswith("json"):
                raw = raw[4:]
        return raw.strip()
    except Exception as exc:
        logger.debug(f"[reasoning] Claude call failed: {exc}")
        return ""
