"""The dashboard's trading endpoints (`/api/trade/*`) and the reconcile poller.

Off unless `trading.enabled: true`. Every endpoint except `/api/trade/config` needs the trade
token in the `X-Trade-Token` header (the file `trading.token_file`, mode 600, written by
`python -m sp2l trade-token`); the API middleware lets POST through only under `/api/trade/` and
only from the dashboard's own origin. Credentials are read from `trading.credentials_file` and
never leave the process.
"""

from __future__ import annotations

import hmac
import logging
import threading
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Header, Query
from fastapi.responses import JSONResponse
from sqlalchemy import Engine

from sp2l.trading.client import ExchangeError
from sp2l.trading.manager import TradeError, TradeManager, TradingConfig, load_token

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
        self.token_path = Path(self.cfg.token_file).expanduser()
        if not self.cfg.enabled or manager is not None:
            return
        from sp2l.secrets import CredentialError, load_credentials
        from sp2l.trading.client import TabdealTrade

        try:
            creds = load_credentials(Path(self.cfg.credentials_file).expanduser())
        except CredentialError as e:
            self.problem = f"Tabdeal credentials: {e}"
            return
        self.manager = TradeManager(
            db, TabdealTrade(creds), self.cfg, {s: cfg.instrument(s) for s in self.symbols}
        )

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
    def _guard(self, token: str | None) -> JSONResponse | TradeManager:
        if not self.cfg.enabled:
            return _err(403, "trading is off (trading.enabled in the config)")
        if self.manager is None:
            return _err(503, self.problem or "trading is not ready")
        want = load_token(self.token_path)
        if want is None:
            return _err(503, "no trade token on the server: run `python -m sp2l trade-token`")
        if not token or not hmac.compare_digest(token.encode(), want.encode()):
            return _err(401, "wrong or missing trade token")
        return self.manager

    def _do(self, token: str | None, f: Any) -> Any:
        m = self._guard(token)
        if isinstance(m, JSONResponse):
            return m
        try:
            return f(m)
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
        tok = Header(None, alias="X-Trade-Token")

        @app.get("/api/trade/config")
        def trade_config() -> Any:
            return {**self.cfg.public(), "ready": self.manager is not None,
                    "problem": self.problem, "symbols": self.symbols,
                    "poll_error": self.manager.poll_error if self.manager else None}

        @app.get("/api/trade/account")
        def trade_account(symbol: str | None = None, x_trade_token: str | None = tok) -> Any:
            return self._do(x_trade_token, lambda m: m.account(self._sym(symbol)))

        @app.get("/api/trade/trades")
        def trade_list(
            symbol: str | None = None,
            scope: str = Query("open", pattern="^(open|history)$"),
            limit: int = Query(100, ge=1, le=500),
            x_trade_token: str | None = tok,
        ) -> Any:
            return self._do(x_trade_token, lambda m: {
                "scope": scope,
                "items": m.trades(self._sym(symbol) if symbol else None, scope, limit),
                "poll_error": m.poll_error,
            })

        @app.post("/api/trade/open")
        def trade_open(body: dict[str, Any] = JSON_BODY, x_trade_token: str | None = tok) -> Any:
            return self._do(x_trade_token, lambda m: m.open(body))

        @app.post("/api/trade/{tid}/cancel")
        def trade_cancel(tid: int, x_trade_token: str | None = tok) -> Any:
            return self._do(x_trade_token, lambda m: m.cancel(tid))

        @app.post("/api/trade/{tid}/close")
        def trade_close(tid: int, x_trade_token: str | None = tok) -> Any:
            return self._do(x_trade_token, lambda m: m.close(tid))


def _err(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)
