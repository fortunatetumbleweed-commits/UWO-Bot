"""refresh_market VERIFY path: success is the sold-out good's TILE going active again
(stock>0), per user 2026-08-18 — more reliable than the restock-timer OCR, and it's
what the barter sub-task actually needs. `accepted` alone must NOT pass when the tile
is still sold out. All external detectors mocked — no device."""
from dataclasses import dataclass
from types import SimpleNamespace
from unittest import mock

from actions.buy_materials import refresh_market


@dataclass
class _Good:
    name: str
    available_qty: int
    sold_out: bool = False


_BTN = SimpleNamespace(cx=1522, cy=157, currency="blue_gem", timer="00.10.00")
_CONFIRM = SimpleNamespace(cx=1200, cy=600, currency="blue_gem", cost=4, verb="Refresh")


def _run(coral):
    """Drive refresh_market with a market that returns `coral` (a _Good) after refresh."""
    with mock.patch("vision.region_detectors.market_restock.find_restock_button",
                    return_value=_BTN), \
         mock.patch("vision.region_detectors.commit_button.detect_commit_buttons",
                    return_value=[_CONFIRM]), \
         mock.patch("vision.omniparser.parse_fast_cached", return_value=[]):
        return refresh_market(
            capture_fn=lambda: "frame", tap_fn=lambda *a, **k: None,
            ocr_fn=lambda im, **k: [], verify_good="Coral", port="Male",
            read_market_fn=lambda f, **k: [coral], omni_fn=lambda f: [], settle=0)


def test_tile_active_after_refresh_is_success():
    res = _run(_Good("Coral", available_qty=23, sold_out=False))
    assert res["ok"] is True and "tile[Coral] active=True" in res["reason"]


def test_tile_still_sold_out_fails_even_though_confirm_accepted():
    # The confirm WAS tapped (accepted=True), but the tile never reactivated → NOT ok.
    res = _run(_Good("Coral", available_qty=0, sold_out=True))
    assert res["ok"] is False and "active=False" in res["reason"]
