"""Shadow cost evidence from Tabdeal public data (read-only). B16: never hardcoded.

- Fees: Tabdeal's public special-margin commission table (get-commission-levels-v2), the
  same table Tabdeal's futures UI uses. The account level is selected by 30-day volume.
- SL slippage allowance (Tabdeal publishes none): measured.
    a) half-spread of the public special-margin order book (sampled), and
    b) overshoot of a stop level by the first trade through it, from the canonical trade tape:
       a level uniformly placed inside a price jump p[i-1] -> p[i] is filled at p[i].
  allowance = p99(half-spread) + p99(overshoot), as a fraction of price (conservative sum).

    uv run python scripts/cost_evidence.py --config config/server.yaml --samples 90 --every 10
"""

from __future__ import annotations

import argparse
import json
import random
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, text

from sp2l.config import RuntimeConfig
from sp2l.marketdata.tabdeal_ws import ws_market

MIN_DB_TAPE = 2000  # below this (e.g. a newly configured symbol) the polled public tape is used

BASE = "https://api-web.tabdeal.org"


def get(path: str) -> dict:  # type: ignore[type-arg]
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "sp2l/5.7"})
    with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 - fixed https host
        return json.load(r)  # type: ignore[no-any-return]


def q(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * (len(xs) - 1)))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/runtime.yaml")
    ap.add_argument("--samples", type=int, default=90)
    ap.add_argument("--every", type=float, default=10.0)
    ap.add_argument("--volume-30d-usdt", type=float, default=0.0)
    ap.add_argument("--out", default="docs/cost_evidence.json")
    a = ap.parse_args()
    cfg = RuntimeConfig.load(Path(a.config))
    levels = get("/get-commission-levels-v2/")["special_margin_commission_levels"]
    level = next(
        lv
        for lv in levels
        if Decimal(lv["from_volume"]) <= Decimal(a.volume_30d_usdt) < Decimal(lv["to_volume"])
    )
    market = ws_market(cfg.symbol)  # the configured symbol, e.g. XAUTUSDT -> XAUT_USDT
    spreads, tops = [], []
    polled: dict[tuple[str, str, str], float] = {}  # public recent-trades seen while sampling
    rules = None
    for i in range(a.samples):
        ob = get(f"/special-margin/order-book/?symbol={market}")
        for t in get(f"/special-margin/recent-trades/?symbol={market}").get("trades", []):
            polled[(str(t["created"]), str(t["price"]), str(t["amount"]))] = float(t["price"])
        ask, bid = float(ob["asks"][0]["price"]), float(ob["bids"][0]["price"])
        mid = (ask + bid) / 2
        spreads.append((ask - bid) / 2 / mid)
        tops.append(min(float(ob["asks"][0]["amount"]), float(ob["bids"][0]["amount"])))
        rules = ob["market_info"]["market_rules"]
        if i + 1 < a.samples:
            time.sleep(a.every)
    db = create_engine(cfg.database_url)
    since = datetime.now(UTC) - timedelta(hours=24)
    with db.connect() as c:
        px = [
            float(r[0])
            for r in c.execute(
                text(
                    "SELECT price FROM raw_trades WHERE symbol = :s AND exch_ts >= :a AND NOT late"
                    " AND (sources IS NULL OR sources NOT LIKE 'DUPLICATE_OF:%')"
                    " ORDER BY exch_ts, recv_ts, trade_id"
                ),
                {"s": cfg.symbol, "a": since},
            )
        ]
    tape_source = "raw_trades (canonical, last 24 h)"
    if len(px) < MIN_DB_TAPE:  # not enough canonical history yet for this symbol
        px = [polled[k] for k in sorted(polled)]
        tape_source = "public recent-trades polled during the order-book sampling"
    rng = random.Random(0)
    over = []
    for p0, p1 in zip(px, px[1:], strict=False):
        if p1 != p0:
            level_ = p0 + (p1 - p0) * rng.random()  # stop level inside the jump
            over.append(abs(p1 - level_) / level_)
    half99, over99 = q(spreads, 0.99), q(over, 0.99)
    ev = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_fees": BASE + "/get-commission-levels-v2/ (special_margin_commission_levels)",
        "fee_level": level,
        "fee_level_basis": f"30-day special-margin volume {a.volume_30d_usdt} USDT",
        "maker_fee": level[
            "maker_commission_percent"
        ],  # a fraction (Tabdeal UI multiplies notional)
        "taker_fee": level["taker_commission_percent"],
        "order_book_samples": len(spreads),
        "half_spread_frac": {
            "p50": q(spreads, 0.5),
            "p90": q(spreads, 0.9),
            "p99": half99,
            "max": max(spreads),
        },
        "market": market,
        "top_of_book_min_amount_base": {"p1": q(tops, 0.01), "p50": q(tops, 0.5)},
        "trade_tape": {"trades": len(px), "since": since.isoformat(), "source": tape_source},
        "stop_overshoot_frac": {
            "p50": q(over, 0.5),
            "p90": q(over, 0.9),
            "p99": over99,
            "max": max(over),
        },
        "slippage_allowance": str(
            Decimal(str(half99 + over99)).quantize(Decimal("0.000001"), rounding="ROUND_UP")
        ),
        "slippage_method": (
            "p99(half-spread) + p99(stop overshoot by first trade through the level)"
        ),
        "market_rules_public": rules,
    }
    Path(a.out).write_text(json.dumps(ev, indent=2, default=str) + "\n")
    print(
        json.dumps(
            {
                k: ev[k]
                for k in (
                    "maker_fee",
                    "taker_fee",
                    "slippage_allowance",
                    "half_spread_frac",
                    "stop_overshoot_frac",
                )
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
