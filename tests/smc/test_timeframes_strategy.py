"""Timeframe aggregation (Tabdeal grid) and invariants of every accepted setup."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from sp2l.core.types import Candle, Side
from sp2l.smc.backtest import run
from sp2l.smc.lifecycle import risk_unit
from sp2l.smc.model import Costs, SmcParams
from sp2l.smc.timeframes import aggregate, bucket_start

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def m1_walk(n: int, seed: int) -> list[Candle]:
    rng = random.Random(seed)
    px, out = 4000.0, []
    for i in range(n):
        o = px
        c = o + rng.gauss(0, 1.2) + (0.4 if (i // 900) % 2 == 0 else -0.4) * rng.random()
        h, lo = max(o, c) + abs(rng.gauss(0, 0.6)), min(o, c) - abs(rng.gauss(0, 0.6))
        px = c
        out.append(
            Candle(
                T0 + timedelta(minutes=i),
                D(f"{o:.2f}"),
                D(f"{h:.2f}"),
                D(f"{lo:.2f}"),
                D(f"{c:.2f}"),
                D(1),
            )
        )
    return out


def test_hourly_bars_follow_the_tehran_half_hour_grid():
    assert bucket_start(datetime(2026, 1, 1, 10, 29, tzinfo=UTC), "1h") == datetime(
        2026, 1, 1, 9, 30, tzinfo=UTC
    )
    assert bucket_start(datetime(2026, 1, 1, 3, 0, tzinfo=UTC), "4h") == datetime(
        2026, 1, 1, 0, 30, tzinfo=UTC
    )
    assert bucket_start(datetime(2026, 1, 1, 10, 14, tzinfo=UTC), "15m") == datetime(
        2026, 1, 1, 10, 0, tzinfo=UTC
    )


def test_aggregate_keeps_only_closed_buckets():
    m1 = m1_walk(130, 1)  # 00:00 .. 02:09
    upto = m1[-1].open_time + timedelta(minutes=1)
    h1 = aggregate(m1, "1h", upto)
    assert [b.open_time.strftime("%H:%M") for b in h1] == ["23:30", "00:30"]  # 01:30 still forming
    assert all(b.open_time + timedelta(hours=1) <= upto for b in h1)
    first = [c for c in m1 if bucket_start(c.open_time, "1h") == h1[0].open_time]
    assert h1[0].open == first[0].open and h1[0].close == first[-1].close
    assert h1[0].high == max(c.high for c in first) and h1[0].low == min(c.low for c in first)


# a dense configuration for random data: 5m zones, 15m bias, 1m execution swings
DENSE = SmcParams(
    swing_len=2,
    ob_min_atr=D(0),
    sweep_max_bars=40,
    zone_tf="5m",
    exec_tf="1m",
    bias_tf="15m",
)


def test_every_accepted_setup_is_consistent():
    m1 = m1_walk(6 * 1440, 3)
    costs = Costs(D("0.0002"), D("0.0005"), D("0.0001"))
    res = run(m1, DENSE, costs)
    accepted = [s for s in res["setups"] if s.accepted]
    assert res["stats"]["setups"] > 0 and accepted
    for s in accepted:
        long = s.direction is Side.LONG
        z = s.zone
        assert z.sweep.idx <= z.ob.idx < z.event.break_idx  # sweep -> OB -> break
        assert z.event.break_idx - z.sweep.idx <= DENSE.sweep_max_bars
        assert s.entry == z.edge and s.created_at >= z.confirmed_at
        assert s.sl == (z.ob.bottom - DENSE.tick if long else z.ob.top + DENSE.tick)
        assert s.tp1 is not None
        prices = [x.price for x in (s.tp1, s.tp2, s.tp3) if x is not None]
        path = [s.sl, s.entry, *prices]
        assert path == sorted(path) if long else path == sorted(path, reverse=True)
        assert s.bias == (1 if long else -1)
        ru = risk_unit(s.direction, s.entry, s.sl, costs)
        assert ru > abs(s.entry - s.sl)
    for _, t in res["trades"]:
        assert t.created_at <= (t.filled_at or t.created_at)
        if t.closed_at:
            assert t.closed_at > t.created_at
        if t.result_r is not None:
            assert t.result_r == sum(x.r for x in t.parts) and t.result_r >= -1
