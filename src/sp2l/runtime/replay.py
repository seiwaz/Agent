"""Replay driver: feeds stored raw trades through the canonical pipeline in arrival order.

Events are ordered by receive time, exactly as Live sees them:
- before each trade, every minute whose end + grace has passed is finalized (M1 -> M5 ->
  engine M1-close procedure);
- then the trade goes to the engine (PullbackStart, fills and exits on already-active
  orders) and, if not late, to the candle builder.
Feed coverage comes from the recorded healthy watermark and gaps (B31).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sp2l.engine.symbol_engine import ShadowSymbolEngine
from sp2l.marketdata.m1_builder import M1Builder, Trade


def replay(
    engine: ShadowSymbolEngine,
    trades: Iterable[Trade],
    *,
    start: datetime,
    end: datetime,
    healthy_until: datetime,
    gaps: Iterable[tuple[datetime, datetime]] = (),
) -> M1Builder:
    builder = M1Builder(start)
    builder.mark_healthy_until(healthy_until)
    for g0, g1 in gaps:
        builder.add_coverage_gap(g0, g1)
    for t in sorted(trades, key=lambda x: (x.recv_ts, x.exch_ts, x.trade_id)):
        for m1 in builder.advance(t.recv_ts):
            engine.on_m1(m1)
        engine.on_trade(t.price, t.exch_ts)
        builder.add_trade(t)
    for m1 in builder.advance(end):
        engine.on_m1(m1)
    return builder
