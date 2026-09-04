"""The screen as owned data: one observation, shared, with a lifecycle.

See `docs/perceive_repository.md` for the design and the evidence behind it.

WHY THIS EXISTS. 215 call sites capture their own frame, so two readers in the same tick can
be looking at different moments with nothing to reconcile them. That is not merely wasteful —
it is DISAGREEMENT. Live 2026-08-30 at Faro the dispatcher classified one capture while
`sell_goods` took another: the chromed title had flipped to 'Sell' while the goods grid was
still 'Purchase', each true of its own frame, and the bot loaded goods it had never bought
into a sell basket. No downstream check could catch that, because both readings were correct.

THE RULES, in the order they were decided:

  * CAPTURE is sealed here; the FRAME is shared read-only. Some consumers need pixels — the
    family classifier is a CNN over the raw image — and handing out copies would give each
    caller a fresh `id(frame)`, missing every cache in `vision/` (they key on identity, which
    is how `classify_family` shares inference cost across a tick). Read-only is NOT enforced
    in code; it is caught by review, because nothing here has any reason to modify a frame.

  * STALENESS IS GENERATION, NOT AGE. Data read off generation N is superseded the moment
    N+1 exists. Age decides nothing: at sea a twenty-minute-old observation is still current
    because the pacing sleep means nothing newer was taken, while in a market a two-second-old
    one is superseded as soon as a tap produces a new frame.

  * EXACTLY ONE MEMBER ACQUIRES. `get()` may capture; `read_region` and `for_logging` never
    do. That makes "who can cost ~4.6 s, and who can create a new generation" answerable by
    looking at one function instead of auditing 215 call sites.

  * A STALE REGION REQUEST IS A DIAGNOSIS. It should not arise: if the bot wants a region, it
    wants it on the current frame. A caller asking after it acted has lost track of the world
    it is acting in, and should hand back to the dispatcher rather than quietly re-acquire.
    Serving the old crop would be the Faro failure in miniature; capturing silently would hide
    the bug that produced the request.

  * A BLIND BOT STOPS. A capture that will not succeed after retries means we cannot see, and
    a bot that cannot see must not act on remembered pixels.

WHAT THIS IS NOT. It does not interpret. It manages frames and their lifecycle and serves the
primitive info in a region — bbox, icon, text. "What does this panel mean" belongs to the
readers, which consume these outputs. That line is what keeps this from absorbing the vision
layer.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple

from loguru import logger

# How many times a capture may fail before we call the bot blind. A transient ADB hiccup is
# worth retrying; a disconnected device is not going to reappear because we asked again.
_CAPTURE_ATTEMPTS = 3
_RETRY_PAUSE_S = 1.0


class Blind(RuntimeError):
    """Capture failed repeatedly: the bot cannot see, so it must not keep acting.

    Raised rather than returning a stale observation. The alternative — serving the last frame
    and letting callers proceed — is how a bot ends up tapping remembered coordinates on a
    screen it can no longer see. Live 2026-08-30 the device disconnected mid-session
    (`device '31101JEHN26098' not found`).
    """


class Stale(RuntimeError):
    """A read was asked for against an observation that has been superseded.

    This is a DIAGNOSIS, not a failure to work around: the caller acted, the screen moved, and
    it is still reasoning from what it saw before. The correct response is to finish and hand
    control back to the dispatcher — or at minimum re-perceive — because it is already lost.
    """


@dataclass(frozen=True)
class Observation:
    """One look at the screen, with the identity that makes staleness answerable.

    `frame` is SHARED and must not be modified — see the module docstring. `generation`
    increases with every new observation, and is the whole definition of stale: anything
    derived from an earlier one has been superseded.
    """

    frame: Any
    generation: int
    taken_at: float


class PerceiveRepository:
    """Owns the current observation, and is the only thing that captures.

    Collaborators are injected so this is testable without a device: `capture_fn` takes the
    screenshot, `parse_fn` turns a frame into elements, `ocr_fn` reads text from an image.
    """

    def __init__(
        self,
        *,
        capture_fn: Optional[Callable[[], Any]] = None,
        parse_fn: Optional[Callable[[Any], Any]] = None,
        ocr_fn: Optional[Callable[[Any], str]] = None,
        clock: Callable[[], float] = _time.monotonic,
        sleep_fn: Callable[[float], None] = _time.sleep,
    ) -> None:
        self._capture_fn = capture_fn
        self._parse_fn = parse_fn
        self._ocr_fn = ocr_fn
        self._clock = clock
        self._sleep = sleep_fn

        self._observation: Optional[Observation] = None
        self._generation = 0
        self._valid = False
        self._not_before = 0.0
        self._invalidated_by = "nothing observed yet"
        self._derived: dict = {}          # (generation, key) -> value

    # ── the only acquiring call ───────────────────────────────────────────────

    def get(self, *, why: str = "") -> Observation:
        """The current observation, capturing first if what we hold has been superseded.

        Blocks. This is where the ~4.6 s of a capture-and-parse is paid, and the only place a
        new generation is created.
        """
        if self._valid and self._observation is not None:
            return self._observation
        return self._capture(why=why)

    # ── non-acquiring readers ─────────────────────────────────────────────────

    def read_region(self, box: Tuple[int, int, int, int], *, how: str = "text") -> Any:
        """A primitive read of one region of the CURRENT observation. Never captures.

        Raises `Stale` when the observation has been invalidated: see the module docstring —
        a caller asking about a superseded frame has lost track of the world, and the answer
        is to hand back, not to re-acquire behind its back.

        A tight crop is not only cheaper than a whole-frame read, it is often MORE accurate:
        the Lisboa price scored 0.372 across the full frame — under the 0.40 cutoff, so it was
        dropped — and 0.634 from its own tile.
        """
        obs = self._require_current("read_region")
        key = (obs.generation, "region", tuple(box), how)
        if key in self._derived:
            return self._derived[key]
        crop = obs.frame.crop(box)
        if how == "text":
            value = (self._ocr(crop) or "").strip()
        elif how == "crop":
            value = crop
        else:
            raise ValueError(f"unknown region read {how!r}")
        self._derived[key] = value
        return value

    def elements(self) -> Any:
        """The parsed elements of the current observation — bbox / icon / text. Never captures."""
        obs = self._require_current("elements")
        key = (obs.generation, "elements")
        if key not in self._derived:
            self._derived[key] = self._parse(obs.frame)
        return self._derived[key]

    def current_if_valid(self) -> Optional[Observation]:
        """The observation IF it is still current. Never captures, never raises.

        `for_logging` hands back whatever is held, stale or not, because a log line is better
        with an old frame than with none. This is the stricter question — "is what I hold
        still the screen?" — and it is answered by GENERATION, not by comparing pixels. The
        action trace needs that: this game animates every frame (flags, water, crowds), so a
        pixel diff between two captures of one unchanged screen reports CHANGED, which is why
        the trace attached its live perception to only 22 of 122 frames.
        """
        return self._observation if (self._valid and self._observation is not None) else None

    def elements_if_ready(self) -> Optional[Any]:
        """The parse of the current observation IF one was already made — never makes one.

        Recording what the bot SAW must not change what the run costs, and must never invent
        a reading the run never had.
        """
        obs = self.current_if_valid()
        if obs is None:
            return None
        return self._derived.get((obs.generation, "elements"))

    def for_logging(self) -> Optional[Observation]:
        """Whatever is held, stale or not, or None. NEVER captures.

        A log line changes nothing, so it must never cost a frame. The codebase already names
        this failure at `brain/barter_command.py:669` — "a full OmniParser pass for a log line".
        """
        return self._observation

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def expect_changed(self, why: str, *, settle_s: float = 0.0) -> None:
        """The CALLER expects the screen to have changed, so the next read looks again.

        Distinct from `invalidate` only in who is speaking, and that distinction is the point
        (user, 2026-08-31): this repository does not model the world and cannot know whether
        the game moved. Two callers legitimately hold that expectation —

          * a sub-loop that has just ACTED (told to us automatically by the action layer, so
            it never has to remember), and
          * the dispatcher regaining control and taking a fresh look.

        Anything else asking is a caller guessing at the world on the repository's behalf.
        """
        self.invalidate(why, settle_s=settle_s)

    def invalidate(self, why: str, *, settle_s: float = 0.0) -> None:
        """Mark what we hold as superseded. Called for us by every action primitive.

        `settle_s` is a NOT-BEFORE, not a sleep: capturing straight after a tap yields a
        mid-animation frame, which is worse than none. The primitives know their own dwell,
        which is what the settle waits scattered through `actions/` are really expressing.
        """
        self._valid = False
        self._invalidated_by = why
        if settle_s:
            self._not_before = max(self._not_before, self._clock() + settle_s)

    def generation(self) -> int:
        """Which observation is current. Zero when nothing has been observed."""
        return self._generation

    # ── internals ─────────────────────────────────────────────────────────────

    def _capture(self, *, why: str) -> Observation:
        wait = self._not_before - self._clock()
        if wait > 0:
            self._sleep(wait)

        last: Optional[Exception] = None
        for attempt in range(1, _CAPTURE_ATTEMPTS + 1):
            try:
                frame = self._capture_screen()
            except Exception as exc:                      # noqa: BLE001 — retried below
                last = exc
                logger.warning(f"[perceive_repo] capture failed "
                               f"({attempt}/{_CAPTURE_ATTEMPTS}): {exc}")
                if attempt < _CAPTURE_ATTEMPTS:
                    self._sleep(_RETRY_PAUSE_S)
                continue

            self._generation += 1
            self._observation = Observation(frame=frame, generation=self._generation,
                                            taken_at=self._clock())
            self._valid = True
            self._not_before = 0.0
            # INFO, not debug. The whole claim of this repository is that captures now track
            # ACTIONS rather than reads, and that claim is unverifiable if the only evidence
            # is filtered out of the session log — which it was on the first live run
            # (2026-08-31: zero DEBUG lines reach run.log). One line per capture is low
            # volume precisely BECAUSE the change works; if it ever gets noisy, that is the
            # defect announcing itself.
            logger.info(f"[perceive_repo] observation {self._generation}"
                         + (f" ({why})" if why else "")
                         + f" — previous was superseded by: {self._invalidated_by}")
            return self._observation

        # A BLIND BOT STOPS. Not "serve the last frame": acting on remembered pixels is how a
        # disconnected device turns into taps at coordinates nobody can see.
        raise Blind(f"could not capture the screen after {_CAPTURE_ATTEMPTS} attempts"
                    + (f" (wanted for: {why})" if why else "")) from last

    def _require_current(self, caller: str) -> Observation:
        if self._observation is None:
            raise Stale(f"{caller}: nothing has been observed yet")
        if not self._valid:
            raise Stale(
                f"{caller}: the observation was superseded ({self._invalidated_by}). "
                "This caller acted and did not look again — it is reasoning about a screen "
                "that is no longer there, and should hand back rather than re-acquire.")
        return self._observation

    def _capture_screen(self) -> Any:
        if self._capture_fn is not None:
            return self._capture_fn()
        from capture.adb_capture import capture_screen
        return capture_screen()

    def _parse(self, frame: Any) -> Any:
        if self._parse_fn is not None:
            return self._parse_fn(frame)
        from vision.omniparser import parse_fast_cached
        return list(parse_fast_cached(frame))

    def _ocr(self, image: Any) -> str:
        if self._ocr_fn is not None:
            return self._ocr_fn(image)
        from vision.ocr import read_text
        return read_text(image)

