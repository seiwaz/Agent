"""Zone / liquidity state inside a bar that has not closed yet (touch, wick fill, sweep)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from sp2l.api.smc import liquidity, live_zone
from sp2l.core.types import Candle, Side
from sp2l.smc.model import SmcParams, Swing, Zone
from sp2l.smc.strategy import filled_live, m1_range
from sp2l.smc.structure import analyze

T0 = datetime(2026, 1, 1, tzinfo=UTC)
P = SmcParams()


def zd(kind: str, direction: str, top: str, bottom: str, tested: bool = False) -> dict:
    return {
        "kind": kind,
        "direction": direction,
        "top": top,
        "bottom": bottom,
        "tested": tested,
        "status": "TESTED" if tested else "ACTIVE",
    }


def test_first_touch_in_the_forming_bar_ends_fresh_at_once():
    z = zd("OB", "LONG", "105", "100")
    assert live_zone(z, (D(120), D(106)), P) == z  # not reached yet: still fresh
    out = live_zone(z, (D(120), D(104)), P)
    assert out is not None and out["tested"] and out["status"] == "TESTED" and out["live"]


def test_a_wick_through_a_whole_fvg_removes_it_but_an_order_block_needs_a_close():
    assert live_zone(zd("FVG", "LONG", "105", "100"), (D(110), D(99.5)), P) is None
    assert live_zone(zd("FVG", "SHORT", "105", "100"), (D(105), D(95)), P) is None
    ob = live_zone(zd("OB", "LONG", "105", "100"), (D(110), D(99.5)), P)
    assert ob is not None and ob["tested"]  # an OB is invalid only after a close beyond it
    keep = SmcParams(fvg_fill="close")
    assert live_zone(zd("FVG", "LONG", "105", "100"), (D(110), D(99.5)), keep) is not None


def bars(rows: list[tuple[float, float]]) -> list[Candle]:
    out = []
    for i, (h, lo) in enumerate(rows):
        m = (h + lo) / 2
        out.append(Candle(T0 + timedelta(minutes=i), D(str(m)), D(str(h)), D(str(lo)), D(str(m))))
    return out


def test_m1_range_covers_only_the_minutes_of_the_forming_bar():
    m1 = analyze(bars([(10, 9), (11, 10), (12, 8), (13, 11), (14, 12)]), "1m", P)
    assert m1_range(m1, T0 + timedelta(minutes=2), 3) == (D(13), D(8))
    assert m1_range(m1, T0 + timedelta(minutes=9), 4) is None
    gap = Zone("15m:FVG:LONG:1", "FVG", Side.LONG, D("8.5"), D(8), 0, T0, 0)
    assert filled_live(gap, m1_range(m1, T0 + timedelta(minutes=2), 3), P)  # low 8 fills it
    assert not filled_live(gap, m1_range(m1, T0 + timedelta(minutes=3), 4), P)


def test_liquidity_traded_through_by_the_forming_bar_is_swept():
    a = analyze(bars([(10, 9)] * 3), "15m", P)
    a.swings = [Swing("HIGH", 0, T0, D(12), 0), Swing("LOW", 1, T0, D(8), 1)]
    both = liquidity(a, D(10))
    assert {x["kind"] for x in both} == {"BSL", "SSL"}
    swept = liquidity(a, D(10), rng=(D("12.5"), D(9)))  # the forming bar ran above 12
    assert [x["kind"] for x in swept] == ["SSL"]


def test_poi_kinds_limit_which_zones_can_be_a_point_of_interest():
    from sp2l.smc.strategy import find_poi

    m1 = analyze(bars([(10, 9)] * 30), "1m", P)
    h1 = analyze(bars([(10, 9)] * 30), "1h", P)
    fvg = Zone("1h:FVG:LONG:1", "FVG", Side.LONG, D("9.8"), D("9.2"), 0, T0, 0, expires_idx=10**6)
    h1.zones = [fvg]
    ob = Zone("1m:OB:LONG:1", "OB", Side.LONG, D("9.6"), D("9.1"), 0, T0, 0)
    t = T0 + timedelta(hours=10)
    ctx = {"1m": m1, "1h": h1}
    p_any = SmcParams(poi_tfs=("1h",), poi_kinds=("OB", "FVG"))
    p_ob = SmcParams(poi_tfs=("1h",), poi_kinds=("OB",))
    assert find_poi(ctx, p_any, ob, t, Side.LONG) == (fvg, "1h")
    assert find_poi(ctx, p_ob, ob, t, Side.LONG) is None  # an FVG alone is no POI any more
