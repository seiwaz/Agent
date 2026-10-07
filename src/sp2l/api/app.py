"""HTTP API + static WebUI (the browser renders backend truth only).

- Every route is GET, except the trading routes under /api/trade/ (POST, only from the
  dashboard's own origin, with the trade token; off unless `trading.enabled`; see
  sp2l/trading/api.py). No route returns credentials.
- Served on 127.0.0.1 by default (`--host 0.0.0.0` exposes it).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from sp2l.api import queries as qx
from sp2l.api import smc as smc_q
from sp2l.api.live import LiveHub, psycopg_dsn, sse
from sp2l.api.smc import SmcView
from sp2l.config import ConfigError, RuntimeConfig
from sp2l.marketdata.tabdeal_ws import ws_market
from sp2l.smc.timeframes import ORDER

WEB = Path(__file__).resolve().parents[3] / "web"
TF_PATTERN = "^(" + "|".join(ORDER) + ")$"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


class SecurityHeaders:
    """Pure ASGI (streams are never buffered): read-only methods (POST only for trading, from
    the dashboard's own origin), security headers."""

    HEADERS = (
        (b"content-security-policy", CSP.encode()),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
        (b"cache-control", b"no-store"),
    )

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["method"] not in ("GET", "HEAD"):
            trade = scope["method"] == "POST" and scope["path"].startswith("/api/trade/")
            if not trade:
                await JSONResponse({"detail": "read-only API"}, status_code=405)(
                    scope, receive, send
                )
                return
            if not same_origin(scope):
                await JSONResponse({"detail": "cross-origin request"}, status_code=403)(
                    scope, receive, send
                )
                return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                names = {k for k, _ in self.HEADERS}
                hdrs = [(k, v) for k, v in message.get("headers", []) if k.lower() not in names]
                message["headers"] = hdrs + list(self.HEADERS)
            await send(message)

        await self.app(scope, receive, send_with_headers)


def same_origin(scope: Scope) -> bool:
    """A POST must come from the dashboard itself: its Origin (sent by every browser on a POST)
    names the host it was sent to."""
    hdr: dict[str, str] = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
    origin, host = hdr.get("origin"), hdr.get("host")
    if not origin or not host:
        return False
    return origin.split("://", 1)[-1].rstrip("/") == host


def create_app(cfg: RuntimeConfig, trading: Any = None) -> FastAPI:
    db = create_engine(cfg.database_url, pool_pre_ping=True)
    symbols = cfg.symbols
    try:
        costs = cfg.costs()
        costs_problem: str | None = None
    except ConfigError as exc:
        costs, costs_problem = None, str(exc)
    views = {s: SmcView(db, s, cfg.symbol_params(s), costs) for s in symbols}
    sparams = {s: v.params for s, v in views.items()}
    display = {s: ws_market(s).replace("_", "/") for s in symbols}
    from sp2l.trading.api import TradingDesk

    desk: TradingDesk = trading or TradingDesk(cfg, db)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        stop = desk.start()  # the trade poller, while trading is on
        yield
        if stop is not None:
            stop.set()

    app = FastAPI(title="Eiwaz Trading System API", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.add_middleware(SecurityHeaders)

    def view(symbol: str | None) -> SmcView:
        if symbol is None:
            return views[symbols[0]]
        if symbol not in views:
            raise HTTPException(404, f"symbol must be one of {symbols}")
        return views[symbol]

    def sym(symbol: str | None) -> str:
        return view(symbol).symbol

    @app.get("/api/overview")
    def overview() -> Any:
        markets = []
        for s in symbols:
            tick, step = cfg.instrument(s)
            markets.append(
                {
                    "symbol": s,
                    "display": display[s],
                    "tick": qx.jsonable(tick),
                    "step": qx.jsonable(step),
                    "decimals": max(0, -tick.normalize().as_tuple().exponent),  # type: ignore[operator]
                    "market": qx.market_summary(db, s, display[s]),
                    "price": qx.jsonable(smc_q.last_price(db, s)),
                    "collector": qx.collector_health(db, s),
                    "smc": views[s].status(),
                }
            )
        return {
            "symbols": symbols,
            "markets": markets,
            "wallet": smc_q.wallet(db, symbols, costs, sparams),
            "costs_problem": costs_problem,
            "live": qx.live_status(db),
        }

    # ---- SMC ------------------------------------------------------------------------------
    @app.get("/api/smc/analysis")
    def analysis(
        symbol: str | None = None,
        tf: str = Query("15m", pattern=TF_PATTERN),
        bars: int = Query(300, ge=20, le=1000),
    ) -> Any:
        return view(symbol).analysis(tf, bars)

    @app.get("/api/smc/radar")
    def radar(symbol: str | None = None) -> Any:
        return view(symbol).radar()

    @app.get("/api/smc/signals")
    def signals(
        symbol: str | None = None,
        state: str = Query("all", pattern="^(all|active|closed)$"),
        limit: int = Query(50, ge=1, le=500),
        since_hours: float | None = Query(None, ge=0.1, le=24 * 90),
    ) -> Any:
        since = None if since_hours is None else datetime.now(UTC) - timedelta(hours=since_hours)
        act = {"all": None, "active": True, "closed": False}[state]
        syms = symbols if symbol is None else [sym(symbol)]
        return {
            "items": smc_q.signals(
                db, syms, active=act, limit=limit, since=since, costs=costs, params=sparams
            )
        }

    @app.get("/api/smc/signals/{sid}/events")
    def signal_events(sid: int) -> Any:
        return {"items": smc_q.signal_events(db, sid)}

    @app.get("/api/smc/performance")
    def performance(symbol: str | None = None) -> Any:
        return smc_q.performance(db, symbols if symbol is None else [sym(symbol)])

    @app.get("/api/smc/wallet")
    def wallet() -> Any:
        return smc_q.wallet(db, symbols, costs, sparams)

    @app.get("/api/smc/backtest")
    def backtest(symbol: str | None = None, days: int = Query(30, ge=1, le=90)) -> Any:
        return view(symbol).backtest(days)

    @app.get("/api/smc/params")
    def params(symbol: str | None = None) -> Any:
        return view(symbol).params_view()

    # ---- market data ----------------------------------------------------------------------
    @app.get("/api/market/candles")
    def candles(
        symbol: str | None = None,
        tf: str = Query("1m", pattern=TF_PATTERN),
        limit: int = Query(300, ge=1, le=1000),
    ) -> Any:
        return view(symbol).candles(tf, limit)

    @app.get("/api/market/quality")
    def quality(symbol: str | None = None, minutes: int = Query(180, ge=5, le=1440)) -> Any:
        return qx.data_quality(db, sym(symbol), minutes)

    @app.get("/api/collector")
    def collector(symbol: str | None = None) -> Any:
        s = sym(symbol)
        return {**qx.collector_health(db, s), "diagnostics": qx.diagnostics(db, s)}

    @app.get("/api/feed")
    def feed(symbol: str | None = None, hours: float = Query(24.0, ge=0.1, le=168.0)) -> Any:
        from sp2l.runtime.feed_report import report

        s = sym(symbol)
        rep = report(db, s, datetime.now(UTC) - timedelta(hours=hours))
        rep["live"] = qx.collector_health(db, s).get("heartbeat")
        rep["correlated_closes"] = [qx.split_close_pair(c) for c in rep["correlated_closes"]]
        return qx.jsonable(rep)

    @app.get("/api/integrity")
    def integrity(symbol: str | None = None) -> Any:
        return qx.integrity(db, sym(symbol))

    @app.get("/api/validation")
    def validation() -> Any:
        return {"live": qx.live_status(db), "items": qx.validation(db)}

    hub = LiveHub(psycopg_dsn(cfg.database_url), symbols)

    @app.get("/api/live/snapshot")
    def live_snapshot(symbol: str | None = None) -> Any:
        """The forming candle(s) and latest price, for (re)connecting clients."""
        return qx.live_snapshot(db, sym(symbol))

    @app.get("/api/live/stream")
    async def live_stream(request: Request, symbol: str | None = None) -> StreamingResponse:
        """Server-Sent Events of one market: forming M1 on every canonical trade, then its
        final candle and any revision. Display only."""
        s = sym(symbol)
        return StreamingResponse(
            sse(hub, s, lambda: qx.live_snapshot(db, s), request.is_disconnected),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    # ---- chart workspace (display only) -----------------------------------------------------
    from sp2l.chart.overlays import ChartParams
    from sp2l.chart.service import CHART_TFS, ChartService

    charts = {s: ChartService(db, s, sparams[s]) for s in symbols}
    chart_tf = "^(" + "|".join(CHART_TFS) + ")$"

    def chart(symbol: str | None) -> ChartService:
        return charts[sym(symbol)]

    def from_tabdeal(f: Any) -> Any:
        """Chart data comes from Tabdeal's chart feed: an unreachable feed is a 502, not a 500."""
        try:
            return f()
        except (OSError, ValueError) as exc:
            detail = f"Tabdeal chart feed unavailable: {exc}"
            return JSONResponse({"detail": detail}, status_code=502)

    @app.get("/api/chart/candles")
    def chart_candles(
        symbol: str | None = None,
        tf: str = Query("1h", pattern=chart_tf),
        limit: int = Query(500, ge=1, le=1500),
        before: int | None = Query(None, ge=0, description="epoch s: bars opening before it"),
    ) -> Any:
        return from_tabdeal(lambda: chart(symbol).candles(tf, limit, before))

    @app.get("/api/chart/overlays")
    def chart_overlays(
        symbol: str | None = None,
        tf: str = Query("1h", pattern=chart_tf),
        bars: int = Query(500, ge=50, le=1500),
        since: int | None = Query(None, alias="from", ge=0, description="epoch s: first bar"),
        swing_len: int = Query(5, ge=2, le=20),
        sr_tol_atr: float = Query(0.25, gt=0, le=2),
        sr_touches: int = Query(2, ge=2, le=10),
        sr_max: int = Query(8, ge=1, le=30),
        tl_max: int = Query(3, ge=1, le=10),
    ) -> Any:
        cp = ChartParams(swing_len, sr_tol_atr, sr_touches, sr_max, tl_max)
        return from_tabdeal(lambda: chart(symbol).overlays(tf, since, bars, cp))

    @app.get("/api/chart/trends")
    def chart_trends(symbol: str | None = None, swing_len: int = Query(5, ge=2, le=20)) -> Any:
        return chart(symbol).trends(swing_len)

    @app.get("/api/trend")
    def trend(journal_days: int = Query(60, ge=1, le=400)) -> Any:
        """System B paper wallet: state after the last closed UTC day, and the journal."""
        from sp2l.trend import live as tl

        try:
            p, lc = tl.from_runtime(cfg)
        except (ConfigError, ValueError) as exc:
            return {"ready": False, "reason": str(exc)}
        out = tl.current(db, p, lc)
        try:
            out["journal"] = tl.journal(db, lc.symbol, journal_days)
        except Exception:  # table missing before `alembic upgrade head`
            out["journal"] = []
        return out

    # ---- trading from the chart (Tabdeal futures) --------------------------------------------
    desk.mount(app)

    @app.get("/api/health")
    def health() -> Any:
        return {"ok": True}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB / "index.html")

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app
