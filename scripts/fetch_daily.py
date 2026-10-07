"""Read-only: download daily OHLCV candles into a CSV for the System B backtest.

    uv run python scripts/fetch_daily.py --source binance --symbol BTCUSDT   # from 2017-08
    uv run python scripts/fetch_daily.py --source bitstamp --symbol btcusd   # from 2011-08
    uv run python scripts/fetch_daily.py --source binance_vision --symbol BTCUSDT  # bulk archive

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
        # days without trades are left out (2011-2013), so a short page is not the end
        if not rows or int(rows[-1]["timestamp"]) + 2 * 86400 > time.time():
            return out
        start = int(rows[-1]["timestamp"]) + 86400
        time.sleep(0.3)


def binance_vision(symbol: str, since: datetime) -> list[Candle]:
    """Binance's bulk archive (data.binance.vision): monthly files, then daily files for the
    current month. Reachable where the REST API answers 451 (restricted locations)."""
    import csv
    import io
    import urllib.error
    import zipfile

    base = "https://data.binance.vision/data/spot"
    out: list[Candle] = []

    def read(url: str) -> bool:
        req = urllib.request.Request(url, headers={"User-Agent": "sp2l-fetch-daily"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                blob = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            raise
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            text = z.read(z.namelist()[0]).decode()
        for row in csv.reader(io.StringIO(text)):
            if not row or not row[0].isdigit():
                continue  # header line in some files
            ts = int(row[0])
            ms = ts // 1000 if ts > 10**14 else ts  # microseconds from 2025 on
            out.append(_candle(ms, row[1], row[2], row[3], row[4], row[5]))
        return True

    now = datetime.now(UTC)
    y, m = max(since, datetime(2017, 8, 1, tzinfo=UTC)).year, since.month
    if since < datetime(2017, 8, 1, tzinfo=UTC):
        y, m = 2017, 8
    while (y, m) < (now.year, now.month):
        read(f"{base}/monthly/klines/{symbol}/1d/{symbol}-1d-{y}-{m:02d}.zip")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    day = datetime(now.year, now.month, 1, tzinfo=UTC)
    while day + timedelta(days=1) <= now:
        read(f"{base}/daily/klines/{symbol}/1d/{symbol}-1d-{day.date().isoformat()}.zip")
        day += timedelta(days=1)
    return out


SOURCES = {"binance": binance, "binance_vision": binance_vision, "bitstamp": bitstamp}


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
