# vision/claude_vision.py
# Claude Vision API client for structured scene analysis.
#
# This is the L3 tier of the tiered vision pipeline: used on the FIRST visit
# to any new scene to extract a structured inventory of all UI elements.
# Results are cached in memory/knowledge/scenes/ so the API is never called
# again for the same (scene_type, screen_title) combination.
#
# Why Claude API instead of llava:
#   llava:7b is a general-purpose 7B model — it can describe a scene but
#   halluculates heavily when asked to precisely identify interactive UI elements,
#   their positions, and their game-specific purposes.
#   Claude (claude-sonnet-4-6 or better) reliably reads text, identifies icons,
#   distinguishes buttons from labels, and understands mobile game UI conventions.
#
# Usage:
#   from vision.claude_vision import get_claude_vision
#   inv = get_claude_vision().analyse_scene(frame, scene_type="building_interior",
#                                            screen_title="market")

from __future__ import annotations

import base64
import io
import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from loguru import logger
from PIL import Image

# ── data structures ────────────────────────────────────────────────────────────

@dataclass
class UIElement:
    """A single discovered UI element (text label, icon, or interactive component)."""
    label: str               # visible text or icon description
    element_type: str        # "text", "icon", "button", "tab", "list_item", "panel"
    purpose: str             # inferred game purpose (e.g. "opens purchase screen")
    # Position as fraction of screen (0.0–1.0), resolution-independent
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    # Tap-target centre in absolute pixels (set after scaling to actual resolution)
    tap_x: int | None = None
    tap_y: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "UIElement":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class SceneInventory:
    """
    Everything the bot has discovered about a specific scene.

    Keyed by (scene_type, screen_title) — e.g. ("building_interior", "market").
    Stored in memory/knowledge/scenes/<scene_type>__<screen_title>.json.
    """
    scene_type: str
    screen_title: str
    layout_description: str = ""
    navigation_hints: str = ""    # how to navigate: what to tap for what outcome
    elements: list[UIElement] = field(default_factory=list)
    visit_count: int = 0
    first_analysed_at: str = ""
    last_analysed_at: str = ""
    raw_api_response: dict = field(default_factory=dict)

    # ── convenience accessors ─────────────────────────────────────────────────

    def interactive(self) -> list[UIElement]:
        """Return only tappable elements (buttons, tabs, list items)."""
        return [e for e in self.elements
                if e.element_type in ("button", "tab", "list_item", "interactive")]

    def find(self, label: str) -> UIElement | None:
        """Case-insensitive search for an element by label."""
        label_l = label.lower()
        return next(
            (e for e in self.elements if label_l in e.label.lower()),
            None,
        )

    def summary(self) -> str:
        interactive = self.interactive()
        labels = [e.label for e in interactive]
        return (
            f"{self.scene_type}/{self.screen_title}: "
            f"{len(self.elements)} elements, "
            f"interactive={labels}"
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["elements"] = [e.to_dict() for e in self.elements]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SceneInventory":
        elements = [UIElement.from_dict(e) for e in d.pop("elements", [])]
        obj = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        obj.elements = elements
        return obj


# ── storage helpers ────────────────────────────────────────────────────────────

_SCENES_DIR = Path("memory/knowledge/scenes")
_LEARNED_FP_DIR = Path("memory/knowledge/learned_fingerprints")


def _slug(s: str) -> str:
    return s.lower().replace(" ", "_").replace("/", "_").replace("\\", "_")


def _scene_path(scene_type: str, screen_title: str) -> Path:
    key = f"{_slug(scene_type)}__{_slug(screen_title)}" if screen_title else _slug(scene_type)
    return _SCENES_DIR / f"{key}.json"


def load_inventory(scene_type: str, screen_title: str) -> SceneInventory | None:
    """Load a cached scene inventory, or None if not yet analysed."""
    path = _scene_path(scene_type, screen_title)
    if not path.exists():
        return None
    try:
        return SceneInventory.from_dict(json.loads(path.read_text()))
    except Exception as exc:
        logger.warning(f"Failed to load scene inventory {path}: {exc}")
        return None


def save_inventory(inv: SceneInventory) -> None:
    """Persist a scene inventory to disk."""
    _SCENES_DIR.mkdir(parents=True, exist_ok=True)
    path = _scene_path(inv.scene_type, inv.screen_title)
    path.write_text(json.dumps(inv.to_dict(), indent=2, ensure_ascii=False))
    logger.debug(f"Scene inventory saved: {path}")


def _derive_state_id(inv: SceneInventory, button_labels: list[str]) -> str:
    """Pick a stable state_id for a learned screen.

    Priority:
      1. Two distinctive button labels joined ('quick_revive_port_return') —
         picks the two most action-y labels Claude saw.  Stable across
         visits because it's anchored on actionable buttons rather than
         decorative text.
      2. Fall back to a slug of Claude's first noun-phrase if no
         distinctive buttons.

    The state_id is intended to be unique per distinct game screen.
    """
    candidates = [
        l for l in button_labels
        if l and 2 <= len(l) <= 30 and not l.lower().isdigit()
    ]
    # Skip generic chrome words.
    SKIP = {"icon", "ok", "yes", "no", "cancel", "x", "close", "?", "settings"}
    candidates = [l for l in candidates if l.lower() not in SKIP]
    if len(candidates) >= 2:
        slug = "_".join(candidates[:2]).lower()
        slug = "".join(c if (c.isalnum() or c == "_") else "_" for c in slug)
        slug = "_".join(p for p in slug.split("_") if p)
        return f"learned_{slug}"
    # Fallback to Claude description first words.
    desc = (inv.layout_description or "").lower().strip()
    first_words = "_".join(desc.split()[:4])
    slug = "".join(c if (c.isalnum() or c == "_") else "_" for c in first_words)
    slug = "_".join(p for p in slug.split("_") if p) or "unknown"
    return f"learned_{slug}"


def _build_learned_fingerprint(
    state_id: str,
    button_labels: list[str],
    frame_w: int,
    frame_h: int,
    labeled_positions: list[tuple[str, int, int]] | None = None,
) -> dict:
    """Generate a candidate Fingerprint shape from observed labels.

    When *labeled_positions* (list of (label, cx, cy)) is provided and
    the distinctive labels form a vertical column (≥ 2 labels on
    similar cx, distinct cy), emit a VerticalListSignal restricted to
    that column band — structural fingerprints are far less prone to
    false-firing on unrelated screens whose text happens to include
    one of the labels.

    Falls back to LabelSetSignal in the broad centre when no column
    structure is detectable (sparse hit set, scattered labels, or
    horizontal layouts).

    Origin: 2026-05-14.  The original LabelSetSignal-only builder
    produced learned_deposit_withdrawal_savings_acco which poisoned
    every subsequent classification — its labels ('insurance',
    'savings acco') appeared in passing text on the port_overworld
    too.  Anchoring to layout (vertical column) eliminates that class
    of false positive.
    """
    SKIP = {"icon", "ok", "yes", "no", "cancel", "x", "close", "?", "settings"}

    def _is_distinctive(label: str) -> bool:
        if not label or not (2 <= len(label) <= 30):
            return False
        l = label.lower().strip()
        if l.isdigit() or l in SKIP:
            return False
        return True

    distinctive = [l.lower().strip() for l in button_labels
                   if _is_distinctive(l)][:4]
    if not distinctive:
        return {}

    # Prefer a structural VerticalListSignal when the labels arrange
    # themselves into a column in the live frame.
    if labeled_positions:
        col = _detect_label_column(
            labeled_positions, distinctive, frame_w, frame_h,
        )
        if col is not None:
            region, column_labels, x_tolerance = col
            return {
                "state_id":              state_id,
                "description":           (
                    "Learned via Claude scene analysis (vertical-list "
                    "layout — labels arranged as a column at a fixed "
                    "region)."
                ),
                "positive_signals": [{
                    "kind":         "VerticalListSignal",
                    "name":         "claude_observed_column",
                    "region":       list(region),
                    "labels":       column_labels,
                    "min_matches":  min(2, len(column_labels)),
                    "x_tolerance":  x_tolerance,
                    "y_min_gap":    60,
                }],
                "negative_signals":      [],
                "min_positive_to_match": 1,
            }

    # Fallback: broad-centre LabelSetSignal (preserved for
    # backward compatibility on screens without a list layout).
    return {
        "state_id":              state_id,
        "description":           "Learned via Claude scene analysis.",
        "positive_signals": [{
            "kind":       "LabelSetSignal",
            "name":       "claude_observed_labels",
            "region":     [0.20, 0.10, 0.80, 0.80],
            "labels":     distinctive,
            "min_matches": min(2, len(distinctive)),
        }],
        "negative_signals":      [],
        "min_positive_to_match": 1,
    }


def _detect_label_column(
    labeled_positions: list[tuple[str, int, int]],
    distinctive: list[str],
    frame_w: int,
    frame_h: int,
    x_tol: int = 80,
    y_min_gap: int = 60,
):
    """Look for a vertical column where ≥ 2 of *distinctive* labels
    appear at similar cx with distinct cy values.

    Returns (region, column_labels, x_tolerance) when a column is
    found, else None.

    *region* is the normalised bbox around the detected column with
    generous padding — wide enough to absorb minor render drift
    between visits, narrow enough that unrelated-screen text doesn't
    sneak in.
    """
    # Restrict to elements whose label contains one of the distinctive
    # substrings.
    candidates: list[tuple[str, int, int]] = []
    for label, cx, cy in labeled_positions:
        if not label:
            continue
        lab_l = label.lower().strip()
        for target in distinctive:
            if target in lab_l:
                candidates.append((target, cx, cy))
                break
    if len(candidates) < 2:
        return None

    # Cluster by cx; pick the column with the most distinct labels
    # at vertically-separated cy values.
    candidates.sort(key=lambda h: h[1])
    clusters: list[list[tuple[str, int, int]]] = []
    for h in candidates:
        if clusters and abs(h[1] - sum(c[1] for c in clusters[-1]) / len(clusters[-1])) <= x_tol:
            clusters[-1].append(h)
        else:
            clusters.append([h])

    def _stacked_labels(cluster):
        cluster_sorted = sorted(cluster, key=lambda h: h[2])
        kept_y: list[int] = []
        kept_labels: set[str] = set()
        for label, _cx, cy in cluster_sorted:
            if not kept_y or cy - kept_y[-1] >= y_min_gap:
                kept_y.append(cy)
                kept_labels.add(label)
        return kept_labels

    best_cluster = max(clusters, key=lambda c: len(_stacked_labels(c)))
    column_labels = sorted(_stacked_labels(best_cluster))
    if len(column_labels) < 2:
        return None

    # Build a tight region around the column — wide enough to absorb
    # ±x_tol drift, top-to-bottom across the cluster's cy range with
    # padding so the column can grow/shrink slightly between visits.
    cx_vals = [h[1] for h in best_cluster]
    cy_vals = [h[2] for h in best_cluster]
    cx_min, cx_max = min(cx_vals), max(cx_vals)
    cy_min, cy_max = min(cy_vals), max(cy_vals)
    pad_x = x_tol + 40
    pad_y = max(y_min_gap, 80)
    region = (
        max(0.0, (cx_min - pad_x) / frame_w),
        max(0.0, (cy_min - pad_y) / frame_h),
        min(1.0, (cx_max + pad_x) / frame_w),
        min(1.0, (cy_max + pad_y) / frame_h),
    )
    return region, column_labels, x_tol


def _maybe_save_learned_candidate(
    inv: SceneInventory,
    detected_elements: list,
    frame_w: int,
    frame_h: int,
) -> None:
    """Phase 6.5 L3 — when the registry's verdict on this OmniParser
    element list disagrees with the L0/chrome verdict, persist a
    LEARNED FINGERPRINT — not just a raw description.  The candidate
    is a usable Fingerprint shape that the registry auto-loads on the
    next startup, so the bot recognises the screen on its very next
    encounter without any manual review.

    The user can still review and promote learned fingerprints into
    vision/state_fingerprints_data.py for permanent foundational
    status, but the bot doesn't wait for that promotion to use them.
    """
    from datetime import datetime, timezone

    try:
        import vision.state_fingerprints_data  # noqa: F401
        from vision.state_fingerprints import classify_via_registry
    except Exception:
        return

    registry_result = classify_via_registry(detected_elements, frame_w, frame_h)
    registry_state = registry_result.state if registry_result else None

    # Agreement → registry already classifies this screen correctly.
    if registry_state == inv.scene_type:
        return

    # Extract distinctive labels from Claude's "interactive" elements
    # — those are what Claude itself thought were tap-worthy.  Falls
    # back to all OmniParser button labels.
    interactive = inv.interactive() if hasattr(inv, "interactive") else []
    button_labels = [el.label for el in interactive if el.label]
    labeled_positions: list[tuple[str, int, int]] = [
        (el.label, el.tap_x or 0, el.tap_y or 0)
        for el in interactive
        if el.label and el.tap_x is not None and el.tap_y is not None
    ]
    if not button_labels:
        button_labels = [
            el.label for el in detected_elements
            if el.element_type in ("button", "text") and el.label
        ]
        labeled_positions = [
            (el.label, el.cx, el.cy)
            for el in detected_elements
            if el.element_type in ("button", "text") and el.label
        ]

    state_id = _derive_state_id(inv, button_labels)

    fingerprint_shape = _build_learned_fingerprint(
        state_id=state_id,
        button_labels=button_labels,
        frame_w=frame_w,
        frame_h=frame_h,
        labeled_positions=labeled_positions,
    )

    _LEARNED_FP_DIR.mkdir(parents=True, exist_ok=True)
    path = _LEARNED_FP_DIR / f"{_slug(state_id)}.json"

    # Existing candidate?  Bump visit_count.
    visit_count = 1
    first_seen_at = None
    if path.exists():
        try:
            prev = json.loads(path.read_text())
            visit_count = int(prev.get("visit_count", 0)) + 1
            first_seen_at = prev.get("first_seen_at")
        except Exception:
            pass

    now = datetime.now(timezone.utc).isoformat()
    record = {
        # Discovery metadata
        "scene_type_at_discovery":  inv.scene_type,
        "screen_title":             inv.screen_title,
        "registry_verdict_at_discovery": registry_state,
        "visit_count":              visit_count,
        "first_seen_at":            first_seen_at or now,
        "last_seen_at":             now,
        "frame_size":               [frame_w, frame_h],
        "claude_layout":            inv.layout_description,
        "claude_navigate":          inv.navigation_hints,
        # Usable Fingerprint shape — auto-loaded by the registry at startup.
        "fingerprint":              fingerprint_shape,
        # Raw OmniParser elements for review / future fingerprint refinement.
        "elements": [
            {
                "type":   el.element_type,
                "label":  el.label,
                "cx":     el.cx, "cy": el.cy,
                "cx_norm": round(el.cx / frame_w, 4),
                "cy_norm": round(el.cy / frame_h, 4),
            }
            for el in detected_elements
        ],
        "promotion_hint": (
            "This file is auto-loaded by the registry at startup.  To "
            "promote it to a permanent foundational fingerprint, copy "
            "the fingerprint block into vision/state_fingerprints_data.py "
            "as a register_fingerprint(Fingerprint(...)) call and delete "
            "this file."
        ),
    }
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
    logger.info(
        f"Learned fingerprint saved: {state_id!r} → {path}  "
        f"(registry said {registry_state!r}, L0 said {inv.scene_type!r}, "
        f"visit #{visit_count})"
    )

    # Hot-register the new fingerprint into the live registry so the
    # NEXT classify_screen call sees it — no process restart required.
    # If the state_id collides with a foundational fingerprint, we
    # silently skip (foundation wins).
    try:
        from vision.state_fingerprints import (
            FINGERPRINT_REGISTRY, Fingerprint,
            LabelSetSignal, ElementCountSignal, TextContainsSignal,
            _signal_from_dict,
        )
        if state_id in FINGERPRINT_REGISTRY:
            return
        positive = tuple(
            _signal_from_dict(s) for s in fingerprint_shape.get("positive_signals", [])
        )
        negative = tuple(
            _signal_from_dict(s) for s in fingerprint_shape.get("negative_signals", [])
        )
        fp = Fingerprint(
            state_id=state_id,
            description=fingerprint_shape.get("description", ""),
            positive_signals=positive,
            negative_signals=negative,
            min_positive_to_match=fingerprint_shape.get("min_positive_to_match", 1),
        )
        FINGERPRINT_REGISTRY[state_id] = fp
        logger.info(
            f"Learned fingerprint hot-registered: {state_id!r} now "
            f"available for the next classify_screen call"
        )
    except Exception as exc:
        logger.debug(f"Hot-registration skipped: {exc}")


# ── Claude Vision API client ───────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a game UI analyst specialising in the mobile game "Uncharted Waters Origin" (UWO).
You will receive a screenshot and must return a structured JSON object describing every
visible UI element — text labels, icons, buttons, tabs, panels — and infer their game purpose.

Rules:
- Focus on UI elements, not narrative content (don't describe sea waves, building decor, etc.)
- Positions are fractions of screen width/height (0.0 = top/left, 1.0 = bottom/right)
- Be precise: distinguish clickable buttons from decorative labels
- Infer purpose from game context (e.g. a sword icon near a list = combat menu)
- If you see a left-side vertical list of large buttons, those are the primary sub-menu items
- Screen resolution is 2400×1080 (landscape)\
"""

_ANALYSE_PROMPT = """\
This is a screen from the mobile game "Uncharted Waters Origin" (UWO), an Age of Sail trading/exploration RPG.
Scene type: {scene_type}
Screen title (OCR): {screen_title}

A local UI detector (OmniParser) has already identified the following elements on screen:
{element_list}

Using these detected elements and the thumbnail image for visual context, return ONLY a JSON object:
{{
  "layout_description": "one paragraph describing the overall screen layout and purpose",
  "navigation_hints": "concise guide: what to tap for what outcome",
  "elements": [
    {{
      "label": "exact label from the detected list above",
      "element_type": "button|tab|list_item|text|icon|panel",
      "purpose": "what this element does in the game context",
      "tap_x": 0,
      "tap_y": 0
    }}
  ]
}}

Rules:
- Use the tap_x/tap_y values from the detected list — do not guess positions
- Focus on game purpose: what does tapping this element DO in UWO?
- Left-side vertical button lists = the building's primary services
- Right-side panels = information display (prices, inventory, status)
- If you see "Purchase" and "Sell" tabs: this is a market building
- If you see "Hire Sailors" or "Rest": this is an inn/tavern
- If you see "Exchange", "Deposit", "Withdraw": this is a bank\
"""


class ClaudeVision:
    """
    Wrapper around the Anthropic Claude API for structured game UI analysis.

    Requires the ANTHROPIC_API_KEY environment variable.  Falls back to a
    no-op if the key is missing, so the rest of the bot continues to work.
    """

    def __init__(self, model: str = "claude-sonnet-4-6") -> None:
        self.model = model
        self._client = None
        self._available: bool | None = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import anthropic
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                logger.warning(
                    "ANTHROPIC_API_KEY not set — Claude Vision analysis disabled. "
                    "Set the env var to enable structured scene analysis."
                )
                self._available = False
                return None
            self._client = anthropic.Anthropic(api_key=api_key)
            self._available = True
            return self._client
        except ImportError:
            logger.warning(
                "anthropic package not installed — run: pip install anthropic"
            )
            self._available = False
            return None

    @property
    def available(self) -> bool:
        if self._available is None:
            self._get_client()
        return bool(self._available)

    def _encode_image(self, frame: Image.Image) -> str:
        """Encode a PIL image as base64 JPEG for the API."""
        buf = io.BytesIO()
        # Resize to reduce tokens/cost while keeping readable
        w, h = frame.size
        if w > 1200:
            frame = frame.resize((1200, int(h * 1200 / w)), Image.LANCZOS)
        frame.save(buf, format="JPEG", quality=85)
        return base64.standard_b64encode(buf.getvalue()).decode()

    def analyse_scene(
        self,
        frame: Image.Image,
        scene_type: str,
        screen_title: str,
        detected_elements: list | None = None,
        force: bool = False,
    ) -> SceneInventory | None:
        """
        Analyse a scene and return a structured SceneInventory.

        Args:
            frame:             Screenshot (used as visual context thumbnail).
            scene_type:        L0 classifier result.
            screen_title:      OCR title from top-left.
            detected_elements: OmniParser DetectedElement list. When provided,
                               Claude reasons over this structured text list
                               rather than pixel-hunting on the raw image.
                               This is faster, cheaper, and more accurate.
            force:             Re-analyse even if cached.

        "Learn once, reuse forever":
          - First call → API call → cache written to memory/knowledge/scenes/
          - Subsequent calls → cache hit, no API call, instant return
        """
        from datetime import datetime, timezone

        # Cache hit
        if not force:
            cached = load_inventory(scene_type, screen_title)
            if cached is not None:
                cached.visit_count += 1
                cached.last_analysed_at = datetime.now(timezone.utc).isoformat()
                save_inventory(cached)
                logger.debug(
                    f"Scene inventory cache hit: {scene_type}/{screen_title} "
                    f"(visit #{cached.visit_count})"
                )
                return cached

        client = self._get_client()
        if client is None:
            return None

        logger.info(
            f"Analysing new scene via Claude API: "
            f"{scene_type!r} / {screen_title!r}  "
            f"({len(detected_elements or [])} OmniParser elements)"
        )

        # Format the OmniParser element list as a readable text table for Claude
        if detected_elements:
            lines = ["idx | type   | tap_x | tap_y | label"]
            lines.append("----|--------|-------|-------|------")
            for i, el in enumerate(detected_elements):
                lines.append(
                    f"{i:3d} | {el.element_type:6s} | {el.cx:5d} | {el.cy:5d} | {el.label}"
                )
            element_list_str = "\n".join(lines)
        else:
            element_list_str = "(OmniParser not available — infer from image only)"

        prompt = _ANALYSE_PROMPT.format(
            scene_type=scene_type,
            screen_title=screen_title or "(unknown)",
            element_list=element_list_str,
        )

        try:
            image_data = self._encode_image(frame)
            response = client.messages.create(
                model=self.model,
                max_tokens=8192,   # bumped 4096→8192 (2026-05-15): dense screens
                                  # like Bank Insurance, Item Shop Tool / Black
                                  # Market / Sell were truncating mid-response
                                  # and producing unparseable JSON, wasting the
                                  # API call and forcing a re-fire next run.
                system=_SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": image_data,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            raw_text = response.content[0].text
        except Exception as exc:
            logger.error(f"Claude Vision API error: {exc}")
            return None

        parsed = _extract_json(raw_text)
        if not parsed:
            logger.warning(
                f"Claude Vision: could not parse JSON:\n{raw_text[:500]}"
            )
            return None

        now = datetime.now(timezone.utc).isoformat()

        # Build UIElement list — tap coordinates come from OmniParser (ground truth),
        # falling back to Claude's suggestion only when OmniParser wasn't available.
        omni_by_label: dict[str, Any] = {}
        for el in (detected_elements or []):
            omni_by_label[el.label.lower()] = el

        elements = []
        for e in parsed.get("elements", []):
            label = e.get("label", "")
            # Prefer OmniParser tap coords; fall back to Claude's suggestion
            omni = omni_by_label.get(label.lower())
            tap_x = omni.cx if omni else e.get("tap_x")
            tap_y = omni.cy if omni else e.get("tap_y")
            elements.append(UIElement(
                label=label,
                element_type=e.get("element_type", "text"),
                purpose=e.get("purpose", ""),
                tap_x=tap_x,
                tap_y=tap_y,
            ))

        inv = SceneInventory(
            scene_type=scene_type,
            screen_title=screen_title,
            layout_description=parsed.get("layout_description", ""),
            navigation_hints=parsed.get("navigation_hints", ""),
            elements=elements,
            visit_count=1,
            first_analysed_at=now,
            last_analysed_at=now,
            raw_api_response=parsed,
        )

        save_inventory(inv)

        # Phase 6.5 L3: learning hook — when the registry's verdict on
        # the same OmniParser elements differs from the input scene_type
        # (which came from L0 / chrome / OCR), persist a candidate
        # fingerprint to memory/knowledge/learned_fingerprints/.  This
        # builds a review queue: each candidate is a screen the bot
        # didn't already have a registry fingerprint for, with the full
        # OmniParser element list ready for the user to promote into
        # vision/state_fingerprints_data.py.
        try:
            _maybe_save_learned_candidate(
                inv=inv,
                detected_elements=detected_elements or [],
                frame_w=frame.width,
                frame_h=frame.height,
            )
        except Exception as exc:
            logger.debug(f"Learned-fingerprint hook skipped: {exc}")

        interactive = inv.interactive()
        logger.info(
            f"Scene analysed: {scene_type}/{screen_title}\n"
            f"  layout   : {inv.layout_description}\n"
            f"  interact : {[(e.label, e.tap_x, e.tap_y) for e in interactive]}\n"
            f"  navigate : {inv.navigation_hints}"
        )

        return inv


def _extract_json(text: str) -> dict | None:
    """Extract a JSON object from a text response that may contain prose."""
    import re
    # Try direct parse first
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    # Strip markdown code fences (```json ... ``` or ``` ... ```)
    fence_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass
    # Find first {...} block (greedy — gets the outermost object)
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return None


# ── singleton ──────────────────────────────────────────────────────────────────

_instance: ClaudeVision | None = None


def get_claude_vision() -> ClaudeVision:
    global _instance
    if _instance is None:
        _instance = ClaudeVision()
    return _instance
