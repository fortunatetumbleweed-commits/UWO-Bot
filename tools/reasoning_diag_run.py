"""Instrumented live reasoning run → per-step diagnostic HTML report.

Runs the real reasoning loop (resolve) live, but wraps observe / llm / execute to
CAPTURE, for every step:
  - the screen frame (PNG),
  - the RAW OmniParser elements (label/type/bbox/conf + a yellow-fill check),
  - the exact prompt fed to the LLM (world model + inventory + primer),
  - the LLM's raw reply + parsed action,
  - the executor result.
Then writes /tmp/crew_diag/report.html so we can see WHERE it went wrong.

    ANTHROPIC_API_KEY=... python -m tools.reasoning_diag_run "<goal>" [max_steps]
"""
import sys
import json
import shutil
import html
from pathlib import Path

import numpy as np

OUT = Path("/tmp/crew_diag")


def _yellow_frac(arr, x1, y1, x2, y2) -> float:
    """Fraction of yellow/gold pixels in a bbox — the commit-button signature."""
    h, w = arr.shape[:2]
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
    c = arr[y1:y2, x1:x2].reshape(-1, 3)
    if len(c) == 0:
        return 0.0
    r, g, b = c[:, 0].astype(int), c[:, 1].astype(int), c[:, 2].astype(int)
    return float(((r > 150) & (g > 120) & (b < 110) & (abs(r - g) < 80)).mean())


def _analyse(step: dict) -> list:
    """Heuristic flags: where might this step have gone wrong?"""
    flags = []
    raw = step.get("raw", [])
    # commit-button candidates = yellow buttons
    yellows = [e for e in raw if e.get("element_type") == "button"
               and e.get("yellow_frac", 0) >= 0.2]
    inv_labels = {e for e in step.get("perception", "").split()}
    for y in yellows:
        lab = (y.get("label") or "").strip()
        numeric = lab.replace(",", "").replace(".", "").isdigit()
        if numeric:
            flags.append(f"⚠ yellow COMMIT button @({y['x1']},{y['y1']}) is labelled "
                         f"'{lab}' (its COST) — the action verb was lost by OCR")
    act = step.get("action") or {}
    if act.get("op") == "tap" and act.get("arg") in ("Normal Recruit", "Emergency Recruit @",
                                                     "Emergency Recruit"):
        flags.append(f"⚠ tapped a TYPE toggle ('{act.get('arg')}'), not the commit button")
    if yellows and not any((e.get('label') or '').strip().lower() in
                           ("recruit", "buy", "sell", "purchase", "confirm") for e in yellows):
        flags.append("⚠ a yellow commit button exists but NO bare action-verb label "
                     "reached the LLM inventory")
    return flags


def main() -> None:
    goal = sys.argv[1] if len(sys.argv) > 1 else "Recruit crew then depart."
    max_steps = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    from capture.adb_capture import capture_screen
    from brain.perceive import perceive
    from brain.reasoning_loop import Observation, _element_inventory, _merge_commit_buttons
    from brain.reasoning_loop import resolve
    from vision.omniparser import parse_fast_cached
    from vision.region_detectors.left_menu import detect_left_menu
    from brain.world_model import WorldModel

    steps: list = []

    def observe_fn():
        frame = capture_screen()
        idx = len(steps) + 1
        frame.save(OUT / f"step{idx}_frame.png")
        arr = np.asarray(frame.convert("RGB"))
        try:
            res = perceive(frame)
            perceived, port = res.perceived, res.port
        except Exception as exc:
            perceived, port = None, None
            print(f"[diag] perceive failed: {exc}")
        els = parse_fast_cached(frame) or []
        raw = []
        for e in els:
            d = e.to_dict()
            if e.element_type == "button":
                d["yellow_frac"] = round(_yellow_frac(arr, e.x1, e.y1, e.x2, e.y2), 2)
            raw.append(d)
        menu = []
        lm = detect_left_menu(els, frame.width, frame.height)
        if lm and lm.items:
            menu = [it["label"] for it in lm.items]
        inv = _element_inventory(els, frame.width, frame.height)
        inv = _merge_commit_buttons(inv, els, frame)   # mirror live_observe
        obs = Observation(perceived=perceived, port=port, menu=menu,
                          buttons=[e["label"] for e in inv if e["type"] == "button"],
                          elements=inv, frame=frame)
        steps.append({"idx": idx, "frame": f"step{idx}_frame.png",
                      "raw": raw, "perception": obs.text})
        print(f"[diag] step {idx}: {len(raw)} raw elements captured")
        return obs

    def llm_fn(prompt):
        from brain.llm_client import claude_llm_fn
        reply = claude_llm_fn(prompt)
        if steps:
            steps[-1]["prompt"] = prompt
            steps[-1]["reply"] = reply
        return reply

    import os
    live_commit = os.environ.get("DIAG_LIVE_COMMIT") == "1"

    def exec_fn(action, frame=None, elements=None):
        from brain.action_executor import execute, ExecResult, _resolve_element
        # DRY-RUN commit/spend by default (prove the LLM PICKS it, no spend). With
        # DIAG_LIVE_COMMIT=1 commits ACT for real (needed to progress a buy and hit
        # + handle downstream dialogs like the load-ratio warning).
        tgt = _resolve_element(action.get("arg"), elements) if action.get("op") == "tap" else None
        if tgt and tgt.get("type") == "commit" and not live_commit:
            note = f"DRY-COMMIT: would tap [{tgt['label']}] @({tgt['cx']},{tgt['cy']}) cost={tgt.get('cost')}"
            r = ExecResult(True, note)
            print(f"[diag] {note}")
        else:
            r = execute(action, frame=frame, elements=elements)
        if steps:
            steps[-1]["action"] = action
            steps[-1]["exec"] = {"ok": r.ok, "note": r.note, "refused": r.refused}
        return r

    wm = WorldModel()
    print(f"GOAL: {goal}\n(LIVE, instrumented; max_steps={max_steps})\n")
    out = resolve(goal, wm, observe_fn=observe_fn, llm_fn=llm_fn, execute_fn=exec_fn,
                  shadow=False, max_steps=max_steps, trigger="diag")

    for s in steps:
        s["flags"] = _analyse(s)
    (OUT / "diag.json").write_text(json.dumps(
        {"goal": goal, "result": out, "steps": steps}, indent=2, default=str))
    _write_report(goal, out, steps, wm)
    print(f"\nREPORT: {OUT / 'report.html'}")


def _write_report(goal, out, steps, wm) -> None:
    e = html.escape
    p = ['<!doctype html><meta charset="utf-8"><title>Reasoning diagnostic</title>',
         '<style>body{font:14px/1.5 -apple-system,sans-serif;margin:24px;max-width:1200px}'
         'h2{border-top:2px solid #ccc;padding-top:16px;margin-top:32px}'
         'table{border-collapse:collapse;font:12px monospace}td,th{border:1px solid #ddd;padding:2px 6px}'
         'img{max-width:760px;border:1px solid #ccc}.f{background:#fff3cd;padding:6px 10px;border-left:4px solid #e0a800;margin:4px 0}'
         'pre{background:#f6f8fa;padding:10px;white-space:pre-wrap;border-radius:6px}'
         '.commit{background:#fff3cd}.reply{background:#eef7ee;padding:10px;border-radius:6px}'
         'code{background:#eee;padding:1px 4px}</style>']
    p.append(f"<h1>Reasoning diagnostic</h1><p><b>Goal:</b> {e(goal)}<br>"
             f"<b>Result:</b> {e(str(out.get('reason')))} · steps: {len(steps)}</p>")
    for s in steps:
        p.append(f"<h2>Step {s['idx']}</h2>")
        for fl in s.get("flags", []):
            p.append(f'<div class="f">{e(fl)}</div>')
        p.append(f'<img src="{s["frame"]}"><br>')
        # LLM data + reply first (the decision), then the raw dump
        act = s.get("action")
        p.append(f'<h3>Parsed action</h3><pre>{e(json.dumps(act))}</pre>')
        p.append(f'<h3>LLM reply (raw)</h3><div class="reply"><pre>{e(str(s.get("reply","")))}</pre></div>')
        if s.get("exec"):
            p.append(f'<h3>Executor</h3><pre>{e(json.dumps(s["exec"]))}</pre>')
        p.append('<h3>Perception fed to LLM (inventory)</h3>'
                 f'<pre>{e(s.get("perception",""))}</pre>')
        if s.get("prompt"):
            p.append('<details><summary><b>Full LLM input (prompt)</b></summary>'
                     f'<pre>{e(s["prompt"])}</pre></details>')
        # raw omniparser table — buttons first, yellow highlighted
        rows = sorted(s.get("raw", []),
                      key=lambda d: (d.get("element_type") != "button",
                                     -d.get("yellow_frac", 0), -d.get("confidence", 0)))
        p.append('<h3>Raw OmniParser elements</h3><table>'
                 '<tr><th>label</th><th>type</th><th>bbox</th><th>conf</th><th>yellow</th></tr>')
        for d in rows:
            cls = ' class="commit"' if d.get("yellow_frac", 0) >= 0.2 else ""
            bbox = f'{d.get("x1")},{d.get("y1")},{d.get("x2")},{d.get("y2")}'
            p.append(f'<tr{cls}><td>{e(str(d.get("label")))}</td>'
                     f'<td>{e(str(d.get("element_type")))}</td><td>{bbox}</td>'
                     f'<td>{d.get("confidence",0):.2f}</td>'
                     f'<td>{d.get("yellow_frac","")}</td></tr>')
        p.append('</table>')
    (OUT / "report.html").write_text("\n".join(p))


if __name__ == "__main__":
    main()
