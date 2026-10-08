"""The dashboard's trading endpoints (`/api/trade/*`) and the reconcile poller.

Off unless `trading.enabled: true`, and only behind the dashboard login (`auth.enabled`,
sp2l/api/auth.py): the API middleware refuses every request without a valid session and lets
POST through only under `/api/trade/` and from the dashboard's own origin (the session cookie is
SameSite=Strict as well). Credentials are read from `trading.credentials_file` and never leave
the process.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Query
from fastapi.responses import JSONResponse
from sqlalchemy import Engine

from sp2l.trading.client import ExchangeError
from sp2l.trading.manager import TradeError, TradeManager, TradingConfig

log = logging.getLogger("sp2l.trading")
JSON_BODY = Body(...)
PRICE_TTL_S = 1.0  # the live price of a market is fetched at most once a second


def last_price(market: str) -> tuple[float, float]:
    """(price, epoch s) of the newest trade on Tabdeal's futures market (public feed)."""
    from sp2l.marketdata.tabdeal_public import recent_trades

    t = recent_trades(market, timeout=3.0)
    if not t:
        raise ValueError(f"no recent trades on {market}")
    return float(t[-1].price), t[-1].created.timestamp()


class Prices:
    """The live price per market for the trades table, cached PRICE_TTL_S."""

    def __init__(self, fetch: Callable[[str], tuple[float, float]] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.fetch, self.clock = fetch or last_price, clock
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def get(self, symbol: str) -> dict[str, Any]:
        from sp2l.marketdata.tabdeal_ws import ws_market

        with self._lock:
            hit = self._cache.get(symbol)
            if hit is not None and self.clock() - hit[0] < PRICE_TTL_S:
                return hit[1]
            try:
                price, at = self.fetch(ws_market(symbol))
                out: dict[str, Any] = {"price": price, "at": at}
            except (OSError, ValueError) as e:
                out = {"price": None, "error": str(e)[:200]}
            self._cache[symbol] = (self.clock(), out)
            return out


class TradingDesk:
    def __init__(self, cfg: Any, db: Engine, manager: TradeManager | None = None,
                 prices: Prices | None = None) -> None:
        self.symbols: list[str] = [str(s) for s in cfg.symbols]
        self.prices = prices or Prices()
        self.problem: str | None = None
        self.manager: TradeManager | None = manager
        try:
            self.cfg = TradingConfig.from_mapping(dict(cfg.section("trading")))
        except ValueError as e:
            self.cfg, self.problem = TradingConfig(), str(e)
            return
        if not self.cfg.enabled or manager is not None:
            return
        from sp2l.secrets import CredentialError, load_credentials
        from sp2l.trading.client import TabdealTrade

        try:
            creds = load_credentials(Path(self.cfg.credentials_file).expanduser())
        except (CredentialError, OSError) as e:  # trading stays off; the dashboard still runs
            self.problem = f"Tabdeal credentials: {e}"
            return
        self.manager = TradeManager(
            db, TabdealTrade(creds), self.cfg, {s: cfg.instrument(s) for s in self.symbols}
        )

    def require_login(self, login_on: bool) -> None:
        """Real orders only behind the dashboard login."""
        if self.cfg.enabled and not login_on:
            self.manager = None
            self.problem = "trading needs the dashboard login: set auth.enabled: true"

    # ---- the poller (started and stopped with the API server) -----------------------------------
    def start(self) -> threading.Event | None:
        if self.manager is None:
            return None
        stop = threading.Event()
        threading.Thread(target=self.manager.run, args=(stop,), name="trade-poller",
                         daemon=True).start()
        log.info("trading on: polling Tabdeal every %.0f s", self.cfg.poll_s)
        return stop

    # ---- guards -----------------------------------------------------------------------------
    def _do(self, f: Any) -> Any:
        if not self.cfg.enabled:
            return _err(403, "trading is off (trading.enabled in the config)")
        if self.manager is None:
            return _err(503, self.problem or "trading is not ready")
        try:
            return f(self.manager)
        except TradeError as e:
            return _err(400, str(e))
        except ExchangeError as e:
            return _err(502, f"Tabdeal: {e}")

    def _sym(self, symbol: str | None) -> str:
        if symbol is None:
            return self.symbols[0]
        if symbol not in self.symbols:
            raise TradeError(f"symbol must be one of {self.symbols}")
        return symbol

    # ---- routes -------------------------------------------------------------------------------
    def mount(self, app: FastAPI) -> None:
        @app.get("/api/trade/config")
        def trade_config() -> Any:
            return {**self.cfg.public(), "ready": self.manager is not None,
                    "problem": self.problem, "symbols": self.symbols,
                    "poll_error": self.manager.poll_error if self.manager else None}

        @app.get("/api/trade/prices")
        def trade_prices(symbols: str = Query("", max_length=200)) -> Any:
            """The latest traded price of each market (the live columns of the trades table)."""
            want = [s for s in symbols.split(",") if s in self.symbols] or self.symbols
            return {s: self.prices.get(s) for s in want}

        @app.get("/api/trade/raw")
        def trade_raw() -> Any:
            """What Tabdeal answers for the wallet and the positions, as is (no credentials):
            for comparing with Tabdeal's app when a number looks wrong."""
            def raw(m: TradeManager) -> Any:
                from sp2l.marketdata.tabdeal_ws import ws_market

                out: dict[str, Any] = {"balance": m.ex.balance()}
                for s in self.symbols:
                    out[f"positionRisk {s}"] = m.ex.position_risk(ws_market(s))
                out["wallet (as used here)"] = {k: str(v) for k, v in m.wallet().items()}
                return out

            return self._do(raw)

        @app.get("/api/trade/account")
        def trade_account(symbol: str | None = None) -> Any:
            return self._do(lambda m: m.account(self._sym(symbol)))

        @app.get("/api/trade/trades")
        def trade_list(
            symbol: str | None = None,
            scope: str = Query("open", pattern="^(open|history)$"),
            limit: int = Query(100, ge=1, le=500),
        ) -> Any:
            return self._do(lambda m: {
                "scope": scope,
                "items": m.trades(self._sym(symbol) if symbol else None, scope, limit),
                "poll_error": m.poll_error,
            })

        @app.post("/api/trade/open")
        def trade_open(body: dict[str, Any] = JSON_BODY) -> Any:
            return self._do(lambda m: m.open(body))

        @app.post("/api/trade/{tid}/cancel")
        def trade_cancel(tid: int) -> Any:
            return self._do(lambda m: m.cancel(tid))

        @app.post("/api/trade/{tid}/sltp")
        def trade_sltp(tid: int, body: dict[str, Any] = JSON_BODY) -> Any:
            return self._do(lambda m: m.set_sltp(tid, body.get("sl"), body.get("tp")))

        @app.post("/api/trade/{tid}/close")
        def trade_close(tid: int) -> Any:
            return self._do(lambda m: m.close(tid))


def _err(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)
