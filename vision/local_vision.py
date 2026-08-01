# vision/local_vision.py
# Interface to the local vision/LLM model running in Ollama.
#
# Uses llava:7b by default — a multimodal model capable of both visual
# perception and text reasoning. A single LocalVision instance should be
# reused across calls; Ollama keeps the model loaded between requests.
#
# Run standalone to test:
#   python -m vision.local_vision

from __future__ import annotations

import base64
import io
import json
import re

from loguru import logger
from PIL import Image

import ollama

DEFAULT_MODEL = "llava:7b"

# Vision-capable models, in preference order.
# The first one found installed will be used automatically.
VISION_MODEL_PREFERENCE = [
    "llava:7b",
    "llava:13b",
    "llava-phi3",
    "llava:latest",
    "moondream",
]


def list_installed_models() -> list[str]:
    """Return names of all models currently installed in Ollama."""
    try:
        result = ollama.list()
        # ollama.list() returns an object with a 'models' attribute
        models = result.get("models", []) if isinstance(result, dict) else getattr(result, "models", [])
        return [m.get("name", "") if isinstance(m, dict) else getattr(m, "model", "") for m in models]
    except Exception as exc:
        logger.error(f"Could not query Ollama model list: {exc}")
        return []


def find_vision_model() -> str | None:
    """
    Return the name of the best available vision model, or None if none
    are installed.  Logs a helpful message listing pull commands.
    """
    installed = list_installed_models()
    logger.debug(f"Installed Ollama models: {installed}")

    for preferred in VISION_MODEL_PREFERENCE:
        # Match by prefix so "llava:7b" matches "llava:7b", "llava:7b-q4" etc.
        for name in installed:
            if name.startswith(preferred.split(":")[0]) or name == preferred:
                return name

    logger.error(
        "No vision-capable Ollama model found.\n"
        "Install one with:\n"
        "  ollama pull llava:7b        # recommended (4.7 GB)\n"
        "  ollama pull llava-phi3      # smaller alternative (3.8 GB)\n"
        "  ollama pull moondream       # lightest option (1.8 GB, limited)\n"
    )
    return None


class LocalVision:
    """
    Wrapper around Ollama for visual perception and text reasoning.

    All methods are synchronous. The caller is responsible for threading
    if non-blocking behaviour is needed.
    """

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self.model = model
        self._available: bool | None = None  # None = not yet checked

    def check_available(self) -> bool:
        """
        Verify the model is installed in Ollama.  Logs a clear error and
        returns False if it is not.  Result is cached after the first call.
        """
        if self._available is not None:
            return self._available

        installed = list_installed_models()
        base = self.model.split(":")[0]
        for name in installed:
            if name.startswith(base) or name == self.model:
                self._available = True
                return True

        logger.error(
            f"Model '{self.model}' is not installed in Ollama.\n"
            f"Pull it with:  ollama pull {self.model}\n"
            f"Installed models: {installed or ['(none)']}"
        )
        self._available = False
        return False

    # ── internal helpers ──────────────────────────────────────────────────────

    def _encode(self, frame: Image.Image) -> str:
        """Encode a PIL image to base64 PNG string for Ollama."""
        buf = io.BytesIO()
        frame.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("utf-8")

    def _parse_json(self, text: str) -> dict:
        """
        Extract a JSON object from a model response.
        Handles markdown fences, escaped underscores, and truncated output.
        """
        # Strip markdown code fences
        text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
        # Some models escape underscores in keys: "action\_type" → "action_type"
        text = text.replace(r"\_", "_")

        # Try the whole text first
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Fall back to finding the first { ... } block
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

        # Last resort: the response may have been truncated mid-object.
        # Try to salvage by closing any open strings and the object itself.
        salvage = text.strip()
        if salvage.startswith("{") and not salvage.endswith("}"):
            # Strip the incomplete last field and close the object
            salvage = re.sub(r',?\s*"[^"]*"?\s*:?\s*[^,}\n]*$', "", salvage).strip()
            if not salvage.endswith("}"):
                salvage += "}"
            try:
                return json.loads(salvage)
            except json.JSONDecodeError:
                pass

        logger.warning(f"Could not parse JSON from model response:\n{text[:300]}")
        return {}

    # ── public interface ──────────────────────────────────────────────────────

    def ask(
        self,
        prompt: str,
        frame: Image.Image | None = None,
        system: str = "",
    ) -> str:
        """
        Send a text prompt (optionally with a screenshot) and return the raw
        response string. Foundation for all higher-level methods.
        Returns "" and logs an error if the model is unavailable.
        """
        if not self.check_available():
            return ""

        messages = []
        if system:
            messages.append({"role": "system", "content": system})

        user_msg: dict = {"role": "user", "content": prompt}
        if frame is not None:
            user_msg["images"] = [self._encode(frame)]

        messages.append(user_msg)

        try:
            response = ollama.chat(
                model=self.model,
                messages=messages,
                options={"num_predict": 512, "temperature": 0.1},
            )
            return response["message"]["content"].strip()
        except Exception as exc:
            logger.error(f"Ollama call failed ({self.model}): {exc}")
            self._available = None   # reset so next call retries the availability check
            return ""

    def ask_json(
        self,
        prompt: str,
        frame: Image.Image | None = None,
        system: str = "",
    ) -> dict:
        """Ask a question and parse the response as a JSON dict."""
        raw = self.ask(prompt, frame=frame, system=system)
        return self._parse_json(raw)

    def describe_screen(self, frame: Image.Image, system: str = "") -> dict:
        """
        Describe the current game screen.

        Returns a dict with keys: scene_type, screen_title, visible_elements,
        text_labels, interactive_hints, description.
        """
        from config.prompts import PERCEIVE_PROMPT
        result = self.ask_json(PERCEIVE_PROMPT, frame=frame, system=system)
        logger.debug(f"Screen → {result.get('scene_type', '?')} | {result.get('description', '')!r}")
        return result

    def reason(self, prompt: str, system: str = "") -> dict:
        """
        Pure text reasoning (no image). Used for planning and deciding actions.
        Returns a parsed JSON dict.
        """
        return self.ask_json(prompt, system=system)

    def describe_change(
        self,
        before: Image.Image,
        after: Image.Image,
        action_description: str,
        system: str = "",
    ) -> dict:
        """
        Given before/after frames and the action that was taken, ask the model
        what changed and what can be learned. Returns a parsed JSON dict with
        keys: confirmed, refuted, new_knowledge, summary.
        """
        from config.prompts import LEARN_PROMPT_TEMPLATE

        before_desc = self.ask(
            "Describe this game screenshot in one sentence.",
            frame=before,
            system=system,
        )
        after_desc = self.ask(
            "Describe this game screenshot in one sentence.",
            frame=after,
            system=system,
        )

        prompt = LEARN_PROMPT_TEMPLATE.format(
            before_description=before_desc,
            action_description=action_description,
            after_description=after_desc,
        )
        return self.ask_json(prompt, system=system)

    def filter_port_map_labels(
        self, ocr_labels: list[str], frame: Image.Image
    ) -> list[str]:
        """
        Given raw OCR labels from the port map and the screenshot, ask the
        model to separate real building names from noise (NPC appellation text,
        numbers, fragments).

        Returns the confirmed building name list in lowercase.
        """
        from config.prompts import FILTER_BUILDINGS_PROMPT_TEMPLATE

        if not ocr_labels:
            return []

        prompt = FILTER_BUILDINGS_PROMPT_TEMPLATE.format(ocr_labels=ocr_labels)
        result = self.ask_json(prompt, frame=frame, system="")

        buildings = [b.lower().strip() for b in result.get("buildings", []) if b]
        noise = result.get("noise", [])
        reasoning = result.get("reasoning", "")

        if noise:
            logger.info(f"LLM filtered as noise: {noise}")
        if reasoning:
            logger.debug(f"Filter reasoning: {reasoning}")
        logger.info(f"LLM confirmed buildings: {buildings}")
        return buildings


# ── module-level singleton ────────────────────────────────────────────────────

_vision: LocalVision | None = None


def get_vision(model: str | None = None) -> LocalVision:
    """
    Return the shared LocalVision instance, creating it on first call.

    If *model* is not specified, auto-selects the best available vision
    model from VISION_MODEL_PREFERENCE.  Falls back to DEFAULT_MODEL so
    the instance is always created (check_available() will report the error).
    """
    global _vision
    if _vision is None:
        chosen = model or find_vision_model() or DEFAULT_MODEL
        if chosen != DEFAULT_MODEL:
            logger.info(f"Using vision model: {chosen}")
        _vision = LocalVision(model=chosen)
    return _vision


# ── standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    from memory.logger import setup_logging
    from capture.adb_capture import capture_screen
    from config.prompts import SEED_KNOWLEDGE

    setup_logging()
    logger.info(f"Testing LocalVision with model {DEFAULT_MODEL}")

    frame = capture_screen()
    vision = get_vision()

    logger.info("Describing current screen...")
    state = vision.describe_screen(frame, system=SEED_KNOWLEDGE)
    print(json.dumps(state, indent=2, ensure_ascii=False))
