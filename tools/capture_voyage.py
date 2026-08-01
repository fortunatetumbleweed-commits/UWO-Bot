"""Reference-voyage capture for offline log-replay testing.

Runs the bot's full perception + planning pipeline alongside a manual
sailing session so we get a deterministic, replayable dataset to
iterate against without re-launching the live game.

Three streams are captured in the session directory:
  - tick_NNNN.png         — mini-map crop per tick (same as live runs)
  - trace.jsonl           — bot's per-tick observation + computed wp +
                            intended steering (action it WOULD have
                            taken if not in capture mode)
  - keystrokes.jsonl      — global keyboard events (timestamped
                            press/release of every key) captured via
                            pynput.  Joins to trace by wall_iso to
                            give ground-truth human action per tick.

Capture mode behaviour:
  - All `actions.sea_actions` mutators are monkey-patched to no-ops.
    The bot perceives, plans, and writes its intended action to
    trace.jsonl, but no taps fire — you steer manually via scrcpy.
  - The captured `result.action` field tells you what the bot *wanted*
    to do; comparing against your keystrokes is the diff signal.
  - sail_start / sail_stop / turn_left / turn_right / hold_left /
    hold_right all stubbed.  `is_ship_moving` left intact (read-only).

Replay (in a later commit) reads tick_NNNN.png + keystrokes.jsonl
and runs the bot's logic offline to produce a comparable trace, then
diffs the two.

Usage:
  python -m tools.capture_voyage --name nile_left_hug --max-ticks 600

Macros + Accessibility:
  pynput needs macOS Accessibility permission on the *terminal app*
  that launches this script.  Confirmed working in the project's
  setup as of 2026-06-04 (Python 3.12.13 + pynput 1.8.2).  If keys
  are silently dropped, re-check System Settings → Privacy & Security
  → Accessibility.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional


# ── Monkey-patch sea_actions BEFORE hug_shore is imported ──────────────────
#
# hug_shore imports sea_actions inside functions, so patching at module
# load time works.  We replace the mutating functions with no-ops that
# log "would have called X" so the trace records the bot's intent
# without firing any ADB taps.

import actions.sea_actions as _sea_actions  # noqa: E402

_REAL_FNS = {}
_INTENT_LOG: list = []


def _make_stub(name: str, real_fn):
    def stub(*args, **kwargs):
        ts = datetime.now().isoformat()
        _INTENT_LOG.append({
            "wall_iso": ts, "fn": name,
            "args": list(args), "kwargs": dict(kwargs),
        })
        # Print so it's visible in the live console.
        arg_repr = ", ".join(
            [repr(a) for a in args] +
            [f"{k}={v!r}" for k, v in kwargs.items()]
        )
        print(f"[capture] would-have-called sea_actions.{name}({arg_repr})",
              flush=True)
        # Return a successful-looking result.  The real fn returns
        # SeaActionResult; we mimic its truthy shape.
        try:
            from actions.sea_actions import SeaActionResult
            return SeaActionResult(
                ok=True, action=name,
                detail=f"capture_stub: tap suppressed",
            )
        except Exception:
            return None
    stub.__name__ = name
    stub.__wrapped__ = real_fn
    return stub


_STUBBED_FNS = (
    "sail_start", "sail_stop",
    "turn_left", "turn_right",
    "hold_left", "hold_right",
)
for _fn_name in _STUBBED_FNS:
    if hasattr(_sea_actions, _fn_name):
        _REAL_FNS[_fn_name] = getattr(_sea_actions, _fn_name)
        setattr(_sea_actions, _fn_name, _make_stub(_fn_name, _REAL_FNS[_fn_name]))


# ── Keystroke listener ─────────────────────────────────────────────────────

class KeystrokeRecorder:
    """Background thread writing every press/release event to a JSONL
    file via pynput.Listener.  Captures every keypress globally — make
    sure the terminal app has macOS Accessibility permission.
    """

    def __init__(self, out_path: Path):
        self.out_path = out_path
        self._fp = open(out_path, "w")
        self._lock = threading.Lock()
        self._listener = None

    def _key_repr(self, key) -> str:
        # pynput KeyCode → char if printable, otherwise the Key enum name.
        try:
            return key.char if key.char is not None else str(key)
        except AttributeError:
            return str(key)

    def _emit(self, event: str, key) -> None:
        rec = {
            "wall_iso": datetime.now().isoformat(),
            "key": self._key_repr(key),
            "event": event,
        }
        with self._lock:
            self._fp.write(json.dumps(rec) + "\n")
            self._fp.flush()

    def _on_press(self, key):
        self._emit("press", key)

    def _on_release(self, key):
        self._emit("release", key)

    def start(self):
        from pynput import keyboard
        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self._listener.start()
        print(f"[capture] keystroke listener started → {self.out_path}",
              flush=True)

    def stop(self):
        if self._listener is not None:
            self._listener.stop()
        with self._lock:
            self._fp.close()
        print(f"[capture] keystroke listener stopped", flush=True)


# ── Main ───────────────────────────────────────────────────────────────────

def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Capture a reference voyage for offline replay.",
    )
    ap.add_argument("--name", required=True,
                    help="Session name (becomes "
                         "data/sessions/reference_<name>/).")
    ap.add_argument("--side", choices=("port", "starboard"), default="port",
                    help="Hug side (port = shore on left of bow, "
                         "starboard = shore on right). Default: port "
                         "(matches the existing Nile-hug-left task).")
    ap.add_argument("--max-ticks", type=int, default=600,
                    help="Stop after N ticks. Default 600.")
    ap.add_argument("--interval", nargs=2, type=float, default=(0.15, 0.40),
                    help="Random sleep range between ticks (seconds).")
    args = ap.parse_args(argv)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = Path("data/sessions") / f"reference_{args.name}_{ts}"
    session_dir.mkdir(parents=True, exist_ok=True)
    print(f"[capture] session → {session_dir}", flush=True)
    print(f"[capture] sail manually via scrcpy (a/d/w/s); Cmd-Tab to "
          f"this Terminal and press Ctrl-C to end the trip cleanly.",
          flush=True)

    # Start keystroke recorder before launching the bot loop so we
    # don't miss the initial seconds.
    recorder = KeystrokeRecorder(session_dir / "keystrokes.jsonl")
    recorder.start()

    # Defer hug_shore import until after monkey-patch is installed.
    from brain.goals.hug_shore import run_hug_shore_loop

    try:
        run_hug_shore_loop(
            side=args.side,
            max_ticks=args.max_ticks,
            tick_interval_s=tuple(args.interval),
            debug_dir=session_dir,
        )
    except KeyboardInterrupt:
        print("\n[capture] interrupted by user, flushing…", flush=True)
    finally:
        recorder.stop()
        # Persist the intent log alongside trace.jsonl so we can
        # see what the bot WANTED to do (vs the keystrokes for what
        # the human did).
        (session_dir / "bot_intent.jsonl").write_text(
            "\n".join(json.dumps(r) for r in _INTENT_LOG) + "\n"
        )
        print(f"[capture] session complete: {session_dir}", flush=True)
        print(f"[capture]   ticks    → {session_dir}/trace.jsonl", flush=True)
        print(f"[capture]   keys     → {session_dir}/keystrokes.jsonl",
              flush=True)
        print(f"[capture]   intent   → {session_dir}/bot_intent.jsonl",
              flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
