"""Tabdeal public, unauthenticated, READ-ONLY market data used by the Tabdeal web app.

Discovered 2026-09-27 from Tabdeal's own futures (special-margin) trading UI bundle
(TradingView datafeed and the recent-trades panel); not part of the documented FAPI:

- GET https://api-web.tabdeal.org/special-margin/recent-trades/?symbol=BTC_USDT
    -> {"trades": [{"created", "updated", "market_id", "amount", "price", "side",
                    "side_name", ...commission fields}]}, newest first, the last 50 trades,
       no paging. `created` is Tabdeal's own record time (microseconds, UTC); it trails the
       WebSocket `updated` time of the same trade by 1-163 ms (measured).
- GET https://api-web.tabdeal.org/special-margin/plots/history/?first_currency_symbol=BTC
      &second_currency_symbol=USDT&symbol=BTC_USDT&resolution=1&from=<s>&to=<s>&countback=N
    -> {"data": [{"time" (bar open, epoch s), "open", "high", "low", "close", "volume"}],
        "no_data"}. TradingView-style CONTINUOUS bars: open = previous bar's close and
       high/low include that open; trades bucketed by Tabdeal record time. Diagnostic only:
       not a canonical source (V5.6, BLOCKERS B47).

Only these two GET paths exist here; there is no way to reach any other endpoint.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

BASE = "https://api-web.tabdeal.org"
_ALLOWED = ("/special-margin/recent-trades/", "/special-margin/plots/history/")


@dataclass(frozen=True, slots=True)
class RestTrade:
    created: datetime
    price: Decimal
    qty: Decimal
    side: str | None
    raw: dict[str, Any]


def _get(path: str, params: dict[str, Any], timeout: float) -> Any:
    if path not in _ALLOWED:  # pragma: no cover - defensive
        raise ValueError(f"path not allowlisted: {path}")
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "sp2l/5.6"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https host
        return json.load(r)


def parse_recent_trades(payload: Any) -> list[RestTrade]:
    out = []
    for t in payload.get("trades", []):
        created = datetime.fromisoformat(str(t["created"]).replace("Z", "+00:00"))
        side = str(t.get("side_name", "")).upper()
        out.append(
            RestTrade(
                created=created.astimezone(UTC),
                price=Decimal(str(t["price"])),
                qty=Decimal(str(t["amount"])),
                side=side if side in ("BUY", "SELL") else None,
                raw=t,
            )
        )
    return sorted(out, key=lambda x: x.created)


def recent_trades(market: str, timeout: float = 10.0) -> list[RestTrade]:
    return parse_recent_trades(_get(_ALLOWED[0], {"symbol": market}, timeout))


def chart_history(
    market: str, resolution: str, start: datetime, end: datetime, timeout: float = 20.0
) -> list[dict[str, Any]]:
    base, quote = market.split("_")
    f, t = int(start.timestamp()), int(end.timestamp())
    step = 60 if resolution == "1" else int(resolution) * 60
    data = _get(
        _ALLOWED[1],
        {
            "first_currency_symbol": base,
            "second_currency_symbol": quote,
            "from": f,
            "to": t,
            "resolution": resolution,
            "countback": max(1, (t - f) // step),
            "symbol": market,
        },
        timeout,
    )
    return list(data.get("data", []))
