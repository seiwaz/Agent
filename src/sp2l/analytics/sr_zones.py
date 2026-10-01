"""Support / resistance zones for the live chart (DISPLAY ONLY - zero trading authority).

Owner decision 2026-09-30: swing-pivot zones on 15m, 30m and 4h, built from the stored
canonical M1 series (the same source the strategy uses; nothing here feeds any gate).

- Timeframe bars are aggregated from canonical M1 (a missing minute is simply absent; a bar
  with no minute at all does not exist). 15m and 30m are UTC-aligned; 4h is aligned to Tehran
  time like Tabdeal's own chart (bars open at 00:30, 04:30, ... UTC).
- Only CLOSED timeframe bars are used; the bar still forming is ignored.
- Pivot = the strategy's rule (CTX-03): a bar whose high is strictly above the highs of the two
  bars on each side is a swing high; a low strictly below both neighbours' lows is a swing low.
- A swing high gives a RESISTANCE zone from the pivot bar's body top to its high; a swing low
  gives a SUPPORT zone from its low to its body bottom (the wick).
- A zone is broken - and removed - once a later closed bar CLOSES beyond it (above a
  resistance's top / below a support's bottom).
- Overlapping unbroken zones of the same timeframe and kind merge into one.
- For display, the nearest `per_side` zones above and below the current price are kept.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sp2l.core.types import Candle

TIMEFRAMES: dict[str, tuple[timedelta, timedelta]] = {
    # name -> (bar length, alignment offset from the UTC epoch)
    "15m": (timedelta(minutes=15), timedelta(0)),
    "30m": (timedelta(minutes=30), timedelta(0)),
    "4h": (timedelta(hours=4), timedelta(minutes=30)),  # Tehran-aligned (UTC+03:30)
}
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PIVOT_SIDE = 2  # bars on each side (CTX-03)


@dataclass(frozen=True, slots=True)
class Bar:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(slots=True)
class Zone:
    timeframe: str
    kind: str  # RESISTANCE / SUPPORT
    bottom: Decimal
    top: Decimal
    since: datetime  # open time of the (earliest) pivot bar
    pivots: int = 1  # pivots merged into this zone


def bucket_start(t: datetime, length: timedelta, offset: timedelta) -> datetime:
    n = (t - EPOCH - offset) // length
    return EPOCH + offset + n * length


def aggregate(m1: Iterable[Candle], timeframe: str, now: datetime) -> list[Bar]:
    """Closed timeframe bars from M1 candles (sorted by time)."""
    length, offset = TIMEFRAMES[timeframe]
    out: list[Bar] = []
    cur: list[Candle] = []
    key: datetime | None = None

    def flush() -> None:
        if cur and key is not None and key + length <= now:  # closed bars only
            out.append(
                Bar(
                    key,
                    cur[0].open,
                    max(c.high for c in cur),
                    min(c.low for c in cur),
                    cur[-1].close,
                )
            )

    for c in m1:
        k = bucket_start(c.open_time, length, offset)
        if k != key:
            flush()
            cur, key = [], k
        cur.append(c)
    flush()
    return out


def zones_for(bars: list[Bar], timeframe: str) -> list[Zone]:
    """Unbroken swing-pivot zones of one timeframe, overlapping ones merged."""
    raw: list[tuple[int, Zone]] = []
    s = PIVOT_SIDE
    for i in range(s, len(bars) - s):
        b = bars[i]
        nb = bars[i - s : i] + bars[i + 1 : i + s + 1]
        if all(b.high > x.high for x in nb):
            raw.append(
                (i, Zone(timeframe, "RESISTANCE", max(b.open, b.close), b.high, b.open_time))
            )
        if all(b.low < x.low for x in nb):
            raw.append((i, Zone(timeframe, "SUPPORT", b.low, min(b.open, b.close), b.open_time)))
    alive: list[Zone] = []
    for i, z in raw:
        later = bars[i + 1 :]
        broken = any(
            (x.close > z.top) if z.kind == "RESISTANCE" else (x.close < z.bottom) for x in later
        )
        if not broken:
            alive.append(z)
    merged: list[Zone] = []
    for kind in ("RESISTANCE", "SUPPORT"):
        for z in sorted((z for z in alive if z.kind == kind), key=lambda z: z.bottom):
            last = merged[-1] if merged and merged[-1].kind == kind else None
            if last is not None and z.bottom <= last.top:  # overlapping (or touching) bands
                last.top = max(last.top, z.top)
                last.since = min(last.since, z.since)
                last.pivots += 1
            else:
                merged.append(Zone(z.timeframe, kind, z.bottom, z.top, z.since, z.pivots))
    return merged


def nearest(zones: list[Zone], price: Decimal, per_side: int = 3) -> list[Zone]:
    """The `per_side` closest zones above the price and below it (a zone containing the price
    counts on its own kind's side)."""
    above = sorted(
        (z for z in zones if z.top >= price and z.kind == "RESISTANCE"), key=lambda z: z.bottom
    )
    below = sorted(
        (z for z in zones if z.bottom <= price and z.kind == "SUPPORT"), key=lambda z: -z.top
    )
    return above[:per_side] + below[:per_side]


def compute(
    m1: list[Candle], price: Decimal, now: datetime, timeframes: Iterable[str], per_side: int = 3
) -> list[Zone]:
    out: list[Zone] = []
    for tf in timeframes:
        out += nearest(zones_for(aggregate(m1, tf, now), tf), price, per_side)
    return out
