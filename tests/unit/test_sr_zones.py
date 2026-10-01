"""Support / resistance zones for the live chart (display only; owner decision 2026-09-30)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from sp2l.analytics.sr_zones import Bar, aggregate, bucket_start, compute, nearest, zones_for
from sp2l.core.types import Candle

T0 = datetime(2026, 9, 30, 0, 0, tzinfo=UTC)
M15 = timedelta(minutes=15)


def b(i: int, o: str, h: str, lo: str, c: str) -> Bar:
    return Bar(T0 + i * M15, D(o), D(h), D(lo), D(c))


def test_4h_bars_are_aligned_to_tehran_time_and_15m_30m_to_utc():
    four = timedelta(hours=4)
    assert bucket_start(
        datetime(2026, 9, 30, 3, 10, tzinfo=UTC), four, timedelta(minutes=30)
    ) == datetime(2026, 9, 30, 0, 30, tzinfo=UTC)
    assert bucket_start(
        datetime(2026, 9, 30, 4, 40, tzinfo=UTC), four, timedelta(minutes=30)
    ) == datetime(2026, 9, 30, 4, 30, tzinfo=UTC)
    assert bucket_start(datetime(2026, 9, 30, 3, 44, tzinfo=UTC), M15, timedelta(0)) == datetime(
        2026, 9, 30, 3, 30, tzinfo=UTC
    )


def test_aggregate_uses_only_closed_bars_from_m1():
    m1 = [
        Candle(T0 + timedelta(minutes=i), D(100 + i), D(101 + i), D(99 + i), D(100 + i) + D("0.5"))
        for i in range(40)
    ]
    now = T0 + timedelta(minutes=40)  # the third 15m bar (00:30-00:45) is still forming
    bars = aggregate(m1, "15m", now)
    assert [x.open_time for x in bars] == [T0, T0 + M15]
    assert bars[0].open == D(100) and bars[0].high == D(115) and bars[0].low == D(99)
    assert bars[0].close == D("114.5")


def test_swing_high_and_low_make_wick_zones():
    bars = [
        b(0, "100", "101", "99", "100"),
        b(1, "100", "103", "99.5", "102"),
        b(2, "102", "108", "101", "105"),  # swing high: zone 105..108
        b(3, "105", "104", "100", "101"),
        b(4, "101", "102", "95", "97"),  # swing low: zone 95..97
        b(5, "97", "101", "96", "100"),
        b(6, "100", "103", "98", "102"),
    ]
    zs = zones_for(bars, "15m")
    res = [z for z in zs if z.kind == "RESISTANCE"]
    sup = [z for z in zs if z.kind == "SUPPORT"]
    assert [(z.bottom, z.top) for z in res] == [(D(105), D(108))]
    assert [(z.bottom, z.top) for z in sup] == [(D(95), D(97))]
    assert res[0].since == T0 + 2 * M15


def test_a_close_through_the_zone_removes_it_but_a_wick_does_not():
    base = [
        b(0, "100", "101", "99", "100"),
        b(1, "100", "103", "99.5", "102"),
        b(2, "102", "108", "101", "105"),
        b(3, "105", "104", "100", "101"),
        b(4, "101", "102", "99", "100"),
    ]
    wick = base + [b(5, "100", "109", "99", "107")]  # high above 108, close 107 inside
    assert any(z.kind == "RESISTANCE" for z in zones_for(wick, "15m"))
    close = base + [b(5, "100", "109", "99", "108.5")]  # closes above the top: broken
    assert not any(z.kind == "RESISTANCE" and z.top == D(108) for z in zones_for(close, "15m"))


def test_overlapping_zones_merge_and_nearest_selects_each_side():
    bars = [
        b(0, "100", "101", "99", "100"),
        b(1, "100", "102", "99", "101"),
        b(2, "101", "110", "100", "106"),  # R 106..110
        b(3, "106", "105", "100", "101"),
        b(4, "101", "102", "99", "100"),
        b(5, "100", "103", "99", "102"),
        b(6, "102", "109", "101", "107"),  # R 107..109 overlaps -> merged 106..110
        b(7, "107", "106", "100", "101"),
        b(8, "101", "102", "99", "100"),
    ]
    zs = zones_for(bars, "30m")
    res = [z for z in zs if z.kind == "RESISTANCE"]
    assert [(z.bottom, z.top, z.pivots) for z in res] == [(D(106), D(110), 2)]
    near = nearest(zs, D(100), per_side=1)
    assert [z.kind for z in near].count("RESISTANCE") <= 1 and [z.kind for z in near].count(
        "SUPPORT"
    ) <= 1


def test_compute_returns_zones_per_requested_timeframe():
    m1 = []
    for i in range(24 * 60):
        t = T0 + timedelta(minutes=i)
        px = D(4190) + D((i // 37) % 9) * 2 - D((i // 91) % 5)
        m1.append(Candle(t, px, px + 1, px - 1, px))
    out = compute(m1, D(4195), T0 + timedelta(days=1), ("15m", "30m"), per_side=2)
    assert {z.timeframe for z in out} <= {"15m", "30m"} and out
    for tf in ("15m", "30m"):
        mine = [z for z in out if z.timeframe == tf]
        assert (
            sum(z.kind == "RESISTANCE" for z in mine) <= 2
            and sum(z.kind == "SUPPORT" for z in mine) <= 2
        )
