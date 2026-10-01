"""Candle forensics (read-only): canonical M1 vs Tabdeal's own chart, by lineage; B48 rates.

Tabdeal's chart bars (special-margin/plots/history) are TradingView-continuous: open = the
previous bar's close and high/low include that open; trades are bucketed by Tabdeal record time.
They are compared under that model: expected = (prev_close, max(prev_close, H), min(prev_close,
L), C, V). A remaining difference is classified by the layer that explains it.

    uv run python scripts/candle_forensics.py --config config/server.yaml --hours 3
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, text

from sp2l.config import RuntimeConfig
from sp2l.marketdata.tabdeal_public import chart_history
from sp2l.marketdata.tabdeal_ws import ws_market


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/runtime.yaml")
    ap.add_argument("--hours", type=float, default=3.0)
    ap.add_argument("--since", default=None, help="ISO start (overrides --hours)")
    a = ap.parse_args()
    cfg = RuntimeConfig.load(Path(a.config))
    db = create_engine(cfg.database_url)
    now = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=2)
    since = datetime.fromisoformat(a.since) if a.since else now - timedelta(hours=a.hours)
    with db.connect() as c:
        rows = c.execute(
            text(
                "SELECT open_time, open, high, low, close, volume, trade_count, quality,"
                " rest_only_trade_count FROM candles_1m WHERE symbol = :s AND open_time >= :a"
                " AND open_time < :b ORDER BY open_time"
            ),
            {"s": cfg.symbol, "a": since, "b": now},
        ).all()
        prov = c.execute(
            text(
                "SELECT date_trunc('hour', exch_ts) AS h, COUNT(*) AS n,"
                " COUNT(*) FILTER (WHERE source = 'REST') AS rest_only,"
                " COUNT(*) FILTER (WHERE sources LIKE '%REST%') AS rest_seen"
                " FROM raw_trades WHERE symbol = :s AND exch_ts >= :a AND exch_ts < :b"
                " AND sources IS NOT NULL GROUP BY 1 ORDER BY 1"
            ),
            {"s": cfg.symbol, "a": since, "b": now},
        ).all()
    hist: dict[datetime, dict[str, float]] = {}
    t = since - timedelta(minutes=1)
    while t < now:
        e = min(t + timedelta(minutes=290), now)
        for b in chart_history(ws_market(cfg.symbol), "1", t, e):
            hist[datetime.fromtimestamp(b["time"], UTC)] = b
        t = e
    res: dict[str, Counter[str]] = {}
    examples: list[dict[str, object]] = []
    for r in rows:
        ot = r[0].astimezone(UTC)
        h, hp = hist.get(ot), hist.get(ot - timedelta(minutes=1))
        cnt = res.setdefault(r[7], Counter())
        cnt["minutes"] += 1
        if h is None or hp is None:
            cnt["no_tabdeal_bar"] += 1
            continue
        pc = Decimal(str(hp["close"]))
        exp = (pc, max(pc, r[2]), min(pc, r[3]), r[4], r[5])
        got = tuple(Decimal(str(h[k])) for k in ("open", "high", "low", "close", "volume"))
        diff = [n for n, x, y in zip("OHLCV", exp, got, strict=True) if x != y]
        cnt["exact" if not diff else "differs"] += 1
        for n in diff:
            cnt[n] += 1
        if diff and len(examples) < 12:
            examples.append(
                {
                    "minute": ot.isoformat(),
                    "quality": r[7],
                    "fields": diff,
                    "ours": [str(x) for x in exp],
                    "tabdeal": [str(x) for x in got],
                    "rest_only": r[8],
                }
            )
    print(
        json.dumps(
            {
                "window": [since.isoformat(), now.isoformat()],
                "by_quality": {k: dict(v) for k, v in res.items()},
                "rest_reconciliation_per_hour": [
                    {
                        "hour": p[0].isoformat(),
                        "canonical": p[1],
                        "rest_only": p[2],
                        "rest_only_pct": round(100 * p[2] / p[1], 2) if p[1] else None,
                        "seen_by_rest": p[3],
                    }
                    for p in prov
                ],
                "examples": examples,
            },
            indent=1,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
