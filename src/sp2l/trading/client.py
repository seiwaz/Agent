"""Signed Tabdeal futures client for manual trades placed from the dashboard.

Only the endpoints listed in ENDPOINTS can be called, each with its own method
(docs.tabdeal.org v0.9.0, docs/tabdeal_endpoint_map.md). Signing as in the read-only client:
query string = urlencode(params + timestamp[ms] + recvWindow), signature = HMAC-SHA256(secret,
query string) hex, API key in the X-MBX-APIKEY header; the signed parameters travel in the query
string (the docs allow it for every signed request). The key and the secret are never logged or
returned.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from sp2l.secrets import Credentials

BASE = "https://api1.tabdeal.org"
ENDPOINTS: dict[tuple[str, str], str] = {
    ("GET", "/r/fapi/v1/time"): "server time",
    ("GET", "/r/fapi/v3/balance"): "balances",
    ("GET", "/r/fapi/v3/positionRisk"): "open position with mark price, PnL, liquidation",
    ("GET", "/r/fapi/v1/position"): "positions (id, status, realized PnL)",
    ("GET", "/r/fapi/v1/order"): "one order",
    ("GET", "/r/fapi/v1/openOrders"): "open orders",
    ("POST", "/fapi/v1/leverage"): "set leverage",
    ("POST", "/fapi/v1/order"): "new LIMIT order",
    ("DELETE", "/fapi/v1/order"): "cancel an order",
    ("POST", "/fapi/v1/positionSlTp"): "position stop loss / take profit",
    ("DELETE", "/fapi/v1/position"): "close the whole position at market",
}


class ExchangeError(RuntimeError):
    """The exchange refused the request (`code`, `msg` from its error body) or was unreachable."""

    def __init__(self, msg: str, code: int | None = None, status: int | None = None) -> None:
        super().__init__(msg)
        self.code, self.status = code, status


Opener = Callable[[urllib.request.Request, float], Any]


def _default_opener(req: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - fixed https host


class TabdealTrade:
    def __init__(
        self,
        creds: Credentials,
        *,
        base: str = BASE,
        recv_window: int = 5000,
        timeout: float = 10.0,
        opener: Opener = _default_opener,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._creds, self.base, self.recv_window = creds, base, recv_window
        self.timeout, self._open, self._clock = timeout, opener, clock

    def _signed(self, params: dict[str, Any]) -> str:
        full = {**params, "timestamp": int(self._clock() * 1000), "recvWindow": self.recv_window}
        qs = urlencode(full)
        sig = hmac.new(self._creds.api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        return f"{qs}&signature={sig}"

    def call(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        if (method, path) not in ENDPOINTS:
            raise PermissionError(f"{method} {path} is not an allowed endpoint")
        url = f"{self.base}{path}?{self._signed(dict(params or {}))}"
        headers = {
            "User-Agent": "sp2l-trade/1",
            "Accept": "application/json",
            "X-MBX-APIKEY": self._creds.api_key,
        }
        req = urllib.request.Request(url, headers=headers, method=method)
        try:
            with self._open(req, self.timeout) as resp:
                raw = resp.read().decode()
                status = resp.status
        except urllib.error.HTTPError as e:
            raw, status = e.read().decode(errors="replace"), e.code
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ExchangeError(f"exchange unreachable: {type(e).__name__}") from e
        try:
            body = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            body = {"msg": raw[:300]}
        # an error is an HTTP error status, or a {code, msg} body with a non-success code
        info = body if isinstance(body, dict) else {}
        err = "code" in info and "msg" in info
        if status >= 400 or (err and info.get("code") not in (0, 200)):
            code = info.get("code") if err else None
            raise ExchangeError(str(info.get("msg") or f"HTTP {status}"), code, status)
        return body

    # ---- the calls the trade manager uses ---------------------------------------------------
    def balance(self) -> Any:
        return self.call("GET", "/r/fapi/v3/balance")

    def position_risk(self, market: str) -> Any:
        return self.call("GET", "/r/fapi/v3/positionRisk", {"symbol": market})

    def positions(self, market: str, active: bool, limit: int = 20) -> Any:
        return self.call(
            "GET", "/r/fapi/v1/position",
            {"symbol": market, "isActive": 1 if active else 0, "limit": limit},
        )

    def order(self, market: str, order_id: int) -> Any:
        return self.call("GET", "/r/fapi/v1/order", {"symbol": market, "orderId": order_id})

    def open_orders(self, market: str) -> Any:
        return self.call("GET", "/r/fapi/v1/openOrders", {"symbol": market})

    def set_leverage(self, market: str, leverage: int) -> Any:
        return self.call("POST", "/fapi/v1/leverage", {"symbol": market, "leverage": leverage})

    def limit_order(
        self, market: str, side: str, qty: str, price: str, client_id: str
    ) -> Any:
        return self.call(
            "POST", "/fapi/v1/order",
            {"symbol": market, "side": side, "type": "LIMIT", "quantity": qty, "price": price,
             "timeInForce": "GTC", "newClientOrderId": client_id},
        )

    def cancel(self, market: str, order_id: int) -> Any:
        return self.call("DELETE", "/fapi/v1/order", {"symbol": market, "orderId": order_id})

    def position_sl_tp(
        self, position_id: int, market: str, sl: str | None, tp: str | None, working_type: str
    ) -> Any:
        params: dict[str, Any] = {"positionId": position_id, "symbol": market,
                                  "workingType": working_type}
        if sl is not None:
            params["slPrice"] = sl
        if tp is not None:
            params["tpPrice"] = tp
        return self.call("POST", "/fapi/v1/positionSlTp", params)

    def close_position(self, market: str) -> Any:
        return self.call("DELETE", "/fapi/v1/position", {"symbol": market})
