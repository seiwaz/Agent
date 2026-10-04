"""Timeframes and aggregation of canonical M1 candles.

Alignment follows Tabdeal's own chart (measured 2026-10-04): 1m..15m on the UTC grid, 1h and
4h on Tehran time (UTC+03:30), i.e. 1h bars open at hh:30 UTC and 4h bars at 00:30, 04:30, ...
Only CLOSED bars are analysed: a bucket counts once its end is at or before the end of the
last final M1.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sp2l.core.types import Candle

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
TIMEFRAMES: dict[str, tuple[timedelta, timedelta]] = {
    # name -> (bar length, alignment offset from the UTC epoch)
    "1m": (MINUTE, timedelta(0)),
    "5m": (timedelta(minutes=5), timedelta(0)),
    "15m": (timedelta(minutes=15), timedelta(0)),
    "1h": (timedelta(hours=1), timedelta(minutes=30)),
    "4h": (timedelta(hours=4), timedelta(minutes=30)),
}
ORDER = tuple(TIMEFRAMES)


def length(tf: str) -> timedelta:
    return TIMEFRAMES[tf][0]


def offset(tf: str) -> timedelta:
    return TIMEFRAMES[tf][1]


def bucket_start(t: datetime, tf: str) -> datetime:
    ln, off = TIMEFRAMES[tf]
    return EPOCH + off + ((t - EPOCH - off) // ln) * ln


def close_time(open_time: datetime, tf: str) -> datetime:
    return open_time + length(tf)


def aggregate(m1: Iterable[Candle], tf: str, upto: datetime) -> list[Candle]:
    """Closed `tf` bars from M1 candles (sorted by time). `upto` = end of the last final M1."""
    if tf == "1m":
        return [c for c in m1 if c.open_time + MINUTE <= upto]
    out: list[Candle] = []
    cur: list[Candle] = []
    key: datetime | None = None

    def flush() -> None:
        if cur and key is not None and key + length(tf) <= upto:
            out.append(
                Candle(
                    key,
                    cur[0].open,
                    max(c.high for c in cur),
                    min(c.low for c in cur),
                    cur[-1].close,
                    sum((c.volume for c in cur), start=cur[0].volume * 0),
                    sum((c.trade_count or 0) for c in cur),
                )
            )

    for c in m1:
        k = bucket_start(c.open_time, tf)
        if k != key:
            flush()
            cur, key = [], k
        cur.append(c)
    flush()
    return out
