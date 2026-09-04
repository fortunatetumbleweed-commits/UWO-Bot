"""Trade/task process viewer — visualise a captured action-trace session
frame-by-frame with each perception component's output in tabs, to aid market/
flow development (the analogue of the navigation tick viewer, for buy/sell/
overworld/harbor/dialog flows).

    python -m tools.trace_viewer data/sessions/trace_<name>/ [--qwen] [--limit N] [--rebuild]

For every captured frame it runs OmniParser, OCR, and nav-state classification
(and Qwen with --qwen), caches the results in <session>/viewer_data/, renders an
OmniParser-overlay image, then writes <session>/viewer.html — a self-contained
tabbed browser:
  Screen     — the frame with the tap crosshair
  OmniParser — bounding boxes + an element table
  OCR        — the recognised tokens
  Perception — nav-state + detail + Qwen's structured read + the action taken
Navigate with ←/→ (frames) and 1–4 (tabs), or click the frame list.
"""
from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path

from PIL import Image


def _run_components(frame: Image.Image, qwen: bool) -> dict:
    from vision.omniparser import parse_fast_cached
    from actions.sail_actions import _ocr_frame
    from brain.perceive import _classify_nav_state
    try:
        els = parse_fast_cached(frame)
        omni = [e.to_dict() for e in els]
    except Exception as exc:
        els, omni = [], [{"error": str(exc)}]
    ocr = [{"text": t, "conf": round(float(c), 2), "cx": int(cx), "cy": int(cy)}
           for t, c, cx, cy in _ocr_frame(frame, 0.3)]
    state = detail = None
    try:
        cls = _classify_nav_state(frame)
        if isinstance(cls, dict):
            state = cls.get("location") or cls.get("state") or cls.get("nav_state")
            detail = cls.get("detail")
        else:
            state, detail = getattr(cls, "state", None), getattr(cls, "detail", None)
    except Exception as exc:
        detail = f"classify error: {exc}"
    if not state and detail:                      # derive coarse state from the detail
        state = detail.split(":")[0].split(" ")[0]
    qres = None
    if qwen:
        try:
            from vision.qwen_perception import qwen_perceive
            toks = [(t["text"], t["conf"], t["cx"], t["cy"]) for t in ocr]
            qres = qwen_perceive(state or "unknown", detail or "", toks, elements=els)
        except Exception as exc:
            qres = {"error": str(exc)}
    return {"state": state, "detail": detail, "omni": omni, "ocr": ocr, "qwen": qres}


def build(session: Path, qwen: bool, limit: int, rebuild: bool,
          eager: bool = False) -> list:
    """Run OmniParser/OCR/classify per frame (cached to viewer_data/) and collect the
    element data.  Bounding boxes are NOT drawn here — the browser overlays them from the
    box coords in the payload (client-side, on-demand highlight), so cached rebuilds do no
    image work at all (was: re-open + re-draw every frame)."""
    actions = [json.loads(l) for l in (session / "actions.jsonl").read_text().splitlines() if l.strip()]
    if limit:
        actions = actions[:limit]
    cache_dir = session / "viewer_data"
    cache_dir.mkdir(exist_ok=True)
    frames = []
    for i, a in enumerate(actions):
        fp = session / a["frame"]
        if not fp.exists():
            continue
        # Prefer perception RECORDED LIVE by action_trace (frame_XXXX.json beside the png):
        # OmniParser already ran during the bot's perceive on this frame, so reuse it —
        # no re-run.  (Absent for traces recorded before this landed → fall through.)
        sidecar = session / a["frame"].replace(".png", ".json")
        if sidecar.exists() and not rebuild and not qwen:
            data = json.loads(sidecar.read_text())
            if not data.get("state") and data.get("detail"):
                data["state"] = data["detail"].split(":")[0].split(" ")[0]
            frames.append({**a, **data, "perception": "live"})
            print(f"  [{i+1}/{len(actions)}] {a['frame']} (live perception)")
            continue
        cache = cache_dir / f"{a['frame'].replace('.png', '')}.json"
        if cache.exists() and not rebuild:
            data = json.loads(cache.read_text())
            if not data.get("state") and data.get("detail"):      # backfill derived state
                data["state"] = data["detail"].split(":")[0].split(" ")[0]
            if not qwen or data.get("qwen") is not None:
                frames.append({**a, **data,
                               "perception": data.get("perception") or "rebuilt"})
                print(f"  [{i+1}/{len(actions)}] {a['frame']} (cached)")
                continue
        # NOTHING IS RE-PERCEIVED UNTIL SOMEBODY ASKS (user, 2026-09-01). A report is ~120
        # frames and you open two or three of them; parsing the rest costs about five seconds
        # each and answers a question nobody put. So the build carries what the run recorded
        # and marks the rest UNREAD — the image, the tap and the action are there, which is
        # what browsing needs — and `--serve` parses a frame when you press Parse on it.
        if not eager:
            frames.append({**a, "perception": "unread"})
            continue
        # RE-PERCEIVED, AND THE REPORT MUST SAY SO. A second look can succeed where the run
        # failed, and then the misread this report exists to show is the one thing it cannot
        # show. Marked `rebuilt` so a reading nobody acted on is never mistaken for one the
        # bot had (user, 2026-09-01).
        print(f"  [{i+1}/{len(actions)}] {a['frame']} — running components…")
        data = _run_components(Image.open(fp).convert("RGB"), qwen=qwen)
        data["perception"] = "rebuilt"
        cache.write_text(json.dumps(data))
        frames.append({**a, **data})
    return frames


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# How long after a capture a classify may still be about it. Perception takes a few
# seconds; past this the line belongs to a frame the trace did not record.
_MAX_CLASSIFY_LAG_S = 20
# A PERCEIVE LOGS A CHAIN, NOT A LINE (user, 2026-09-03). One look emits the cascade's
# PROPOSAL, then any gates that overrule it, then the verdict:
#
#   _classify_nav_state_inner  [classify] → port_overworld (omniparser, conf=high, ...)
#   _classify_nav_state        [classify] STRUCTURE GATE: ... the left menu is a VILLAGE's
#   _classify_nav_state        [classify] family=chromed@0.99 GATE: ... — → unrecognized_chromed_screen
#   _classify_nav_state        [classify] → village (left-menu vocab match [...])
#
# So the arrow is not always the second token: a gate writes its override at the END of its
# line, after an em-dash. Anchoring on "[classify] <arrow>" saw only the PROPOSAL and missed
# every correction — which is how frames 338-345 of the San run, a fleet plainly standing in
# a village, were reported as `port_overworld`. Worse for the transient gate, whose override
# to `unknown` was invisible entirely, so those frames showed a state the run had explicitly
# rejected.
#
# Non-greedy on both sides: the first `[classify]` in the line, then the first arrow after it.
# Lines with no arrow (`chrome: home=False ...`) and arrows to non-states
# (`read_port_name → 'Barter'`, quoted) do not match.
_CLASSIFY = re.compile(
    r"\d{4}-\d\d-\d\d (\d\d:\d\d:\d\d)\.\d+ .*?\[classify\].*?(?:→|->) ([a-z_]+)")
_PORT = re.compile(r"port='([^']+)'")


def _log_timeline(session: Path) -> list:
    """(hh:mm:ss, state, port) for every classify line the RUN logged — several per look.

    THE RUN ALREADY SAID WHERE IT WAS. Each perceive logs a CHAIN of `[classify]` lines —
    the cascade's proposal, the gates that overrule it, the verdict — with the port when it
    read one, so a frame's state is in the session before anyone re-parses anything — and it is better evidence than a rebuild, because it is the state the bot
    ACTED ON rather than one derived afterwards from the same picture.
    """
    log = session / "run.log"
    if not log.exists():
        return []
    out = []
    for line in log.read_text(errors="replace").splitlines():
        m = _CLASSIFY.search(_ANSI.sub("", line))
        if m:
            port = _PORT.search(line)
            out.append((m.group(1), m.group(2), port.group(1) if port else None))
    return out


def _secs(hms: str) -> int:
    h, m, sec = (int(x) for x in hms.split(":"))
    return h * 3600 + m * 60 + sec


def _annotate_from_log(session: Path, frames: list) -> None:
    """Give each frame the state the run logged for it, and the gap since the frame before.

    The gap is what makes a report readable at a glance: five seconds between frames is the
    bot working, and ninety is it waiting out a settle or a voyage. Reading that off absolute
    timestamps means doing arithmetic on every row."""
    # A FRAME IS CAPTURED AND THEN CLASSIFIED, so the line that describes it comes AFTER its
    # timestamp — never before. Taking the last classify at-or-before a frame labelled every
    # frame with the PREVIOUS one's state: frame_0008, captured 08:41:06 on the world map,
    # read `port_overworld` from a line logged at 08:40:57 while the line describing it sat at
    # 08:41:11 (user, 2026-09-03, who spotted it and guessed the cause exactly).
    #
    # A wrong state in a report is worse than none — it is the report asserting something
    # about a frame you can see is false, which costs the whole thing its authority.
    #
    # So: the classify lines at or after the capture, and only those that still belong to this
    # frame — a line logged after the NEXT capture describes that one instead, and a line far
    # later describes a frame the trace never recorded. Neither is ours to claim.
    #
    # And within that window, the LAST of them (user, 2026-09-03). This used to stop at the
    # first, which is a DIFFERENT axis of the same mistake the direction fix addressed: one
    # perceive logs a chain — proposal, gates, verdict — so the first line is the cascade's
    # guess and the gates that overrule it come after. See `_CLASSIFY`.
    #
    # The test that covered the direction fix could never have caught this: its fixture log
    # has exactly one classify line per perceive, so first and last are the same line.
    timeline = _log_timeline(session)
    prev = None
    for i, f in enumerate(frames):
        t = f.get("t")
        nxt = frames[i + 1].get("t") if i + 1 < len(frames) else None
        if t and timeline:
            for ct, state, port in timeline:
                if ct < t:
                    continue                      # describes an earlier frame
                if nxt and ct >= nxt:
                    break                         # describes the NEXT frame, not this one
                if _secs(ct) - _secs(t) > _MAX_CLASSIFY_LAG_S:
                    break                         # too late to be about this capture
                # LAST WINS, NOT FIRST. Every line in this window belongs to the same look,
                # and they are ordered proposal → gates → verdict, so the last one is what the
                # run actually acted on. Taking the first reported the guess the run itself
                # threw away one line later.
                f["log_state"], f["log_port"] = state, port
        if t and prev:
            f["gap_s"] = max(0, _secs(t) - _secs(prev))
        prev = t or prev


def _write_html(session: Path, frames: list) -> Path:
    payload = json.dumps(frames).replace("</", "<\\/")
    out = session / "viewer.html"
    out.write_text(_HTML.replace("__TITLE__", html.escape(session.name)).replace("__DATA__", payload))
    return out


_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>__TITLE__</title>
<style>
 body{margin:0;font-family:ui-monospace,Menlo,monospace;background:#1e1e1e;color:#ddd;font-size:13px}
 #wrap{display:flex;height:100vh}
 #list{width:230px;overflow-y:auto;border-right:1px solid #444;background:#252525}
 .fitem{padding:6px 9px;border-bottom:1px solid #333;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .fitem:hover{background:#333}.fitem.sel{background:#0a4a6e}
 /* The nav state is a PARSE result and an unparsed frame has none. Showing '?' implied the
    run had failed to see where it was, when in fact nobody had looked yet. The trace always
    knows what the bot DID at that frame, so show that instead, dimmed to mark it as the
    action rather than the state. (user, 2026-09-03) */
 .unparsed{color:#8a8a8a;font-style:italic}
 .fitem.dec{border-left:4px solid #e9a}
 .fidx{color:#7bd;font-weight:bold}.fstate{color:#9c9}.fact{color:#e9a}
 #main{flex:1;display:flex;flex-direction:column;overflow:hidden}
 #tabs{display:flex;border-bottom:1px solid #444;background:#2b2b2b}
 .tab{padding:8px 16px;cursor:pointer;border-right:1px solid #444}
 .tab:hover{background:#3a3a3a}.tab.sel{background:#0a4a6e;color:#fff}
 #body{flex:1;overflow:auto;padding:10px}
 img{max-width:100%;border:1px solid #444}
 .ovl{position:absolute;inset:0;pointer-events:none}
 .obox{position:absolute;border:2px solid;box-sizing:border-box;pointer-events:auto;opacity:.55}
 .obox.hl{border-width:4px;opacity:1;background:rgba(255,255,255,.18)}
 .otap{position:absolute;width:22px;height:22px;margin:-11px 0 0 -11px;border:3px solid #f00;border-radius:50%;box-shadow:0 0 0 2px rgba(255,0,0,.4);pointer-events:none}
 tr.hl td{background:#0a4a6e}
 table{border-collapse:collapse;font-size:12px}td,th{border:1px solid #444;padding:2px 6px;text-align:left}
 th{background:#2b2b2b;position:sticky;top:0}
 .hd{padding:6px 10px;background:#2b2b2b;border-bottom:1px solid #444}
 .k{color:#8bd}.v{color:#dea}.lossred{color:#f66}
 pre{white-space:pre-wrap;margin:4px 0}
</style></head><body>
<div id="wrap">
 <div id="list"></div>
 <div id="main">
  <div class="hd" id="hd"></div>
  <div id="tabs"></div>
  <div id="body"></div>
 </div>
</div>
<script>
const F = __DATA__;
const TABS = ["Screen / OmniParser","OCR","Perception","Decision"];
let cur = 0, tab = 0;
const listEl=document.getElementById('list'), tabsEl=document.getElementById('tabs'),
      bodyEl=document.getElementById('body'), hdEl=document.getElementById('hd');
function esc(s){return (s==null?'':(''+s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function tapStr(f){
  if(f.kind==='decision') return 'DECIDE: '+((f.output&&f.output.op)||'?').toUpperCase();
  return f.kind==='back'?'BACK':(f.x>=0?`tap(${f.x},${f.y})`:f.kind);
}
// WHAT THE FRAME IS, from the best source that has it: a parse if one was taken, else the
// state the RUN logged at that moment, else the action the trace recorded. Only the last of
// those is dimmed — the first two are real answers about where the bot was.
function whereStr(f){
  if(f.state) return esc(f.state);
  if(f.log_state) return esc(f.log_state) + (f.log_port?` <span style="color:#8ab">${esc(f.log_port)}</span>`:'');
  return `<span class="unparsed">${esc(f.kind||'frame')}</span>`;
}
function gapStr(f){
  if(f.gap_s===undefined) return '';
  const g=f.gap_s;
  const txt = g>=60 ? `+${Math.floor(g/60)}m${String(g%60).padStart(2,'0')}s` : `+${g}s`;
  // a long gap is the interesting one — a settle waited out, a voyage, a stall
  return `<span style="color:${g>=45?'#d98':'#777'}">${txt}</span>`;
}
function buildList(){
  listEl.innerHTML=F.map((f,i)=>`<div class="fitem${i==cur?' sel':''}${f.kind==='decision'?' dec':''}" onclick="sel(${i})">
    <span class="fidx">#${String(f.idx).padStart(3,'0')}</span>
    <span class="fstate"> ${whereStr(f)}</span> ${gapStr(f)}<br>
    <span class="fact">${esc(tapStr(f))}</span> <span style="color:#888">${esc(f.t||'')}</span></div>`).join('');
}
function buildTabs(){
  tabsEl.innerHTML=TABS.map((t,i)=>`<div class="tab${i==tab?' sel':''}" onclick="seltab(${i})">${i+1}. ${t}</div>`).join('');
}
function img(name){return `<img src="${name}">`;}
const TYPECOL={button:'#46c846',text:'#4696ff',icon:'#ffa528',commit:'#ffd700',checkbox:'#d246d2',image:'#969696'};
const NATW=2400,NATH=1080;   // capture resolution — omni box coords are in these pixels
function hlbox(i,on){
  for(const id of ['ob'+i,'or'+i]){const el=document.getElementById(id); if(el)el.classList.toggle('hl',!!on);}
}
// Boxes are drawn client-side (CSS) from the payload — no server-side render — so the
// frame + all boxes appear instantly; hover a box OR its table row to isolate one.
function omniView(f){
  const els=(f.omni||[]).filter(e=>e.x1!=null);
  const boxes=els.map((e,i)=>{
    const c=TYPECOL[e.element_type]||'#b9b9b9';
    return `<div class="obox" id="ob${i}" onmouseenter="hlbox(${i},1)" onmouseleave="hlbox(${i},0)" title="${esc(e.label||'')}"
      style="left:${100*e.x1/NATW}%;top:${100*e.y1/NATH}%;width:${100*(e.x2-e.x1)/NATW}%;height:${100*(e.y2-e.y1)/NATH}%;border-color:${c}"></div>`;
  }).join('');
  const tap=(f.x>=0)?`<div class="otap" style="left:${100*f.x/NATW}%;top:${100*f.y/NATH}%"></div>`:'';
  const rows=els.map((e,i)=>`<tr id="or${i}" style="cursor:pointer" onmouseenter="hlbox(${i},1)" onmouseleave="hlbox(${i},0)">
    <td style="color:${TYPECOL[e.element_type]||'#b9b9b9'}">${esc(e.element_type)}</td><td>${esc(e.label||'')}</td>
    <td>${e.cx},${e.cy}</td><td>${e.x2-e.x1}×${e.y2-e.y1}</td>
    <td>${(typeof e.confidence==='number')?e.confidence.toFixed(2):esc(e.confidence||'')}</td></tr>`).join('');
  // align-items:flex-start — WITHOUT it the flex row STRETCHES the image box to the tall
  // table's height, so the overlay (inset:0) maps boxes onto that stretched height and they
  // scatter far below the frame.  The inner wrapper shrink-wraps the image so the overlay's
  // coordinate space is EXACTLY the image.
  // THE FRAME STAYS PUT; ONLY THE ELEMENT TABLE SCROLLS (user, 2026-08-24).
  // Reading a long element list while the picture scrolls away defeats the point of the
  // report — you check a row against the frame constantly. `position:sticky` on the image
  // column pins it inside the scrolling body; `align-self:flex-start` keeps the flex row from
  // stretching it (the overlay is `inset:0`, so a stretched box scatters the drawn boxes
  // below the image).
  return `<div style="display:flex;gap:12px;flex-wrap:nowrap;align-items:flex-start">
    <div style="flex:1 1 55%;min-width:420px;position:sticky;top:0;align-self:flex-start">
     <div style="position:relative;display:block;line-height:0">
      <img src="${esc(f.frame)}" style="width:100%;display:block">
      <div class="ovl">${boxes}${tap}</div></div></div>
    <div style="flex:1 1 45%;min-width:340px"><b>${els.length} elements</b>
    <table><tr><th>type</th><th>label</th><th>cx,cy</th><th>w×h</th><th>conf</th></tr>${rows}</table></div></div>`;
}
function ocrView(f){
  let rows=(f.ocr||[]).map(t=>`<tr><td>${esc(t.text)}</td><td>${t.cx},${t.cy}</td><td>${t.conf}</td></tr>`).join('');
  return `<b>${(f.ocr||[]).length} tokens</b><table><tr><th>text</th><th>cx,cy</th><th>conf</th></tr>${rows}</table>`;
}
function percView(f){
  const q=f.qwen;
  let ql = q ? Object.entries(q).map(([k,v])=>`<div><span class="k">${esc(k)}:</span> <span class="v">${esc(JSON.stringify(v))}</span></div>`).join('')
             : '<i style="color:#888">Qwen not run (rebuild with --qwen)</i>';
  return `<div class="hd" style="margin:-10px -10px 10px">
     <div><span class="k">nav_state:</span> <span class="v">${esc(f.state)}</span></div>
     <div><span class="k">detail:</span> <span class="v">${esc(f.detail)}</span></div>
     <div><span class="k">action:</span> <span class="fact">${esc(tapStr(f))}</span>${f.label?(' — '+esc(f.label)):''}</div></div>
   <b>Qwen (L2.5)</b>${ql}`;
}
function kv(o){return Object.entries(o||{}).map(([k,v])=>
  `<tr><td class="k">${esc(k)}</td><td class="v">${esc(typeof v==='object'?JSON.stringify(v):v)}</td></tr>`).join('');}
function decisionView(f){
  if(f.kind==='decision' && f.output){
    return `<h3 style="margin:2px 0">Decision — <span class="v">${esc(f.model)}</span></h3>
      <div style="display:flex;gap:24px;flex-wrap:wrap">
        <div><b>INPUT (observed state)</b><table>${kv(f.inputs)}</table></div>
        <div><b>OUTPUT (chosen action)</b><table>${kv(f.output)}</table></div></div>
      <p><b>Reason:</b> <span class="v">${esc((f.output||{}).why||'')}</span></p>`;
  }
  let last=null; for(let i=cur;i>=0;i--){ if(F[i].kind==='decision'){last=F[i];break;} }
  if(last) return `<i style="color:#888">No decision at this frame. Most recent (frame #${last.idx}):</i>
     <div style="margin-top:6px"><b class="k">${esc((last.output||{}).op||'').toUpperCase()}</b> — ${esc((last.output||{}).why||'')}
     <span style="color:#888">(${esc(last.model)})</span></div>`;
  return '<i style="color:#888">No decision recorded (session predates the Decision tab, or a non-smart_trade run).</i>';
}
function render(){
  const src=F[cur].perception||'rebuilt';
  // WHOSE READING IS THIS? `live` is what the bot actually saw and acted on; `rebuilt` is a
  // second look taken while making this report, which can succeed where the run failed and
  // must never be mistaken for evidence of what the bot had.
  const srcTag=
     (src==='live')    ? `<span title="the perception the bot acted on" style="color:#7c7">● live</span>`
   : (src==='reading') ? `<span style="color:#cc8">… reading</span>`
   : (src==='unread')  ? (SERVED
        ? `<span style="color:#888">○ unread</span> <button onclick="readFrame(cur)" title="parse THIS frame now — a few seconds" style="font:inherit;padding:1px 8px;margin-left:4px;cursor:pointer">Parse</button>`
        : `<span title="nobody has read this frame; serve the report to parse it" style="color:#888">○ unread</span>`)
   :                     `<span title="re-perceived for this report — NOT what the bot saw" style="color:#c96">○ rebuilt</span>`;
  hdEl.innerHTML=`<b>#${String(F[cur].idx).padStart(3,'0')}</b> — <span class="fstate">${whereStr(F[cur])}</span> ${gapStr(F[cur])}
    — <span class="fact">${esc(tapStr(F[cur]))}</span> ${srcTag} <span style="color:#888">(${cur+1}/${F.length})</span>`;
  const f=F[cur];
  bodyEl.innerHTML=[omniView,ocrView,percView,decisionView][tab](f);
  buildList();buildTabs();
  const s=document.querySelector('.fitem.sel'); if(s)s.scrollIntoView({block:'nearest'});
}
// PARSING IS ASKED FOR, NEVER ASSUMED (user, 2026-09-03).
//
// Opening a frame used to start a read. Most looks at a report are "what did the bot SEE" —
// the picture and the line from the log — and a parse takes seconds of CPU that competes with
// serving the very image you are waiting for. So a frame arrives with what the run recorded
// and nothing else, and the Parse button is the request.
//
// The debounce that used to tame arrow-key reads is gone with it: nothing fires from
// navigation any more, so there is nothing to debounce.
const SERVED = location.protocol.startsWith('http');
async function readFrame(i){
  const f=F[i];
  if(f.perception!=='unread') return;
  f.perception='reading'; if(i===cur) render();
  try{
    const r=await fetch('parse?frame='+encodeURIComponent(f.frame));
    Object.assign(f, await r.json());
    f.perception='rebuilt';
  }catch(e){ f.perception='unread'; f.detail='read failed: '+e; }
  if(i===cur) render();            // a stale answer must not redraw someone else's frame
}
function sel(i){cur=Math.max(0,Math.min(F.length-1,i));render();}
function seltab(i){tab=i;render();}
document.onkeydown=e=>{
  if(e.key==='ArrowRight'||e.key==='ArrowDown')sel(cur+1);
  else if(e.key==='ArrowLeft'||e.key==='ArrowUp')sel(cur-1);
  else if(e.key>='1'&&e.key<='4')seltab(+e.key-1);
};
render();
</script></body></html>"""


def _read_one(session: Path, frame_name: str, qwen: bool = False) -> dict:
    """Run the components for ONE frame and cache them. This is the on-demand read."""
    fp = session / frame_name
    if not fp.exists():
        return {"error": f"no such frame: {frame_name}"}
    cache_dir = session / "viewer_data"
    cache_dir.mkdir(exist_ok=True)
    cache = cache_dir / f"{frame_name.replace('.png', '')}.json"
    if cache.exists():
        data = json.loads(cache.read_text())
    else:
        data = _run_components(Image.open(fp).convert("RGB"), qwen=qwen)
        cache.write_text(json.dumps(data))
    if not data.get("state") and data.get("detail"):
        data["state"] = data["detail"].split(":")[0].split(" ")[0]
    data["perception"] = "rebuilt"
    return data


def serve(session: Path, port: int = 0) -> None:
    """Serve the report and read a frame WHEN IT IS OPENED.

    The build no longer perceives anything it was not given, which makes it quick and leaves
    most frames unread — the right trade, because a report is a hundred frames and you look at
    two. Opening one is the request, and this answers it: about five seconds, once, for the
    frame you actually care about, cached in `viewer_data/` afterwards.

    Bound to localhost. It runs models and reads the session directory, so it is a tool for
    the machine that recorded the run, not a service.
    """
    import http.server
    import threading
    import urllib.parse

    # ONE READ AT A TIME, BUT IT MUST NOT BLOCK THE PAGE. The server was single-threaded, so
    # a five-second parse also stalled every image and asset request behind it and the report
    # appeared to load frame by frame. Threaded now — while the lock serialises the MODELS,
    # which share one GPU and would only thrash if run in parallel.
    read_lock = threading.Lock()

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(session), **kw)

        def log_message(self, fmt, *a):            # one line per read, not per asset
            pass

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path.rstrip("/") != "/parse":
                return super().do_GET()
            name = (urllib.parse.parse_qs(parsed.query).get("frame") or [""])[0]
            # The frame name comes from the page, but treat it as untrusted anyway: only a
            # plain file inside the session may be read.
            if "/" in name or "\\" in name or not name.endswith(".png"):
                body = json.dumps({"error": "bad frame name"}).encode()
            else:
                print(f"  reading {name} on request…")
                with read_lock:
                    body = json.dumps(_read_one(session, name)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler) as srv:
        actual = srv.server_address[1]
        print(f"\n  Serving {session} at http://127.0.0.1:{actual}/viewer.html")
        print("  Frames are read when you open them. Ctrl-C to stop.\n")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n  stopped")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("session", type=Path)
    ap.add_argument("--qwen", action="store_true", help="also run Qwen (slow, ~5-14s/frame)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--rebuild", action="store_true", help="ignore cached component data")
    ap.add_argument("--eager", action="store_true",
                    help="parse every frame up front (the old behaviour; ~5s a frame)")
    ap.add_argument("--serve", nargs="?", const=0, type=int, metavar="PORT",
                    help="serve the report; each page gets a Parse button")
    args = ap.parse_args()
    if not (args.session / "actions.jsonl").exists():
        raise SystemExit(f"no actions.jsonl in {args.session}")
    print(f"Building viewer for {args.session} (qwen={args.qwen})…")
    eager = args.eager or args.qwen          # --qwen only means anything if it runs
    frames = build(args.session, args.qwen, args.limit, args.rebuild, eager=eager)
    _annotate_from_log(args.session, frames)
    out = _write_html(args.session, frames)
    unread = sum(1 for f in frames if f.get("perception") == "unread")
    live = sum(1 for f in frames if f.get("perception") == "live")
    print(f"\nWrote {out}  ({len(frames)} frames: {live} live, {unread} unread)")
    if args.serve is None:
        print(f"  open: file://{out.resolve()}")
        if unread:
            print(f"  {unread} frame(s) carry no perception — they show what the run "
                  "recorded and nothing more. Serve it (--serve) for a Parse button on "
                  "each frame, or --eager to read them all now.")
    else:
        serve(args.session, args.serve)


if __name__ == "__main__":
    main()
