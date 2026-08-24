# brain/agent.py
# The bot's brain — perceive → recall → reason → act → learn.
#
# The agent drives all high-level decisions. It does not contain any
# hardcoded navigation rules; instead it asks the vision/LLM to figure
# out what is on screen and what to do next, and saves what it learns
# to the knowledge base for reuse on future visits.
#
# Run standalone:
#   python -m brain.agent explore        # explore current port
#   python -m brain.agent goto castle    # navigate to a specific building
#   python -m brain.agent learn          # observe current screen and learn

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from PIL import Image

from actions.adb_actions import tap, swipe, press_back
from capture.adb_capture import capture_screen
from config.prompts import (
    SEED_KNOWLEDGE,
    REASON_PROMPT_TEMPLATE,
    CLASSIFY_BUILDING_PROMPT_TEMPLATE,
)
from vision.local_vision import get_vision
from vision.ocr import read_screen_title
from vision.chrome_detector import get_chrome_detector, KNOWN_BUILDING_TITLES
from classifier.predict import ScreenClassifier
from memory.knowledge_base import KnowledgeBase
from brain.goal import Goal, GoalStack


# ── ScreenState ───────────────────────────────────────────────────────────────

@dataclass
class ScreenState:
    """Everything the agent knows about the current game screen."""

    frame: Image.Image
    scene_type: str = "unknown"
    screen_title: str = ""
    description: str = ""
    visible_elements: list[str] = field(default_factory=list)
    text_labels: list[str] = field(default_factory=list)
    interactive_hints: list[str] = field(default_factory=list)
    kb_context: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.monotonic)
    # UI landmark flags (from PERCEIVE_PROMPT)
    has_mini_map: bool = False
    has_hamburger: bool = False
    has_home_button: bool = False
    home_button_coords: tuple[int, int] | None = None
    has_back_arrow: bool = False

    def summary(self) -> str:
        """Compact text representation for LLM prompts."""
        lines = [
            f"Scene type : {self.scene_type}",
            f"Screen title: {self.screen_title or '(none)'}",
            f"Description : {self.description}",
        ]
        # UI landmarks — always include so the LLM can use them for reasoning
        landmark_flags = []
        if self.has_mini_map:
            landmark_flags.append("mini_map")
        if self.has_hamburger:
            landmark_flags.append("hamburger_menu")
        if self.has_home_button:
            landmark_flags.append(
                f"home_button@{self.home_button_coords}" if self.home_button_coords
                else "home_button"
            )
        if self.has_back_arrow:
            landmark_flags.append("back_arrow")
        lines.append(f"UI landmarks: {', '.join(landmark_flags) or 'none detected'}")

        if self.visible_elements:
            lines.append(f"Elements    : {', '.join(self.visible_elements[:10])}")
        if self.text_labels:
            lines.append(f"Text labels : {', '.join(self.text_labels[:12])}")
        if self.interactive_hints:
            lines.append(f"Interactive : {', '.join(self.interactive_hints[:8])}")
        if self.kb_context:
            kb_str = json.dumps(self.kb_context, ensure_ascii=False)[:400]
            lines.append(f"KB context  : {kb_str}")
        return "\n".join(lines)


# ── AgentAction ───────────────────────────────────────────────────────────────

@dataclass
class AgentAction:
    """An action the agent has decided to take."""

    action_type: str                        # tap | swipe | press_back | wait |
                                            # open_port_map | open_world_map |
                                            # done | stuck
    target_description: str = ""           # plain-English description of the target
    coords: tuple[int, int] | None = None  # screen coordinates if known
    reasoning: str = ""
    hypothesis: str = ""
    confidence: float = 0.0


# ── Agent ─────────────────────────────────────────────────────────────────────

class Agent:
    """
    The bot's reasoning core.

    One agent instance is created and reused for the lifetime of the session.
    The vision model and KB are singletons underneath.
    """

    # Confidence threshold: if the classifier scores below this, treat as unknown
    # and fall through to chrome/OCR/llava.
    _CLASSIFIER_MIN_CONFIDENCE = 0.70

    def __init__(self) -> None:
        self.vision = get_vision()
        self.kb = KnowledgeBase()
        self.goals = GoalStack()
        self._stop_event = threading.Event()
        # Track consecutive failures of the same action_type so we can break
        # loops and investigating before retrying.
        self._last_action_type: str = ""
        self._consecutive_action_failures: int = 0
        # Name of the building most recently tapped from the port map, so we
        # can correctly classify the arrival scene even when OCR garbles the title.
        self._pending_building: str | None = None
        # Navigation failure tracking: { building_name → consecutive_fail_count }
        # Reset when the character successfully enters any building.
        # Buildings with count >= _NAV_FAIL_SKIP_THRESHOLD are skipped on the
        # next port map sweep and tried again only as a last resort.
        self._nav_failures: dict[str, int] = {}
        self._NAV_FAIL_SKIP_THRESHOLD = 2
        # Screen classifier — loaded once, reused every tick.
        try:
            self._classifier: ScreenClassifier | None = ScreenClassifier()
            logger.info("Screen classifier loaded")
        except FileNotFoundError:
            self._classifier = None
            logger.warning("Screen classifier model not found — run classifier/train.py first")

    # ── perceive ──────────────────────────────────────────────────────────────

    def _quick_classify(self, frame: Image.Image) -> tuple[str, str]:
        """
        Fast scene + title detection using the Phase 6 OmniParser registry,
        then L0 (CNN classifier) and L1 (chrome) as fallback.  No llava call
        — takes ~0.5s instead of ~30s.

        Used in _wait_for_outcome polling where we only need to know whether
        the scene has changed, not a full description of it.

        Returns (scene_type, screen_title).  Falls back to 'unknown' when
        neither the registry nor L0 nor L1 can confidently classify.
        """
        title = read_screen_title(frame)
        scene = "unknown"

        # Priority: registry > chrome > L0 (matches perceive()).
        try:
            from vision.screen_classifier import (
                classify_screen, CONFIDENCE_HIGH, CONFIDENCE_MEDIUM,
            )
            sc_result = classify_screen(frame)
            if sc_result.confidence in (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM):
                scene = sc_result.state
        except Exception:
            pass

        if scene == "unknown":
            chrome_scene, _ = get_chrome_detector().classify_scene(frame, title)
            if chrome_scene != "unknown":
                scene = chrome_scene

        if scene == "unknown" and self._classifier is not None:
            try:
                pred = self._classifier.predict(frame)
                if pred.confidence >= self._CLASSIFIER_MIN_CONFIDENCE:
                    scene = pred.screen_type
            except Exception:
                pass

        # OCR title fallback (same as L2 in perceive)
        if scene == "unknown" and title:
            t = title.lower()
            if t in KNOWN_BUILDING_TITLES:
                scene = "building_interior"

        # Port name correction (same as in perceive)
        known_port = self._current_port()
        if known_port and title and scene in ("port_overworld", "port_map"):
            from brain.states.port_map import _label_similarity
            sim = _label_similarity(title.lower(), known_port.lower())
            if sim >= 0.6 and title.lower() != known_port.lower():
                title = known_port.title()

        return scene, title

    def _current_port(self) -> str:
        """
        Return the port name from the active explore_port goal, or "".
        Used to distinguish 'inside a building' from 'port overworld'
        when the LLM misclassifies the scene.
        """
        goal = self.goals.current()
        if goal and goal.type == "explore_port":
            return goal.target.lower().strip()
        return ""

    def perceive(self, frame: Image.Image) -> ScreenState:
        """
        Classify the current screen, then ask llava to describe its content.

        Scene classification uses four layers in order:

          L0 — Trained screen classifier (MobileNetV3-small)
               Fast neural classifier trained on labeled screenshots.
               Covers all 19 screen types. Used when confidence ≥ 0.70.
               Falls through to L1 if model not loaded or confidence too low.

          L1 — Chrome detector (template matching on stable UI buttons)
               Deterministic — unaffected by day/night, weather, NPCs.
               Returns a scene type when chrome templates are matched.
               Also provides UI landmark flags (hamburger, back arrow, etc.)
               regardless of whether L0 already gave a confident scene type.

          L2 — OCR title fallback
               If L0+L1 give "unknown" but OCR reads a known building name,
               force building_interior.

          L3 — llava fallback
               Only when all above are inconclusive. llava is always used for
               CONTENT description (building purpose, labels, interactive hints)
               regardless of which layer classified the scene.
        """
        # ── OCR: read top-left title (used by L2 and chrome detector) ─────────
        title_ocr = read_screen_title(frame)

        # ── Phase 6: OmniParser fingerprint registry (PRIMARY) ─────────────────
        # The registry (vision/state_fingerprints_data.py) is data-derived from
        # data/labels.jsonl and uses structural OmniParser signals.  When it
        # returns a HIGH or MEDIUM verdict, that verdict OVERRIDES the L0/L1/L2
        # chain because the registry has fewer false-positive failure modes
        # (e.g. the May-3 defeat-dialog case where L0 MobileNetV3 said
        # 'port_overworld' on a coastal scene that was actually a revive popup).
        registry_scene: str | None = None
        try:
            from vision.screen_classifier import (
                classify_screen, CONFIDENCE_HIGH, CONFIDENCE_MEDIUM,
            )
            sc_result = classify_screen(frame)
            if sc_result.confidence in (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM):
                registry_scene = sc_result.state
                logger.debug(
                    f"Registry: {sc_result.state!r} "
                    f"({sc_result.confidence}) signals={sc_result.signals}"
                )
        except Exception as exc:
            logger.debug(f"Registry path skipped: {exc}")

        # ── L0: trained classifier ────────────────────────────────────────────
        clf_scene = "unknown"
        clf_confidence = 0.0
        if self._classifier is not None:
            try:
                pred = self._classifier.predict(frame)
                clf_confidence = pred.confidence
                if pred.confidence >= self._CLASSIFIER_MIN_CONFIDENCE:
                    clf_scene = pred.screen_type
                    logger.debug(
                        f"L0 classifier: {pred.screen_type!r} "
                        f"({pred.confidence:.0%})  "
                        f"alts={pred.top3[1:]}"
                    )
                else:
                    logger.debug(
                        f"L0 classifier: low confidence {pred.confidence:.0%} "
                        f"for {pred.screen_type!r} — falling through"
                    )
            except Exception as exc:
                logger.warning(f"L0 classifier error: {exc}")

        # ── L1: chrome detection (always run — provides landmark flags) ────────
        chrome_scene, chrome_state = get_chrome_detector().classify_scene(
            frame, title_ocr
        )

        # ── L2: OCR title fallback ─────────────────────────────────────────────
        if chrome_scene == "unknown" and title_ocr:
            t = title_ocr.lower()
            if t in KNOWN_BUILDING_TITLES:
                chrome_scene = "building_interior"
                logger.debug(f"L2 OCR override: title {title_ocr!r} → building_interior")

        # ── Merge: pick best scene type ────────────────────────────────────────
        # Priority order (highest to lowest):
        #   1. Phase 6 OmniParser registry (HIGH/MEDIUM) — structural fingerprint
        #      derived from labelled data; lowest false-positive rate.
        #   2. Chrome detector — deterministic on stable UI elements.
        #   3. L0 MobileNetV3 — neural classifier covering anything else.
        if registry_scene is not None:
            scene = registry_scene
            if clf_scene not in ("unknown", registry_scene):
                logger.debug(
                    f"Scene: registry={registry_scene!r} (overrides "
                    f"classifier={clf_scene!r} @ {clf_confidence:.0%})"
                )
            if chrome_scene not in ("unknown", registry_scene):
                logger.debug(
                    f"Scene: registry={registry_scene!r} (overrides "
                    f"chrome={chrome_scene!r})"
                )
        elif chrome_scene != "unknown":
            scene = chrome_scene
            if clf_scene not in ("unknown", chrome_scene):
                logger.debug(
                    f"Scene: chrome={chrome_scene!r} (overrides "
                    f"classifier={clf_scene!r} @ {clf_confidence:.0%})"
                )
        elif clf_scene != "unknown":
            scene = clf_scene
        else:
            scene = "unknown"

        # ── L3: llava for content (and scene fallback when still unknown) ──────
        port = self._current_port()
        context = SEED_KNOWLEDGE
        if port:
            context += f"\n\nCurrently exploring port: {port.title()}"

        vision_result = self.vision.describe_screen(frame, system=context)

        llava_scene = vision_result.get("scene_type", "unknown")
        if scene == "unknown":
            scene = llava_scene
            logger.debug(f"Scene: llava fallback → {scene!r}")
        elif llava_scene != scene:
            logger.debug(
                f"Scene: {scene!r} confirmed (llava disagrees: {llava_scene!r})"
            )

        # Screen title: prefer OCR over llava.
        # If OCR returns a garbled port name (e.g. "cocotra" for "Socotra"),
        # fall back to the goal's known port name when the scene is port-level
        # and the OCR result is suspiciously similar to the goal port name.
        title = title_ocr or vision_result.get("screen_title") or ""
        known_port = self._current_port()
        if known_port and title and scene in ("port_overworld", "port_map"):
            from brain.states.port_map import _label_similarity
            sim = _label_similarity(title.lower(), known_port.lower())
            if sim >= 0.6 and title.lower() != known_port.lower():
                logger.debug(
                    f"OCR port name {title!r} looks like known port "
                    f"{known_port!r} (sim={sim:.2f}) — correcting"
                )
                title = known_port.title()

        logger.debug(
            f"{chrome_state.summary()}  scene={scene!r}  title={title!r}"
        )

        # ── L3 (structured): Claude Vision API scene inventory ─────────────────
        # On first encounter of any (scene_type, title) pair, call Claude Vision
        # to extract a structured inventory of all UI elements — text, icons,
        # buttons, tabs — with positions and inferred purposes.  Cached forever.
        # Runs asynchronously in a background thread so it doesn't block the loop.
        if scene not in ("unknown",):
            self._trigger_scene_analysis(frame, scene, title)

        return ScreenState(
            frame=frame,
            scene_type=scene,
            screen_title=title,
            description=vision_result.get("description", ""),
            visible_elements=vision_result.get("visible_elements", []),
            text_labels=vision_result.get("text_labels", []),
            interactive_hints=vision_result.get("interactive_hints", []),
            kb_context=self._recall(title, scene),
            # Chrome flags (used for exit strategy decisions)
            has_mini_map=chrome_state.has_right_panel,
            has_hamburger=chrome_state.has_hamburger,
            has_home_button=chrome_state.has_home,
            home_button_coords=None,                     # fixed coord used in act()
            has_back_arrow=chrome_state.has_back_arrow,
        )

    # ── scene analysis (Claude Vision API) ───────────────────────────────────

    def _trigger_scene_analysis(
        self, frame: Image.Image, scene_type: str, screen_title: str
    ) -> None:
        """
        Fire OmniParser + Claude Vision analysis of this scene on first encounter.
        Runs in a background thread — does not block the main bot loop.

        Pipeline:
          1. OmniParser (local, ~2s) — detects all interactive elements with
             pixel-accurate bounding boxes
          2. Claude Vision API (~5s) — receives OmniParser's structured text list
             + small thumbnail, returns game-context understanding of each element
          3. Result cached in memory/knowledge/scenes/ — never repeated

        "Learn once, reuse forever": subsequent calls return cached inventory instantly.
        """
        from vision.claude_vision import get_claude_vision, load_inventory
        from vision.omniparser import get_omniparser

        # Check cache first to avoid spawning unnecessary threads
        if load_inventory(scene_type, screen_title) is not None:
            return   # already analysed

        cv = get_claude_vision()
        if not cv.available:
            return

        # Snapshot the frame (PIL images are not thread-safe to share)
        frame_copy = frame.copy()

        def _run():
            try:
                # L1.5: OmniParser — detect all interactive elements
                omni = get_omniparser()
                detected = []
                if omni.available():
                    detected = omni.parse(frame_copy)
                    logger.debug(
                        f"OmniParser detected {len(detected)} elements "
                        f"for {scene_type}/{screen_title}"
                    )

                # L4: Claude — understand what those elements mean
                inv = cv.analyse_scene(
                    frame_copy, scene_type, screen_title,
                    detected_elements=detected or None,
                )
                if inv:
                    logger.info(f"Scene inventory ready: {inv.summary()}")
            except Exception as exc:
                logger.warning(
                    f"Scene analysis failed for {scene_type}/{screen_title}: {exc}"
                )

        t = threading.Thread(
            target=_run, daemon=True,
            name=f"scene-{scene_type}-{screen_title}",
        )
        t.start()

    # ── recall ────────────────────────────────────────────────────────────────

    def _recall(self, title: str, scene_type: str) -> dict[str, Any]:
        """Pull relevant KB entries for the current screen into a context dict."""
        from vision.claude_vision import load_inventory
        ctx: dict[str, Any] = {}

        if scene_type == "building_interior" and title:
            btype = self.kb.get_building_type(title)
            if btype:
                ctx["building_type"] = btype
            screen = self.kb.get_screen_structure(title)
            if screen:
                ctx["screen_structure"] = screen

        elif scene_type in ("port_overworld", "port_map") and title:
            port = self.kb.get_port(title)
            if port:
                ctx["port"] = port

        # Always attach scene inventory if available — gives reason() a map of
        # every interactive element and its purpose for the current screen
        inv = load_inventory(scene_type, title)
        if inv:
            ctx["scene_inventory"] = {
                "layout": inv.layout_description,
                "navigation": inv.navigation_hints,
                "interactive_elements": [
                    {"label": e.label, "purpose": e.purpose,
                     "tap_x": e.tap_x, "tap_y": e.tap_y}
                    for e in inv.interactive()
                ],
            }

        return ctx

    # ── reason ────────────────────────────────────────────────────────────────

    def reason(self, state: ScreenState) -> AgentAction:
        """
        Ask the LLM to decide the next action given the current state and goal.
        Returns an AgentAction with action_type, optional coords, and reasoning.
        """
        goal = self.goals.current()
        goal_str = str(goal) if goal else "idle — observe and learn whatever is useful"
        kb_str = (
            json.dumps(state.kb_context, ensure_ascii=False)[:500]
            if state.kb_context
            else "none"
        )

        prompt = REASON_PROMPT_TEMPLATE.format(
            state_summary=state.summary(),
            goal=goal_str,
            kb_context=kb_str,
        )

        result = self.vision.reason(prompt, system=SEED_KNOWLEDGE)

        if not result:
            logger.warning("Reasoning returned empty — defaulting to wait")
            return AgentAction(action_type="wait", reasoning="no response from model")

        raw_coords = result.get("coords")
        coords: tuple[int, int] | None = None
        if isinstance(raw_coords, (list, tuple)) and len(raw_coords) == 2:
            try:
                coords = (int(raw_coords[0]), int(raw_coords[1]))
            except (TypeError, ValueError):
                pass

        return AgentAction(
            action_type=result.get("action_type", "wait"),
            target_description=result.get("target_description", ""),
            coords=coords,
            reasoning=result.get("reasoning", ""),
            hypothesis=result.get("hypothesis", ""),
            confidence=float(result.get("confidence", 0.5)),
        )

    # ── act ───────────────────────────────────────────────────────────────────

    def act(self, action: AgentAction) -> None:
        """Execute the decided action via ADB."""
        logger.info(
            f"Acting: {action.action_type!r}  target={action.target_description!r}"
            f"  coords={action.coords}  confidence={action.confidence:.2f}"
        )
        if action.reasoning:
            logger.debug(f"Reasoning: {action.reasoning}")

        atype = action.action_type

        if atype == "tap":
            if action.coords:
                tap(*action.coords)
            else:
                logger.warning("tap action has no coords — skipping")

        elif atype == "swipe":
            # For swipe the model may encode 4 values as coords
            raw = action.coords
            if raw and len(raw) == 4:  # type: ignore[arg-type]
                x1, y1, x2, y2 = raw  # type: ignore[misc]
                swipe(int(x1), int(y1), int(x2), int(y2), 300)
            else:
                logger.warning("swipe action needs [x1,y1,x2,y2] coords — skipping")

        elif atype == "press_back":
            press_back()

        elif atype == "open_port_map":
            from brain.states.port_map import open_port_map
            success = open_port_map()
            if not success:
                # Treat a failed port-map open as a stuck signal so the loop
                # can count consecutive failures and break out of the pattern.
                logger.warning("open_port_map failed — signalling stuck")
                # We do NOT return "stuck" here (act() has no return value),
                # but we log it; the step() caller will see the same scene
                # unchanged on the next perceive() and the LLM will adapt.
            return   # open_port_map has its own timing; skip the pause below

        elif atype == "open_world_map":
            from actions.sail_actions import open_world_map
            open_world_map()   # ONE canonical open (port globe / sea minimap)

        elif atype in ("wait", "done", "stuck"):
            pass   # handled by the caller

        else:
            logger.warning(f"Unknown action type: {atype!r}")

        # Human-like pause after any physical action
        time.sleep(random.uniform(2.0, 4.0))

    # ── learn ─────────────────────────────────────────────────────────────────

    def learn(
        self,
        before: ScreenState,
        action: AgentAction,
        after: ScreenState,
    ) -> None:
        """
        Ask the model what changed as a result of the action and persist
        any new knowledge to the KB.
        """
        action_desc = (
            f"{action.action_type} on '{action.target_description}'"
            + (f" — hypothesis: {action.hypothesis}" if action.hypothesis else "")
        )

        learning = self.vision.describe_change(
            before.frame,
            after.frame,
            action_description=action_desc,
            system=SEED_KNOWLEDGE,
        )

        if not learning:
            return

        if learning.get("summary"):
            logger.info(f"Learned: {learning['summary']}")

        for item in learning.get("new_knowledge", []):
            ktype = item.get("type", "")
            key = item.get("key", "")
            value = item.get("value", "")
            confidence = float(item.get("confidence", 0.5))

            if not key or not value:
                continue

            if ktype == "screen_structure":
                screen_name = after.screen_title or before.screen_title or key
                self.kb.save_screen_structure(screen_name, value, confidence)

            elif ktype == "building_purpose":
                bname = after.screen_title or key
                port_name = before.kb_context.get("port", {}).get("port", "")
                self.kb.save_building_type(
                    building_type=bname,
                    description=value,
                    port=port_name,
                )

            elif ktype == "interaction":
                screen_name = before.screen_title or before.scene_type
                self.kb.save_interaction(screen_name, key, value, confidence)

            logger.debug(f"KB ← [{ktype}] {key!r}: {value!r} (conf={confidence:.2f})")

    # ── investigate building ──────────────────────────────────────────────────

    def _read_building_submenus(self, frame: Image.Image) -> list[str]:
        """
        OCR the left-side sub-menu panel inside a building and return the list
        of service labels (e.g. ['Hire Sailors', 'Rest'] for an Inn).

        These labels are the most reliable in-game source of what a building
        offers — far more accurate than asking llava to describe the scene.
        """
        from vision.ocr import _get_reader
        from config.settings import BUILDING_SUBMENU_REGION
        import numpy as np

        ox, oy = BUILDING_SUBMENU_REGION[0], BUILDING_SUBMENU_REGION[1]
        region = frame.crop(BUILDING_SUBMENU_REGION)
        raw = _get_reader().readtext(np.array(region), detail=1)

        items = []
        for bbox, text, conf in raw:
            text = text.strip()
            if conf < 0.4 or len(text) < 2:
                continue
            # Skip pure numbers, single chars, and UI noise
            if text.replace(" ", "").isnumeric():
                continue
            # Skip very long strings (likely scene bleed-through, not menu labels)
            if len(text) > 30:
                continue
            items.append(text)

        return items

    def _investigate_building(self, state: ScreenState, port: str) -> None:
        """
        Learn what a building does before leaving it.

        Sources used (most reliable first):
          1. Left-panel sub-menu OCR — the actual service list the game shows
             (e.g. 'Hire Sailors', 'Rest' for Inn; 'Exchange', 'Deposit' for Bank)
          2. L1 KB lookup — if we've seen this building type before, reuse it
          3. L2 LLM visual classification — only for unknown buildings where OCR
             alone isn't enough to understand purpose

        Results are saved to both the building-type record (cross-port) and the
        port-specific record, then logged clearly.
        """
        title = state.screen_title

        # ── Sanity check: title must not be the port name ─────────────────────
        if title and port and title.lower() == port.lower():
            logger.warning(
                f"_investigate_building: title {title!r} matches port name — "
                "not a building, aborting investigation"
            )
            return

        # ── Market: read price snapshot before doing anything else ────────────
        from brain.kb import control as _ckb
        if title and _ckb().detail_mentions_building(title, "market"):
            self._read_market_prices(state, port)

        # ── OCR: read sub-menu items from the left panel ───────────────────────
        # These are the most accurate signal of what the building does.
        submenu_items = self._read_building_submenus(state.frame)
        if submenu_items:
            logger.info(
                f"Building '{title or '?'}' sub-menus (OCR): {submenu_items}"
            )
        else:
            logger.debug(f"No sub-menu items OCR'd for '{title or '?'}'")

        # ── L1: KB lookup ─────────────────────────────────────────────────────
        if title:
            existing = self.kb.get_building_type(title)
            if existing and existing.get("description"):
                logger.info(
                    f"KB hit for building type '{title}' — "
                    f"known purpose: {existing['description']!r}"
                )
                # Update the port-specific record with fresh sub-menu data
                self.kb.save_building(
                    port=port,
                    building=title,
                    purpose=existing["description"],
                    actions=submenu_items or None,
                )
                self._log_learned(port, title, existing["description"], submenu_items)
                return

        # ── L2: LLM visual classification ─────────────────────────────────────
        logger.info(
            f"New/unknown building '{title or '(untitled)'}' — "
            "asking LLM to classify it (visual re-examination)"
        )

        prompt = CLASSIFY_BUILDING_PROMPT_TEMPLATE.format(
            state_summary=state.summary()
        )
        result = self.vision.ask_json(prompt, frame=state.frame, system=SEED_KNOWLEDGE)

        if not result:
            logger.warning("Building classification returned empty — saving basic info")
            if title:
                purpose = state.description or "(unknown)"
                self.kb.save_building(
                    port=port, building=title, purpose=purpose,
                    actions=submenu_items or None,
                )
                self._log_learned(port, title, purpose, submenu_items)
            return

        building_type = result.get("building_type") or title or "unknown"
        purpose       = result.get("purpose") or state.description or "(unknown)"
        actions       = result.get("available_actions", [])
        ui_elements   = result.get("ui_elements", [])
        is_known_type = bool(result.get("is_known_type", False))
        confidence    = float(result.get("confidence", 0.5))
        notes         = result.get("notes", "")

        if notes:
            logger.debug(f"LLM notes: {notes}")

        # Prefer OCR sub-menu items as actions over LLM-hallucinated actions
        final_actions = submenu_items if submenu_items else actions

        # Save building-type record (cross-port generalisation)
        self.kb.save_building_type(
            building_type=building_type,
            description=purpose,
            port=port,
        )

        # Save port-specific building record
        if title:
            self.kb.save_building(
                port=port, building=title, purpose=purpose,
                actions=final_actions or None,
                ui_elements=ui_elements or None,
            )

        # Save screen structure
        if (ui_elements or final_actions) and title:
            self.kb.save_screen_structure(
                title,
                {
                    "sub_menu_items": submenu_items,
                    "ui_elements": ui_elements,
                    "available_actions": final_actions,
                    "notes": notes,
                },
                confidence,
            )

        self._log_learned(port, title or building_type, purpose, final_actions)

    def _read_market_prices(self, state: ScreenState, port: str) -> None:
        """
        OCR the market Purchase and Sell tabs, merge the data, and save a
        timestamped price snapshot to memory/knowledge/markets/.
        Called automatically by _investigate_building when title == "market".
        """
        from datetime import datetime, timezone
        from vision.market_reader import read_both_tabs
        from memory.market_kb import MarketSnapshot, MarketGood as KBGood, save_snapshot

        logger.info(f"Reading market prices for {port}…")
        try:
            purchase_goods, sell_goods = read_both_tabs(
                capture_fn=capture_screen,
                tap_fn=tap,
            )
        except Exception as exc:
            logger.warning(f"Market price read failed: {exc}")
            return

        if not purchase_goods and not sell_goods:
            logger.warning("Market reader returned no goods — skipping snapshot")
            return

        snap = MarketSnapshot(
            port=port,
            timestamp=datetime.now(timezone.utc).isoformat(),
            purchase_goods=purchase_goods,
            sell_goods=sell_goods,
        )
        save_snapshot(snap)

        logger.info(
            f"MARKET PRICES [{port}]  "
            f"{len(purchase_goods)} buyable / {len(sell_goods)} sellable from cargo\n"
            + "  PURCHASE:\n"
            + "\n".join(
                f"    {g.name:<30} {g.buy_price or 'SOLD OUT':>8}  "
                f"{g.index_pct}%  {g.trend}"
                for g in sorted(purchase_goods, key=lambda x: x.name)
            )
            + ("\n  SELL (current cargo):\n" if sell_goods else "")
            + "\n".join(
                f"    {g.name:<30} {g.sell_price or '?':>8}  "
                f"{g.index_pct}%  {g.trend}"
                for g in sorted(sell_goods, key=lambda x: x.name)
            )
        )

    def _log_learned(
        self,
        port: str,
        building: str,
        purpose: str,
        actions: list[str],
    ) -> None:
        """Emit a concise summary of what was learned about a building."""
        actions_str = ", ".join(actions) if actions else "(none found)"
        logger.info(
            f"LEARNED  [{port}] {building.upper()}\n"
            f"  purpose  : {purpose}\n"
            f"  sub-menus: {actions_str}"
        )
        # Also log the port's full known building list so progress is visible
        port_record = self.kb.get_port(port) or {}
        known = sorted(port_record.get("buildings", []))
        if known:
            logger.info(f"  known buildings in {port}: {known}")

    # ── port map navigation ───────────────────────────────────────────────────

    def _navigate_from_port_map(self, goal: Goal, frame: Image.Image) -> str:
        """
        When the port map is open and the goal is explore_port, deterministically
        pick the next unvisited building and tap it on the map.

        *frame* is the already-captured screenshot from perceive() — we reuse it
        so we don't re-capture and potentially see a different (closed) map.

        Returns:
          "tap"       — tapped a building; caller should poll for arrival
          "done"      — all buildings already visited
          "no_labels" — port map not open or OCR found nothing; retry next step
        """
        from brain.states.port_map import (
            read_port_map_buildings, close_port_map,
        )

        port = goal.target

        # The caller already verified before_state.scene_type == "port_map",
        # so the map is open in this frame. Skip the unreliable saturation
        # heuristic guard — just proceed directly to OCR.

        # Read the building labels from the already-captured frame
        buildings = read_port_map_buildings(frame)

        if not buildings:
            logger.warning("Port map OCR returned no labels — closing map to retry")
            close_port_map()
            return "no_labels"

        logger.debug(
            f"Port map labels: {[name for name, *_ in buildings]}"
        )

        # Cross-reference with KB: which buildings have we visited?
        port_record = self.kb.get_port(port) or {}
        visited: set[str] = {b.lower() for b in port_record.get("buildings", [])}
        logger.debug(f"Already visited: {sorted(visited)}")

        # Separate buildings into priority tiers:
        #   tier 1 — unvisited, no failures yet
        #   tier 2 — unvisited, failed < threshold (retry with lower priority)
        #   tier 3 — unvisited, failed >= threshold (last resort)
        tier1, tier2, tier3 = [], [], []
        for name, cx, cy in buildings:
            n = name.lower()
            if n in visited:
                continue
            fails = self._nav_failures.get(n, 0)
            if fails == 0:
                tier1.append((name, cx, cy))
            elif fails < self._NAV_FAIL_SKIP_THRESHOLD:
                tier2.append((name, cx, cy))
            else:
                tier3.append((name, cx, cy))

        if tier1:
            target = tier1[0]
        elif tier2:
            target = random.choice(tier2)
            logger.info(f"All fresh buildings tried — retrying from tier-2 failures")
        elif tier3:
            target = random.choice(tier3)
            logger.warning(
                f"All buildings have failed {self._NAV_FAIL_SKIP_THRESHOLD}+ times — "
                f"retrying randomly from tier-3 as last resort"
            )
        else:
            if not any(n.lower() not in visited for n, *_ in buildings):
                logger.info(
                    f"All {len(buildings)} port-map buildings already visited "
                    f"in {port!r} — marking explore_port done"
                )
                return "done"
            # All unvisited buildings have exhausted retries — try random tap
            logger.warning(
                "All navigation attempts failed — switching to random tap exploration"
            )
            close_port_map()
            return self._random_tap_exploration()

        name, cx, cy = target
        fails = self._nav_failures.get(name.lower(), 0)

        # The OCR returns the TEXT LABEL position. The tappable building ICON
        # sits above the label on the port map. Tap above the text to hit it.
        # On repeated failures, increase the offset to try hitting the icon
        # from different vertical positions.
        ICON_OFFSET_BASE = 70    # pixels above text label → icon centre
        ICON_OFFSET_STEP = 15    # additional offset per failure
        tap_y = max(55, cy - ICON_OFFSET_BASE - fails * ICON_OFFSET_STEP)
        tap_x = cx + random.randint(-8, 8)   # small horizontal jitter

        logger.info(
            f"Port map: tapping '{name}' — label at ({cx}, {cy}), "
            f"tapping icon at ({tap_x}, {tap_y})"
            + (f"  [attempt #{fails + 1}]" if fails > 0 else "")
        )
        # Record the building name so the arrival handler can correctly classify
        # the scene even when OCR garbles the title (e.g. 'exnlore' for 'bureau').
        self._pending_building = name.lower()
        from actions.adb_actions import tap as _tap
        _tap(tap_x, tap_y)
        time.sleep(random.uniform(1.5, 2.5))
        return "tap"

    # ── random exploration fallback ───────────────────────────────────────────

    def _random_tap_exploration(self) -> str:
        """
        Last-resort exploration: when all known navigation paths have failed,
        tap random visible text labels or interactive elements on the current
        screen to discover new interactions.

        Strategy:
          1. Capture the current screen
          2. Run OCR on the full screen to collect all visible text fragments
          3. Filter out known-useless regions (port name, chrome labels)
          4. Tap a randomly chosen fragment
          5. Wait briefly and observe what changed

        Returns "tap" so the caller's polling loop handles the outcome.
        """
        from vision.ocr import _get_reader
        from config.settings import SCREEN_WIDTH, SCREEN_HEIGHT

        logger.info("Random tap exploration — scanning screen for tappable elements")
        time.sleep(random.uniform(1.0, 2.0))
        frame = capture_screen()

        # OCR the full screen
        raw = _get_reader().readtext(
            __import__("numpy").array(frame), detail=1
        )

        # Collect candidate tap positions: text fragments with decent confidence
        # that are not in the top chrome strip (port name, back arrow, etc.)
        CHROME_HEIGHT = 80   # skip top N pixels (chrome bar)
        candidates: list[tuple[int, int, str]] = []
        for bbox, text, conf in raw:
            if conf < 0.4 or len(text.strip()) < 2:
                continue
            ys = [p[1] for p in bbox]
            cy = (min(ys) + max(ys)) / 2
            if cy < CHROME_HEIGHT:
                continue   # skip chrome
            xs = [p[0] for p in bbox]
            cx = int((min(xs) + max(xs)) / 2)
            candidates.append((int(cx), int(cy), text.strip()))

        if not candidates:
            logger.warning("Random tap: no candidates found — pressing Back to reset")
            from actions.adb_actions import press_back
            press_back()
            return "tap"

        cx, cy, label = random.choice(candidates)
        logger.info(
            f"Random tap: '{label}' at ({cx}, {cy})  "
            f"(chose 1 of {len(candidates)} candidates)"
        )
        from actions.adb_actions import tap as _tap
        _tap(cx, cy)
        time.sleep(random.uniform(2.0, 3.5))
        # Reset nav failures after random exploration so we try buildings again next cycle
        self._nav_failures.clear()
        return "tap"

    # ── post-action verification ──────────────────────────────────────────────

    def _wait_for_outcome(
        self,
        action: AgentAction,
        before_state: ScreenState,
        poll_interval: float = 5.0,
        max_polls: int = 4,
    ) -> ScreenState:
        """
        Poll the screen after an action until the state visibly changes
        or max_polls × poll_interval seconds have elapsed.

        A state is considered "changed" when scene_type or screen_title
        differs from before.  This handles the case where a character must
        walk across a large port before entering a building — the scene stays
        `port_overworld` while walking, then flips to `building_interior`
        on arrival.

        Returns the settled ScreenState (may still be the same as before if
        nothing happened, so the caller should check).
        """
        last_frame: Image.Image | None = None
        for poll in range(1, max_polls + 1):
            time.sleep(poll_interval)
            frame = capture_screen()

            # Fast path: use classifier + chrome only (~0.2s) to detect change.
            # Full perceive (~30s llava call) is deferred until change confirmed.
            scene, title = self._quick_classify(frame)

            scene_changed = scene != before_state.scene_type
            title_changed = title != before_state.screen_title

            if scene_changed or title_changed:
                # State changed — now run full perceive to get complete info.
                full_state = self.perceive(frame)
                logger.info(
                    f"State settled after {poll * poll_interval:.0f}s: "
                    f"[{before_state.scene_type}]{before_state.screen_title!r}"
                    f" → [{full_state.scene_type}]{full_state.screen_title!r}"
                )
                return full_state

            logger.debug(
                f"Still [{scene}]{title!r} "
                f"after {poll * poll_interval:.0f}s — polling again "
                f"({poll}/{max_polls})"
            )
            last_frame = frame

        logger.warning(
            f"Action {action.action_type!r} → no visible state change "
            f"after {max_polls * poll_interval:.0f}s"
        )
        # Return a full perceive on the last frame so callers always get a
        # complete ScreenState (description, visible_elements, etc.)
        if last_frame is not None:
            return self.perceive(last_frame)
        return before_state

    def _verify_outcome(
        self,
        action: AgentAction,
        before_state: ScreenState,
        after_state: ScreenState,
    ) -> None:
        """
        Check whether the action landed us where expected.

        Handles two bad cases:
        - Accidentally entered a building (tapped something that wasn't the
          intended target, e.g. tapped where mini map used to be and hit a
          building entrance instead).
        - No state change at all (the target element did not exist on screen).

        Logging here is informational; recovery is left to the next step()
        call, which will re-perceive and handle the resulting state.
        """
        goal = self.goals.current()

        no_change = (
            after_state.scene_type == before_state.scene_type
            and after_state.screen_title == before_state.screen_title
        )
        if no_change:
            logger.warning(
                f"Action {action.action_type!r} on "
                f"{action.target_description!r} had no visible effect — "
                "the tapped element may not exist on this screen"
            )
            return

        # Accidentally entered a building when we didn't intend to
        entered_building_unexpectedly = (
            after_state.scene_type == "building_interior"
            and before_state.scene_type != "building_interior"
            and action.action_type not in ("tap",)   # intentional taps may navigate
        )
        if entered_building_unexpectedly:
            logger.warning(
                f"Unexpected building entry after {action.action_type!r}: "
                f"now inside '{after_state.screen_title}'. "
                "Next step will investigate and exit."
            )

    # ── main loop ─────────────────────────────────────────────────────────────

    def step(self) -> str:
        """
        One full perceive → reason → act → learn cycle.
        Returns the action_type string, or 'done'/'stuck' as control signals.
        """
        before_frame = capture_screen()
        before_state = self.perceive(before_frame)

        logger.info(
            f"[{before_state.scene_type}] title={before_state.screen_title!r}  "
            f"goal={self.goals.current()}"
        )

        goal = self.goals.current()

        # Hard rule: if the explore_port goal has an unknown port name and we can
        # now read the port name (we're on the overworld), update the goal target.
        _PLACEHOLDER_NAMES = {"unknown_port", "unknown port", ""}
        if (
            goal
            and goal.type == "explore_port"
            and goal.target.lower() in _PLACEHOLDER_NAMES
            and before_state.scene_type == "port_overworld"
        ):
            # Re-read the title directly from OCR (not from state, which may have
            # a stale llava-provided placeholder like "Unknown_Port").
            from vision.ocr import read_screen_title
            live_title = read_screen_title(before_state.frame) or ""
            if live_title and live_title.lower() not in _PLACEHOLDER_NAMES:
                logger.info(
                    f"Recovering port name from overworld OCR: {live_title!r}"
                )
                goal.target = live_title
                goal.description = (
                    f"Visit every building in {live_title} and learn what each one does"
                )

        # Hard rule: if we're inside a building and the goal is to explore the
        # port, exit the building first.
        if (
            before_state.scene_type == "building_interior"
            and goal
            and goal.type == "explore_port"
        ):
            # Learn about this building before leaving — L1 KB check first,
            # then LLM classification if it's new or poorly known.
            self._investigate_building(before_state, port=goal.target)

            # Prefer tapping the home button (direct jump to overworld) over
            # pressing back (which may navigate through sub-menus).
            # Fall back to press_back if the home button was not detected.
            HOME_BUTTON_FALLBACK = (2350, 40)  # known approximate location
            if before_state.has_home_button:
                coords = before_state.home_button_coords or HOME_BUTTON_FALLBACK
                action = AgentAction(
                    action_type="tap",
                    target_description="home button (⌂) — return to port overworld",
                    coords=coords,
                    reasoning="home button jumps directly back to port overworld",
                    hypothesis="scene type will change to port_overworld",
                    confidence=0.95,
                )
            else:
                action = AgentAction(
                    action_type="press_back",
                    target_description="back — exit building toward port overworld",
                    reasoning="no home button visible; pressing back to navigate out",
                    hypothesis="scene type will change toward port_overworld",
                    confidence=0.8,
                )

            logger.info(
                f"Inside building '{before_state.screen_title}' during port explore "
                f"— exiting via {action.action_type} "
                f"(home_button={before_state.has_home_button})"
            )
            self._consecutive_action_failures = 0
            self._last_action_type = ""
            self.act(action)
            # Poll until we're back on the overworld (or give up after 20s)
            after_state = self._wait_for_outcome(action, before_state)
            if after_state.scene_type == "building_interior":
                logger.warning(
                    "Still inside a building after exit attempt — "
                    f"now at '{after_state.screen_title}'"
                )
            return action.action_type

        # Hard rule: inside a sub_menu while exploring — press back to escape.
        # The sub_menu scene appears when we're inside a building's nested screen
        # (e.g. Harbor → Supply, Market → Purchase).  We don't want to learn
        # anything here; just back out until we reach the overworld.
        if (
            before_state.scene_type == "sub_menu"
            and goal
            and goal.type == "explore_port"
        ):
            action = AgentAction(
                action_type="press_back",
                target_description="back — exit sub-menu toward port overworld",
                reasoning="in a sub-menu during port explore — backing out",
                hypothesis="scene will change toward building_interior or port_overworld",
                confidence=0.9,
            )
            logger.info(
                f"Sub-menu (title={before_state.screen_title!r}) during port explore "
                "— pressing back to exit"
            )
            self._consecutive_action_failures = 0
            self._last_action_type = ""
            self.act(action)
            after_state = self._wait_for_outcome(action, before_state)
            return action.action_type

        # Hard rule: port map is open — navigate deterministically.
        # The LLM has no idea what to do on the port map; hard-code the
        # "read labels → tap first unvisited building" logic here.
        if (
            before_state.scene_type == "port_map"
            and goal
            and goal.type == "explore_port"
        ):
            self._consecutive_action_failures = 0
            self._last_action_type = ""
            nav_result = self._navigate_from_port_map(goal, before_state.frame)

            if nav_result == "done":
                # All buildings visited — pop goal
                logger.info(f"explore_port({goal.target!r}) — all buildings visited")
                self.goals.pop()
                return "done"

            if nav_result == "no_labels":
                # Map closed by helper; will re-open on next step
                time.sleep(random.uniform(1.5, 2.5))
                return "wait"

            # nav_result == "tap" — tapped a building; wait until character arrives.
            # After tapping a building on the port map:
            #   1. Port map closes → scene goes port_map → port_overworld
            #   2. Character walks to building (can take 10-60s)
            #   3. Character auto-enters → scene goes port_overworld → building_interior
            #
            # _wait_for_outcome returns on the FIRST state change (port_map→port_overworld),
            # which is too early — the character is still walking.  Poll again with
            # a longer timeout specifically waiting for building_interior.
            action = AgentAction(
                action_type="tap",
                target_description="building on port map",
                reasoning="port map navigation — deterministic building tap",
                confidence=0.95,
            )
            # Phase 1: wait for port map to close (up to 15s)
            after_state = self._wait_for_outcome(
                action, before_state, poll_interval=5.0, max_polls=3
            )
            # Track whether the building was entered via a sub_menu screen.
            # Many buildings (Bank, Bureau, Inn, …) open a service-selection
            # dialog immediately on entry — the classifier sees sub_menu, not
            # building_interior.  The next step() will see sub_menu and press
            # back before we can investigate, so we must investigate HERE.
            _entered_via_sub_menu = False

            # If the character was very close to the building, phase 1 may
            # already land on sub_menu.  Patch immediately so we skip the
            # 60s phase 2 wait and go straight to investigation.
            if after_state.scene_type == "sub_menu" and self._pending_building:
                logger.info(
                    f"Phase 1 landed on sub_menu — treating as "
                    f"building_interior '{self._pending_building}' "
                    f"(OCR title was {after_state.screen_title!r})"
                )
                after_state.scene_type = "building_interior"
                after_state.screen_title = self._pending_building
                _entered_via_sub_menu = True
            # Phase 2: if still on overworld (character walking), keep polling
            # for up to 60s waiting for building_interior arrival.
            if after_state.scene_type != "building_interior":
                logger.info(
                    "Character navigating to building — polling for arrival "
                    "(up to 60s)"
                )
                walk_action = AgentAction(
                    action_type="wait",
                    target_description="waiting for character to arrive at building",
                )
                arrived_state = self._wait_for_outcome(
                    walk_action, after_state, poll_interval=10.0, max_polls=6
                )
                # Accept sub_menu as "arrived" when we just tapped a known
                # building.  Some buildings (e.g. bureau) open a sub-menu
                # dialog immediately on entry, so OCR reads a garbled/menu
                # title instead of the building name.  Patch the state so
                # _investigate_building gets the correct title.
                if (
                    arrived_state.scene_type == "sub_menu"
                    and self._pending_building
                ):
                    logger.info(
                        f"Sub-menu after building tap — treating as "
                        f"building_interior '{self._pending_building}' "
                        f"(OCR title was {arrived_state.screen_title!r})"
                    )
                    arrived_state.scene_type = "building_interior"
                    arrived_state.screen_title = self._pending_building
                    _entered_via_sub_menu = True

                if arrived_state.scene_type == "building_interior":
                    # Success — reset failure count for this building
                    if self._pending_building:
                        self._nav_failures.pop(self._pending_building, None)
                    after_state = arrived_state
                else:
                    logger.warning(
                        f"Character did not enter building after 60s — "
                        f"still at {arrived_state.scene_type!r} "
                        f"'{arrived_state.screen_title}'"
                    )
                    # Record navigation failure so this building is deprioritised
                    if self._pending_building:
                        count = self._nav_failures.get(self._pending_building, 0) + 1
                        self._nav_failures[self._pending_building] = count
                        logger.info(
                            f"Navigation failure #{count} for "
                            f"'{self._pending_building}' — "
                            f"{'will skip next attempt' if count >= self._NAV_FAIL_SKIP_THRESHOLD else 'will retry'}"
                        )
                    after_state = arrived_state
            self._pending_building = None  # consumed
            # If the building opened as a sub_menu dialog (no building_interior
            # phase), investigate NOW — the next step() will see sub_menu and
            # press back without giving us another investigation window.
            if _entered_via_sub_menu and after_state.scene_type == "building_interior":
                self._investigate_building(after_state, port=goal.target)
            self.learn(before_state, action, after_state)
            return "tap"

        action = self.reason(before_state)

        if action.action_type in ("done", "stuck"):
            logger.info(f"Agent signal: {action.action_type!r} — {action.reasoning}")
            self._last_action_type = action.action_type
            self._consecutive_action_failures = 0
            return action.action_type

        # ── Failure loop detection ─────────────────────────────────────────────
        # If the LLM picks the same action_type twice and neither led to a
        # visible state change, it is operating on a wrong mental model.
        # Interrupt: investigate the screen first, then let the LLM re-plan.
        if action.action_type == self._last_action_type:
            self._consecutive_action_failures += 1
        else:
            self._consecutive_action_failures = 0
        self._last_action_type = action.action_type

        if self._consecutive_action_failures >= 2:
            logger.warning(
                f"Same action {action.action_type!r} chosen {self._consecutive_action_failures + 1} "
                "times in a row — re-examining current screen before retrying."
            )
            self._consecutive_action_failures = 0
            self._last_action_type = ""

            if before_state.scene_type == "building_interior":
                # Inside a building — investigate what it is
                port = goal.target if goal else ""
                self._investigate_building(before_state, port=port)
            else:
                # On overworld or unknown — just re-perceive fresh; the
                # chrome detector will correct any misclassification.
                logger.info(
                    f"Stuck on {before_state.scene_type!r} — "
                    "pausing so next step re-perceives with fresh chrome detection"
                )

            time.sleep(random.uniform(1.5, 2.5))
            return "wait"

        # ── Pre-action guard ──────────────────────────────────────────────────
        # Validate that required UI elements are actually present before acting.
        # If they aren't, the tap will land on whatever happens to be at those
        # coordinates — which is how the bot ends up inside the Harbor while
        # trying to open the port map.
        if action.action_type == "open_port_map" and before_state.scene_type != "port_overworld":
            logger.warning(
                f"open_port_map requested but scene is {before_state.scene_type!r} "
                f"(title={before_state.screen_title!r}) — not on port overworld, skipping."
            )
            if before_state.scene_type == "building_interior":
                port = goal.target if goal else ""
                self._investigate_building(before_state, port=port)
            self._consecutive_action_failures = 0
            self._last_action_type = ""
            time.sleep(random.uniform(1.5, 2.5))
            return "wait"

        self.act(action)

        # ── Post-action: poll until screen settles ────────────────────────────
        # Wait up to 20 s in 5-second intervals.  A character walking to a
        # building in a large port may take longer than a fixed 2-second pause.
        after_state = self._wait_for_outcome(action, before_state)

        # Check if the outcome is what we expected — log unexpected outcomes
        # so the next step() can re-perceive and adapt.
        self._verify_outcome(action, before_state, after_state)

        self.learn(before_state, action, after_state)

        return action.action_type

    def run(
        self,
        goal: Goal,
        max_steps: int = 200,
        stop_event: threading.Event | None = None,
    ) -> None:
        """
        Run the agent loop until the goal is complete, max_steps is reached,
        or stop_event is set.
        """
        self._stop_event = stop_event or threading.Event()
        self.goals.push(goal)

        logger.info(f"Agent starting  goal={goal}  max_steps={max_steps}")

        if not self.vision.check_available():
            logger.error(
                "Agent cannot start — vision model not available. "
                "See above for install instructions."
            )
            return

        consecutive_stuck = 0

        for step_num in range(1, max_steps + 1):
            if self._stop_event.is_set():
                logger.info("Agent stopped by external signal")
                break

            logger.info(f"── step {step_num}/{max_steps} ──")

            result = self.step()

            if result == "done":
                logger.info(f"Goal achieved: {goal}")
                self.goals.pop()
                break

            if result == "stuck":
                consecutive_stuck += 1
                if consecutive_stuck >= 3:
                    logger.warning(
                        f"Agent stuck for {consecutive_stuck} steps in a row — stopping"
                    )
                    break
                logger.warning(
                    f"Agent stuck ({consecutive_stuck}/3) — pausing before retry"
                )
                time.sleep(random.uniform(4.0, 8.0))
            else:
                consecutive_stuck = 0

        logger.info(f"Agent run finished after {step_num} step(s)")


# ── standalone entry point ────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from memory.logger import setup_logging

    setup_logging()
    agent = Agent()

    args = sys.argv[1:]
    if not args or args[0] == "learn":
        goal = Goal.idle()
    elif args[0] == "explore":
        from vision.ocr import read_port_name
        port = read_port_name(capture_screen()) or "unknown_port"
        goal = Goal.explore_port(port)
    elif args[0] == "goto" and len(args) > 1:
        goal = Goal.go_to_building(" ".join(args[1:]))
    elif args[0] == "sail" and len(args) > 1:
        goal = Goal.sail_to(" ".join(args[1:]))
    else:
        print(f"Usage: python -m brain.agent [learn | explore | goto <building> | sail <port>]")
        sys.exit(1)

    agent.run(goal, max_steps=50)
