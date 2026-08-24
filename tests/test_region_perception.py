"""Region-cluster perception — the validated capability is the rotation-robust
title/port-name read (title_text). Synthetic elements + frames; no device."""
import numpy as np
from PIL import Image

from vision.region_perception import perceive_regions, title_text


class E:
    def __init__(self, content, cx, cy, w=100, h=40, etype="text"):
        self.content = content
        self.element_type = etype
        self.cx, self.cy = cx, cy
        self.x1, self.y1 = cx - w // 2, cy - h // 2
        self.x2, self.y2 = cx + w // 2, cy + h // 2
        self.width, self.height = w, h


def _frame():
    return Image.fromarray(np.full((1080, 2400, 3), 40, dtype=np.uint8), "RGB")


def test_reads_port_name_from_top_left_title():
    els = [E("Malé", cx=360, cy=45, w=150, h=56),          # big title, top-left
           E("random npc", cx=1200, cy=560)]               # noise, centre
    assert title_text(_frame(), els) == "Malé"
    r = perceive_regions(_frame(), els)
    assert r["title"].populated and r["title"].dominant == "Malé"


def test_title_read_is_rotation_robust():
    # the 118px camera-notch shift moves the whole UI right; the title must still read.
    base = E("Amsterdam", cx=360, cy=45, w=180, h=56)
    shifted = E("Amsterdam", cx=360 + 118, cy=45, w=180, h=56)
    assert title_text(_frame(), [base]) == "Amsterdam"
    assert title_text(_frame(), [shifted]) == "Amsterdam"   # still inside the title region


def test_no_title_when_top_left_empty():
    els = [E("Purchase", cx=1200, cy=540), E("Sell", cx=1300, cy=560)]   # centre only
    assert title_text(_frame(), els) is None
    assert not perceive_regions(_frame(), els)["title"].populated


def test_region_bucketing_and_signature():
    els = [E("Harbor", cx=2100, cy=470), E("Market", cx=2100, cy=540)]   # right_panel
    r = perceive_regions(_frame(), els)
    assert r["right_panel"].populated and r["right_panel"].n == 2
    assert not r["left_menu"].populated
    assert r["right_panel"].signature.startswith("2:")


def test_dominant_is_largest_element():
    els = [E("tiny", cx=300, cy=45, w=40, h=20),
           E("BigTitle", cx=360, cy=45, w=200, h=60)]
    assert perceive_regions(_frame(), els)["title"].dominant == "BigTitle"
