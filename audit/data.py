"""A. Data integrity of the merged M1 series the backtest reads.

A1 holes / duplicates / order / impossible OHLC inside the audited range;
A2 a random sample (>= 1,000 minutes, spread over the period) re-fetched from Tabdeal's chart
   and compared field by field;
A3 the first / last minute of every stored fetch chunk (one `fetched_at` per chunk);
A4 every higher timeframe built two ways - the backtest's `aggregate` (Python) and the live
   engine's `load_bars` (SQL date_bin) - must be identical; bars built from incomplete
   buckets are counted;
A5 synthetic (zero-volume, flat) minutes are counted.

Usage: uv run python -m audit.data  ->  docs/audit/data.json
"""

from __future__ import annotations

import json
import random
import time
from collections import Counter
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from audit.common import DAYS, MAIN, OUT, UPTO, db, m1
from sp2l.marketdata.history import parse_bars
from sp2l.smc.history import load_bars
from sp2l.smc.timeframes import MINUTE, aggregate, bucket_start, length

FIELDS = ("open", "high", "low", "close", "volume")


def holes(bars: list[Any]) -> dict[str, Any]:
    gaps: list[tuple[str, int]] = []
    dup = back = 0
    for a, b in zip(bars, bars[1:], strict=False):
        d = b.open_time - a.open_time
        if d == timedelta(0):
            dup += 1
        elif d < timedelta(0):
            back += 1
        elif d > MINUTE:
            gaps.append((a.open_time.isoformat(), int(d / MINUTE) - 1))
    by_len = Counter(
        "1" if n == 1 else "2-4" if n < 5 else "5-29" if n < 30 else "30-1439" if n < 1440 else ">=1440"
        for _, n in gaps
    )
    expected = int((bars[-1].open_time - bars[0].open_time) / MINUTE) + 1
    bad = {
        "nonpositive": sum(1 for b in bars if min(b.open, b.high, b.low, b.close) <= 0),
        "high_below_body": sum(1 for b in bars if b.high < max(b.open, b.close)),
        "low_above_body": sum(1 for b in bars if b.low > min(b.open, b.close)),
        "high_below_low": sum(1 for b in bars if b.high < b.low),
    }
    return {
        "first": bars[0].open_time.isoformat(),
        "last": bars[-1].open_time.isoformat(),
        "minutes_present": len(bars),
        "minutes_expected": expected,
        "missing_minutes": expected - len(bars),
        "holes": len(gaps),
        "holes_by_length": dict(by_len),
        "longest": sorted(gaps, key=lambda g: -g[1])[:5],
        "duplicates": dup,
        "out_of_order": back,
        "impossible_ohlc": bad,
        "zero_volume_minutes": sum(1 for b in bars if b.volume == 0),
        "flat_zero_volume_minutes": sum(
            1 for b in bars if b.volume == 0 and b.open == b.high == b.low == b.close
        ),
    }


def sources(symbol: str) -> dict[str, Any]:
    """Which table each minute of the merged series comes from, and agreement where both."""
    a, b = UPTO - timedelta(days=DAYS), UPTO
    with db().connect() as c:
        live = c.execute(
            text(
                "SELECT count(*) FROM candles_1m WHERE symbol=:s AND open_time>=:a AND open_time<:b"
            ),
            {"s": symbol, "a": a, "b": b},
        ).scalar_one()
        both = c.execute(
            text(
                "SELECT count(*), sum((l.open<>h.open)::int), sum((l.high<>h.high)::int),"
                " sum((l.low<>h.low)::int), sum((l.close<>h.close)::int),"
                " sum((l.volume<>h.volume)::int) FROM candles_1m l JOIN exchange_m1 h"
                " USING (symbol, open_time) WHERE symbol=:s AND open_time>=:a AND open_time<:b"
            ),
            {"s": symbol, "a": a, "b": b},
        ).one()
    return {
        "live_collector_minutes": live,
        "overlap_with_chart": both[0],
        "overlap_mismatch": dict(zip(FIELDS, [int(x or 0) for x in both[1:]], strict=True)),
    }


def chunk_edges(symbol: str) -> list[Any]:
    """(first, last) minute of every stored chunk (rows written in one transaction share
    `fetched_at`)."""
    with db().connect() as c:
        rows = c.execute(
            text(
                "SELECT fetched_at, min(open_time), max(open_time), count(*) FROM exchange_m1"
                " WHERE symbol=:s AND open_time>=:a AND open_time<:b GROUP BY 1 ORDER BY 2"
            ),
            {"s": symbol, "a": UPTO - timedelta(days=DAYS), "b": UPTO},
        ).all()
    return [(r[1], r[2], r[3]) for r in rows]


def fetch(symbol: str, a: Any, b: Any) -> dict[Any, Any]:
    """Tabdeal's chart for [a, b): requested 5 minutes early, lead dropped (the first bar of a
    response is unreliable, docs/diagnostics/findings.md item 2)."""
    from sp2l.marketdata.tabdeal_public import chart_history
    from sp2l.marketdata.tabdeal_ws import ws_market

    for attempt in range(3):
        try:
            raw = chart_history(ws_market(symbol), "1", a - timedelta(minutes=5), b + MINUTE, 60)
            bars = parse_bars(raw) or {}
            return {t: c for t, c in bars.items() if a <= t < b}
        except Exception:  # network: retry
            time.sleep(2 + attempt * 3)
    return {}


def compare(stored: dict[Any, Any], got: dict[Any, Any]) -> dict[str, Any]:
    common = sorted(set(stored) & set(got))
    mism = {f: 0 for f in FIELDS}
    rel = {f: Decimal(0) for f in FIELDS}
    examples = []
    for t in common:
        s, g = stored[t], got[t]
        for f in FIELDS:
            x, y = getattr(s, f), getattr(g, f)
            if x != y:
                mism[f] += 1
                if f != "volume" and y:
                    rel[f] = max(rel[f], abs(x - y) / y)
                if len(examples) < 8:
                    examples.append((t.isoformat(), f, str(x), str(y)))
    return {
        "compared": len(common),
        "only_stored": len(set(stored) - set(got)),
        "only_exchange": len(set(got) - set(stored)),
        "mismatch": mism,
        "mismatch_rate": {f: round(mism[f] / len(common), 5) if common else None for f in FIELDS},
        "max_rel_diff": {f: str(rel[f]) for f in FIELDS if f != "volume"},
        "examples": examples,
    }


def exchange_sample(symbol: str, bars: list[Any], seed: int = 7) -> dict[str, Any]:
    """24 random 60-minute windows over the period (>= 1,000 minutes)."""
    rng = random.Random(seed)
    idx = {b.open_time: b for b in bars}
    span = (bars[-1].open_time - bars[0].open_time) / MINUTE
    stored: dict[Any, Any] = {}
    got: dict[Any, Any] = {}
    for w in range(24):  # stratified: one window per 1/24 of the period
        start = bars[0].open_time + MINUTE * int(span * (w + rng.random()) / 24)
        start = start.replace(second=0, microsecond=0)
        end = min(start + timedelta(minutes=60), UPTO - timedelta(hours=1))
        got.update(fetch(symbol, start, end))
        stored.update({t: c for t, c in idx.items() if start <= t < end})
    return compare(stored, got)


def boundary_sample(symbol: str, bars: list[Any], n: int = 30, seed: int = 11) -> dict[str, Any]:
    """The first and last minute of n random stored chunks (+-3 minutes around each edge)."""
    rng = random.Random(seed)
    edges = [e for e in chunk_edges(symbol) if e[2] > 30]
    pick = rng.sample(edges, min(n, len(edges)))
    idx = {b.open_time: b for b in bars}
    stored: dict[Any, Any] = {}
    got: dict[Any, Any] = {}
    edge_t: list[Any] = []
    for first, last, _ in pick:
        for t in (first, last):
            a, b = t - timedelta(minutes=3), t + timedelta(minutes=4)
            got.update(fetch(symbol, a, b))
            stored.update({k: c for k, c in idx.items() if a <= k < b})
            edge_t.append(t)
    out = compare(stored, got)
    on_edge = compare({t: stored[t] for t in edge_t if t in stored}, got)
    out["edge_minutes_only"] = {k: on_edge[k] for k in ("compared", "mismatch", "examples")}
    out["chunks_stored"] = len(edges)
    return out


def htf_consistency(symbol: str, bars: list[Any]) -> dict[str, Any]:
    """Backtest aggregation (Python) vs live aggregation (SQL) of every timeframe."""
    out: dict[str, Any] = {}
    upto = bars[-1].open_time + MINUTE
    present = {b.open_time for b in bars}
    for grid in ("utc", "tehran"):
        for tf in ("5m", "15m", "1h", "4h", "1d"):
            py = aggregate(bars, tf, upto, grid)
            n = int(timedelta(days=DAYS) / length(tf)) + 2
            sql = [b for b in load_bars(db(), symbol, tf, n, upto, grid=grid) if b.open_time >= py[0].open_time]
            sq = {b.open_time: b for b in sql}
            diff = sum(
                1
                for b in py
                if b.open_time not in sq
                or any(getattr(b, f) != getattr(sq[b.open_time], f) for f in ("open", "high", "low", "close"))
            )
            minutes = int(length(tf) / MINUTE)
            partial = [
                b.open_time
                for b in py
                if sum(1 for k in range(minutes) if b.open_time + MINUTE * k in present) < minutes
            ]
            first_partial = py[0].open_time < bars[0].open_time or (
                bucket_start(bars[0].open_time, tf, grid) != bars[0].open_time and py[0].open_time == bucket_start(bars[0].open_time, tf, grid)
            )
            out[f"{grid}:{tf}"] = {
                "bars": len(py),
                "python_vs_sql_mismatch": diff,
                "sql_only": len(set(sq) - {b.open_time for b in py}),
                "bars_from_incomplete_buckets": len(partial),
                "first_bar_partial": bool(first_partial),
            }
    return out


def main() -> None:
    res: dict[str, Any] = {"upto": UPTO.isoformat(), "days": DAYS}
    for s in MAIN:
        bars = m1(s)
        r = {"A1": holes(bars), "sources": sources(s), "A4": htf_consistency(s, bars)}
        r["A2"] = exchange_sample(s, bars)
        r["A3"] = boundary_sample(s, bars)
        res[s] = r
        print(s, json.dumps(r, default=str)[:600])
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "data.json").write_text(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
