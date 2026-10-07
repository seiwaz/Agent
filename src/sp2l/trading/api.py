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
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Query
from fastapi.responses import JSONResponse
from sqlalchemy import Engine

from sp2l.trading.client import ExchangeError
from sp2l.trading.manager import TradeError, TradeManager, TradingConfig

log = logging.getLogger("sp2l.trading")
JSON_BODY = Body(...)


class TradingDesk:
    def __init__(self, cfg: Any, db: Engine, manager: TradeManager | None = None) -> None:
        self.symbols: list[str] = [str(s) for s in cfg.symbols]
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
