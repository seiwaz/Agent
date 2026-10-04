"""Public (unauthenticated) Tabdeal FAPI reads. No API key is used here.

Tabdeal futures REST uses the market name with an underscore (BTC_USDT), like the stream;
the docs' "BTCUSDT" examples return "Invalid symbol" (code 1208) - observed 2026-09-26.

Exchange info documents only pricePrecision / quantityPrecision (docs 2026-09-26); filters
derived from them are PROVISIONAL (signals / development only).
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sp2l.marketdata.tabdeal_ws import ws_market


@dataclass(frozen=True, slots=True)
class ExchangeFilters:
    tick: Decimal
    step: Decimal
    min_qty: Decimal | None
    min_notional: Decimal | None
    verified: bool  # established by a runtime probe; precision-derived = False

BASE = "https://api1.tabdeal.org"


def get_json(path: str, timeout: float = 10.0) -> Any:
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "sp2l/5"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https host
        return json.loads(resp.read().decode())


def exchange_symbol(symbol: str) -> str:
    """Internal symbol (BTCUSDT) -> Tabdeal futures REST/stream market name (BTC_USDT)."""
    return ws_market(symbol)


def provisional_filters(symbol: str) -> ExchangeFilters:
    market = exchange_symbol(symbol)
    data = get_json(f"/r/fapi/v1/exchangeInfo?symbol={market}")
    rows = [s for s in data.get("symbols", []) if s.get("symbol") == market]
    if not rows:
        raise ValueError(f"{symbol} not listed in Tabdeal FAPI exchangeInfo")
    s = rows[0]
    tick = Decimal(1).scaleb(-int(s["pricePrecision"]))
    step = Decimal(1).scaleb(-int(s["quantityPrecision"]))
    return ExchangeFilters(tick=tick, step=step, min_qty=None, min_notional=None, verified=False)
