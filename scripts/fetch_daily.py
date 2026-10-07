"""Read-only: download daily OHLCV candles into a CSV for the System B backtest.

    uv run python scripts/fetch_daily.py --source binance --symbol BTCUSDT   # from 2017-08
    uv run python scripts/fetch_daily.py --source bitstamp --symbol btcusd   # from 2011-08

Public endpoints only (no API key), GET requests only. Days are UTC; today's still-forming
day is dropped. Output: data/<SOURCE>_<SYMBOL>_1d.csv (columns date, open, high, low, close,
volume), readable by `python -m sp2l trend-backtest --csv ...`. Tabdeal's own history (about
300 days of 1-minute bars) is read from the database instead: `trend-backtest --days N`.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sp2l.core.types import Candle
from sp2l.trend.data import drop_forming, quality, write_csv

DAY_MS = 86_400_000


def _get(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "sp2l-fetch-daily"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _candle(ms: int, o: Any, h: Any, lo: Any, c: Any, v: Any) -> Candle:
    o_, h_, l_, c_ = (Decimal(str(x)) for x in (o, h, lo, c))
    return Candle(
        datetime.fromtimestamp(ms / 1000, UTC),
        o_,
        max(h_, o_, c_),
        min(l_, o_, c_),
        c_,
        Decimal(str(v)),
        None,
    )


def binance(symbol: str, since: datetime) -> list[Candle]:
    out: list[Candle] = []
    start = int(since.timestamp() * 1000)
    while True:
        rows = _get(
            "https://api.binance.com/api/v3/klines"
            f"?symbol={symbol}&interval=1d&limit=1000&startTime={start}"
        )
        out += [_candle(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows]
        if len(rows) < 1000:
            return out
        start = rows[-1][0] + DAY_MS
        time.sleep(0.3)


def bitstamp(symbol: str, since: datetime) -> list[Candle]:
    out: list[Candle] = []
    start = int(since.timestamp())
    while True:
        d = _get(
            f"https://www.bitstamp.net/api/v2/ohlc/{symbol.lower()}/"
            f"?step=86400&limit=1000&start={start}"
        )
        rows = d["data"]["ohlc"]
        out += [
            _candle(
                int(r["timestamp"]) * 1000, r["open"], r["high"], r["low"], r["close"], r["volume"]
            )
            for r in rows
        ]
        if len(rows) < 1000:
            return out
        start = int(rows[-1]["timestamp"]) + 86400
        time.sleep(0.3)


SOURCES = {"binance": binance, "bitstamp": bitstamp}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=sorted(SOURCES), default="binance")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--since", default="2010-01-01")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args()
    since = datetime.fromisoformat(a.since).replace(tzinfo=UTC)
    bars = drop_forming(SOURCES[a.source](a.symbol, since))
    uniq = {b.open_time: b for b in bars}
    bars = [uniq[k] for k in sorted(uniq)]
    out = a.out or Path(f"data/{a.source}_{a.symbol.upper()}_1d.csv")
    write_csv(out, bars)
    print(json.dumps({"out": str(out), **quality(bars, source=a.source)}, indent=1))
    if bars and bars[-1].open_time < datetime.now(UTC) - timedelta(days=3):
        print("warning: the last bar is more than 3 days old")


if __name__ == "__main__":
    main()
