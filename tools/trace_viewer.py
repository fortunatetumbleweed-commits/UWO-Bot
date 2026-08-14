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
from pathlib import Path

from PIL import Image, ImageDraw

_TYPE_COLORS = {
    "button": (70, 200, 70), "text": (70, 150, 255), "icon": (255, 165, 40),
    "commit": (255, 215, 0), "checkbox": (210, 70, 210), "image": (150, 150, 150),
}


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


def _render_omni(frame: Image.Image, omni: list, out: Path, tap=None) -> None:
    """OmniParser boxes + (combined) the tap crosshair, on one image."""
    img = frame.convert("RGB").copy()
    d = ImageDraw.Draw(img)
    for e in omni:
        if "x1" not in e:
            continue
        col = _TYPE_COLORS.get(e.get("element_type", "?"), (185, 185, 185))
        d.rectangle([e["x1"], e["y1"], e["x2"], e["y2"]], outline=col, width=3)
        lbl = (e.get("label") or "")[:26]
        if lbl:
            d.text((e["x1"] + 3, max(0, e["y1"] - 14)), lbl, fill=col)
    if tap and tap[0] >= 0:                     # the tap crosshair (was the Screen tab)
        x, y, r = tap[0], tap[1], 34
        d.ellipse([x - r, y - r, x + r, y + r], outline=(255, 0, 0), width=6)
        d.line([x - r, y, x + r, y], fill=(255, 0, 0), width=3)
        d.line([x, y - r, x, y + r], fill=(255, 0, 0), width=3)
    img.save(out)


def build(session: Path, qwen: bool, limit: int, rebuild: bool) -> list:
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
        cache = cache_dir / f"{a['frame'].replace('.png', '')}.json"
        need_qwen = qwen
        omni_out = session / a["frame"].replace(".png", "_omni.png")
        tap = (a.get("x", -1), a.get("y", -1))
        if cache.exists() and not rebuild:
            data = json.loads(cache.read_text())
            if not data.get("state") and data.get("detail"):      # backfill derived state
                data["state"] = data["detail"].split(":")[0].split(" ")[0]
            if not need_qwen or data.get("qwen") is not None:
                # re-render the combined overlay (cheap draw — no OmniParser) so the
                # tap crosshair is baked into the OmniParser image
                _render_omni(Image.open(fp).convert("RGB"), data.get("omni", []), omni_out, tap=tap)
                frames.append({**a, **data})
                print(f"  [{i+1}/{len(actions)}] {a['frame']} (cached)")
                continue
        print(f"  [{i+1}/{len(actions)}] {a['frame']} — running components…")
        frame = Image.open(fp).convert("RGB")
        data = _run_components(frame, qwen=need_qwen)
        _render_omni(frame, data["omni"], omni_out, tap=tap)
        cache.write_text(json.dumps(data))
        frames.append({**a, **data})
    return frames


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
 .fitem.dec{border-left:4px solid #e9a}
 .fidx{color:#7bd;font-weight:bold}.fstate{color:#9c9}.fact{color:#e9a}
 #main{flex:1;display:flex;flex-direction:column;overflow:hidden}
 #tabs{display:flex;border-bottom:1px solid #444;background:#2b2b2b}
 .tab{padding:8px 16px;cursor:pointer;border-right:1px solid #444}
 .tab:hover{background:#3a3a3a}.tab.sel{background:#0a4a6e;color:#fff}
 #body{flex:1;overflow:auto;padding:10px}
 img{max-width:100%;border:1px solid #444}
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
function buildList(){
  listEl.innerHTML=F.map((f,i)=>`<div class="fitem${i==cur?' sel':''}${f.kind==='decision'?' dec':''}" onclick="sel(${i})">
    <span class="fidx">#${String(f.idx).padStart(3,'0')}</span>
    <span class="fstate"> ${esc((f.state||'?'))}</span><br>
    <span class="fact">${esc(tapStr(f))}</span> <span style="color:#888">${esc(f.t||'')}</span></div>`).join('');
}
function buildTabs(){
  tabsEl.innerHTML=TABS.map((t,i)=>`<div class="tab${i==tab?' sel':''}" onclick="seltab(${i})">${i+1}. ${t}</div>`).join('');
}
function img(name){return `<img src="${name}">`;}
function screenView(f){
  const m=f.frame.replace('.png', f.kind==='back'?'.png':'_marked.png');
  return img(m);
}
function omniView(f){
  const om=f.frame.replace('.png','_omni.png');
  let rows=(f.omni||[]).filter(e=>e.x1!=null).map(e=>`<tr><td>${esc(e.element_type)}</td><td>${esc(e.label||'')}</td>
    <td>${e.cx},${e.cy}</td><td>${e.x2-e.x1}×${e.y2-e.y1}</td><td>${(e.confidence||0).toFixed?e.confidence.toFixed(2):e.confidence||''}</td></tr>`).join('');
  return `<div style="display:flex;gap:12px;flex-wrap:wrap">
    <div style="flex:1;min-width:420px">${img(om)}</div>
    <div style="flex:1;min-width:340px"><b>${(f.omni||[]).length} elements</b>
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
  hdEl.innerHTML=`<b>#${String(F[cur].idx).padStart(3,'0')}</b> — <span class="fstate">${esc(F[cur].state||'?')}</span>
    — <span class="fact">${esc(tapStr(F[cur]))}</span> <span style="color:#888">(${cur+1}/${F.length})</span>`;
  const f=F[cur];
  bodyEl.innerHTML=[omniView,ocrView,percView,decisionView][tab](f);
  buildList();buildTabs();
  const s=document.querySelector('.fitem.sel'); if(s)s.scrollIntoView({block:'nearest'});
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("session", type=Path)
    ap.add_argument("--qwen", action="store_true", help="also run Qwen (slow, ~5-14s/frame)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--rebuild", action="store_true", help="ignore cached component data")
    args = ap.parse_args()
    if not (args.session / "actions.jsonl").exists():
        raise SystemExit(f"no actions.jsonl in {args.session}")
    print(f"Building viewer for {args.session} (qwen={args.qwen})…")
    frames = build(args.session, args.qwen, args.limit, args.rebuild)
    out = _write_html(args.session, frames)
    print(f"\nWrote {out}  ({len(frames)} frames)\n  open: file://{out.resolve()}")


if __name__ == "__main__":
    main()
