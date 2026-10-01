"""Tabdeal futures public trade stream (docs.tabdeal.org, "special_margin/broadcast").

Connect to WS_URL, send the plain-text market name (e.g. "BTC_USDT"), then receive one
JSON message per trade. Format observed on 2026-09-26:

    {"trade": {"amount": "0.00005", "price": "84026.2",
               "updated": "2026-09-26 15:43:40.031000+00:00",
               "sequence": 38127895806, "side_name": "Buy", "symbol": "BTC_USDT"}}

`sequence` is increasing but NOT contiguous and NOT a trade identity: one sequence can carry
several fills (observed 2026-09-26/27: up to 23 fills, different price/amount, occasionally
1 ms apart). De-duplication uses the fingerprint in collector.py; missed trades cannot be
detected from `sequence`, and coverage comes from connection liveness instead.
An invalid subscription returns {"error": ...} and the server closes the connection.
No initial snapshot is sent.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sp2l.marketdata.m1_builder import Trade

WS_URL = "wss://api1.tabdeal.org/special_margin/broadcast/"
QUOTES = ("USDT", "IRT")


class StreamError(RuntimeError):
    """The server reported an error for the subscription."""


def ws_market(symbol: str) -> str:
    """REST symbol -> broadcast market name: BTCUSDT -> BTC_USDT."""
    for q in QUOTES:
        if symbol.endswith(q) and len(symbol) > len(q):
            return f"{symbol[: -len(q)]}_{q}"
    raise ValueError(f"cannot map symbol {symbol!r} to a Tabdeal futures market")


def parse_message(raw: str | bytes, recv_ts: datetime, market: str) -> Trade | None:
    """Parse one broadcast frame. Returns None for frames that are not trades of `market`."""
    text = raw.decode() if isinstance(raw, bytes) else raw
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if "error" in data:
        raise StreamError(str(data["error"]))
    t = data.get("trade")
    if not isinstance(t, dict) or t.get("symbol") != market:
        return None
    try:
        exch_ts = datetime.fromisoformat(str(t["updated"]))
        if exch_ts.tzinfo is None:
            exch_ts = exch_ts.replace(tzinfo=UTC)
        side = str(t.get("side_name", "")).upper()
        return Trade(
            trade_id=str(int(t["sequence"])),
            exch_ts=exch_ts.astimezone(UTC),
            recv_ts=recv_ts,
            price=Decimal(str(t["price"])),
            qty=Decimal(str(t["amount"])),
            taker_side=side if side in ("BUY", "SELL") else None,
            raw=text,
        )
    except (KeyError, ValueError, InvalidOperation, TypeError):
        return None
