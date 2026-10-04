"""Authenticated READ-ONLY Tabdeal runtime-validation checks (RV checklist; LIVE-01).

Hard guarantees:
- this module can only issue HTTP GET requests, and only to READ_ONLY_PATHS; there is no
  code path for POST/PUT/DELETE (no order, cancel, leverage/margin change or transfer);
- the API key/secret are only used to sign requests. They are never logged, never
  persisted (evidence is redacted and checked) and never returned by the API/WebUI;
- every check writes its raw evidence and result to runtime_validation_runs;
- nothing here can enable Live: LIVE_AUTOMATION_DISABLED stays until every item,
  including the order-placement probes that are NOT run here, has passed.

Signing (docs.tabdeal.org + official SDK tabdeal-python client.py): query string =
urlencode(params + timestamp[ms] + recvWindow); signature = HMAC-SHA256(secret, qs) as a hex
digest, appended as `signature`; API key in header X-MBX-APIKEY.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import Engine, create_engine, text

from sp2l.config import RuntimeConfig
from sp2l.marketdata.tabdeal_rest import exchange_symbol
from sp2l.secrets import Credentials, load_credentials

# the account leverage the read-only check expects (only READ, never changed from here)
STRATEGY_LEVERAGE = 10

log = logging.getLogger("sp2l.validation")

BASE = "https://api1.tabdeal.org"
API_VERSION = "tabdeal-docs v0.9.0 (fetched 2026-09-26)"
READ_ONLY_PATHS = frozenset(
    {
        "/r/fapi/v1/time",
        "/r/fapi/v1/exchangeInfo",
        "/r/fapi/v3/balance",
        "/r/fapi/v3/account",
        "/r/fapi/v3/positionRisk",
        "/r/fapi/v1/leverage",
        "/r/fapi/v1/openOrders",
        "/r/fapi/v1/position",
    }
)


class NotReadOnly(PermissionError):
    pass


@dataclass
class Probe:
    path: str
    params: dict[str, Any]
    status: int | None
    body: Any
    error: str | None
    sent_at: datetime
    recv_at: datetime

    def evidence(self) -> dict[str, Any]:
        return {
            "request": {"method": "GET", "path": self.path, "params": self.params},
            "http_status": self.status,
            "response": self.body,
            "error": self.error,
            "local_sent": self.sent_at.isoformat(),
            "local_recv": self.recv_at.isoformat(),
        }


Opener = Callable[[urllib.request.Request, float], Any]


def _default_opener(req: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - fixed https host


class ReadOnlyTabdeal:
    def __init__(
        self,
        creds: Credentials | None,
        *,
        recv_window: int = 5000,
        timeout: float = 10.0,
        opener: Opener = _default_opener,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._creds = creds
        self.recv_window = recv_window
        self.timeout = timeout
        self._open = opener
        self._clock = clock

    def signed_query(self, params: dict[str, Any]) -> str:
        if self._creds is None:
            raise NotReadOnly("credentials required for a signed read")
        full = {**params, "timestamp": int(self._clock() * 1000), "recvWindow": self.recv_window}
        qs = urlencode(full)
        sig = hmac.new(self._creds.api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        return f"{qs}&signature={sig}"

    def get(self, path: str, params: dict[str, Any] | None = None, *, signed: bool) -> Probe:
        if path not in READ_ONLY_PATHS:
            raise NotReadOnly(f"{path} is not an allowed read-only endpoint")
        params = dict(params or {})
        qs = self.signed_query(params) if signed else urlencode(params)
        url = f"{BASE}{path}" + (f"?{qs}" if qs else "")
        headers = {"User-Agent": "sp2l-readonly/5", "Accept": "application/json"}
        if signed:
            assert self._creds is not None
            headers["X-MBX-APIKEY"] = self._creds.api_key
        req = urllib.request.Request(url, headers=headers, method="GET")
        if req.get_method() != "GET":  # defence in depth
            raise NotReadOnly("only GET is allowed")
        sent = datetime.now(UTC)
        status: int | None = None
        body: Any = None
        err: str | None = None
        try:
            with self._open(req, self.timeout) as resp:
                status = resp.status
                raw = resp.read().decode()
        except urllib.error.HTTPError as e:
            status = e.code
            raw = e.read().decode(errors="replace")
            err = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raw = ""
            err = f"{type(e).__name__}: {e}"
        recv = datetime.now(UTC)
        try:
            body = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            body = raw[:2000]
        return Probe(path, params, status, body, err, sent, recv)

    def redact(self, obj: Any) -> Any:
        """Remove any occurrence of key material; fails closed if something slips through."""
        blob = json.dumps(obj, default=str)
        if self._creds is not None:
            for secret in (self._creds.api_key, self._creds.api_secret):
                blob = blob.replace(secret, "<redacted>")
        return json.loads(blob)


# ---- checks ----------------------------------------------------------------------------


@dataclass
class Result:
    item: str
    passed: bool
    notes: str
    evidence: dict[str, Any]


def _ok(p: Probe) -> bool:
    return (
        p.status == 200
        and p.error is None
        and p.body is not None
        and not (isinstance(p.body, dict) and "code" in p.body and "msg" in p.body)
    )


def check_server_time(c: ReadOnlyTabdeal, max_offset_ms: int, samples: int = 20) -> Result:
    """B42: offset = server_time - local_midpoint, local_midpoint = (send + receive) / 2.
    Live requires the worst absolute measured offset <= max_offset_ms over >= 20 samples."""
    samples = max(samples, 20)
    obs = []
    for _ in range(samples):
        p = c.get("/r/fapi/v1/time", signed=False)
        if not _ok(p) or not isinstance(p.body, dict) or "serverTime" not in p.body:
            return Result(
                "SERVER_TIME_DRIFT",
                False,
                f"time endpoint failed: {p.error}",
                {"probe": p.evidence()},
            )
        mid = (p.sent_at.timestamp() + p.recv_at.timestamp()) / 2 * 1000
        rtt = (p.recv_at - p.sent_at).total_seconds() * 1000
        obs.append(
            {
                "server_ms": p.body["serverTime"],
                "offset_ms": round(p.body["serverTime"] - mid, 1),
                "rtt_ms": round(rtt, 1),
            }
        )
        time.sleep(0.2)
    offsets = [o["offset_ms"] for o in obs]
    worst = max(abs(o) for o in offsets)
    best = min(obs, key=lambda o: o["rtt_ms"])
    passed = worst <= max_offset_ms
    notes = (
        f"{len(obs)} samples; offset (server - local midpoint) min {min(offsets)} ms, "
        f"max {max(offsets)} ms, worst |{worst}| ms vs bound {max_offset_ms} ms; "
        f"lowest-RTT sample offset {best['offset_ms']} ms (rtt {best['rtt_ms']} ms); "
        "positive = local clock behind"
    )
    return Result(
        "SERVER_TIME_DRIFT",
        passed,
        notes,
        {"samples": obs, "bound_ms": max_offset_ms, "method": "request midpoint"},
    )


def check_symbol_filters(c: ReadOnlyTabdeal, symbol: str) -> Result:
    p = c.get("/r/fapi/v1/exchangeInfo", {"symbol": symbol}, signed=False)
    sym = None
    if _ok(p) and isinstance(p.body, dict):
        sym = next((s for s in p.body.get("symbols", []) if s.get("symbol") == symbol), None)
    have = set(sym or {})
    needed = {"tickSize", "stepSize", "minQty", "minNotional"}
    found = {k for k in needed if k in have or k in json.dumps((sym or {}).get("filters", []))}
    passed = sym is not None and found == needed
    notes = (
        f"{symbol} fields: {sorted(have)}; accepted increments / minimums present: "
        f"{sorted(found)}; missing: {sorted(needed - found)} (B26: Live needs these proven)"
    )
    return Result("SYMBOL_FILTERS", passed, notes, {"probe": p.evidence()})


def check_balance(c: ReadOnlyTabdeal) -> Result:
    p = c.get("/r/fapi/v3/balance", signed=True)
    acct = c.get("/r/fapi/v3/account", signed=True)
    rows = p.body if isinstance(p.body, list) else []
    usdt = next((r for r in rows if isinstance(r, dict) and r.get("asset") == "USDT"), None)
    passed = _ok(p) and usdt is not None
    notes = (
        f"USDT walletBalance={usdt.get('walletBalance')} availableBalance="
        f"{usdt.get('availableBalance')} crossUnPnl={usdt.get('crossUnPnl')}"
        if usdt
        else f"balance read failed or no USDT row: {p.error} {str(p.body)[:200]}"
    )
    return Result(
        "BALANCE_READ", passed, notes, {"balance": p.evidence(), "account": acct.evidence()}
    )


def check_position_risk(c: ReadOnlyTabdeal, symbol: str) -> tuple[Result, Probe]:
    p = c.get("/r/fapi/v3/positionRisk", {"symbol": symbol}, signed=True)
    rows = p.body if isinstance(p.body, list) else []
    fields = sorted({k for r in rows if isinstance(r, dict) for k in r})
    notes = (
        f"{len(rows)} row(s); fields {fields}"
        if _ok(p)
        else f"read failed: {p.error} {str(p.body)[:200]}"
    )
    return Result("POSITION_RISK_READ", _ok(p), notes, {"probe": p.evidence()}), p


def check_open_positions(c: ReadOnlyTabdeal, symbol: str) -> Result:
    p = c.get("/r/fapi/v1/position", {"symbol": symbol, "isActive": 1}, signed=True)
    rows = (
        p.body
        if isinstance(p.body, list)
        else (p.body or {}).get("positions", [])
        if isinstance(p.body, dict)
        else []
    )
    active = [
        r
        for r in rows
        if isinstance(r, dict) and Decimal(str(r.get("positionAmt", "0") or "0")) != 0
    ]
    notes = (
        f"{len(active)} active {symbol} position(s)"
        + (" - an unattributable position means ERROR_HOLD for Live" if active else "")
        if _ok(p)
        else f"read failed: {p.error} {str(p.body)[:200]}"
    )
    return Result("OPEN_POSITIONS_READ", _ok(p), notes, {"probe": p.evidence()})


def check_open_orders(c: ReadOnlyTabdeal, symbol: str) -> Result:
    p = c.get("/r/fapi/v1/openOrders", {"symbol": symbol}, signed=True)
    rows = p.body if isinstance(p.body, list) else []
    notes = (
        f"{len(rows)} open {symbol} order(s)"
        + (" - unattributable open orders mean ERROR_HOLD for Live" if rows else "")
        if _ok(p)
        else f"read failed: {p.error} {str(p.body)[:200]}"
    )
    return Result("OPEN_ORDERS_READ", _ok(p), notes, {"probe": p.evidence()})


def check_cross_10x(c: ReadOnlyTabdeal, symbol: str, risk: Probe) -> Result:
    """B40/B41: leverage must read exactly 10; Cross is never inferred while flat."""
    lev = c.get("/r/fapi/v1/leverage", {"symbol": symbol}, signed=True)
    leverage = lev.body.get("leverage") if _ok(lev) and isinstance(lev.body, dict) else None
    rows = risk.body if isinstance(risk.body, list) else []
    margin = next(
        (
            str(r.get("marginType"))
            for r in rows
            if isinstance(r, dict) and r.get("symbol") == symbol and r.get("marginType")
        ),
        None,
    )
    lev_ok = str(leverage) == str(STRATEGY_LEVERAGE)  # V5.8: exactly the strategy's 10x
    cross = (
        "CROSS_MARGIN_UNVERIFIED"
        if margin is None
        else ("CROSS" if margin.lower() == "cross" else f"NOT_CROSS({margin})")
    )
    passed = lev_ok and cross == "CROSS"
    notes = (
        f"leverage={leverage} ({'OK' if lev_ok else 'LEVERAGE_MISMATCH: must be exactly 10'});"
        f" margin={cross}"
        + (
            "; not inferred while flat - verify on an approved minimum-size position (B41)"
            if margin is None
            else ""
        )
        + "; read-only: nothing was changed"
    )
    return Result(
        "CROSS_10X",
        passed,
        notes,
        {
            "leverage": lev.evidence(),
            "position_risk": risk.evidence(),
            "margin_status": cross,
            "exchange_leverage": leverage,
            "strategy_leverage": STRATEGY_LEVERAGE,
            "leverage_blocker": None if lev_ok else "LEVERAGE_MISMATCH",
            "automatic_leverage_change": False,  # never: no write path exists here
        },
    )


def record(engine: Engine, c: ReadOnlyTabdeal, r: Result, started: datetime) -> None:
    evidence = c.redact({"read_only": True, **r.evidence})
    blob = json.dumps(evidence, default=str)
    if c._creds is not None and (c._creds.api_key in blob or c._creds.api_secret in blob):
        raise RuntimeError("refusing to persist evidence containing credentials")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runtime_validation_runs (item, started_at, finished_at, passed,"
                " api_version, evidence, notes, approved_by) VALUES (:i, :s, :f, :p, :v,"
                " CAST(:e AS jsonb), :n, 'user-approved read-only (2026-09-26)')"
            ),
            {
                "i": r.item,
                "s": started,
                "f": datetime.now(UTC),
                "p": r.passed,
                "v": API_VERSION,
                "e": blob,
                "n": r.notes,
            },
        )


def run_readonly_checks(cfg: RuntimeConfig, only: set[str] | None = None) -> list[Result]:
    creds = load_credentials()
    log.info("read-only checks with %s (GET only; %d allowed paths)", creds, len(READ_ONLY_PATHS))
    engine = create_engine(cfg.database_url)
    c = ReadOnlyTabdeal(creds)
    symbol = exchange_symbol(cfg.symbol)  # Tabdeal FAPI REST uses BTC_USDT, not BTCUSDT
    v = cfg.section("validation")
    bound = int(v.get("server_time_max_offset_ms", 500))
    samples = int(v.get("server_time_samples", 20))
    risk_probe: dict[str, Probe] = {}

    def position_risk() -> Result:
        r, p = check_position_risk(c, symbol)
        risk_probe["p"] = p
        return r

    def cross() -> Result:
        if "p" not in risk_probe:
            _, risk_probe["p"] = check_position_risk(c, symbol)
        return check_cross_10x(c, symbol, risk_probe["p"])

    checks: list[tuple[str, Callable[[], Result]]] = [
        ("SERVER_TIME_DRIFT", lambda: check_server_time(c, bound, samples)),
        ("SYMBOL_FILTERS", lambda: check_symbol_filters(c, symbol)),
        ("BALANCE_READ", lambda: check_balance(c)),
        ("POSITION_RISK_READ", position_risk),
        ("OPEN_POSITIONS_READ", lambda: check_open_positions(c, symbol)),
        ("OPEN_ORDERS_READ", lambda: check_open_orders(c, symbol)),
        ("CROSS_10X", cross),
    ]
    unknown = (only or set()) - {name for name, _ in checks}
    if unknown:
        raise ValueError(f"unknown read-only item(s): {sorted(unknown)}")
    results: list[Result] = []
    for name, fn in checks:
        if only and name not in only:
            continue
        started = datetime.now(UTC)
        r = fn()
        record(engine, c, r, started)
        results.append(r)
    for r in results:
        log.info("%-22s %s  %s", r.item, "PASS" if r.passed else "FAIL", r.notes)
    with engine.connect() as conn:
        status: str = conn.execute(text("SELECT status FROM live_automation_status")).scalar_one()
    log.info("live automation status: %s", status)
    return results
