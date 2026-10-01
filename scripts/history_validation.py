"""Tier-3 source validation (read-only): Tabdeal chart history vs reconciled canonical M1.

Compares every canonical LIVE_RECONCILED minute in the window with Tabdeal's own chart bar
(special-margin/plots/history, resolution 1) field by field, under two models:

- RAW: the chart bar taken literally (O, H, L, C, V);
- CONTINUITY: TradingView continuous bars - open = previous bar's close and high/low include
  that open. Expected chart bar = (prev_close_chart, max(prev_close_chart, H), min(.., L), C, V).

A remaining difference is explained, when possible, by the boundary effect: the chart
buckets trades by Tabdeal record time (`created`), which trails the stream time by up to
~0.7 s; shifting canonical trades within `--shift-ms` of a minute boundary into the next
minute reproduces the chart bar. TradeCount is not provided by the source (reported).
Also compares M5 (resolution 5) with the canonical M5 built from canonical M1.

    uv run python scripts/history_validation.py --config config/server.yaml --hours 12
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

from sp2l.config import RuntimeConfig
from sp2l.marketdata.tabdeal_public import chart_history
from sp2l.marketdata.tabdeal_ws import ws_market

MIN = timedelta(minutes=1)


def fetch(market: str, res: str, start: datetime, end: datetime) -> dict[datetime, dict[str, Any]]:
    out: dict[datetime, dict[str, Any]] = {}
    step = timedelta(minutes=1000 if res == "1" else 5000)
    t = start
    while t < end:
        e = min(t + step, end)
        for b in chart_history(market, res, t, e):
            out[datetime.fromtimestamp(b["time"], UTC)] = b
        t = e
    return out


def d(x: Any) -> Decimal:
    return Decimal(str(x))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/runtime.yaml")
    ap.add_argument("--hours", type=float, default=12.0)
    ap.add_argument("--shift-ms", type=int, default=1000)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    cfg = RuntimeConfig.load(Path(a.config))
    db = create_engine(cfg.database_url)
    now = datetime.now(UTC).replace(second=0, microsecond=0) - 2 * MIN
    since = now - timedelta(hours=a.hours)
    with db.connect() as c:
        rows = c.execute(
            text(
                "SELECT open_time, open, high, low, close, volume, trade_count, quality"
                " FROM candles_1m WHERE symbol = :s AND open_time >= :a AND open_time < :b"
                " ORDER BY open_time"
            ),
            {"s": cfg.symbol, "a": since, "b": now},
        ).all()
        trades = c.execute(
            text(
                "SELECT exch_ts, price, qty FROM raw_trades WHERE symbol = :s AND exch_ts >= :a"
                " AND exch_ts < :b AND NOT late AND (sources IS NULL OR sources NOT LIKE"
                " 'DUPLICATE_OF:%') ORDER BY exch_ts, recv_ts, trade_id"
            ),
            {"s": cfg.symbol, "a": since - MIN, "b": now + MIN},
        ).all()
        m5rows = c.execute(
            text(
                "SELECT open_time, high, low, close, volume FROM candles_5m WHERE symbol = :s"
                " AND open_time >= :a AND open_time < :b ORDER BY open_time"
            ),
            {"s": cfg.symbol, "a": since, "b": now - 5 * MIN},
        ).all()
    market = ws_market(cfg.symbol)
    h1 = fetch(market, "1", since - 2 * MIN, now + MIN)
    h5 = fetch(market, "5", since - 10 * MIN, now + MIN)
    by_min: dict[datetime, list[tuple[datetime, Decimal, Decimal]]] = {}
    for ts, px, q in trades:
        ts = ts.astimezone(UTC)
        by_min.setdefault(ts.replace(second=0, microsecond=0), []).append((ts, px, q))

    def shifted_bar(m: datetime, ms: int) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
        """Canonical trades re-bucketed with trades in the last `ms` of each minute moved on."""
        cut = timedelta(milliseconds=ms)
        own = [t for t in by_min.get(m, []) if t[0] < m + MIN - cut]
        carried = [t for t in by_min.get(m - MIN, []) if t[0] >= m - cut]
        ts = carried + own
        if not ts:
            return None
        return (max(t[1] for t in ts), min(t[1] for t in ts), ts[-1][1], sum(t[2] for t in ts))

    fields = ("O", "H", "L", "C", "V")
    res: dict[str, Counter[str]] = {"RAW": Counter(), "CONTINUITY": Counter()}
    hl_diff: list[Decimal] = []
    explained = Counter[str]()
    mism: list[dict[str, Any]] = []
    compared = 0
    for r in rows:
        ot = r[0].astimezone(UTC)
        if r[7] != "LIVE_RECONCILED":
            continue
        h, hp = h1.get(ot), h1.get(ot - MIN)
        if h is None or hp is None:
            res["RAW"]["no_chart_bar"] += 1
            continue
        compared += 1
        got = tuple(d(h[k]) for k in ("open", "high", "low", "close", "volume"))
        ours = (r[1], r[2], r[3], r[4], r[5])
        pc = d(hp["close"])
        cont = (pc, max(pc, r[2]), min(pc, r[3]), r[4], r[5])
        for model, exp in (("RAW", ours), ("CONTINUITY", cont)):
            diff = [n for n, x, y in zip(fields, exp, got, strict=True) if x != y]
            res[model]["exact" if not diff else "differs"] += 1
            for n in diff:
                res[model][n] += 1
            if model == "CONTINUITY" and diff:
                hl_diff.append(max(abs(cont[1] - got[1]), abs(cont[2] - got[2])))
                why = "unexplained"
                for ms in (100, 250, 500, 750, a.shift_ms):
                    sb = shifted_bar(ot, ms)
                    if sb is None:
                        continue
                    exp_s = (max(pc, sb[0]), min(pc, sb[1]), sb[2], sb[3])
                    if exp_s == got[1:]:
                        why = f"record_time_boundary<={ms}ms"
                        break
                else:
                    if got[4] == cont[4] and got[1:3] == cont[1:3]:
                        why = "same_trades_close_order"
                explained[why] += 1
                if len(mism) < 40:
                    mism.append(
                        {
                            "minute": ot.isoformat(),
                            "fields": diff,
                            "canonical_continuity": [str(x) for x in cont],
                            "tabdeal": [str(x) for x in got],
                            "explanation": why,
                        }
                    )
    m5 = Counter[str]()
    for r in m5rows:
        ot = r[0].astimezone(UTC)
        h = h5.get(ot)
        hp = h1.get(ot - MIN)
        if h is None or hp is None:
            m5["no_chart_bar"] += 1
            continue
        pc = d(hp["close"])
        exp = (max(pc, r[1]), min(pc, r[2]), r[3], r[4])
        got5 = (d(h["high"]), d(h["low"]), d(h["close"]), d(h["volume"]))
        m5["compared"] += 1
        m5["exact" if exp == got5 else "differs"] += 1
        for n, x, y in zip("HLCV", exp, got5, strict=True):
            if x != y:
                m5[n] += 1
    hl_sorted = sorted(hl_diff)
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "window": [since.isoformat(), now.isoformat()],
        "source": "GET /special-margin/plots/history/ (public, unauthenticated)",
        "compared_minutes": compared,
        "m1": {k: dict(v) for k, v in res.items()},
        "m1_exact_rate_continuity": round(res["CONTINUITY"]["exact"] / compared, 4)
        if compared
        else None,
        "mismatch_explanations": dict(explained),
        "hl_abs_diff": {
            "n": len(hl_sorted),
            "p50": str(hl_sorted[len(hl_sorted) // 2]) if hl_sorted else None,
            "max": str(hl_sorted[-1]) if hl_sorted else None,
        },
        "trade_count_in_source": False,
        "m5": dict(m5),
        "examples": mism,
    }
    s = json.dumps(report, indent=1, default=str)
    if a.out:
        Path(a.out).write_text(s + "\n")
    print(s)


if __name__ == "__main__":
    main()
