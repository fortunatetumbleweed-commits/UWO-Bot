"""BotObservation — the single per-tick perception record.

Bridge 1 from docs/memory_and_agent_architecture.md: every consumer of
perception (flows, planner, recovery, learning hooks) reads the same
immutable observation per tick instead of re-querying pixels.

Carry-forward
-------------
When the current tick can't fully identify the scene (e.g. an overlay
obscures the title region), the observation borrows `last_known_*`
fields from the previous tick.  Consumers read `scene_source` to know
whether the answer came from detection or from memory.

Decay
-----
`last_known_*` fields carry an age in ticks.  We do NOT auto-expire —
that's a policy decision belonging to consumers (the planner may
tolerate a 100-tick-old settlement; recovery probably won't).
Consumers read `*_age_ticks` and decide.

Singleton
---------
There is exactly one bot instance per process, so the observation
lives as a module-level singleton.  `update()` produces the next
observation from the current frame's perception; `current()` returns
the latest one.  If we ever need replay or testing across instances,
we add a context-local stack.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

from loguru import logger


SceneSource = Literal["detected", "remembered", "guessed", "unknown"]


# ── Disk persistence ──────────────────────────────────────────────
# last_known_settlement must survive process restart.  The in-memory
# singleton is reset every run, but the bot's *physical* location in
# the game persists.  Without disk persistence, a bot started at sea
# (e.g. after a previous run crashed mid-voyage) has no origin to
# dead-reckon from when planning a destination on the world map.
#
# Mirrors the world-map scale cache pattern in actions/world_map_nav.

_SETTLEMENT_PATH = Path("memory/knowledge/state/last_settlement.json")


@dataclass(frozen=True)
class ActionRecord:
    """What the bot just did, and (later) how it turned out.

    `expected_post` and `outcome` are placeholders for Bridge 2
    (action-outcome detection).  Populating them is out of scope for
    this slice; the fields exist so the schema doesn't have to change
    when Bridge 2 lands.
    """
    name:           str                    # e.g. "tap", "swipe", "press_back"
    target:         Optional[str] = None   # element label / coordinates string
    at_tick:        int = 0
    expected_post:  Optional[str] = None
    outcome:        Optional[Literal["success", "failure", "pending"]] = None


@dataclass(frozen=True)
class Overlay:
    """Detected overlay (dialog / popup / city-info / NPC dialogue).

    See docs/scene_model_design.md → 'Overlays are not independent
    scenes'.  This is a placeholder for the OmniParser-driven overlay
    cluster detector.  Until that lands, perceive() may construct an
    Overlay from existing interruptor detection so consumers can
    start reading the field.
    """
    kind:           str                              # e.g. "transaction_dialog", "city_info"
    bbox:           Optional[tuple[int, int, int, int]] = None
    content_tokens: tuple[str, ...] = ()
    is_modal:       bool = True                      # blocks input until resolved


@dataclass(frozen=True)
class BotObservation:
    # ── This tick ──────────────────────────────────────────────────
    tick:           int
    timestamp:      datetime
    frame_id:       Optional[str] = None     # cache key for replay
    scene:          Any = None               # vision.scene_model.SceneModel
    overlay:        Optional[Overlay] = None
    scene_source:   SceneSource = "unknown"
    # Sea-only navigation perception.  Populated when base_scene == "sea";
    # None on every other screen.  `nav` is the source-agnostic
    # NavigationView (Protocol in vision.navigation_view) used by
    # steering goals — currently backed by MinimapNavigationView.
    nav:            Any = None               # vision.navigation_view.NavigationView
    # Deprecated aliases — populated for one cycle to keep older
    # consumers compiling.  Read `nav` instead going forward; these
    # may stay None when only the new pipeline runs.
    minimap:        Any = None               # vision.minimap_reader.MinimapVerdict
    shoreline:      Any = None               # vision.shoreline_reader.ShorelineVerdict

    # ── Persistent across ticks ────────────────────────────────────
    # Carried forward when the current tick can't re-derive them.
    last_known_base_scene:  Optional[str] = None
    last_known_settlement:  Optional[str] = None    # port OR village; carried forward across sea/world_map ticks as the voyage's origin
    last_action:            Optional[ActionRecord] = None

    # Ticks since each last_known_* was directly observed (not
    # remembered).  0 = freshly observed this tick.
    last_known_base_scene_age_ticks: int = 0
    last_known_settlement_age_ticks: int = 0

    def fresh_scene(self) -> bool:
        return self.scene_source == "detected"

    @property
    def is_departed(self) -> bool:
        # True when the bot has left a settlement and is en route.
        # Consumers reading last_known_settlement should interpret it as
        # the voyage's *origin* when this is True, vs the *current
        # location* when False.
        return self.last_known_base_scene in ("sea", "sea_cinematic", "world_map")


# ── Module-level singleton ─────────────────────────────────────────

_current: Optional[BotObservation] = None

# Disk-loaded settlement, used to seed the singleton on the first
# update() of a new process when no in-memory value is available.
# A sentinel (_PERSISTED_UNLOADED) distinguishes "not loaded yet" from
# "loaded and found nothing", so we only hit the disk once.
_PERSISTED_UNLOADED: object = object()
_persisted_settlement: Any = _PERSISTED_UNLOADED


def _load_persisted_settlement() -> Optional[str]:
    """Read the last known settlement from disk, or None if missing/corrupt."""
    try:
        data = json.loads(_SETTLEMENT_PATH.read_text())
        name = data.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return None


def _save_persisted_settlement(name: str) -> None:
    """Write the latest settlement to disk for the next process."""
    if not name or not isinstance(name, str):
        return
    try:
        _SETTLEMENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "name": name,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        _SETTLEMENT_PATH.write_text(json.dumps(payload, indent=2))
    except OSError as exc:
        logger.warning(f"[observation] failed to persist settlement: {exc}")


def _ensure_persisted_loaded() -> Optional[str]:
    global _persisted_settlement
    if _persisted_settlement is _PERSISTED_UNLOADED:
        _persisted_settlement = _load_persisted_settlement()
        if _persisted_settlement:
            logger.info(
                f"[observation] loaded persisted settlement: {_persisted_settlement!r}"
            )
    return _persisted_settlement   # type: ignore[return-value]


def current() -> Optional[BotObservation]:
    """Return the latest observation, or None before the first tick."""
    return _current


def reset() -> None:
    """Clear the singleton AND the cached persisted settlement.

    Used by tests so that each test starts with a clean slate, and by
    test fixtures that patch _SETTLEMENT_PATH to point to a tmp file.
    """
    global _current, _persisted_settlement
    _current = None
    _persisted_settlement = _PERSISTED_UNLOADED


def update(
    *,
    tick:            Optional[int] = None,
    timestamp:       Optional[datetime] = None,
    frame_id:        Optional[str] = None,
    scene:           Any = None,
    overlay:             Optional[Overlay] = None,
    detected_settlement: Optional[str] = None,
    last_action:         Optional[ActionRecord] = None,
    nav:                 Any = None,
    minimap:             Any = None,
    shoreline:           Any = None,
) -> BotObservation:
    """Build the next observation, carrying forward fields when the
    current tick can't re-derive them, and store it as the singleton.

    A field is "fresh" when this tick produced a non-None value for
    it.  Otherwise the previous observation's value is reused and its
    age counter bumped.
    """
    global _current
    prev = _current
    ts = timestamp or datetime.now()
    if tick is None:
        tick = (prev.tick + 1) if prev is not None else 1

    # Auto-extract overlay from scene when not explicitly passed.
    # Real SceneModel instances carry their typed overlay
    # (DialogModel / BuildingNpcOverlay / keyword Overlay) in .overlay;
    # the duck-typed perceive adapter doesn't, in which case overlay
    # stays None until the caller passes one explicitly.
    if overlay is None and scene is not None:
        overlay = getattr(scene, "overlay", None)

    # ── Base scene ─────────────────────────────────────────────────
    detected_base = _extract_base_scene(scene)
    if detected_base is not None:
        base       = detected_base
        base_age   = 0
        src: SceneSource = "detected"
    elif prev is not None and prev.last_known_base_scene is not None:
        base       = prev.last_known_base_scene
        base_age   = prev.last_known_base_scene_age_ticks + 1
        src        = "remembered"
    else:
        base       = None
        base_age   = 0
        src        = "unknown"

    # ── Settlement (port or village) ───────────────────────────────
    # Carry-forward chain:
    #   1. fresh detection this tick wins
    #   2. otherwise reuse the previous observation's value
    #   3. otherwise seed from the disk-persisted value (only happens
    #      on the first update of a new process, when prev is None)
    if detected_settlement:
        settlement     = detected_settlement
        settlement_age = 0
        # Save to disk only on changes so we don't write every tick.
        if prev is None or prev.last_known_settlement != detected_settlement:
            _save_persisted_settlement(detected_settlement)
    elif prev is not None and prev.last_known_settlement is not None:
        settlement     = prev.last_known_settlement
        settlement_age = prev.last_known_settlement_age_ticks + 1
    else:
        persisted = _ensure_persisted_loaded()
        if persisted:
            settlement     = persisted
            settlement_age = 0    # unknown age across restart; treat as fresh-from-disk
        else:
            settlement     = None
            settlement_age = 0

    obs = BotObservation(
        tick=tick,
        timestamp=ts,
        frame_id=frame_id,
        scene=scene,
        overlay=overlay,
        scene_source=src,
        last_known_base_scene=base,
        last_known_settlement=settlement,
        last_action=last_action or (prev.last_action if prev else None),
        last_known_base_scene_age_ticks=base_age,
        last_known_settlement_age_ticks=settlement_age,
        nav=nav,
        minimap=minimap,
        shoreline=shoreline,
    )
    _current = obs
    return obs


def record_action(action: ActionRecord) -> None:
    """Attach an ActionRecord to the current observation.

    Action runners call this immediately after dispatching a tap /
    swipe, so the *next* tick's perceive() can correlate observed
    state changes with what the bot just did.  Bridge 2 (outcome
    detection) will populate `expected_post` and `outcome` here.
    """
    global _current
    if _current is None:
        return
    _current = replace(_current, last_action=action)


# ── Helpers ────────────────────────────────────────────────────────

def _extract_base_scene(scene: Any) -> Optional[str]:
    """Return the base-scene identifier from a SceneModel, or None.

    Accepts duck-typed objects with `.scene_kind` and `.confidence`
    so tests can pass plain dicts.  Low-confidence verdicts return
    None (caller falls back to last_known).
    """
    if scene is None:
        return None
    kind = getattr(scene, "scene_kind", None)
    conf = getattr(scene, "confidence", None)
    if kind is None and isinstance(scene, dict):
        kind = scene.get("scene_kind")
        conf = scene.get("confidence")
    if not kind or kind == "unknown":
        return None
    if conf == "low":
        return None
    return kind
