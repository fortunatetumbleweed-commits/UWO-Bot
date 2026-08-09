# A2 — Structured `PerceivedState`: concrete implementation plan

Turns the design in `docs/refactor_plan_perceive_flow_fsm.md` (the *what*) into
an incremental, test-guarded *how*. Prereqs **A0 + A1 are done** (816fbb9,
b5ba036) — classify is a pure read and the benchmark is deterministic, so it can
gate every phase. Written 2026-08-05.

## STATUS (2026-08-06)
- **Phase 0 ✅** scaffold — `brain/perceived_state.py`, additive `perceived`
  field, round-trip invariant (7f97e88).
- **Phase 2 (partial) ✅** the family CNN is now the base authority: a chromed
  frame can never be an overworld (base gate) + structural fallback on
  back-arrow/left-menu (24473d0). Not yet the full fold to `overworld|world_map|
  loading|panel` as the classifier's native output.
- **Phase 3 ✅** panel context reader — `vision/panel_context.py` identifies the
  panel from its LEFT-MENU items vs the tiered vocab (6054b79). **village recall
  2/11 → 11/11, port held 31/31, overall 84.8% → 98.5%.**
- **Remaining:** Phase 1 (unified overlay axis — infra, not a metric mover),
  finish Phase 2 fold, Phase 3 for buildings (market/inn context → drives D11),
  Phase 4 collapse the 22-return cascade + fold A6, Phase 5 flip authority.
  See `memory/project_perception_panel_identity_from_left_menu.md`.

## The problem being replaced
`brain/perceive.py::_classify_nav_state` is a **563-line, 22-`return` priority
cascade** returning a flat `{location, port, detail}`. Order-dependent early
returns conflate *structure* (sea vs panel) with *identity* (which building) and
with *overlay* (a dialog on top). Symptoms: village-with-overlay frames can't be
represented (benchmark **village 2/11**); the same village/port logic is
duplicated in two branches (A6); "market" is lumped as "building" (A4).

## Target shape
One arbitrated truth per tick, each field read by its **best** signal:

```python
@dataclass
class PerceivedState:
    base:      str            # overworld | world_map | loading | panel      (coarse STRUCTURE)
    overlay:   str            # none | dialog | confirm | negotiation | result | main_menu | announcement   (ORTHOGONAL)
    mode:      Optional[str]  # sea | port      — only when base == overworld
    context:   Optional[str]  # Market | Inn | Village | Bank … — only when base == panel  (top-left title + nav memory)
    menu_item: Optional[str]  # Buy | Sell | Hire … — active left-menu item  (tiered-label FUNCTION field)
    identity:  Optional[str]  # universal top-left slot read (icon+name / title)
    conf:      dict[str,float]# per-field confidence on one 0..1 scale
```

Base scheme is the **folded** one (A2b): `sea`+`port_overworld` → `overworld`
(+`mode`); all chromed screens → `panel` (+`context`); `main_menu` becomes an
**overlay**, not a base. The tiered labeler vocab
(`data/knowledge/screen_tags.json`, built 2026-08-04) is the ground truth for
`context` + `menu_item`.

## The migration lever (why this is additive, not a rewrite)
- `_classify_nav_state` has **exactly one caller**: `_detect_navigation_state`
  (`perceive.py:2975`), which feeds `perceive()`.
- `PerceiveResult` already carries a **compat layer**: `.state`, `.port`,
  `.detail`, `to_location_dict()`, `at_port`, plus `sub_menu`/`scene_type`.
- So: build `PerceivedState` alongside, add a **`legacy_state()`** mapping back
  to the old `state` vocabulary, and derive `PerceiveResult.state` from it. The
  **40+ consumers** (`brain/plan*.py`, `goals/*`, `actions/*`, …) keep reading
  `.state`/`.at_port` unchanged while new code reads `.perceived.base` etc.

`legacy_state()` mapping (the regression contract):

| PerceivedState | legacy `state` |
|---|---|
| base=overworld, mode=sea | `sea` |
| base=overworld, mode=port | `port_overworld` |
| base=panel, context=Village | `village` |
| base=panel, context=Market | `building` (+ `sub_menu` when `menu_item` set) |
| base=panel, other context | `building` / `sub_menu` |
| base=world_map | `world_map` |
| base=loading | `loading` / `pending` |
| overlay=main_menu | `main_menu` (overlay wins for legacy callers) |
| overlay=dialog/announcement/result | keep base's legacy state; interruptors already surface these |

## Phases (each ends green on the benchmark + scoped tests)

**Phase 0 — scaffold + measure (no behavior change).**
- Add `PerceivedState` + enums + `legacy_state()` in a new `brain/perceived_state.py`.
- Add optional `PerceiveResult.perceived: Optional[PerceivedState] = None`.
- Extend `tools/perception_benchmark.py` to score `base` / `mode` / `context` /
  `menu_item` against labels when `.perceived` is present (falls back to
  `location` today). Establishes the multi-axis baseline.
- Cascade still authoritative. Ship, confirm benchmark unchanged (village 2/11,
  port 31/31).

**Phase 1 — overlay axis first (this is A3; highest leverage for village recall).**
- One overlay detector: unify the keyword-interruptor path + structural
  `vision/region_detectors DialogModel` into a single `detect_overlay(frame) →
  Overlay`; structural verdict **first**, keyword as fallback (A3).
- Base classification **ignores** overlays — classify the screen *underneath*.
  So "village + menu" becomes `base=panel, context=Village, overlay=menu` instead
  of collapsing to one flat class. Populate `overlay`.
- Measure: the 8 village misses (village-with-overlay) should now score correct
  on the **base/context** axis. Target: village base-recall ≫ 2/11.

**Phase 2 — base as pure structure (A2b).**
- Wrap the family CNN; fold its 5-way output to `overworld | world_map |
  loading | panel` in code (sea+port_overworld→overworld; chromed→panel;
  transient→loading or overlay). **No retrain needed** — folding is post-hoc.
- `mode: sea|port` from the universal top-left slot (ship vs lighthouse icon)
  cross-checked with the CNN — the dual-signal arbitration from the 08-04 design.

**Phase 3 — context + menu-item reader (consumes the tiered vocab).**
- For `base=panel`: `context` = top-left title via OCR + **nav-state memory**
  (A2c: what building the bot entered) as prior; `menu_item` = active left-menu
  entry via the left-menu region detector + OCR, validated against
  `screen_tags.json[sub_menu].menu_by_context[building]`.
- This is where trading perception improves (Market→Buy/Sell feeds D11).

**Phase 4 — collapse the cascade + fold A6.**
- Replace the 22-return cascade with: compute base, overlay, mode, context
  **independently** → arbitrate on `conf` → `PerceivedState` → `legacy_state()`.
- Fold the two village/port sites (L2429, L2703) into one
  `_resolve_village_or_port(port, family_conf)` (completes A6; A1 guard lives in
  one place). Delete dead branches.
- **Gate:** `legacy_state()` over the labeled corpus must **match or beat** the
  current baseline on every class before this merges.

**Phase 5 — flip authority.**
- Make `PerceivedState` authoritative; keep `legacy_state()` as a permanent shim.
- Migrate consumers to structured fields opportunistically (not a big-bang).

## Regression strategy
The deterministic benchmark (A0) is the gate. At each phase:
`legacy_state()` on `data/labels.jsonl` ≥ baseline (village 2/11, port 31/31),
and the new axes (base/overlay/context/menu_item) scored against the tiered
labels only go up. Scoped tests: `tests/test_village_recognition.py`,
`test_classify_sea_hud_override.py`, `test_phase5a.py`, `test_phase5b.py`,
`test_family_classifier_wirein.py`.

## Effort / sequencing
Phase 0 (~small) → **Phase 1 overlay** (medium; do first for the village-recall
win) → Phase 2 (small, folding logic) → Phase 3 (medium; unlocks trading) →
Phase 4 (medium; the actual cascade deletion) → Phase 5 (ongoing). Phases 0–1
are independently shippable and de-risk the rest.
