"""Per-action frame recorder — capture (screenshot, tap-target) at every tap so a
flow can be replayed and analysed frame-by-frame afterwards.

Motivation: reading logs to infer "did the bot tap the Purchase button?" is
guesswork. This records, for EVERY tap, the frame the bot saw *before* tapping
plus the exact tap point — so after a run we can look at each frame one by one and
see precisely what action was taken on it (like the navigation tick-frame dumps).

Usage (wrap a run):
    from actions import action_trace
    action_trace.start("london_amsterdam")
    ...run the flow...
    action_trace.stop()

Output: data/sessions/trace_<name>_<ts>/
    frame_0000.png            the pre-tap screen
    frame_0000_marked.png     same, with a red crosshair at the tap point
    actions.jsonl             one row per action: {idx,kind,x,y,label,t,frame}

Analyse with tools/analyze_action_trace.py.
"""
from __future__ import annotations

import json
import time
import weakref
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from loguru import logger

_SESSIONS = Path("data/sessions")

_dir: Optional[Path] = None
_idx = 0
_label: Optional[str] = None      # optional semantic hint set by callers


def start(name: str) -> Path:
    """Begin recording taps to a fresh session dir; returns its path."""
    global _dir, _idx
    ts = time.strftime("%Y-%m-%dT%H-%M-%S")
    _dir = _SESSIONS / f"trace_{name}_{ts}"
    _dir.mkdir(parents=True, exist_ok=True)
    _idx = 0
    from capture.adb_capture import set_capture_sink
    set_capture_sink(record_capture)
    from vision.omniparser import set_parse_sink
    set_parse_sink(record_parse)
    from vision.llm_trace import set_sink as set_llm_sink
    set_llm_sink(record_llm)
    _PARSES.clear()
    _SAVED.clear()
    logger.info(f"[action_trace] recording every capture → {_dir}")
    return _dir


_in_record = False


def record_capture(frame) -> None:
    """Save EVERY captured frame, before anything decides what it means.

    Registered as the capture sink by `start`. The tap-triggered records below still add the
    tap target and the decision metadata; this makes sure the frame itself is never lost when
    the bot LOOKS and then chooses not to act — the case that left the 2026-08-30 Hutu Village
    failure with no evidence at all.

    NOTE: this records the captures the code makes today, which is 215 scattered call sites.
    It is a floodlight, not the fix — capture belongs in perceive, one per tick, handed down.
    """
    global _idx, _in_record
    if _dir is None or _in_record:
        return                      # re-entry: record_tap/record_decision capture their own
    try:
        _in_record = True
        fn = f"frame_{_idx:04d}.png"
        frame.save(_dir / fn)
        _remember_saved(frame, _idx)   # a later parse writes its side-car alongside
        entry = {"idx": _idx, "kind": "capture", "x": -1, "y": -1, "label": "capture",
                 "t": time.strftime("%H:%M:%S"), "frame": fn}
        with (_dir / "actions.jsonl").open("a") as f:
            f.write(json.dumps(entry) + "\n")
        # ATTACH WHAT THE BOT SAW, ON EVERY FRAME AND NOT JUST THE TAPS (user, 2026-09-05).
        #
        # `record_tap` has done this since 2026-09-01; a plain capture did not, and plain
        # captures are most of a session — 375 of the 401 frames in the Berber run carried no
        # perception, so the viewer either showed nothing or re-parsed them, which is the one
        # thing the tap path exists to avoid. A misread cannot be investigated from a report
        # that re-reads the frame correctly.
        #
        # Free when it fires: the elements come out of OmniParser's own frame cache, so this
        # never runs inference and never changes the run it is describing. When the frame was
        # not the one perceived, nothing is written and the viewer parses it on demand as
        # before.
        perception = _perception_for(frame)
        if perception is not None:
            (_dir / f"frame_{_idx:04d}.json").write_text(json.dumps(perception))
        _idx += 1
    except Exception as exc:
        logger.debug(f"[action_trace] record_capture failed: {exc}")
    finally:
        _in_record = False


def record_llm(consult) -> None:
    """Append one LLM consult to `llm.jsonl`, tagged with the frame it was asked about.

    The frame index is the NEXT one to be written, i.e. the capture the consult is reasoning
    over — perception consults the model about a frame already captured, so `_idx - 1` is the
    picture the question was asked of. Recorded rather than logged because a prompt runs to
    ~12,000 characters and run.log is read by people.
    """
    if _dir is None:
        return
    try:
        row = dict(consult)
        row["frame_idx"] = max(_idx - 1, 0)
        row["t"] = time.strftime("%H:%M:%S")
        with (_dir / "llm.jsonl").open("a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as exc:
        logger.debug(f"[action_trace] record_llm failed: {exc}")


def stop() -> Optional[Path]:
    global _dir
    try:
        from capture.adb_capture import set_capture_sink
        set_capture_sink(None)
        from vision.llm_trace import set_sink as set_llm_sink
        set_llm_sink(None)
    except Exception:
        pass
    try:
        from vision.omniparser import set_parse_sink
        set_parse_sink(None)
    except Exception:
        pass
    _PARSES.clear()
    _SAVED.clear()
    d = _dir
    _dir = None
    if d is not None:
        logger.info(f"[action_trace] stopped after {_idx} action(s) → {d}")
    return d


def active() -> bool:
    return _dir is not None


def set_label(label: Optional[str]) -> None:
    """Optional: a caller can name the action it is about to take; recorded on
    the next tap and then cleared."""
    global _label
    _label = label


# EVERY PARSE THE RUN MAKES, KEPT LONG ENOUGH TO BE ATTACHED TO A FRAME.
#
# `vision.omniparser._FRAME_CACHE` is a CACHE — 4 entries, cleared wholesale on overflow —
# so it answers "did I just parse this?" and not "what did the run see?". Over the Berber
# barter it had dropped nearly every parse before the recorder asked, and the report came out
# "66 frames: 1 live, 65 unread" for a run that parsed 27 times.
#
# WEAK REFERENCES, NOT THE FRAMES. Identity is what makes a parse safe to attach — `id()`
# alone is a lie, because CPython reuses an address the moment an image is freed (measured:
# 200 frames, 3 distinct ids). Holding the frame would make identity trivial and the registry
# ruinous: 64 frames of 2400x1080 is roughly half a gigabyte, which is exactly why the cache
# is capped at 4. A weakref proves identity and pins nothing — a freed frame simply becomes a
# miss, which is the honest answer for a frame nobody can show any more.
_PARSES: "OrderedDict[int, tuple]" = OrderedDict()
_SAVED: "OrderedDict[int, tuple]" = OrderedDict()   # id(frame) -> (weakref, idx)
_MAX_PARSES = 64


def record_parse(frame, elements) -> None:
    """Told about every parse by the sink installed in `start`. Never raises, never parses.

    THE PARSE ARRIVES AFTER THE FRAME IS SAVED, so it is written when it arrives.
    `record_capture` is the CAPTURE sink: it fires the moment the screenshot is taken, which
    is necessarily before anything has parsed it, so a side-car written there can only ever
    say "unread". Measured live 2026-09-05: a three-frame session recorded 3 frames and 0
    side-cars, because every parse happened after every save.

    So the association runs the other way — a saved frame registers itself, and the parse
    finds it here and writes `frame_NNNN.json` alongside the PNG it describes.
    """
    if _dir is None:
        return
    try:
        _PARSES[id(frame)] = (weakref.ref(frame), list(elements))
        _PARSES.move_to_end(id(frame))
        while len(_PARSES) > _MAX_PARSES:
            _PARSES.popitem(last=False)
        _write_side_car(frame, {"omni": [e.to_dict() for e in elements]})
    except TypeError:
        pass                                 # not weak-referenceable — nothing to record
    except Exception as exc:
        logger.debug(f"[action_trace] record_parse failed: {exc}")


def _remember_saved(frame, idx: int) -> None:
    """Note that `frame` went to disk as frame_{idx}.png, so a later parse can find it."""
    try:
        _SAVED[id(frame)] = (weakref.ref(frame), idx)
        _SAVED.move_to_end(id(frame))
        while len(_SAVED) > _MAX_PARSES:
            _SAVED.popitem(last=False)
    except TypeError:
        pass


def _saved_index(frame) -> Optional[int]:
    """The index `frame` was saved under, by IDENTITY — an address match is not the frame."""
    entry = _SAVED.get(id(frame))
    if entry is None:
        return None
    ref, idx = entry
    return idx if ref() is frame else None


def _write_side_car(frame, data: dict) -> None:
    """Merge `data` into the side-car of the frame's saved PNG, if it was saved at all.

    MERGE, NEVER CLOBBER. Two things describe a frame and they arrive at different moments:
    the parse (here, as it is made) and the dispatcher's classification (at the tap, which
    knows `state`). Whichever lands second must not erase the first.
    """
    idx = _saved_index(frame)
    if idx is None or _dir is None:
        return                               # a frame nobody saved has nothing to sit beside
    f = _dir / f"frame_{idx:04d}.json"
    try:
        existing = json.loads(f.read_text()) if f.exists() else {}
    except Exception:
        existing = {}
    existing.update({k: v for k, v in data.items() if v is not None})
    f.write_text(json.dumps(existing))


def _parse_for(f):
    """The elements the run produced for THIS frame object, or None. Identity, not id."""
    entry = _PARSES.get(id(f))
    if entry is None:
        return None
    ref, elements = entry
    return elements if ref() is f else None


def _cached_reads(f) -> Optional[dict]:
    """`f`'s omni + ocr, TAKEN ONLY FROM CACHES — never pays for inference.

    Recording what the bot saw must not change what the run costs, so every read here is a
    hit or a miss, never a parse. Identity, not resemblance: both caches are keyed by
    `id(frame)` and store the frame alongside, because CPython reuses an address the moment
    an image is freed — a bare id served one screen's OCR for another (live 2026-08-21).

    OCR is attached only if it too was cached. `omni` present with `ocr` empty means the run
    parsed this frame but never read text from it, which is a fact about the run.
    """
    try:
        elements = _parse_for(f)                 # what the run recorded as it parsed
        if elements is None:
            from vision.omniparser import _FRAME_CACHE
            cached = _FRAME_CACHE.get(id(f))
            if cached is None or cached[0] is not f:
                return None              # parsed elsewhere or not at all — do not re-run
            elements = cached[1]
        from actions.sail_actions import _OCR_CACHE, _ocr_frame
        entry = _OCR_CACHE.get(id(f))
        ocr = ([{"text": t, "conf": round(float(c), 2), "cx": int(cx), "cy": int(cy)}
                for t, c, cx, cy in _ocr_frame(f, 0.3)]          # cache hit, proven above
               if entry is not None and entry[0] is f else [])
        return {"omni": [e.to_dict() for e in elements], "ocr": ocr}
    except Exception as exc:
        logger.debug(f"[action_trace] cached reads unavailable: {exc}")
        return None


def _perception_for(frame):
    """The perception the bot ACTUALLY USED for `frame`, or None if this is not that frame.

    The tap path captures its own frame and then asks the same question (see
    `_capture_with_perception`, whose reasoning applies here in full: a report that quietly
    re-perceives cannot show you the misread you opened it to find). This is the variant for
    a frame we are HANDED rather than one we take.

    Identity, never resemblance. The repository's held observation is valid exactly while
    nothing has acted, and the frame it holds must BE this object — `is`, not a pixel
    comparison — because the game animates every frame and two captures of one unchanged
    screen differ. If it is not the same object, this frame was never perceived and the
    honest answer is None.

    Costs nothing: both reads below are cache hits on a frame the bot has already parsed. A
    recorder that paid for inference would change the run it exists to describe.
    """
    try:
        from actions.perception import screen
        held = screen().current_if_valid()
        if held is None or held.frame is not frame:
            return None
        from brain import perceive as _p
        pr = _p._PERCEIVE_LAST_RESULT if _p._PERCEIVE_LAST_FRAME is frame else None
        reads = _cached_reads(frame)
        if reads is None:
            return None                      # parsed elsewhere or not at all — do not re-run
        return {**reads,
                "state": getattr(pr, "state", None),
                "detail": getattr(pr, "detail", None)}
    except Exception as exc:
        logger.debug(f"[action_trace] perception attach skipped: {exc}")
        return None


def _capture_with_perception():
    """Capture the pre-action frame and attach the perception the bot ACTUALLY USED.

    THE VIEWER MUST SHOW WHAT THE BOT SAW, NOT WHAT A SECOND LOOK SEES (user, 2026-09-01).
    Re-parsing a frame in the report can SUCCEED WHERE THE LIVE RUN FAILED, and then the
    misread you opened the report to find is the one thing it cannot show you. Every defect
    chased through the viewer today was a misread; a report that quietly re-perceives is a
    report that hides them. Reuse is the correctness argument here, and the ~5s/frame it
    saves is the side benefit.

    IS THE PERCEIVE STILL THE SCREEN? Ask the repository, not a pixel diff. This used
    `classify_action_outcome(pf, frame).kind != "unchanged"`, and this game animates every
    frame — flags, water, crowds — so two captures of one unchanged screen compare as
    CHANGED. Measured: perception reached only 22 of 122 frames, and the other 100 were
    re-perceived by the viewer. The repository answers by GENERATION instead: its observation
    is valid exactly while nothing has acted, and `record` runs BEFORE the action, so a valid
    observation IS the screen this action is about to be taken on.

    Returns (frame, perception|None). The omni/ocr reads are cache HITS on the perceive
    frame; nothing here may cost inference, because recording what happened must not change
    what happens."""
    from capture.adb_capture import capture_screen
    frame = capture_screen()
    try:
        from actions.perception import screen
        held = screen().current_if_valid()
        if held is None:
            # Something has acted since the last look. There is no observation that describes
            # the screen this action is about to be taken on, and inventing one is the whole
            # thing this recorder must not do.
            return frame, None

        # THE OBSERVATION THE BOT ACTED ON IS NOT ALWAYS THE DISPATCHER'S (live 2026-09-05).
        #
        # This used to require the held observation to BE `_PERCEIVE_LAST_FRAME`, so any look
        # taken after the tick's perceive disqualified the frame. In a barter that is nearly
        # every frame: the panel reader takes its own observations and each supersedes the
        # perceive. Measured over the Berber run — 27 observations, only 8 of them `perceive`;
        # the rest were the tile read-back, the goods row and the commit before/after. The
        # report came out "66 frames: 1 live, 65 unread", and 65 of those frames HAD a parse
        # sitting in the cache that the run itself had paid for and used.
        #
        # The repository's validity is generation-based: a held observation is valid exactly
        # while nothing has acted, and `record` runs BEFORE the action. So a valid observation
        # IS the screen this action is about to be taken on, whichever collaborator looked.
        reads = _cached_reads(held.frame)
        if reads is None:
            return frame, None

        # `state`/`detail` are the DISPATCHER's classification, and they describe the frame it
        # classified. Attaching them to somebody else's observation would caption the picture
        # with another frame's verdict, so they go on only when it is the same look.
        from brain import perceive as _p
        pr = _p._PERCEIVE_LAST_RESULT if _p._PERCEIVE_LAST_FRAME is held.frame else None
        return frame, {**reads,
                       "state": getattr(pr, "state", None),
                       "detail": getattr(pr, "detail", None)}
    except Exception as exc:
        logger.debug(f"[action_trace] perception attach skipped: {exc}")
        return frame, None


def record_tap(x: int, y: int, kind: str = "tap") -> None:
    """Capture the PRE-action frame + the tap target. Called by the tap/back
    primitives when a session is active. Never raises."""
    global _idx, _label, _in_record
    if _dir is None:
        return
    try:
        _in_record = True
        frame, perception = _capture_with_perception()
        fn = f"frame_{_idx:04d}.png"
        frame.save(_dir / fn)
        _remember_saved(frame, _idx)   # a later parse writes its side-car alongside
        if kind == "tap":
            _save_marked(frame, x, y, fn)
        entry = {"idx": _idx, "kind": kind, "x": x, "y": y,
                 "label": _label, "t": time.strftime("%H:%M:%S"), "frame": fn}
        with (_dir / "actions.jsonl").open("a") as f:
            f.write(json.dumps(entry) + "\n")
        if perception is not None:                      # viewer reuses this — no OmniParser re-run
            (_dir / f"frame_{_idx:04d}.json").write_text(json.dumps(perception))
        _idx += 1
    except Exception as exc:
        logger.debug(f"[action_trace] record failed: {exc}")
    finally:
        _label = None
        _in_record = False


def record_decision(inputs: dict, output: dict, model: str, label: str = "decision") -> None:
    """Capture the current frame + a decision: its INPUTS, the action CHOSEN, and
    WHICH model made it. Shown on the viewer's Decision tab. Never raises."""
    global _idx, _in_record
    if _dir is None:
        return
    try:
        _in_record = True
        frame, perception = _capture_with_perception()
        fn = f"frame_{_idx:04d}.png"
        frame.save(_dir / fn)
        _remember_saved(frame, _idx)   # a later parse writes its side-car alongside
        entry = {"idx": _idx, "kind": "decision", "x": -1, "y": -1, "label": label,
                 "t": time.strftime("%H:%M:%S"), "frame": fn,
                 "inputs": inputs, "output": output, "model": model}
        with (_dir / "actions.jsonl").open("a") as f:
            f.write(json.dumps(entry) + "\n")
        if perception is not None:
            (_dir / f"frame_{_idx:04d}.json").write_text(json.dumps(perception))
        _idx += 1
    except Exception as exc:
        logger.debug(f"[action_trace] record_decision failed: {exc}")
    finally:
        _in_record = False


def _save_marked(frame, x: int, y: int, fn: str) -> None:
    try:
        from PIL import ImageDraw
        marked = frame.convert("RGB").copy()
        d = ImageDraw.Draw(marked)
        r = 30
        d.ellipse([x - r, y - r, x + r, y + r], outline=(255, 0, 0), width=6)
        d.line([x - r, y, x + r, y], fill=(255, 0, 0), width=3)
        d.line([x, y - r, x, y + r], fill=(255, 0, 0), width=3)
        marked.save(_dir / fn.replace(".png", "_marked.png"))
    except Exception:
        pass
