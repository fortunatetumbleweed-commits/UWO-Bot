# brain/plan.py
#
# Plan, Goal, PlanStep, Cue — the canonical data types for the planner
# architecture described in docs/planner_architecture.md.
#
# Layer 1 of the migration: types only, no behaviour change.  Existing
# flows.json / learned_recoveries.json entries can be re-read as Plan
# instances via plan_from_flow_dict() / plan_from_learned_recovery_dict();
# the runtime continues to use FSMFlow for now.  Layer 3 (planner facade)
# switches consumers from FSMFlow to Plan.
#
# Why this lives in its own module:
#   - The existing brain/planner.py holds Edge/EdgeMetadata, which represent
#     single-state-to-state transitions in the FSM graph.  A Plan is a
#     SEQUENCE of actions that may bridge multiple states; it's a higher-
#     level concept that contains PlanSteps.  The two coexist.
#   - Goal and Cue are first-class types used by both the planner and the
#     verification pipeline (light/heavy checks).  They don't fit cleanly
#     in either fsm_registry or planner.

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger


# ── Confidence + provenance vocabulary ───────────────────────────────────────
# Mirrors the existing FSMFlow vocab so plans converted from flows preserve
# their state.

CONFIDENCE_HIGH       = "high"
CONFIDENCE_MEDIUM     = "medium"
CONFIDENCE_LOW        = "low"
CONFIDENCE_UNVERIFIED = "unverified"

PROVENANCE_HAND_AUTHORED   = "hand_authored"
PROVENANCE_CLAUDE_GENERATED = "claude_generated"
PROVENANCE_CLAUDE_GUIDED   = "claude_guided"   # legacy from flows.json
PROVENANCE_CLAUDE_REPLAN   = "claude_replan"
PROVENANCE_HUMAN_TAUGHT    = "human_taught"
PROVENANCE_DISTILLED       = "distilled"
PROVENANCE_INHERITED       = "inherited"

# Architecture-doc threshold: N consecutive failures with zero successes
# demotes a plan to confidence='low' so it stops being selected.
#
# UPDATED rule (Fix C, post-2026-05-04): demotion now also requires N
# consecutive failures on the SAME step.  A failure on a tail step (e.g.
# post-recruit cleanup) right after a transaction step has succeeded is
# transient noise — punishing the whole plan for it kills working plans.
# Additionally, if any commit_via_positive_taps step in the plan has
# succeeded at least once, the plan has demonstrated capability and is
# exempt from confidence demotion (its job IS getting done; tail churn
# is a separate problem).
FAILURE_DEMOTION_THRESHOLD = 3

# Action `kind` values that count as positive transactions for the
# capability-demonstrated rule.  Mirrors the per-step categoriser in
# brain/flow_completeness.py (`CATEGORY_TRANSACTION` semantics) but at
# the plan-step level where the action kind is the authoritative signal.
_TRANSACTION_ACTION_KINDS = frozenset({
    "commit_via_positive_taps",
})


# ── Verification expectations attached to plan steps ──────────────────────────


@dataclass
class ProgressExpectation:
    """
    Light-verification annotation for a plan step — what does "the step
    took effect" look like?  Used by light_check after every action.

    `kind` selects how the expectation is evaluated:
      'state_change'        — the navigation state must change to a named state
      'detail_match'        — perceive_result.detail must contain a substring
      'screen_change'       — frame_signature must differ from before
      'frame_change_required' — same as screen_change but stricter (cheap pre-check)
      'free_text'           — only `hint` is provided; Claude evaluates

    `hint` is a human-readable description used by the Claude prompts when
    light/heavy verification escalates beyond the cheap local check.
    """
    kind: str = "screen_change"
    hint: str = ""
    expected_state:           Optional[str] = None
    expected_detail_substring: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None and v != ""}

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ProgressExpectation":
        if not data:
            return cls()
        return cls(
            kind                       = data.get("kind", "screen_change"),
            hint                       = data.get("hint", ""),
            expected_state             = data.get("expected_state"),
            expected_detail_substring  = data.get("expected_detail_substring"),
        )


@dataclass
class GoalCheckpoint:
    """
    Heavy-verification annotation for a plan step — marks the step as a
    checkpoint where the goal predicate is evaluated.

    Only checkpoint steps trigger heavy_check; all other steps only get
    light_check.  Most plans have one or two checkpoints (typically the
    last step, or a step where the work that achieves the goal is
    expected to have just completed).
    """
    goal_id:   str
    rationale: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> Optional["GoalCheckpoint"]:
        if not data:
            return None
        return cls(
            goal_id   = data["goal_id"],
            rationale = data.get("rationale", ""),
        )


# ── Plan and PlanStep ─────────────────────────────────────────────────────────


@dataclass
class PlanStep:
    """
    One step in a Plan.  Carries:
      - the action to execute (tap / press_back / navigate / etc.)
      - light-verification expectation
      - optional heavy-verification (goal checkpoint)
      - per-step success/failure history (orthogonal to plan-level history;
        a step that's reused across plans accumulates its own stats)
    """
    step_id:           str
    action:            dict   # untyped action payload (tap, navigate_to, etc.)
    expected_progress: ProgressExpectation = field(default_factory=ProgressExpectation)
    expected_goal:     Optional[GoalCheckpoint] = None

    travel_count:       int   = 0
    success_count:      int   = 0
    failure_count:      int   = 0
    avg_duration_secs:  float = 0.0
    last_traveled_at:   Optional[str] = None

    notes: str = ""

    @property
    def is_checkpoint(self) -> bool:
        return self.expected_goal is not None

    def record_success(self, duration_secs: float = 0.0) -> None:
        self.travel_count   += 1
        self.success_count  += 1
        n = self.travel_count
        if n == 1:
            self.avg_duration_secs = duration_secs
        else:
            self.avg_duration_secs = ((n - 1) * self.avg_duration_secs + duration_secs) / n
        self.last_traveled_at = datetime.now(timezone.utc).isoformat()

    def record_failure(self) -> None:
        self.travel_count    += 1
        self.failure_count   += 1
        self.last_traveled_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "step_id":           self.step_id,
            "action":            self.action,
            "expected_progress": self.expected_progress.to_dict(),
        }
        if self.expected_goal is not None:
            d["expected_goal"] = self.expected_goal.to_dict()
        # Counters / metadata
        for k in ("travel_count", "success_count", "failure_count",
                  "avg_duration_secs", "last_traveled_at", "notes"):
            v = getattr(self, k)
            if v not in (0, 0.0, None, ""):
                d[k] = v
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "PlanStep":
        return cls(
            step_id           = data["step_id"],
            action            = dict(data.get("action", {})),
            expected_progress = ProgressExpectation.from_dict(data.get("expected_progress")),
            expected_goal     = GoalCheckpoint.from_dict(data.get("expected_goal")),
            travel_count      = int(data.get("travel_count", 0)),
            success_count     = int(data.get("success_count", 0)),
            failure_count     = int(data.get("failure_count", 0)),
            avg_duration_secs = float(data.get("avg_duration_secs", 0.0)),
            last_traveled_at  = data.get("last_traveled_at"),
            notes             = data.get("notes", ""),
        )


@dataclass
class Plan:
    """
    A Plan is a sequence of steps the planner believes will move the bot
    from its current state to the goal.  Plans differ in quality
    (confidence, success_rate), provenance (hand-authored / claude /
    human-taught), and maturity (travel_count) — not in kind.

    The architecture-doc unification: recipes / edges / flows / resolutions
    are all Plans with different provenance.
    """
    plan_id:    str
    goal_id:    str
    steps:      list[PlanStep] = field(default_factory=list)

    provenance: str = PROVENANCE_HAND_AUTHORED
    confidence: str = CONFIDENCE_UNVERIFIED

    description: str = ""
    notes:       str = ""

    travel_count:      int   = 0
    success_count:     int   = 0
    failure_count:     int   = 0
    avg_duration_secs: float = 0.0
    first_traveled_at: Optional[str] = None
    last_traveled_at:  Optional[str] = None

    supersedes: list[str] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        if self.travel_count == 0:
            return 0.0
        return self.success_count / self.travel_count

    @property
    def is_checkpoint_at_end(self) -> bool:
        """True when the final step is a goal checkpoint — most plans should have this."""
        return bool(self.steps) and self.steps[-1].is_checkpoint

    def should_skip(self) -> bool:
        """Per architecture doc: demoted plans should not be selected.

        Fix C extension (post-2026-05-12 livefix): a plan whose
        transaction-class step has succeeded at least once is exempt
        from being skipped, *even when its on-disk confidence is 'low'*.
        Fix C originally prevented NEW demotions for these plans;
        applying the same exemption at lookup time also rehabilitates
        plans demoted before Fix C shipped.  Without this, a stale
        on-disk demotion strands a working plan and the planner returns
        'no_plan_available' — which is what the live log
        2026-05-12 13:46:57 showed for inn_recruit_route.
        """
        if self.confidence not in (CONFIDENCE_LOW,):
            return False
        if self.failure_count < FAILURE_DEMOTION_THRESHOLD:
            return False
        # Capability-demonstrated exemption: if any transaction step
        # has ever succeeded, the plan demonstrably works — don't skip.
        for s in self.steps:
            if s.action.get("kind") in _TRANSACTION_ACTION_KINDS \
                    and s.success_count > 0:
                return False
        return True

    def record_success(self, duration_secs: float) -> None:
        self.travel_count  += 1
        self.success_count += 1
        n = self.travel_count
        if n == 1:
            self.avg_duration_secs = duration_secs
        else:
            self.avg_duration_secs = ((n - 1) * self.avg_duration_secs + duration_secs) / n
        now = datetime.now(timezone.utc).isoformat()
        if self.first_traveled_at is None:
            self.first_traveled_at = now
        self.last_traveled_at = now
        # Promote unverified → high after two confirmed successes (Rule 2:
        # one trial isn't proof; two is).
        if self.confidence == CONFIDENCE_UNVERIFIED and self.success_count >= 2:
            self.confidence = CONFIDENCE_HIGH

    def record_failure(self, failed_step_id: Optional[str] = None) -> None:
        """Record a failed plan traversal.

        Demotion (Fix C, post-2026-05-04 livefix) requires ALL of:
          1. plan-level failure_count >= FAILURE_DEMOTION_THRESHOLD
          2. plan-level success_count == 0
          3. when failed_step_id is supplied, the SAME step has failed
             on every recent attempt — not a single transient tail-step
             miss after a successful transaction
          4. no transaction-class step in the plan has succeeded yet
             (a plan whose commit step has worked is exempt — it has
             demonstrated capability, tail churn is a separate issue)

        The previous rule demoted on plan-level failures alone, which
        killed the inn_recruit_route plan after a single transient
        post-recruit step miss even though step_3b_commit_recruit had
        successfully recruited crew on every prior attempt.
        """
        self.travel_count   += 1
        self.failure_count  += 1
        self.last_traveled_at = datetime.now(timezone.utc).isoformat()

        # Track consecutive same-step failures.  Stored on the instance,
        # not persisted — this is in-memory state for the current run.
        if failed_step_id is not None:
            prev = getattr(self, "_consec_failure_step_id", None)
            if prev == failed_step_id:
                self._consec_failure_count = getattr(self, "_consec_failure_count", 0) + 1
            else:
                self._consec_failure_step_id = failed_step_id
                self._consec_failure_count   = 1
        same_step_streak = getattr(self, "_consec_failure_count", 0)

        if self.failure_count < FAILURE_DEMOTION_THRESHOLD:
            return
        if self.success_count > 0:
            return
        # Capability-demonstrated exemption: any transaction-class step
        # that has committed at least once means the plan works.
        for s in self.steps:
            if s.action.get("kind") in _TRANSACTION_ACTION_KINDS and s.success_count > 0:
                return
        # Same-step streak rule: when callers supply failed_step_id,
        # require the streak to also reach the threshold.  Callers that
        # don't supply it (legacy) get the old behaviour.
        if failed_step_id is not None and same_step_streak < FAILURE_DEMOTION_THRESHOLD:
            return
        self.confidence = CONFIDENCE_LOW

    def record_step_success(self, step_id: str) -> None:
        """Reset the consecutive-failure tracker.  Called when a plan
        step lands successfully — even if the overall plan later fails,
        intervening successes prove the failed step is the structural
        problem, not the plan as a whole."""
        self._consec_failure_step_id = None
        self._consec_failure_count   = 0

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "plan_id":    self.plan_id,
            "goal_id":    self.goal_id,
            "provenance": self.provenance,
            "confidence": self.confidence,
            "steps":      [s.to_dict() for s in self.steps],
        }
        for k in ("description", "notes", "travel_count", "success_count",
                  "failure_count", "avg_duration_secs", "first_traveled_at",
                  "last_traveled_at", "supersedes"):
            v = getattr(self, k)
            if v not in (0, 0.0, None, "", []):
                d[k] = v
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Plan":
        return cls(
            plan_id           = data["plan_id"],
            goal_id           = data["goal_id"],
            steps             = [PlanStep.from_dict(s) for s in data.get("steps", [])],
            provenance        = data.get("provenance", PROVENANCE_HAND_AUTHORED),
            confidence        = data.get("confidence", CONFIDENCE_UNVERIFIED),
            description       = data.get("description", ""),
            notes             = data.get("notes", ""),
            travel_count      = int(data.get("travel_count", 0)),
            success_count     = int(data.get("success_count", 0)),
            failure_count     = int(data.get("failure_count", 0)),
            avg_duration_secs = float(data.get("avg_duration_secs", 0.0)),
            first_traveled_at = data.get("first_traveled_at"),
            last_traveled_at  = data.get("last_traveled_at"),
            supersedes        = list(data.get("supersedes", [])),
        )


# ── Goal ──────────────────────────────────────────────────────────────────────


@dataclass
class Goal:
    """
    A Goal is what heavy verification evaluates against.  It is NOT the
    action sequence — that's the Plan.  A single goal can have many plans
    (different providers, different routes, different histories).

    `cheap_predicate` is runtime-only (not serialised).  It enables a
    fast local check (e.g. "fleet.crew_count_after > before") that lets
    heavy verification short-circuit without a Claude call when its
    evaluation is decisive.  Free-text `predicate_text` is what Claude
    sees when the cheap path can't decide.
    """
    goal_id:        str
    description:    str
    predicate_text: str
    cue_catalog_path: Optional[str] = None
    cheap_predicate: Optional[Callable[[Any], bool]] = None  # not serialised

    def to_dict(self) -> dict:
        return {
            "goal_id":         self.goal_id,
            "description":     self.description,
            "predicate_text":  self.predicate_text,
            "cue_catalog_path": self.cue_catalog_path,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Goal":
        return cls(
            goal_id           = data["goal_id"],
            description       = data.get("description", ""),
            predicate_text    = data.get("predicate_text", ""),
            cue_catalog_path  = data.get("cue_catalog_path"),
        )


# ── Cue catalog (per-goal verification knowledge) ─────────────────────────────


@dataclass
class CueProvenance:
    """
    Where a cue was acquired — supports auditing of accumulated verification
    knowledge.  See architecture doc § "Cue catalog" for sources.
    """
    source:          str = PROVENANCE_HAND_AUTHORED   # hand_authored | claude_observed | human_taught | distilled | inherited
    first_seen:      Optional[str] = None
    first_seen_in:   Optional[str] = None   # context: recipe + step, teach session, replan call, etc.
    noted_during:    Optional[str] = None   # which verification phase (light/heavy/replan)
    noted_by:        Optional[str] = None   # claude_vision | qwen | moondream | human_operator | hand_authored

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "CueProvenance":
        if not data:
            return cls()
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class CueStats:
    observed:  int = 0
    confirmed: int = 0
    refuted:   int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "CueStats":
        if not data:
            return cls()
        return cls(**{k: int(v) for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Cue:
    """
    A single verification signal for a goal.  Examples:
      "fleet crew count strictly greater than before-frame"
      "no red dot on the Recruit Crew menu item"
      "Depart Now button is gold/yellow rather than gray"

    Cues accumulate through Claude observations during heavy checks and
    through human teaching.  They are filtered for usefulness over time:
    high-confirmed cues become canonical, refuted ones are pruned in
    manual curation passes.
    """
    cue:        str
    polarity:   str = "confirms"     # 'confirms' or 'refutes' the goal
    confidence: str = CONFIDENCE_UNVERIFIED
    stats:      CueStats = field(default_factory=CueStats)
    provenance: CueProvenance = field(default_factory=CueProvenance)

    def to_dict(self) -> dict:
        return {
            "cue":        self.cue,
            "polarity":   self.polarity,
            "confidence": self.confidence,
            "stats":      self.stats.to_dict(),
            "provenance": self.provenance.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Cue":
        return cls(
            cue        = data["cue"],
            polarity   = data.get("polarity", "confirms"),
            confidence = data.get("confidence", CONFIDENCE_UNVERIFIED),
            stats      = CueStats.from_dict(data.get("stats")),
            provenance = CueProvenance.from_dict(data.get("provenance")),
        )


@dataclass
class CueCatalog:
    """
    Per-goal collection of verification cues with provenance tracking.

    Lives at memory/knowledge/verification/<goal_id>.json.  Grows lazily
    via heavy_check observations; curated manually when noisy.
    """
    goal:           str
    description:    str = ""
    predicate_text: str = ""
    cues:           list[Cue] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "goal":           self.goal,
            "description":    self.description,
            "predicate_text": self.predicate_text,
            "cues":           [c.to_dict() for c in self.cues],
            "open_questions": list(self.open_questions),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CueCatalog":
        return cls(
            goal           = data["goal"],
            description    = data.get("description", ""),
            predicate_text = data.get("predicate_text", ""),
            cues           = [Cue.from_dict(c) for c in data.get("cues", [])],
            open_questions = list(data.get("open_questions", [])),
        )


# ── Conversion helpers from existing on-disk formats ──────────────────────────


def plan_from_flow_dict(flow: dict) -> Plan:
    """
    Re-read an existing flows.json entry as a Plan instance.  Used by the
    Layer 3 planner facade to load all today's flows uniformly under the
    new abstraction without changing the on-disk schema.

    Mapping:
      flow.id                      → plan.plan_id
      flow.terminal_state           → plan.goal_id (proxied; real goals
                                      come later)
      flow.steps                    → plan.steps (each becomes a PlanStep
                                      with default ProgressExpectation
                                      derived from the step's detection
                                      keywords if present)
      flow.parent_state            → preserved in plan.notes
      provenance / confidence /     → carried through if present (added
        history fields                by Phase 4 commit 1956c52)

    No expected_progress / expected_goal annotations are inferred; these
    must be added when the plan is first amended via replan or hand-edit.
    """
    plan_id = flow["id"]
    goal_id = flow.get("terminal_state", "unknown_goal")

    steps: list[PlanStep] = []
    for raw_step in flow.get("steps", []):
        step_id = raw_step.get("id", f"step_{len(steps) + 1}")
        action = {
            "kind":   raw_step.get("recovery_action", "unknown"),
            "labels": raw_step.get("recovery_button_labels", []),
            "region": raw_step.get("recovery_region"),
            "wait_seconds": raw_step.get("wait_seconds"),
        }
        # Drop empty entries from the action dict for cleanliness.
        action = {k: v for k, v in action.items() if v not in (None, [], {})}
        progress_hint = raw_step.get("description") or raw_step.get("detection") or ""
        steps.append(PlanStep(
            step_id           = step_id,
            action            = action,
            expected_progress = ProgressExpectation(
                kind = "free_text",
                hint = progress_hint,
            ),
        ))

    plan = Plan(
        plan_id     = plan_id,
        goal_id     = goal_id,
        steps       = steps,
        provenance  = flow.get("provenance", PROVENANCE_HAND_AUTHORED),
        confidence  = flow.get("confidence", CONFIDENCE_HIGH),
        description = flow.get("description", ""),
        notes       = (
            f"parent_state={flow.get('parent_state','?')}; "
            f"trigger_detection={flow.get('trigger_detection','?')}"
        ),
        travel_count      = int(flow.get("travel_count", 0)),
        success_count     = int(flow.get("success_count", 0)),
        failure_count     = int(flow.get("failure_count", 0)),
        avg_duration_secs = float(flow.get("avg_duration_secs", 0.0)),
        first_traveled_at = flow.get("first_traveled_at"),
        last_traveled_at  = flow.get("last_traveled_at"),
        supersedes        = list(flow.get("supersedes", [])),
    )
    return plan


def plan_from_learned_recovery_dict(rec: dict, goal_id: str = "unknown_goal") -> Plan:
    """
    Re-read a learned_recoveries.json entry as a single-step Plan.

    learned_recovery entries are typically one tap with keyword-triggered
    detection; they map to a Plan with a single PlanStep.  goal_id is
    supplied by the caller (the recovery file doesn't have an explicit
    target state; the planner facade decides which goal a recovery
    belongs under).
    """
    actions = rec.get("actions", [])
    steps: list[PlanStep] = []
    for i, raw in enumerate(actions, 1):
        steps.append(PlanStep(
            step_id           = f"step_{i}",
            action            = dict(raw),
            expected_progress = ProgressExpectation(
                kind = "screen_change",
                hint = rec.get("description", ""),
            ),
        ))

    return Plan(
        plan_id     = rec["id"],
        goal_id     = goal_id,
        steps       = steps,
        provenance  = rec.get("provenance", PROVENANCE_HUMAN_TAUGHT),
        confidence  = rec.get("confidence", CONFIDENCE_UNVERIFIED),
        description = rec.get("description", ""),
        notes       = f"learned_recovery; detection_keywords={rec.get('detection_keywords', [])}",
        travel_count  = int(rec.get("travel_count", 0)),
        success_count = int(rec.get("success_count", 0)),
        failure_count = int(rec.get("failure_count", 0)),
    )


# ── KB load / save helpers ────────────────────────────────────────────────────


_PLANS_DIR     = Path("memory/knowledge/plans")
_VERIFY_DIR    = Path("memory/knowledge/verification")


def plan_path(goal_id: str, plan_id: str) -> Path:
    """Canonical on-disk location for a Plan."""
    return _PLANS_DIR / goal_id / f"{plan_id}.json"


def cue_catalog_path(goal_id: str) -> Path:
    """Canonical on-disk location for a CueCatalog."""
    return _VERIFY_DIR / f"{goal_id}.json"


def save_plan(plan: Plan) -> None:
    """Persist a Plan to its canonical path."""
    p = plan_path(plan.goal_id, plan.plan_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(plan.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    logger.debug(f"[plan] saved {p}")


def load_plan(goal_id: str, plan_id: str) -> Optional[Plan]:
    """Load a Plan by (goal_id, plan_id).  Returns None if not present."""
    p = plan_path(goal_id, plan_id)
    if not p.exists():
        return None
    return Plan.from_dict(json.loads(p.read_text(encoding="utf-8")))


def list_plans_for_goal(goal_id: str) -> list[Plan]:
    """All persisted plans targeting the named goal."""
    d = _PLANS_DIR / goal_id
    if not d.exists():
        return []
    plans: list[Plan] = []
    for f in sorted(d.glob("*.json")):
        try:
            plans.append(Plan.from_dict(json.loads(f.read_text(encoding="utf-8"))))
        except Exception as e:
            logger.warning(f"[plan] could not read {f}: {e}")
    return plans


def save_cue_catalog(catalog: CueCatalog) -> None:
    p = cue_catalog_path(catalog.goal)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(catalog.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    logger.debug(f"[plan] saved cue catalog {p}")


def load_cue_catalog(goal_id: str) -> Optional[CueCatalog]:
    p = cue_catalog_path(goal_id)
    if not p.exists():
        return None
    return CueCatalog.from_dict(json.loads(p.read_text(encoding="utf-8")))
