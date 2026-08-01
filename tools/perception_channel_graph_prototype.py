"""Channel-graph perception prototype (a.k.a. V8) — bank line + skeleton.

Standalone evaluation harness for the mini-map channel-graph pipeline.
NOT plumbed into live perception yet.

Pipeline
────────
1. **Bank line** (the foundation — supersedes the older ray-cast
   `perception_bank_prototype.py`):
     a. sprite + text mask (yellow/green/white/red icons; high-luminance
        gray text in the lower 2/3 of the frame),
     b. K-means K=5 on non-masked pixels in a bilateral-filtered RGB
        space,
     c. merge any cluster centre that's "water-coloured" (dark, blue >
        red, blue ≥ green) into a single water mask,
     d. absorb sprite pixels that lie inside the water mask (they're
        ships / NPCs on the water),
     e. fill_holes → dilate(2) → padded binary_opening(disk(5)) to
        smooth the perimeter without eroding frame edges,
     f. mask boundary = bank line.  Reaches ~1-3 px precision against
        hand-labelled GT in clean channels.

2. **Skeleton + channel graph**:
     a. skimage.skeletonize on the water mask,
     b. sknw.build_sknw → graph with nodes (junctions / endpoints) and
        edges (skeleton runs),
     c. prune_spurs (drop leaf branches < SPUR_THRESHOLD px),
     d. classify endpoints by distance-transform:
          - `edge`        — within EDGE_MARGIN_ALL of a frame edge,
          - `dead_end`    — local DT maximum ≥ MIN_DEAD_END_HALFWIDTH,
          - `spurious`    — otherwise (pruned if branch < MAX_PRUNE_LEN),
     e. merge_same_edge_endpoints — collapse near-duplicates on the
        same frame edge (parallel skeleton branches exiting at the
        same offscreen continuation),
     f. final degree-2 collapse so the surviving graph is just
        junctions ↔ endpoints with one edge between each pair.

The 9 LABELED scenarios below cover the failure modes observed in
the 2026-06-06 Nile descent: clean channel, Y-junction, viewport
bend, dead-end, lake mouth, village-text-occluded, U-bend, delta apex.

History
───────
Iteration log (and the V3b "perfect bank line" baseline this builds on)
lived in /tmp/ during the 2026-06-08 session — see
memory/feedback_check_in_major_prototype_milestones.md.

Usage
─────
  python -m tools.perception_channel_graph_prototype

  Reads frames from /tmp/tick_*.png by default.  Plot saved to
  /tmp/channel_graph_sknw_v8.png.  Override the LABELED list (or
  call the pipeline helpers directly) to point at other frames.
"""
from __future__ import annotations

import numpy as np
import matplotlib.pyplot as plt
import cv2
import sknw
import networkx as nx
from PIL import Image
from scipy.ndimage import binary_fill_holes, binary_dilation, distance_transform_edt
from skimage.morphology import skeletonize, binary_opening, disk

SPUR_THRESHOLD          = 22
TRIM_TOP                = 6
TRIM_RIGHT              = 6
EDGE_MARGIN_ALL         = 12
MIN_DEAD_END_HALFWIDTH  = 3
DT_LOCAL_MAX_RADIUS     = 4
MAX_PRUNE_LEN           = 60
EDGE_MERGE_DIST         = 30   # px — endpoints on same edge within this distance get merged

LABELED = [
    (100, "clean_channel", "/tmp/tick_0100.png"),
    (475, "Y_junction",    "/tmp/tick_0475.png"),
    (300, "viewport_bend", "/tmp/tick_0300.png"),
    (500, "dead_end",      "/tmp/tick_0500.png"),
    (786, "lake_mouth",    "/tmp/tick_0786_lake_at_end.png"),
    (152, "nubia_village", "/tmp/tick_0152.png"),
    (571, "bari_village",  "/tmp/tick_0571_bari_village.png"),
    (750, "U_bend",        "/tmp/tick_0750_y_branch.png"),
    (5,   "delta_apex",    "/tmp/tick_0005.png"),
]


def sprite_mask(img, include_text=True):
    R,G,B = (img[:,:,c].astype(np.int16) for c in range(3))
    yellow = (R > 180) & (G > 150) & (B < 130)
    green  = (G > 140) & (G - R > 30) & (G - B > 30)
    white  = (R > 215) & (G > 215) & (B > 215)
    red    = (R > 180) & (R - G > 40) & (R - B > 40)
    base = yellow | green | white | red
    if include_text:
        lum = (R + G + B) // 3
        uniform = (np.abs(R - G) < 18) & (np.abs(G - B) < 18) & (np.abs(R - B) < 18)
        text_like = uniform & (lum > 195) & (lum < 240)
        H = img.shape[0]
        text_band = np.zeros_like(text_like)
        text_band[H//3:, :] = True
        text_like = text_like & text_band
        base = base | text_like
    return base


def is_water_center(c):
    r,g,b = (int(c[i]) for i in range(3))
    return (r+g+b)//3 < 140 and b > r + 10 and b >= g - 5


def water_mask_from_image(path):
    img = np.array(Image.open(path).convert("RGB"))
    img = img[TRIM_TOP:, :-TRIM_RIGHT]
    spr_m = sprite_mask(img, include_text=True)
    bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    blur = cv2.cvtColor(cv2.bilateralFilter(bgr, 7, 40, 10), cv2.COLOR_BGR2RGB)
    H,W = blur.shape[:2]
    Z = blur[~spr_m].reshape(-1,3).astype(np.float32)
    crit = (cv2.TERM_CRITERIA_EPS+cv2.TERM_CRITERIA_MAX_ITER, 25, 0.5)
    _, _, centers = cv2.kmeans(Z, 5, None, crit, 3, cv2.KMEANS_PP_CENTERS)
    all_pix = blur.reshape(-1,3).astype(np.float32)
    dists = np.linalg.norm(all_pix[:,None,:]-centers[None,:,:], axis=-1)
    labels = np.argmin(dists, axis=1).reshape(H,W)
    centers_u8 = centers.astype(np.uint8)
    wc = [j for j,c in enumerate(centers_u8) if is_water_center(c)]
    water = np.isin(labels, wc)
    sprite_in_water = spr_m & binary_dilation(water, iterations=3)
    water = water | sprite_in_water
    m = binary_fill_holes(water)
    m = binary_dilation(m, iterations=2)
    # PAD then open then unpad — prevents erosion at frame boundary
    P = 6
    m_padded = np.pad(m, P, mode="edge")
    m_padded = binary_opening(m_padded, disk(5))
    m = m_padded[P:-P, P:-P]
    return img, m, spr_m


def merge_degree_2(G):
    merged = True
    while merged:
        merged = False
        for node in list(G.nodes):
            if G.degree(node) == 2:
                nbrs = list(G.neighbors(node))
                if len(nbrs) != 2 or nbrs[0] == nbrs[1]: continue
                n1, n2 = nbrs
                d1 = G.get_edge_data(node, n1)
                d2 = G.get_edge_data(node, n2)
                if d1 is None or d2 is None: continue
                pts1 = d1.get("pts", np.array([]).reshape(0,2))
                pts2 = d2.get("pts", np.array([]).reshape(0,2))
                o_pt = np.array(G.nodes[node].get("o", (0,0))).reshape(1,2)
                new_pts = np.vstack([pts1, o_pt, pts2])
                new_w = d1.get("weight", 0) + d2.get("weight", 0)
                G.remove_node(node)
                if not G.has_edge(n1, n2):
                    G.add_edge(n1, n2, pts=new_pts, weight=new_w)
                merged = True; break
    return G


def prune_spurs(G, threshold):
    while True:
        leaves = [n for n in G.nodes if G.degree(n) == 1]
        pruned = False
        for leaf in leaves:
            nbrs = list(G.neighbors(leaf))
            if not nbrs: continue
            d = G.get_edge_data(leaf, nbrs[0])
            if d and d.get("weight", 0) < threshold:
                G.remove_node(leaf); pruned = True
        G = merge_degree_2(G)
        if not pruned: break
    return G


def which_edge(y, x, H, W):
    """Return 'top'/'bottom'/'left'/'right' if (y,x) is within
    EDGE_MARGIN_ALL of that frame edge, else None.  If at a corner,
    returns the closer edge."""
    candidates = []
    if y < EDGE_MARGIN_ALL: candidates.append(("top", y))
    if y >= H - EDGE_MARGIN_ALL: candidates.append(("bottom", H - 1 - y))
    if x < EDGE_MARGIN_ALL: candidates.append(("left", x))
    if x >= W - EDGE_MARGIN_ALL: candidates.append(("right", W - 1 - x))
    if not candidates: return None
    candidates.sort(key=lambda t: t[1])
    return candidates[0][0]


def classify_endpoint(G, n, dt, H, W):
    y, x = G.nodes[n].get("o", (0, 0))
    y, x = int(y), int(x)
    if which_edge(y, x, H, W) is not None:
        return "edge"
    if 0 <= y < H and 0 <= x < W:
        dt_val = dt[y, x]
        if dt_val >= MIN_DEAD_END_HALFWIDTH:
            y0 = max(0, y - DT_LOCAL_MAX_RADIUS)
            y1 = min(H, y + DT_LOCAL_MAX_RADIUS + 1)
            x0 = max(0, x - DT_LOCAL_MAX_RADIUS)
            x1 = min(W, x + DT_LOCAL_MAX_RADIUS + 1)
            local_max = dt[y0:y1, x0:x1].max()
            if dt_val >= local_max - 1e-3:
                return "dead_end"
    return "spurious"


def filter_endpoints_by_dt(G, dt, H, W, max_iter=10):
    for it in range(max_iter):
        endpoints = [n for n in G.nodes if G.degree(n) == 1]
        any_pruned = False
        for ep in endpoints:
            cls = classify_endpoint(G, ep, dt, H, W)
            if cls != "spurious": continue
            nbrs = list(G.neighbors(ep))
            if not nbrs: continue
            d = G.get_edge_data(ep, nbrs[0])
            branch_len = d.get("weight", 0) if d else 0
            if branch_len < MAX_PRUNE_LEN:
                G.remove_node(ep); any_pruned = True
        G = merge_degree_2(G)
        if not any_pruned: break
    return G


def merge_same_edge_endpoints(G, H, W):
    """If two endpoints lie on the SAME frame edge within
    EDGE_MERGE_DIST, they're parts of the same offscreen continuation.
    Drop the shorter branch (keep the longer one)."""
    while True:
        endpoints = [n for n in G.nodes if G.degree(n) == 1]
        by_edge = {}
        for ep in endpoints:
            y, x = G.nodes[ep].get("o", (0, 0))
            edge = which_edge(int(y), int(x), H, W)
            if edge is None: continue
            by_edge.setdefault(edge, []).append((ep, int(y), int(x)))
        merged_any = False
        for edge, eps in by_edge.items():
            if len(eps) < 2: continue
            # Sort by position along the edge
            coord_idx = 1 if edge in ("left", "right") else 2
            eps_sorted = sorted(eps, key=lambda t: t[coord_idx])
            i = 0
            while i + 1 < len(eps_sorted):
                ep1, y1, x1 = eps_sorted[i]
                ep2, y2, x2 = eps_sorted[i+1]
                if edge in ("top", "bottom"):
                    dist = abs(x1 - x2)
                else:
                    dist = abs(y1 - y2)
                if dist < EDGE_MERGE_DIST:
                    nbr1 = list(G.neighbors(ep1))[0] if G.neighbors(ep1) else None
                    nbr2 = list(G.neighbors(ep2))[0] if G.neighbors(ep2) else None
                    if nbr1 is None or nbr2 is None:
                        i += 1; continue
                    d1 = G.get_edge_data(ep1, nbr1)
                    d2 = G.get_edge_data(ep2, nbr2)
                    w1 = d1.get("weight", 0) if d1 else 0
                    w2 = d2.get("weight", 0) if d2 else 0
                    drop = ep1 if w1 < w2 else ep2
                    G.remove_node(drop)
                    merged_any = True
                    break
                i += 1
            if merged_any: break
        G = merge_degree_2(G)
        if not merged_any: break
    return G


def build_channel_graph(img, water_mask):
    """End-to-end: water mask → pruned, classified channel graph."""
    sk = skeletonize(water_mask)
    dt = distance_transform_edt(water_mask)
    H, W = img.shape[:2]
    G = sknw.build_sknw(sk.astype(np.uint16))
    G = prune_spurs(G.copy(), SPUR_THRESHOLD)
    G = filter_endpoints_by_dt(G, dt, H, W)
    G = prune_spurs(G, SPUR_THRESHOLD)
    G = merge_same_edge_endpoints(G, H, W)
    G = prune_spurs(G, SPUR_THRESHOLD)
    return G, sk, dt


def _run_demo():
    fig, axes = plt.subplots(3, 3, figsize=(20, 14))
    for i, (tk, scenario, path) in enumerate(LABELED):
        img, m, spr_m = water_mask_from_image(path)
        H, W = img.shape[:2]
        G, sk, dt = build_channel_graph(img, m)

        final_classes = {}
        for n in G.nodes:
            if G.degree(n) == 1:
                final_classes[n] = classify_endpoint(G, n, dt, H, W)
        junctions = [n for n in G.nodes if G.degree(n) >= 3]
        endpoints = [n for n in G.nodes if G.degree(n) == 1]
        edges = list(G.edges())

        ax = axes[i // 3][i % 3]
        ax.imshow(img, alpha=0.85)
        cmap = plt.cm.tab10(np.linspace(0, 1, max(len(edges), 10)))[:max(len(edges), 1)]
        for k, (u, v) in enumerate(edges):
            d = G.get_edge_data(u, v)
            pts = d.get("pts", np.array([]))
            if pts.size == 0: continue
            ax.plot(pts[:, 1], pts[:, 0], color=cmap[k % len(cmap)],
                    linewidth=3, alpha=0.95)
        if junctions:
            co = np.array([G.nodes[n]["o"] for n in junctions])
            ax.scatter(co[:, 1], co[:, 0], color="red", s=240, marker="o",
                       edgecolor="white", linewidth=2.5, zorder=10,
                       label=f"{len(junctions)} junction(s)")
        edge_eps = [n for n in endpoints if final_classes.get(n) == "edge"]
        dead_eps = [n for n in endpoints if final_classes.get(n) == "dead_end"]
        other_eps = [n for n in endpoints if final_classes.get(n) == "spurious"]
        if edge_eps:
            co = np.array([G.nodes[n]["o"] for n in edge_eps])
            ax.scatter(co[:, 1], co[:, 0], color="lime", s=180, marker="s",
                       edgecolor="black", linewidth=1.5, zorder=10,
                       label=f"{len(edge_eps)} edge")
        if dead_eps:
            co = np.array([G.nodes[n]["o"] for n in dead_eps])
            ax.scatter(co[:, 1], co[:, 0], color="cyan", s=190, marker="D",
                       edgecolor="black", linewidth=1.5, zorder=10,
                       label=f"{len(dead_eps)} dead-end")
        if other_eps:
            co = np.array([G.nodes[n]["o"] for n in other_eps])
            ax.scatter(co[:, 1], co[:, 0], color="yellow", s=170, marker="o",
                       edgecolor="black", linewidth=1.5, zorder=10,
                       label=f"{len(other_eps)} other")
        ax.set_title(f"t{tk} {scenario}   "
                     f"J={len(junctions)} E={len(endpoints)} ({len(edges)} edges)",
                     fontsize=10)
        ax.legend(loc="lower right", fontsize=9)
        ax.axis("off")
        print(f"t{tk:>3} {scenario:<16}: J={len(junctions)} E={len(endpoints)} "
              f"({len(edges)} edges) — edge={len(edge_eps)} dead={len(dead_eps)} other={len(other_eps)}")

    fig.suptitle(f"V8 — padded opening + DT≥3 + EDGE margin 12 + same-edge merge ({EDGE_MERGE_DIST}px)",
                 fontsize=13, fontweight="bold")
    plt.tight_layout()
    out = "/tmp/channel_graph_sknw_v8.png"
    plt.savefig(out, dpi=130, bbox_inches="tight")
    print(f"\nsaved: {out}")


if __name__ == "__main__":
    _run_demo()
