"""Research data: one market's merged 1-minute series as Candles and as numpy arrays, with the
discovery / holdout split (first 2/3 of the market's own history / last 1/3)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import numpy as np
from sqlalchemy import Engine

from sp2l.core.types import Candle
from sp2l.smc.history import load_bars, series_end


@dataclass
class Market:
    symbol: str
    tick: Decimal
    bars: list[Candle]
    t: np.ndarray  # open time, epoch seconds
    o: np.ndarray
    h: np.ndarray
    lo: np.ndarray
    c: np.ndarray
    split: datetime  # discovery = before, holdout = from here

    def index_at(self, ts: float) -> int:
        """First minute opening at or after `ts` (epoch seconds)."""
        return int(np.searchsorted(self.t, ts, side="left"))


def _tick(bars: list[Candle]) -> Decimal:
    """The price increment: the most decimals any close of the sample shows."""
    d = 0
    for b in bars[-5000:]:
        s = format(b.close.normalize(), "f")
        if "." in s:
            d = max(d, len(s.split(".")[1]))
    return Decimal(1).scaleb(-d)


def from_bars(symbol: str, bars: list[Candle], tick: Decimal | None = None) -> Market:
    t = np.array([b.open_time.timestamp() for b in bars], dtype=np.float64)

    def f(xs: Any) -> np.ndarray:
        return np.array([float(x) for x in xs], dtype=np.float64)

    split_ts = t[0] + (t[-1] - t[0]) * 2 / 3
    return Market(
        symbol,
        tick or _tick(bars),
        bars,
        t,
        f(b.open for b in bars),
        f(b.high for b in bars),
        f(b.low for b in bars),
        f(b.close for b in bars),
        datetime.fromtimestamp(float(split_ts), UTC),
    )


def load(
    db: Engine, symbol: str, days: int, tick: Decimal | None = None, upto: datetime | None = None
) -> Market:
    """`upto` fixes the end, so a study is reproducible while the database keeps growing."""
    upto = upto or series_end(db, symbol)
    assert upto is not None, symbol
    return from_bars(symbol, load_bars(db, symbol, "1m", days * 1440, upto), tick)
