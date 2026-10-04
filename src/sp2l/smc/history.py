"""Market history for the SMC engine: no live warmup.

Every bar the engine analyses comes from one merged 1-minute series:
- the canonical live candles (candles_1m, written by the collector) where they exist;
- otherwise Tabdeal's chart history (the same data Tabdeal's own chart shows), fetched on
  demand into `exchange_m1` for the whole lookback the analysis needs.
Higher timeframes are aggregated from that series in SQL at request time (`load_bars`), so any
timeframe is available at any moment, complete back to the configured history depth.

A chart bar is stored only once it is final (its minute ended at least `SETTLE` before the
request), so the still-forming minute is never cached.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, text

from sp2l.core.types import Candle
from sp2l.marketdata.history import parse_bars
from sp2l.smc.timeframes import EPOCH, MINUTE, bucket_start, length, offset

log = logging.getLogger("sp2l.smc.history")
SETTLE = timedelta(seconds=5)
Fetch = Callable[[datetime, datetime], list[dict[str, Any]]]


def _floor(t: datetime) -> datetime:
    return t.astimezone(UTC).replace(second=0, microsecond=0)


def coverage(db: Engine, symbol: str) -> tuple[datetime | None, datetime | None]:
    with db.connect() as c:
        r = c.execute(
            text("SELECT MIN(open_time), MAX(open_time) FROM exchange_m1 WHERE symbol = :s"),
            {"s": symbol},
        ).one()
    return (
        None if r[0] is None else r[0].astimezone(UTC),
        None if r[1] is None else r[1].astimezone(UTC),
    )


def store_bars(db: Engine, symbol: str, raw: list[dict[str, Any]], requested: datetime) -> int:
    bars = parse_bars(raw)
    if bars is None:
        raise ValueError("Tabdeal chart history returned a malformed bar")
    final = [c for t, c in sorted(bars.items()) if t + MINUTE + SETTLE <= requested]
    if not final:
        return 0
    with db.begin() as c:
        c.execute(
            text(
                "INSERT INTO exchange_m1 (symbol, open_time, open, high, low, close, volume)"
                " VALUES (:s, :t, :o, :h, :l, :c, :v) ON CONFLICT (symbol, open_time) DO UPDATE"
                " SET open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,"
                " close = EXCLUDED.close, volume = EXCLUDED.volume, fetched_at = now()"
            ),
            [
                {
                    "s": symbol,
                    "t": b.open_time,
                    "o": b.open,
                    "h": b.high,
                    "l": b.low,
                    "c": b.close,
                    "v": b.volume,
                }
                for b in final
            ],
        )
    return len(final)


def ensure_history(
    db: Engine, symbol: str, fetch: Fetch, days: float, now: datetime | None = None
) -> dict[str, Any]:
    """Make `exchange_m1` cover [now - days, now): the missing head in one request, then the
    tail from the last cached minute (15-minute overlap so late corrections are picked up)."""
    now = now or datetime.now(UTC)
    want = _floor(now - timedelta(days=days))
    first, last = coverage(db, symbol)
    out: dict[str, Any] = {"from": want.isoformat(), "fetched": 0}
    if first is None or first > want + timedelta(minutes=30):
        end = first if first is not None else now
        out["fetched"] += store_bars(db, symbol, fetch(want, end + MINUTE), now)
        first, last = coverage(db, symbol)
    tail_from = (last - timedelta(minutes=15)) if last is not None else want
    out["fetched"] += store_bars(db, symbol, fetch(tail_from, now + MINUTE), now)
    first, last = coverage(db, symbol)
    out["first"] = None if first is None else first.isoformat()
    out["last"] = None if last is None else last.isoformat()
    return out


MERGED_M1 = """
    SELECT open_time, open, high, low, close, volume FROM candles_1m
     WHERE symbol = :s AND open_time >= :a AND open_time < :b
    UNION ALL
    SELECT h.open_time, h.open, h.high, h.low, h.close, h.volume FROM exchange_m1 h
     WHERE h.symbol = :s AND h.open_time >= :a AND h.open_time < :b
       AND NOT EXISTS (SELECT 1 FROM candles_1m c
                        WHERE c.symbol = h.symbol AND c.open_time = h.open_time)
"""


def series_end(db: Engine, symbol: str) -> datetime | None:
    """End of the last final minute of the merged series."""
    with db.connect() as c:
        r = c.execute(
            text(
                "SELECT GREATEST((SELECT MAX(open_time) FROM candles_1m WHERE symbol = :s),"
                " (SELECT MAX(open_time) FROM exchange_m1 WHERE symbol = :s))"
            ),
            {"s": symbol},
        ).scalar()
    return None if r is None else r.astimezone(UTC) + MINUTE


def load_bars(
    db: Engine, symbol: str, tf: str, bars: int, upto: datetime, *, include_forming: bool = False
) -> list[Candle]:
    """The last `bars` closed `tf` bars ending at or before `upto` (end of the last final M1).
    With include_forming, the bucket still in progress is appended (chart display only)."""
    ln = length(tf)
    end_bucket = bucket_start(upto, tf)  # bucket containing `upto` = still forming (or empty)
    start = end_bucket - ln * bars
    stop = upto if include_forming else end_bucket
    origin = EPOCH + offset(tf)
    sql = f"""
        WITH m AS ({MERGED_M1})
        SELECT date_bin(make_interval(secs => :len), open_time, :origin) AS t,
               (array_agg(open ORDER BY open_time))[1] AS o, MAX(high) AS h, MIN(low) AS l,
               (array_agg(close ORDER BY open_time DESC))[1] AS c, SUM(volume) AS v,
               COUNT(*) AS n
          FROM m GROUP BY 1 ORDER BY 1"""
    with db.connect() as c:
        rows = c.execute(
            text(sql),
            {"s": symbol, "a": start, "b": stop, "len": ln.total_seconds(), "origin": origin},
        ).all()
    return [
        Candle(r[0].astimezone(UTC), r[1], r[2], r[3], r[4], r[5] or Decimal(0), None) for r in rows
    ]
