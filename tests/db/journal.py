"""Write a market-event journal exactly as the collector emits it (candle first, then its M1
event; proven trades interleaved in arrival order), optionally with a disconnect gap or a
collector downtime."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Engine, text

from sp2l.engine.checkpoint import dump_m1
from sp2l.marketdata.m1_builder import M1Builder, Trade
from sp2l.marketdata.m5_aggregator import M5Aggregator
from sp2l.persistence.market_store import MarketStore


def write_journal(
    db: Engine,
    symbol: str,
    trades: Sequence[Trade],
    *,
    start: datetime,
    end: datetime,
    window: tuple[datetime, datetime] | None = None,
    mode: str = "disconnect",  # "disconnect": GAP at window start, DATA_GAP minutes
    #                            "downtime": minutes/trades missing, GAP(COLLECTOR_START) after
) -> None:
    store = MarketStore(db, symbol)
    builder = M1Builder(start)
    builder.mark_healthy_until(end)
    if window and mode == "disconnect":
        builder.add_coverage_gap(*window)
    agg = [M5Aggregator()]
    events: list[dict[str, object]] = []
    raw: list[dict[str, object]] = []
    state = {"gap_sent": False, "restart_sent": False}

    def ev(kind: str, ts: datetime, payload: dict[str, object]) -> None:
        events.append({"s": symbol, "k": kind, "t": ts, "p": json.dumps(payload)})

    def minutes(now: datetime) -> None:
        for m1 in builder.advance(now):
            if window and mode == "downtime" and window[0] <= m1.open_time < window[1]:
                agg[0] = M5Aggregator()  # collector down: nothing emitted, fresh aggregator
                continue
            store.upsert_m1(m1)
            ev("M1", now, dump_m1(m1))
            m5 = agg[0].add(m1)
            if m5 is not None:
                store.upsert_m5(m5)

    for t in sorted(trades, key=lambda x: (x.recv_ts, x.exch_ts, x.trade_id)):
        minutes(t.recv_ts)
        inside = bool(window) and window[0] <= t.exch_ts < window[1]  # type: ignore[index]
        if window and mode == "disconnect" and not state["gap_sent"] and t.recv_ts >= window[0]:
            ev("GAP", t.recv_ts, {"start": window[0].isoformat(), "reason": "DISCONNECTED"})
            state["gap_sent"] = True
        if window and mode == "downtime" and not state["restart_sent"] and t.exch_ts >= window[1]:
            ev("GAP", t.recv_ts, {"start": window[0].isoformat(), "reason": "COLLECTOR_START"})
            state["restart_sent"] = True
        if inside:
            continue  # never received
        raw.append(
            {
                "s": symbol,
                "id": t.trade_id,
                "e": t.exch_ts,
                "r": t.recv_ts,
                "p": t.price,
                "q": t.qty,
            }
        )
        ev(
            "TRADE",
            t.recv_ts,
            {
                "trade_id": t.trade_id,
                "exch_ts": t.exch_ts.isoformat(),
                "recv_ts": t.recv_ts.isoformat(),
                "price": str(t.price),
                "qty": str(t.qty),
                "late": False,
                "in_gap": False,
            },
        )
        builder.add_trade(t)
    minutes(end)
    with db.begin() as c:
        c.execute(
            text(
                "INSERT INTO raw_trades (symbol, trade_id, exch_ts, recv_ts, price, qty)"
                " VALUES (:s, :id, :e, :r, :p, :q)"
            ),
            raw,
        )
        c.execute(
            text(
                "INSERT INTO market_events (symbol, kind, ts, payload)"
                " VALUES (:s, :k, :t, CAST(:p AS jsonb))"
            ),
            events,
        )
