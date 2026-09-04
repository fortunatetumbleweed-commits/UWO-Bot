"""Diagnose the village Trade List scroll+read, tick by tick — as an HTML report.

    python -m tools.village_scroll_report "Cheyenne Village"

One section per tick, showing the LIST AREA only:

  * the cropped frame with overlays — the viewport bounds, every detected row tile marked
    GOOD (flush-left, taller) or MATERIAL (indented), and WHOLE vs CLIPPED by the fold;
  * the scroll applied after that tick: distance in px, direction, and why;
  * what was PERCEIVED — the goods and materials parsed from that screen;
  * what it was LOOKING FOR — the next thing expected below what it just read;
  * what is EXPECTED BUT MISSING — names visible in the list that did NOT reach the parse.
    These are the silent losses: a tile whose quantity never read, a group whose good name
    is clipped, a name with no row of its own.

Read-only apart from the scrolling: it never writes the KB.
"""

from __future__ import annotations

import re

import base64
import io
import os
import sys
import time

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from memory.logger import setup_logging

setup_logging()

from loguru import logger

OUT_ROOT = "/tmp/village_scroll_report"
OUT_DIR = OUT_ROOT                      # set per village in run()
# EVERY RAW FRAME IS KEPT. The report's numbers are all derived from the frames, so once they
# are on disk the whole report can be rebuilt — different overlays, different explanations —
# without touching the device again. Re-running a live scroll just to reword a caption is what
# this avoids.
FRAME_DIR = f"{OUT_DIR}/frames"
# The crop shown in the report. Element boxes are published to the page as PERCENTAGES of
# this rectangle, so clicking a row in the OmniParser list can draw its box over the image
# whatever width the page renders it at.
CROP_TOP, CROP_BOT = 100, 1040
MAX_TICKS = 12
LIST_X0, LIST_X1 = 1660, 2400


def _reset_list_position(capture_screen, ui, open_world_map, _is_on_world_map) -> bool:
    """Close the world map and reopen it, so the trade list starts at the TOP.

    THE GAME REMEMBERS WHERE EACH LIST WAS LEFT (user, 2026-08-25), and reopening the village
    panel is not enough to clear it — only leaving the world map and coming back does. Twelve
    ticks of the Cheyenne report were captured against a list that opened on its final row,
    which read as a dead scroll when it was really a stale position.
    """
    for attempt in range(5):
        if not _is_on_world_map(capture_screen()):
            break
        ui.back(why="close the world map to clear the remembered list position")
        time.sleep(1.0)
    else:
        logger.warning("[report] could not leave the world map — list may open where it "
                       "was last left")
        return False
    logger.info("[report] left the world map; reopening it fresh")
    return open_world_map()


from vision.list_position import (bar_position, content_shift as _measure_shift,
                                  scrollbar_thumb as _scrollbar)


def _b64(img) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def run(village: str, *, replay: bool = False) -> int:
    from PIL import ImageDraw
    from capture.adb_capture import capture_screen
    from vision.omniparser import parse_fast_cached, parse_raw
    from actions import ui
    from actions.sail_actions import open_world_map, _try_village_search
    from actions.village_check import parse_trade_list, _PANEL_X_MIN, _CATEGORY_WORDS
    import tools.learn_village_barter as L

    global OUT_DIR, FRAME_DIR
    slug = re.sub(r"[^a-z0-9]+", "_", village.lower()).strip("_")
    OUT_DIR = f"{OUT_ROOT}/{slug}"
    FRAME_DIR = f"{OUT_DIR}/frames"
    os.makedirs(FRAME_DIR, exist_ok=True)

    saved = []
    if replay:
        from PIL import Image
        import glob
        saved = [Image.open(f).convert("RGB")
                 for f in sorted(glob.glob(f"{FRAME_DIR}/tick_*.png"))]
        if not saved:
            logger.error(f"no saved frames in {FRAME_DIR} — run live once first")
            return 1
        logger.info(f"[report] replaying {len(saved)} saved frame(s) — the device is not "
                    "touched and nothing is scrolled")

    if not replay and not open_world_map():
        logger.error("could not open the world map")
        return 1
    # START FROM THE TOP OF THE LIST, not where the last run left it.
    if not replay:
        from actions.sail_actions import _is_on_world_map
        _reset_list_position(capture_screen, ui, open_world_map, _is_on_world_map)
    if not replay and not _try_village_search(village):
        logger.error(f"could not find {village!r}")
        return 1
    if not replay:
        ui.settle("screen", why="village selected")
    # The Village Info panel animates in; one look can land before the tabs render.
    for attempt in range(0 if replay else 4):
        frame = capture_screen()
        if ui.tap_text(frame, "barter", x_min=_PANEL_X_MIN, dwell="dialog",
                       why="Village Info -> Barter tab"):
            break
        logger.info(f"[report] Barter tab not up yet ({attempt + 2}/4)")
        time.sleep(1.5)
    else:
        if not replay:
            logger.error("could not open the Barter tab")
            return 1

    ticks, seen_goods, prev_frame = [], {}, None
    for i in range(len(saved) if replay else MAX_TICKS):
        if replay:
            frame = saved[i]
        else:
            frame = capture_screen()
            frame.save(f"{FRAME_DIR}/tick_{i + 1:02d}.png")
        els = list(parse_fast_cached(frame))
        vp = L._list_viewport(els)
        els2 = L._with_recovered_badges(frame, els, vp)
        recovered = [(getattr(e, "label", "") or "") for e in els2 if e not in els]
        tiles = L._row_tiles(els2)
        whole_els, clipped = L._complete_only(els2, vp)
        trades = parse_trade_list(whole_els if vp else els2)

        names_on_screen = [(getattr(e, "label", "") or "").strip() for e in els2
                           if e.x1 > 1750 and (getattr(e, "label", "") or "").strip()
                           and vp and e.y1 >= vp[0] and e.y2 <= vp[1]]
        parsed = set()
        for t in trades:
            parsed.add((t.good or "").strip())
            parsed.update(t.materials)
        # A QUANTITY IS NOT A NAME. `parsed` holds row NAMES, so a bare number like Corn's
        # "162" never appears in it and was being reported as an unparsed loss while its row
        # was parsed perfectly. Numbers, category chips and panel chrome are all excluded —
        # what remains is a genuine loss: a NAME on screen that reached no recipe.
        missing = [n for n in names_on_screen
                   if n not in parsed
                   and n.lower() not in _CATEGORY_WORDS
                   and not re.fullmatch(r"[\d,.]+", n)
                   and len(n) > 1]

        bar = _scrollbar(frame, vp)
        moved = None
        if prev_frame is not None:
            moved = _measure_shift(prev_frame, frame)
        prev_frame = frame.copy()

        # WHERE THE SCROLL IS AIMED — computed BEFORE the overlay is drawn, so the frame
        # can show the anchor row rather than only describing it in prose.
        dy = L.SCROLL_DY
        why = (f"no row qualified as an anchor, so the fixed fallback step "
               f"{abs(L.SCROLL_DY)}px was used")
        target = "&mdash;"
        expect = "unknown &mdash; a blind step"
        anchor_y = None
        if vp:
            whole_tiles = [t for t in tiles if t.y1 >= vp[0] and t.y2 <= vp[1]]
            goods_now = [t for t in whole_tiles if L._is_good_tile(t, tiles)]
            anchor = goods_now[-1] if goods_now else (whole_tiles[-1] if whole_tiles else None)
            if anchor is not None and anchor.y1 > vp[0] + 20:
                raw = anchor.y1 - vp[0]
                dy = max(min(-raw, -40), -700)
                kind = "GOOD" if goods_now else "material"
                lab = (getattr(anchor, "label", "") or "?").strip()
                anchor_y = anchor.y1
                target = (f"the last complete <b>{kind}</b> row (<b>{lab}</b>, currently at "
                          f"y={anchor.y1})")
                why = (f"distance = that row's y ({anchor.y1}) &minus; the list top "
                       f"({vp[0]}) = <b>{raw}px</b>"
                       + ("" if raw == abs(dy) else f", clamped to {abs(dy)}px"))
                expect = (f"<b>{lab}</b> to sit at the top of the list, with the rows that "
                          "follow it (its materials, then the next good) below &mdash; the "
                          "<span style='color:#f0d'>magenta line</span> on the frame is the "
                          "edge that should land on the blue <i>list top</i>")
            elif clipped is not None:
                raw = clipped.y1 - vp[0]
                dy = max(min(-raw, -40), -700)
                lab = (getattr(clipped, "label", "") or "?").strip()
                anchor_y = clipped.y1
                target = f"the clipped row (<b>{lab}</b>, y={clipped.y1}-{clipped.y2})"
                why = (f"distance = clipped row's y ({clipped.y1}) &minus; list top "
                       f"({vp[0]}) = <b>{raw}px</b>"
                       + ("" if raw == abs(dy) else f", clamped to {abs(dy)}px"))
                expect = (f"<b>{lab}</b> fully in view at the top &mdash; the "
                          "<span style='color:#f0d'>magenta line</span> should land on the "
                          "blue <i>list top</i>")
            elif anchor is not None:
                target = (f"nothing &mdash; the last complete row (<b>"
                          f"{(getattr(anchor,'label','') or '?').strip()}</b>) is already at "
                          f"the top (y={anchor.y1}, list top {vp[0]})")
                why = (f"no anchor is far enough down to scroll to, so the fallback "
                       f"{abs(L.SCROLL_DY)}px step is used instead")
                expect = "roughly one and a half rows of new content"

        shot = frame.copy()
        d = ImageDraw.Draw(shot)
        if vp:
            for y, tag in ((vp[0], "list top"), (vp[1], "list bottom")):
                d.line([(LIST_X0, y), (LIST_X1 - 10, y)], fill=(0, 200, 255), width=5)
                d.text((LIST_X0 + 8, y + 6), tag, fill=(0, 200, 255))
        for t in tiles:
            good = L._is_good_tile(t, tiles)
            wholly = bool(vp and t.y1 >= vp[0] and t.y2 <= vp[1])
            col = (255, 60, 60) if not wholly else ((0, 220, 80) if good else (255, 190, 0))
            d.rectangle([t.x1, t.y1, t.x2, t.y2], outline=col, width=6)
            lab = (getattr(t, "label", "") or "").strip()
            d.text((t.x1 - 250, t.y1 + 8),
                   f"{'GOOD' if good else 'material'} {lab}{'' if wholly else '  CLIPPED'}",
                   fill=col)
        # THE ANCHOR, DRAWN. "Expect X at the top afterwards" is only checkable if the frame
        # says which edge X is; the arrow is the distance the gesture is asking the list to
        # travel, from the anchor's top edge up to the list top.
        if anchor_y is not None and vp:
            mag = (255, 0, 220)
            d.line([(LIST_X0, anchor_y), (LIST_X1 - 10, anchor_y)], fill=mag, width=5)
            d.text((LIST_X0 + 8, anchor_y - 30),
                   "this edge should end up at the list top", fill=mag)
            ax = LIST_X0 + 26
            d.line([(ax, anchor_y), (ax, vp[0])], fill=mag, width=4)
            for ddx in (-13, 13):
                d.line([(ax, vp[0]), (ax + ddx, vp[0] + 20)], fill=mag, width=4)
        if bar:
            d.rectangle([2205, bar[0], 2245, bar[1]], outline=(120, 160, 255), width=5)
            d.text((2100, bar[0] - 30), "thumb", fill=(120, 160, 255))
        crop = shot.crop((LIST_X0, CROP_TOP, LIST_X1, CROP_BOT))

        # THE RAW PARSER OUTPUT, and labelled as such. This panel used to show `els2` — the
        # parser's output PLUS synthetic recovered badges — and called it "OmniParser
        # output", which overstates it twice over: the badges are ours, not the parser's, and
        # `parse_fast_cached` has already dropped YOLO boxes under conf 0.3 and OCR text
        # under conf 0.4 or 2 characters. A row can be missing from this list because the
        # detector never saw it (Corn's thumbnail) or because a threshold ate it, and those
        # are different problems, so the source and confidence of every box is shown.
        cw, chh = LIST_X1 - LIST_X0, CROP_BOT - CROP_TOP
        tile_ids = {id(t) for t in tiles}
        recovered_els = [e for e in els2 if e not in els]
        raw = list(parse_raw(frame)) + recovered_els
        omni = []
        for e in sorted(raw, key=lambda e: (e.y1, e.x1)):
            if e.x2 < LIST_X0 or e.x1 > LIST_X1 or e.y2 < CROP_TOP or e.y1 > CROP_BOT:
                continue
            w, h = e.x2 - e.x1, e.y2 - e.y1
            lab = (getattr(e, "label", "") or "").strip()
            conf = float(getattr(e, "confidence", 0.0) or 0.0)
            etype = getattr(e, "element_type", "") or ""

            # WHY A BOX IS OR IS NOT IN THE PARSE. Corn's thumbnail was proposed at 0.251
            # and dropped for being 0.049 under the icon cutoff, which is invisible in the
            # filtered output — the row simply had no tile and nothing said why.
            if e in recovered_els:
                cls, src, status = "r", "RECOVERED by us (not the parser)", "kept"
            elif etype == "text":
                ok = conf >= 0.40 and len(lab) >= 2
                cls, src = ("o" if ok else "d"), "OCR"
                status = "kept" if ok else (
                    f"DROPPED by the parser: OCR conf {conf:.2f} &lt; 0.40"
                    if conf < 0.40 else "DROPPED by the parser: label shorter than 2 chars")
            else:
                ok = conf >= 0.30
                cls, src = ("o" if ok else "d"), "YOLO"
                status = "kept" if ok else \
                    f"<b>DROPPED by the parser: YOLO conf {conf:.3f} &lt; 0.30</b>"

            role = status
            if status == "kept" and cls != "r":
                match = next((t for t in tiles if abs(t.x1 - e.x1) <= 3
                              and abs(t.y1 - e.y1) <= 3), None)
                if match is not None:
                    role = ("GOOD tile" if L._is_good_tile(match, tiles) else "material tile")
                    cls = "g" if role.startswith("GOOD") else "m"
                elif L._TILE_X_MIN < e.x1 < L._TILE_X_MAX and etype != "text":
                    fails = []
                    if not (L._TILE_MIN_H <= h <= L._TILE_MAX_H):
                        fails.append(f"h {h} outside {L._TILE_MIN_H}-{L._TILE_MAX_H}")
                    if w > L._TILE_MAX_W:
                        fails.append(f"w {w} &gt; {L._TILE_MAX_W}")
                    if fails:
                        role = "kept, but not a row tile: " + "; ".join(fails)
            omni.append({"label": lab, "type": etype, "src": src, "conf": conf,
                         "box": f"({e.x1},{e.y1})-({e.x2},{e.y2})", "wh": f"{w}x{h}",
                         "role": role, "cls": cls,
                         "x": 100.0 * (e.x1 - LIST_X0) / cw,
                         "y": 100.0 * (e.y1 - CROP_TOP) / chh,
                         "w": 100.0 * w / cw, "h": 100.0 * h / chh})

        looking_for = "the next row below the last one read"
        if trades:
            last = trades[-1]
            looking_for = (f"more materials for '{last.good}'" if (last.good or "").strip()
                           else "the good that owns these leading materials")

        ticks.append({"i": i + 1, "img": _b64(crop), "viewport": vp,
                      "tiles": [(("GOOD" if L._is_good_tile(t, tiles) else "material"),
                                 (getattr(t, "label", "") or "").strip(), t.y1, t.y2,
                                 bool(vp and t.y1 >= vp[0] and t.y2 <= vp[1])) for t in tiles],
                      "recovered": recovered,
                      "perceived": [(t.good, t.obtain, dict(t.materials)) for t in trades],
                      "missing": missing, "looking_for": looking_for, "dy": dy, "why": why,
                      "target": target, "expect": expect, "moved": moved, "omni": omni, "bar": bar,
                      "asked": abs(ticks[-1]["dy"]) if ticks else None})
        for t in trades:
            if (t.good or "").strip():
                seen_goods.setdefault(t.good, {}).update(t.materials)

        if not replay:
            ui.scroll(L.SCROLL_X, L.SCROLL_Y, dy, why=f"report tick {i + 1}")
            time.sleep(1.0)

    _write_html(village, ticks, seen_goods)
    return 0


def _write_html(village, ticks, seen_goods):
    rows = []
    for t in ticks:
        if t.get("moved") is None:
            moved_html = ("<p><span class=k>measured movement</span> "
                          "&mdash; <span class=why>first tick, nothing to compare against"
                          "</span></p>")
        else:
            got, err, zero = t["moved"]
            asked = t.get("asked")
            # Convincing = the shift explains the frame far better than "nothing moved".
            if got != 0 and not (err < 0.6 * zero):
                got = None
            if got is None:
                verdict = ("<b>unmeasurable</b> &mdash; no vertical alignment matched "
                           f"(best {err:.1f} vs {zero:.1f} at zero shift)")
            elif got == 0:
                verdict = ("<b style='color:#f55'>the list did NOT move</b> &mdash; the "
                           f"previous tick asked for {asked}px and got nothing")
            else:
                pct = f"{100.0 * abs(got) / asked:.0f}%" if asked else "?"
                verdict = f"<b>{abs(got)}px</b> delivered against {asked}px asked ({pct})"
            if got:
                verdict += (" <span class=why>(content moved "
                            + ("UP, list scrolled down" if got > 0 else
                               "DOWN, list scrolled up") + ")</span>")
            moved_html = (f"<p><span class=k>measured movement</span> {verdict}"
                          f"<br><span class=why>frame-to-frame alignment: residual "
                          f"{err:.2f} at this shift vs {zero:.2f} at zero</span></p>")
        bar, vp = t.get("bar"), t.get("viewport")
        if bar and vp:
            track = (vp[1] + 12) - (vp[0] - 12)
            th = bar[1] - bar[0] + 1
            room_above, room_below = bar[0] - (vp[0] - 12), (vp[1] + 12) - bar[1]
            frac = th / track
            content = int(track / frac) if frac else 0
            at_start, at_end = bar_position(bar, vp)
            where = ("<b style='color:#f55'>at the END of the list</b> &mdash; no down-scroll "
                     "can move it" if at_end else
                     ("<b>at the START of the list</b>" if at_start else "mid-list"))
            pos_html = (
                f"<p><span class=k>list position</span> {where}"
                f"<br><span class=why>thumb y={bar[0]}-{bar[1]} ({th}px) in a {track}px "
                f"track &mdash; the thumb covers {100 * frac:.0f}% of the list, so the "
                f"content is about {content}px tall (~{content / max(1, track):.1f} screens); "
                f"{room_above}px of track above it, {room_below}px below</span></p>")
        else:
            pos_html = ("<p><span class=k>list position</span> &mdash; "
                        "<span class=why>no scrollbar found</span></p>")
        tiles = "".join(
            f"<div class='{'g' if k == 'GOOD' else 'm'}{'' if whole else ' clip'}'>"
            f"{k} <b>{lab or '&mdash;'}</b> y={y1}-{y2}{'' if whole else '  CLIPPED'}</div>"
            for k, lab, y1, y2, whole in t["tiles"]) or "<i>none detected</i>"
        perceived = "".join(
            f"<div><b>{g or '(unnamed group)'}</b> obtain {o} &larr; {mats or '{}'}</div>"
            for g, o, mats in t["perceived"]) or "<i>nothing parsed</i>"
        missing = ", ".join(t["missing"]) or "<i>nothing</i>"
        recovered = ", ".join(t["recovered"]) or "<i>none</i>"
        direction = "UP (towards the list start)" if t["dy"] > 0 else "DOWN (further into the list)"
        els_html = "".join(
            f"<div class='el {o['cls']}' data-x='{o['x']:.3f}' data-y='{o['y']:.3f}' "
            f"data-w='{o['w']:.3f}' data-h='{o['h']:.3f}'>"
            f"<b>{o['label'] or '&mdash;'}</b> <span class=ty>{o['src']} {o['type']}"
            f" &middot; conf {o['conf']:.3f}</span>"
            f"<br><span class=bx>{o['box']} &nbsp;{o['wh']}</span>"
            f"<br><span class=role>{o['role']}</span>"
            + ("" if o['src'] not in ("RECOVERED by us (not the parser)",) else
               f"<br><span class=recov>{o['src']}</span>")
            + "</div>"
            for o in t["omni"]) or "<i>no elements in this crop</i>"
        rows.append(f"""
    <section><h2>Tick {t['i']}</h2>
      <div class=row>
      <div class=framewrap><div class=fr>
        <img src="data:image/png;base64,{t['img']}"><div class=hl></div>
      </div><div class=hint>click any element on the right to box it here</div></div>
      <div class=info>
        <p><span class=k>viewport</span> {t['viewport']}</p>
        <p><span class=k>row tiles</span></p>{tiles}
        <p><span class=k>badges recovered by OCR</span> {recovered}</p>
        <p><span class=k>perceived</span></p>{perceived}
        <p><span class=k>looking for</span> {t['looking_for']}</p>
        <p><span class=k>visible but NOT parsed</span> <span class=miss>{missing}</span></p>
        {pos_html}
        {moved_html}
        <p><span class=k>scroll target</span> {t['target']}</p>
        <p><span class=k>scroll applied</span> <b>{abs(t['dy'])}px {direction}</b><br>
           <span class=why>{t['why']}</span></p>
        <p><span class=k>expected after the scroll</span> {t['expect']}</p>
        <p><span class=k>RAW detector output</span> {len(t['omni'])} boxes overlapping this
           crop, {sum(1 for o in t['omni'] if 'DROPPED' in o['role'])} of them discarded
           before the parse
           <br><span class=why>everything YOLO and EasyOCR proposed, with YOLO's own cutoff
           lowered to 0.01 so nothing is hidden. Dimmed rows never reach the bot: the parser
           drops YOLO under conf 0.30 and OCR under 0.40 or 2 characters. Entries marked
           <span class=recov>RECOVERED</span> are ours, not the detector's.</span></p>
        <div class=els>{els_html}</div>
      </div></div></section>""")
    summary = "".join(f"<li><b>{g}</b> &larr; {m}</li>" for g, m in sorted(seen_goods.items()))
    html = f"""<!doctype html><meta charset="utf-8">
<title>{village} - trade list scroll report</title>
<style>
 body{{background:#1e1e1e;color:#ddd;font:14px/1.55 -apple-system,sans-serif;margin:22px}}
 h1{{font-size:20px}} h2{{font-size:16px;color:#7cc;margin:0 0 8px}}
 section{{border-top:1px solid #444;padding:16px 0}}
 .row{{display:flex;gap:20px;align-items:flex-start}}
 .framewrap{{position:sticky;top:12px;flex:0 0 auto;align-self:flex-start}}
 .fr{{position:relative;width:540px;line-height:0}}
 .fr img{{width:540px;border:1px solid #555;display:block}}
 .hl{{position:absolute;display:none;border:3px solid #fff;
      box-shadow:0 0 0 2px #000,0 0 14px #fff;pointer-events:none;border-radius:2px}}
 .hint{{color:#777;font-size:12px;font-style:italic;line-height:1.4;margin-top:6px}}
 .els{{max-height:560px;overflow-y:auto;border:1px solid #444;border-radius:4px;
       padding:6px;background:#181818;margin-top:6px}}
 .el{{padding:6px 8px;border-bottom:1px solid #2c2c2c;cursor:pointer;font-size:12.5px}}
 .el:hover{{background:#252525}} .el.sel{{background:#2f3a46;outline:1px solid #6af}}
 .el .ty{{color:#789}} .el .bx{{color:#888;font-family:ui-monospace,monospace}}
 .el .role{{color:#777;font-style:italic}} .el .recov{{color:#c9f;font-weight:600}}
 .el.d{{opacity:.62}} .el.d .role{{color:#e77;font-style:normal}}
 .el.r{{background:#241f2e}}
 .el.g .role{{color:#5d5;font-style:normal}} .el.m .role{{color:#fb0;font-style:normal}}
 .info{{flex:1;min-width:320px}} .k{{color:#8ab;font-weight:600}}
 .g{{color:#5d5}} .m{{color:#fb0}} .clip{{color:#f55;font-weight:700}}
 .miss{{color:#f77}} .why{{color:#999;font-style:italic}} ul{{line-height:1.7}}
 p{{margin:8px 0 4px}}
</style>
<h1>{village} &mdash; trade list scroll &amp; read, tick by tick</h1>
<p>Green = good row (flush left, taller). Amber = material (indented ~29px). Red = clipped by
the viewport edge, so excluded from the parse. Blue lines mark the list bounds; the
<b style="color:#f0d">magenta</b> line and arrow show which edge the scroll is trying to bring
up to the list top. The frame stays put while the panel beside it scrolls &mdash; click any
OmniParser box to outline it on the frame.</p>
<h2>Accumulated across all ticks</h2><ul>{summary}</ul>
{''.join(rows)}
<script>
document.addEventListener('click', function (ev) {{
  var el = ev.target.closest('.el');
  if (!el) return;
  var sec = el.closest('section'), hl = sec.querySelector('.hl');
  hl.style.left = el.dataset.x + '%'; hl.style.top = el.dataset.y + '%';
  hl.style.width = el.dataset.w + '%'; hl.style.height = el.dataset.h + '%';
  hl.style.display = 'block';
  sec.querySelectorAll('.el.sel').forEach(function (n) {{ n.classList.remove('sel'); }});
  el.classList.add('sel');
}});
</script>"""
    out = f"{OUT_DIR}/report.html"
    with open(out, "w") as fh:
        fh.write(html)
    print(f"\nwrote {out}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print('Usage: python -m tools.village_scroll_report "<Village Name>"')
        raise SystemExit(1)
    raise SystemExit(run(args[0], replay="--replay" in sys.argv[1:]))
