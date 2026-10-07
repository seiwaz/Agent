"""Daily bars for System B: from a CSV file, or aggregated from the project's 1-minute history.

CSV: a header row; a time column (date / time / timestamp / open_time / datetime, or an unnamed
first column as pandas writes it) as ISO dates or epoch seconds / milliseconds, then open, high,
low, close and optionally volume (any case). Rows are sorted and de-duplicated by day. A row
whose open / close lies outside its high / low (seen in some exchange exports) has its high / low
widened to include them, and is counted in the quality report.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sp2l.core.types import Candle

TIME_COLS = ("date", "time", "timestamp", "open_time", "datetime", "")
DAY = timedelta(days=1)


def _when(v: str) -> datetime:
    v = v.strip()
    try:
        x = float(v)
    except ValueError:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)
    return datetime.fromtimestamp(x / 1000 if x > 1e11 else x, UTC)


def _day(t: datetime) -> datetime:
    return t.replace(hour=0, minute=0, second=0, microsecond=0)


def load_csv(path: Path) -> tuple[list[Candle], dict[str, Any]]:
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"{path}: no rows")
    cols = {k.strip().lower(): k for k in rows[0]}
    tcol = next((cols[c] for c in TIME_COLS if c in cols), None)
    missing = [c for c in ("open", "high", "low", "close") if c not in cols]
    if tcol is None or missing:
        raise ValueError(f"{path}: needs a time column and open/high/low/close, got {list(cols)}")
    by_day: dict[datetime, Candle] = {}
    repaired = bad = 0
    for r in rows:
        try:
            t = _day(_when(r[tcol]))
            o, h, lo, c = (Decimal(r[cols[k]]) for k in ("open", "high", "low", "close"))
            v = Decimal(r[cols["volume"]]) if "volume" in cols and r[cols["volume"]] else Decimal(0)
        except (ValueError, InvalidOperation, KeyError):
            bad += 1
            continue
        if min(o, h, lo, c) <= 0:
            bad += 1
            continue
        if not (lo <= min(o, c) and max(o, c) <= h):
            repaired += 1
            h, lo = max(h, o, c), min(lo, o, c)
        by_day[t] = Candle(t, o, h, lo, c, max(v, Decimal(0)), None)
    bars = [by_day[k] for k in sorted(by_day)]
    return bars, quality(bars, repaired=repaired, unreadable=bad, source=str(path))


def quality(bars: Iterable[Candle], **extra: Any) -> dict[str, Any]:
    bs = list(bars)
    gaps = [
        (a.open_time.date().isoformat(), int((b.open_time - a.open_time) / DAY) - 1)
        for a, b in zip(bs, bs[1:], strict=False)
        if b.open_time - a.open_time > DAY
    ]
    return {
        **extra,
        "bars": len(bs),
        "first": bs[0].open_time.date().isoformat() if bs else None,
        "last": bs[-1].open_time.date().isoformat() if bs else None,
        "missing_days": sum(n for _, n in gaps),
        "largest_gaps": sorted(gaps, key=lambda g: -g[1])[:5],
    }


def drop_forming(bars: list[Candle], now: datetime | None = None) -> list[Candle]:
    """Drop today's still-forming UTC day, if the source included it."""
    now = now or datetime.now(UTC)
    return [b for b in bars if b.open_time + DAY <= now]


def write_csv(path: Path, bars: Iterable[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow([b.open_time.date().isoformat(), b.open, b.high, b.low, b.close, b.volume])


def load_funding_csv(path: Path) -> dict[datetime, float]:
    """Funding rates (columns time, rate; one row per funding event, time in ISO or epoch
    s / ms) -> the sum of the rates per UTC day, keyed by the day's open time."""
    out: dict[datetime, float] = {}
    with path.open(newline="") as f:
        for r in csv.DictReader(f):
            d = _day(_when(r["time"]))
            out[d] = out.get(d, 0.0) + float(r["rate"])
    return out
