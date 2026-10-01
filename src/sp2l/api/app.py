"""Read-only HTTP API + static WebUI (UI-02: backend truth only).

- Every route is GET; nothing here can place, cancel or modify anything, and no route
  touches credentials (they are never persisted).
- Served on 127.0.0.1 by default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from sp2l.api import queries as qx
from sp2l.api.live import LiveHub, psycopg_dsn, sse
from sp2l.config import ConfigError, RuntimeConfig
from sp2l.marketdata.tabdeal_ws import ws_market
from sp2l.spec.loader import SpecIntegrityError, load_rules, verify_manifest

WEB = Path(__file__).resolve().parents[3] / "web"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


class SecurityHeaders:
    """Pure ASGI (streams are never buffered): read-only methods, security headers."""

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
            await JSONResponse({"detail": "read-only API"}, status_code=405)(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                names = {k for k, _ in self.HEADERS}
                hdrs = [(k, v) for k, v in message.get("headers", []) if k.lower() not in names]
                message["headers"] = hdrs + list(self.HEADERS)
            await send(message)

        await self.app(scope, receive, send_with_headers)


def create_app(cfg: RuntimeConfig) -> FastAPI:
    db = create_engine(cfg.database_url, pool_pre_ping=True)
    symbol = cfg.symbol
    rules = load_rules()
    try:
        verify_manifest()
        manifest_ok = True
    except SpecIntegrityError:
        manifest_ok = False
    try:
        cfg.shadow_costs()
        costs_problem: str | None = None
    except ConfigError as exc:
        costs_problem = str(exc)
    display = ws_market(symbol).replace("_", "/")
    app = FastAPI(title="SP2L Console API", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SecurityHeaders)

    @app.get("/api/overview")
    def overview() -> Any:
        col = qx.collector_health(db)
        return {
            "symbol": symbol,
            "symbol_display": display,
            "system": qx.system(
                db, symbol, costs_problem=costs_problem, manifest_ok=manifest_ok, collector=col
            ),
            "market": qx.market_summary(db, symbol, display),
            "spec": qx.spec_info(rules.version, rules.sha256, manifest_ok),
            "live": qx.live_status(db),
            "collector": col,
            "wallet": qx.wallet(db, symbol),
            "active": qx.setup_list(db, symbol, "ACTIVE", 5),
            "quality": {
                k: v
                for k, v in qx.data_quality(db, symbol, 60).items()
                if k in ("counts", "window_minutes")
            },
            "performance": qx.performance(db, symbol),
        }

    @app.get("/api/setups")
    def setups(bucket: str | None = None, limit: int = Query(50, ge=1, le=500)) -> Any:
        if bucket is not None and bucket not in qx.FILTERS:
            raise HTTPException(400, f"bucket must be one of {sorted(qx.FILTERS)}")
        return {
            "buckets": list(qx.FILTERS),
            "primary": list(qx.PRIMARY_FILTERS),
            "counts": qx.setup_counts(db, symbol),
            "items": qx.setup_list(db, symbol, bucket, limit),
        }

    @app.get("/api/setups/{key}")
    def setup(key: str) -> Any:
        d = qx.setup_detail(db, symbol, key)
        if d is None:
            raise HTTPException(404, "unknown setup")
        return d

    @app.get("/api/counterfactuals")
    def counterfactuals(limit: int = Query(100, ge=1, le=1000)) -> Any:
        return {"label": "COUNTERFACTUAL", "items": qx.counterfactuals(db, symbol, limit)}

    @app.get("/api/market/candles")
    def candles(
        tf: str = Query("1m", pattern="^(1m|5m)$"), limit: int = Query(240, ge=1, le=500)
    ) -> Any:
        return {"tf": tf, "items": qx.candles(db, symbol, tf, limit)}

    @app.get("/api/market/zones")
    def zones() -> Any:
        """Support / resistance zones for the live chart (display only)."""
        return qx.sr_zones(db, symbol)

    @app.get("/api/market/quality")
    def quality(minutes: int = Query(180, ge=5, le=1440)) -> Any:
        return qx.data_quality(db, symbol, minutes)

    @app.get("/api/collector")
    def collector() -> Any:
        return {**qx.collector_health(db), "diagnostics": qx.diagnostics(db, symbol)}

    @app.get("/api/feed")
    def feed(hours: float = Query(24.0, ge=0.1, le=168.0)) -> Any:
        from datetime import UTC, datetime, timedelta

        from sp2l.runtime.feed_report import report

        rep = report(db, symbol, datetime.now(UTC) - timedelta(hours=hours))
        rep["live"] = qx.collector_health(db).get("heartbeat")
        rep["correlated_closes"] = [qx.split_close_pair(c) for c in rep["correlated_closes"]]
        return qx.jsonable(rep)

    hub = LiveHub(psycopg_dsn(cfg.database_url), symbol)

    @app.get("/api/live/snapshot")
    def live_snapshot() -> Any:
        """The forming candle(s) and latest price, for (re)connecting clients."""
        return qx.live_snapshot(db, symbol)

    @app.get("/api/live/stream")
    async def live_stream(request: Request) -> StreamingResponse:
        """V5.9 Server-Sent Events: forming M1 on every canonical trade, then its final
        canonical candle and any revision. Display only; never read by trading code."""
        return StreamingResponse(
            sse(hub, lambda: qx.live_snapshot(db, symbol), request.is_disconnected),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @app.get("/api/pgaps")
    def pgaps(limit: int = Query(50, ge=1, le=500)) -> Any:
        return {"items": qx.recent_pgaps(db, symbol, limit)}

    @app.get("/api/indicators")
    def indicators(limit: int = Query(48, ge=2, le=288)) -> Any:
        return qx.indicators(db, symbol, limit)

    @app.get("/api/integrity")
    def integrity() -> Any:
        return qx.integrity(db, symbol)

    @app.get("/api/performance")
    def performance() -> Any:
        return qx.performance(db, symbol)

    @app.get("/api/wallet")
    def wallet() -> Any:
        return qx.wallet(db, symbol)

    @app.get("/api/validation")
    def validation() -> Any:
        return {"live": qx.live_status(db), "items": qx.validation(db)}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB / "index.html")

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app
