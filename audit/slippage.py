"""D3. Slippage evidence for the markets actually traded (the configured allowance was
measured on XAUT_USDT). Same method as scripts/cost_evidence.py, read-only public data:
p99(half-spread of the order book) + p99(overshoot of a stop level placed inside a price jump
by the first trade through it), from the public recent-trades tape polled while sampling.

Usage: uv run python -m audit.slippage [samples] [every_s] -> docs/audit/slippage.json
"""

from __future__ import annotations

import json
import random
import sys
import time
import urllib.request
from datetime import UTC, datetime
from typing import Any

from audit.common import OUT

BASE = "https://api-web.tabdeal.org"


def get(path: str) -> dict[str, Any]:
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "sp2l-audit"})
    with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 - fixed https host
        return json.load(r)  # type: ignore[no-any-return]


def q(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(p * len(s)))]


def main() -> None:
    samples = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    every = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
    markets = ["BTC_USDT", "XRP_USDT"]
    spreads: dict[str, list[float]] = {m: [] for m in markets}
    polled: dict[str, dict[tuple[str, str, str], float]] = {m: {} for m in markets}
    for i in range(samples):
        for m in markets:
            try:
                ob = get(f"/special-margin/order-book/?symbol={m}")
                for t in get(f"/special-margin/recent-trades/?symbol={m}").get("trades", []):
                    polled[m][(str(t["created"]), str(t["price"]), str(t["amount"]))] = float(t["price"])
                ask, bid = float(ob["asks"][0]["price"]), float(ob["bids"][0]["price"])
                spreads[m].append((ask - bid) / 2 / ((ask + bid) / 2))
            except Exception as e:  # network: skip this sample
                print(m, "sample failed", repr(e), flush=True)
        if i + 1 < samples:
            time.sleep(every)
    out: dict[str, Any] = {"generated_at": datetime.now(UTC).isoformat(), "samples": samples}
    rng = random.Random(0)
    for m in markets:
        px = [polled[m][k] for k in sorted(polled[m])]
        over = []
        for p0, p1 in zip(px, px[1:], strict=False):
            if p1 != p0:
                lvl = p0 + (p1 - p0) * rng.random()
                over.append(abs(p1 - lvl) / lvl)
        sp = spreads[m]
        out[m] = {
            "order_book_samples": len(sp),
            "half_spread": {k: q(sp, v) for k, v in (("p50", 0.5), ("p90", 0.9), ("p99", 0.99))} if sp else None,
            "tape_trades": len(px),
            "price_changes": len(over),
            "stop_overshoot": {k: q(over, v) for k, v in (("p50", 0.5), ("p90", 0.9), ("p99", 0.99))} | {"max": max(over)} if over else None,
            "allowance_p99_sum": (q(sp, 0.99) + q(over, 0.99)) if sp and over else None,
        }
    (OUT / "slippage.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
