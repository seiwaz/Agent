"""B46 measurement report computed from stored evidence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from sp2l.runtime.feed_report import report

pytestmark = pytest.mark.db


def test_report_metrics(engine):
    now = datetime.now(UTC).replace(microsecond=0)
    t0 = now - timedelta(hours=20)
    with engine.begin() as c:
        ev = [
            ("A", "CONNECTED", t0, None, "{}"),
            ("A", "CONFIRMED", t0 + timedelta(seconds=1), None, "{}"),
            ("B", "CONNECTED", t0 + timedelta(minutes=10), None, "{}"),
            ("B", "CONFIRMED", t0 + timedelta(minutes=10, seconds=1), None, "{}"),
            (
                "A",
                "CLOSED",
                t0 + timedelta(hours=1),
                "CLOSE_1000:please reconnect",
                '{"lifetime_s": 3600}',
            ),
            (
                "B",
                "CLOSED",
                t0 + timedelta(hours=1, seconds=4),
                "CLOSE_1000:please reconnect",
                '{"lifetime_s": 2996}',
            ),
            ("A", "CONFIRMED", t0 + timedelta(hours=1, seconds=6), None, "{}"),
        ]
        for conn, kind, ts, reason, detail in ev:
            c.execute(
                text(
                    "INSERT INTO feed_connection_events (symbol, conn, kind, ts, reason, detail)"
                    " VALUES ('BTCUSDT', :c, :k, :t, :r, CAST(:d AS jsonb))"
                ),
                {"c": conn, "k": kind, "t": ts, "r": reason, "d": detail},
            )
        c.execute(
            text(
                "INSERT INTO data_gaps (symbol, timeframe, kind, gap_start, gap_end, reason)"
                " VALUES ('BTCUSDT', 'trades', 'DATA_GAP', :a, :b, 'B:CLOSE_1000')"
            ),
            {"a": t0 + timedelta(hours=1, seconds=-2), "b": t0 + timedelta(hours=1, seconds=7)},
        )
        for i, status in enumerate(["OK", "OK", "DATA_GAP", "OK"]):
            c.execute(
                text(
                    "INSERT INTO market_events (symbol, kind, ts, payload) VALUES"
                    " ('BTCUSDT', 'M1', :t, CAST(:p AS jsonb))"
                ),
                {"t": t0 + timedelta(minutes=i), "p": f'{{"status": "{status}"}}'},
            )
        start = (now - timedelta(minutes=5 * 160)).replace(second=0)
        start = start - timedelta(minutes=start.minute % 5)
        for k in range(160):
            ot = start + timedelta(minutes=5 * k)
            c.execute(
                text(
                    "INSERT INTO candles_5m (symbol, open_time, close_time, open, high, low,"
                    " close, volume, trade_count, finalized_at, source_hash) VALUES"
                    " ('BTCUSDT', :o, :c, 1, 1, 1, 1, 1, 1, now(), 'h')"
                ),
                {"o": ot, "c": ot + timedelta(minutes=5)},
            )
    r = report(engine, "BTCUSDT", now - timedelta(hours=24))
    assert r["connections"]["A"]["disconnects"] == 1 and r["connections"]["B"]["disconnects"] == 1
    assert r["connections"]["A"]["reconnect_s"] == [6.0]
    assert r["connections"]["A"]["lifetimes_s"] == [3600.0]
    assert len(r["correlated_closes"]) == 1 and r["correlated_closes"][0]["delta_s"] == 4.0
    assert r["merged_uncovered_seconds"] == 9.0 and r["merged_data_gap_minutes"] == 1
    assert r["m5"]["longest"]["bars"] >= 150 and r["m5"]["target_reached"]
