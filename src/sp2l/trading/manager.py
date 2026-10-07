"""Manual trades placed from the dashboard chart on Tabdeal futures.

One trade = a Long / Short position drawing (entry, stop, target) turned into:

1. `POST /fapi/v1/leverage` (the leverage the user confirmed),
2. a LIMIT GTC entry at the drawing's entry price (`newClientOrderId` = the trade's client id),
3. once (part of) it fills, the position's stop and target (`POST /fapi/v1/positionSlTp`,
   position-level, `trading.working_type`), set again whenever more of the entry fills.

Tabdeal has no futures user-data stream, so `reconcile()` polls the exchange (every
`trading.poll_s` in the API process while a trade is open): fills, the position (mark price,
unrealized PnL, liquidation), and its end (stop, target, closed on Tabdeal, or closed here). The
exchange is the truth; `manual_trades` mirrors it and keeps the trade's own log (`events`).

Safety: one open trade per market (one-way mode, position-level SL/TP), refused while the market
already has a position or open orders on Tabdeal; leverage and margin are capped by the config;
the stop must sit before the estimated cross-margin liquidation; an entry remainder still resting
when the position ends is canceled (it would open a new, unprotected position).
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

from sp2l.marketdata.tabdeal_ws import ws_market
from sp2l.trading.client import ExchangeError

log = logging.getLogger("sp2l.trading")
OPEN = ("PENDING", "ACTIVE")
WORKING_TYPES = ("MARK_PRICE", "CONTRACT_PRICE")
LIQ_BUFFER = Decimal("0.01")  # maintenance margin + fees kept free at the stop (of notional)
ORDER_OPEN = ("NEW", "PARTIALLY_FILLED")
NOT_FOUND_S = 30.0  # an unanswered order request not seen on Tabdeal after this was not placed


class TradeError(ValueError):
    """A request the manager refuses (shown to the user as is)."""


@dataclass(frozen=True)
class TradingConfig:
    enabled: bool = False
    max_leverage: int = 20
    max_margin_usdt: float = 50.0
    working_type: str = "MARK_PRICE"
    poll_s: float = 3.0
    token_file: str = "~/.config/sp2l/trade.token"
    credentials_file: str = "~/.config/sp2l/tabdeal.env"

    @classmethod
    def from_mapping(cls, m: dict[str, Any]) -> TradingConfig:
        unknown = sorted(set(m) - set(cls.__dataclass_fields__))
        if unknown:
            raise ValueError(f"trading: unknown key(s) {', '.join(unknown)}")
        enabled = m.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("trading.enabled must be true or false")
        c = cls(
            enabled,
            int(m.get("max_leverage", cls.max_leverage)),
            float(m.get("max_margin_usdt", cls.max_margin_usdt)),
            str(m.get("working_type", cls.working_type)),
            float(m.get("poll_s", cls.poll_s)),
            str(m.get("token_file", cls.token_file)),
            str(m.get("credentials_file", cls.credentials_file)),
        )
        if not 1 <= c.max_leverage <= 125:
            raise ValueError("trading.max_leverage must be 1..125")
        if c.max_margin_usdt <= 0:
            raise ValueError("trading.max_margin_usdt must be > 0")
        if c.working_type not in WORKING_TYPES:
            raise ValueError(f"trading.working_type must be one of {WORKING_TYPES}")
        if c.poll_s < 1:
            raise ValueError("trading.poll_s must be >= 1")
        return c

    def public(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "max_leverage": self.max_leverage,
            "max_margin_usdt": self.max_margin_usdt,
            "working_type": self.working_type,
        }


# ---- the trade token (the dashboard's permission to trade) --------------------------------------
def new_token(path: Path) -> str:
    """Write a fresh token (file 600 in a 700 directory) and return it."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    tok = secrets.token_urlsafe(24)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(tok + "\n")
    return tok


def load_token(path: Path) -> str | None:
    """The token, or None if the file is missing or readable by others."""
    try:
        if path.stat().st_mode & 0o077:
            log.error("%s must be mode 600; trading is locked", path)
            return None
        tok = path.read_text().strip()
    except OSError:
        return None
    return tok or None


# ---- numbers ------------------------------------------------------------------------------------
def D(v: Any) -> Decimal:
    try:
        d = Decimal(str(v))
    except InvalidOperation as e:
        raise TradeError(f"not a number: {v!r}") from e
    if not d.is_finite():
        raise TradeError(f"not a finite number: {v!r}")
    return d


def down(v: Decimal, step: Decimal) -> Decimal:
    return (v / step).to_integral_value(rounding=ROUND_DOWN) * step


def near(v: Decimal, step: Decimal) -> Decimal:
    return (v / step).to_integral_value() * step


def wire(v: Any, step: Decimal) -> str:
    """A price / quantity as sent to Tabdeal: exactly the market's precision."""
    return str(Decimal(v).quantize(step))


def fnum(v: Any) -> float | None:
    return None if v is None else float(v)


def _first(rows: Any, market: str) -> dict[str, Any] | None:
    """The row of `market` in a list answer (Tabdeal answers a list, sometimes wrapped)."""
    if isinstance(rows, dict):
        rows = rows.get("data") or rows.get("positions") or rows.get("result") or [rows]
    for r in rows or []:
        if isinstance(r, dict) and str(r.get("symbol", market)).replace("_", "") == market.replace(
            "_", ""
        ):
            return r
    return None


def _rows(rows: Any) -> list[dict[str, Any]]:
    if isinstance(rows, dict):
        rows = rows.get("data") or rows.get("orders") or rows.get("result") or []
    return [r for r in rows or [] if isinstance(r, dict)]


COLUMNS = (
    "id, symbol, side, status, leverage, margin_usdt, qty, entry, sl, tp, client_id, order_id,"
    " position_id, filled_qty, avg_entry, sltp_qty, exit_price, realized_pnl, close_reason,"
    " drawing_id, last_error, events, created_at, filled_at, closed_at, updated_at"
)


class TradeManager:
    def __init__(
        self,
        db: Engine,
        client: Any,
        cfg: TradingConfig,
        instruments: dict[str, tuple[Decimal, Decimal]],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db, self.ex, self.cfg, self.instruments = db, client, cfg, instruments
        self.clock = clock
        self._lock = threading.RLock()  # one exchange-changing operation at a time
        self.live: dict[str, dict[str, Any]] = {}  # symbol -> the last positionRisk reading
        self.poll_error: str | None = None

    # ---- storage --------------------------------------------------------------------------------
    def _get(self, tid: int) -> dict[str, Any]:
        with self.db.connect() as c:
            r = c.execute(text(f"SELECT {COLUMNS} FROM manual_trades WHERE id = :i"), {"i": tid})
            row = r.mappings().first()
        if row is None:
            raise TradeError(f"no trade {tid}")
        return dict(row)

    def _set(self, tid: int, event: str | None = None, **fields: Any) -> None:
        sets = [f"{k} = :{k}" for k in fields] + ["updated_at = now()"]
        args: dict[str, Any] = {**fields, "i": tid}
        if event is not None:
            sets.append("events = events || CAST(:ev AS jsonb)")
            args["ev"] = json.dumps([{"at": self.clock(), "event": event}])
        with self.db.begin() as c:
            c.execute(text(f"UPDATE manual_trades SET {', '.join(sets)} WHERE id = :i"), args)

    def _open_ids(self, symbol: str | None = None) -> list[int]:
        q = "SELECT id FROM manual_trades WHERE status IN ('PENDING', 'ACTIVE')"
        with self.db.connect() as c:
            if symbol is None:
                return [int(r[0]) for r in c.execute(text(q + " ORDER BY id"))]
            return [int(r[0]) for r in c.execute(text(q + " AND symbol = :s"), {"s": symbol})]

    def has_open(self) -> bool:
        return bool(self._open_ids())

    # ---- reads ----------------------------------------------------------------------------------
    def account(self, symbol: str) -> dict[str, Any]:
        """Balance and whether a trade can be opened on `symbol` now."""
        market = ws_market(symbol)
        usdt = self._usdt()
        busy = self._busy(symbol, market)
        return {
            "symbol": symbol,
            "available_usdt": fnum(usdt.get("availableBalance")),
            "wallet_usdt": fnum(usdt.get("crossWalletBalance") or usdt.get("balance")),
            "busy": busy,
            **self.cfg.public(),
        }

    def _usdt(self) -> dict[str, Any]:
        rows = _rows(self.ex.balance())
        for r in rows:
            if str(r.get("asset", "")).upper() == "USDT":
                return r
        raise TradeError("no USDT balance in the futures wallet")

    def _busy(self, symbol: str, market: str) -> str | None:
        if self._open_ids(symbol):
            return "a trade on this market is already open here"
        pos = _first(self.ex.position_risk(market), market)
        if pos is not None and D(pos.get("positionAmt", 0)) != 0:
            return "this market already has a position on Tabdeal"
        if _rows(self.ex.open_orders(market)):
            return "this market has open orders on Tabdeal"
        return None

    def trades(self, symbol: str | None, scope: str, limit: int = 100) -> list[dict[str, Any]]:
        where = "status IN ('PENDING', 'ACTIVE')" if scope == "open" else (
            "status NOT IN ('PENDING', 'ACTIVE')"
        )
        args: dict[str, Any] = {"n": limit}
        if symbol:
            where += " AND symbol = :s"
            args["s"] = symbol
        order = "id" if scope == "open" else "COALESCE(closed_at, updated_at) DESC, id DESC"
        with self.db.connect() as c:
            rows = c.execute(
                text(f"SELECT {COLUMNS} FROM manual_trades WHERE {where}"
                     f" ORDER BY {order} LIMIT :n"),
                args,
            ).mappings()
            return [self.view(dict(r)) for r in rows]

    def view(self, r: dict[str, Any]) -> dict[str, Any]:
        """A trade as the dashboard shows it, with the live position while it is open."""
        out: dict[str, Any] = {
            k: (float(v) if isinstance(v, Decimal) else v.isoformat() if hasattr(v, "isoformat")
                else v)
            for k, v in r.items()
        }
        filled = Decimal(r["filled_qty"] or 0)
        out["protected"] = bool(filled > 0 and Decimal(r["sltp_qty"] or 0) >= filled)
        out["events"] = (r.get("events") or [])[-12:]
        if r["status"] == "ACTIVE":
            live = self.live.get(r["symbol"])
            if live:
                avg = Decimal(r["avg_entry"] or r["entry"])
                margin = filled * avg / Decimal(r["leverage"]) if filled else None
                upnl = live.get("upnl")
                out["live"] = {
                    **live,
                    "roe_pct": None if upnl is None or not margin
                    else float(Decimal(str(upnl)) / margin * 100),
                }
        return out

    # ---- opening --------------------------------------------------------------------------------
    def open(self, req: dict[str, Any]) -> dict[str, Any]:
        symbol = str(req.get("symbol", ""))
        if symbol not in self.instruments:
            raise TradeError(f"symbol must be one of {sorted(self.instruments)}")
        side = str(req.get("side", "")).upper()
        if side not in ("LONG", "SHORT"):
            raise TradeError("side must be LONG or SHORT")
        tick, step = self.instruments[symbol]
        entry, sl, tp = (near(D(req.get(k)), tick) for k in ("entry", "sl", "tp"))
        try:
            leverage = int(req.get("leverage", 0))
        except (TypeError, ValueError) as e:
            raise TradeError("leverage must be a whole number") from e
        margin = D(req.get("margin_usdt", 0))
        if not 1 <= leverage <= self.cfg.max_leverage:
            raise TradeError(f"leverage must be 1..{self.cfg.max_leverage} (trading.max_leverage)")
        if not 0 < margin <= Decimal(str(self.cfg.max_margin_usdt)):
            raise TradeError(
                f"margin must be above 0 and at most {self.cfg.max_margin_usdt} USDT"
                " (trading.max_margin_usdt)"
            )
        if min(entry, sl, tp) <= 0:
            raise TradeError("prices must be positive")
        if side == "LONG" and not sl < entry < tp:
            raise TradeError("a long needs stop < entry < target")
        if side == "SHORT" and not tp < entry < sl:
            raise TradeError("a short needs target < entry < stop")
        qty = down(margin * leverage / entry, step)
        if qty <= 0:
            raise TradeError(f"margin × leverage buys less than one step ({step}) at {entry}")
        notional = qty * entry
        loss = qty * abs(entry - sl)
        market = ws_market(symbol)
        with self._lock:
            busy = self._busy(symbol, market)
            if busy:
                raise TradeError(f"{busy}: close or cancel it first")
            usdt = self._usdt()
            avail = D(usdt.get("availableBalance", 0))
            wallet = D(usdt.get("crossWalletBalance") or usdt.get("balance") or 0)
            if notional / leverage > avail:
                raise TradeError(
                    f"needs {notional / leverage:.2f} USDT margin; {avail:.2f} available"
                )
            if loss + notional * LIQ_BUFFER >= wallet:
                raise TradeError(
                    f"the stop loses {loss:.2f} USDT: the position would be liquidated before"
                    f" the stop (futures wallet {wallet:.2f} USDT); lower the size or the leverage"
                )
            client_id = f"ets{secrets.token_hex(8)}"
            with self.db.begin() as c:
                tid = int(
                    c.execute(
                        text(
                            "INSERT INTO manual_trades (symbol, side, status, leverage,"
                            " margin_usdt, qty, entry, sl, tp, client_id, drawing_id, events)"
                            " VALUES (:s, :side, 'PENDING', :lev, :m, :q, :e, :sl, :tp, :cid,"
                            " :d, CAST(:ev AS jsonb)) RETURNING id"
                        ),
                        {"s": symbol, "side": side, "lev": leverage, "m": margin, "q": qty,
                         "e": entry, "sl": sl, "tp": tp, "cid": client_id,
                         "d": req.get("drawing_id"),
                         "ev": json.dumps([{"at": self.clock(), "event":
                                            f"requested {side} {qty} @ {entry} x{leverage}"}])},
                    ).scalar_one()
                )
            try:
                self.ex.set_leverage(market, leverage)
                self._set(tid, f"leverage set to {leverage}")
                o = self.ex.limit_order(market, "BUY" if side == "LONG" else "SELL",
                                        wire(qty, step), wire(entry, tick), client_id)
            except ExchangeError as e:
                if e.status is None:  # the order may or may not exist: look for it
                    self._set(tid, f"order outcome unknown ({e})", last_error=str(e))
                    self._settle(tid)
                else:
                    self._set(tid, f"rejected: {e}", status="REJECTED", last_error=str(e),
                              closed_at=_now())
                return self.view(self._get(tid))
            oid = o.get("orderId") if isinstance(o, dict) else None
            self._set(tid, f"LIMIT order {oid} placed", order_id=oid, last_error=None)
            self._settle(tid)
            return self.view(self._get(tid))

    # ---- cancel / close -------------------------------------------------------------------------
    def cancel(self, tid: int) -> dict[str, Any]:
        with self._lock:
            t = self._get(tid)
            if t["status"] != "PENDING":
                raise TradeError(f"trade {tid} is {t['status']}: only a pending trade is canceled")
            market = ws_market(t["symbol"])
            if t["order_id"] is None:  # an unanswered order request: find the order first
                self._find_order(t, market, wait=False)
                t = self._get(tid)
            if t["status"] == "PENDING" and t["order_id"] is not None:
                try:
                    self.ex.cancel(market, t["order_id"])
                    self._set(tid, "cancel sent")
                except ExchangeError as e:
                    self._set(tid, f"cancel failed: {e}", last_error=str(e))
                    raise TradeError(f"Tabdeal refused the cancel: {e}") from e
            self._settle(tid, canceling=True)
            return self.view(self._get(tid))

    def close(self, tid: int) -> dict[str, Any]:
        with self._lock:
            t = self._get(tid)
            if t["status"] != "ACTIVE":
                raise TradeError(f"trade {tid} is {t['status']}: only an active trade is closed")
            market = ws_market(t["symbol"])
            self._cancel_rest(t, market)
            self._set(tid, "close requested", close_reason="MANUAL")
            try:
                self.ex.close_position(market)
                self._set(tid, "market close sent")
            except ExchangeError as e:
                self._set(tid, f"close failed: {e}", close_reason=None, last_error=str(e))
                raise TradeError(f"Tabdeal refused the close: {e}") from e
            self._settle(tid)
            return self.view(self._get(tid))

    def _cancel_rest(self, t: dict[str, Any], market: str) -> None:
        """Cancel the entry's unfilled remainder, if it still rests."""
        if t["order_id"] is None:
            return
        try:
            o = self.ex.order(market, t["order_id"])
            if str(o.get("status")) in ORDER_OPEN:
                self.ex.cancel(market, t["order_id"])
                self._set(t["id"], "entry remainder canceled")
        except ExchangeError as e:
            self._set(t["id"], f"remainder cancel failed: {e}", last_error=str(e))

    # ---- reconciliation -------------------------------------------------------------------------
    def reconcile(self) -> None:
        """Bring every open trade in line with the exchange (the poller calls this)."""
        ids = self._open_ids()
        if not ids:
            self.live.clear()
            return
        with self._lock:
            for tid in ids:
                try:
                    self._reconcile_one(tid)
                    self.poll_error = None
                except ExchangeError as e:
                    self.poll_error = str(e)
                    log.warning("trade %s: reconcile failed: %s", tid, e)

    def _settle(self, tid: int, canceling: bool = False) -> None:
        """Read the outcome of an action at once; if Tabdeal does not answer, the poller will."""
        try:
            self._reconcile_one(tid, canceling)
        except ExchangeError as e:
            self._set(tid, f"status check failed: {e}", last_error=str(e))

    def _reconcile_one(self, tid: int, canceling: bool = False) -> None:
        t = self._get(tid)
        market = ws_market(t["symbol"])
        if t["status"] == "PENDING" and t["order_id"] is None:
            return self._find_order(t, market)
        if t["status"] not in OPEN:
            return None
        o = self.ex.order(market, t["order_id"])
        st = str(o.get("status", ""))
        executed = D(o.get("executedQty") or o.get("cumQty") or 0)
        if executed > Decimal(t["filled_qty"]):
            avg = D(o.get("avgPrice") or 0) or Decimal(t["entry"])
            fields: dict[str, Any] = {"filled_qty": executed, "avg_entry": avg, "status": "ACTIVE"}
            if t["filled_at"] is None:
                fields["filled_at"] = _now()
            self._set(tid, f"filled {executed} / {t['qty']} @ {avg} ({st})", **fields)
            t = self._get(tid)
        if t["status"] == "PENDING":
            if st in ("CANCELED", "REJECTED", "EXPIRED"):
                why = "canceled" if canceling else f"order {st.lower()} on Tabdeal"
                self._set(tid, why, status="CANCELED" if st != "REJECTED" else "REJECTED",
                          closed_at=_now())
            return None
        return self._active(t, market, st)

    def _find_order(self, t: dict[str, Any], market: str, wait: bool = True) -> None:
        """After an unanswered order request: find the order by its client id (given up as not
        placed after NOT_FOUND_S, or at once when the user cancels)."""
        for o in _rows(self.ex.open_orders(market)):
            if o.get("clientOrderId") == t["client_id"]:
                self._set(t["id"], f"order {o.get('orderId')} found", order_id=o.get("orderId"))
                return
        pos = _first(self.ex.position_risk(market), market)
        amt = D(pos.get("positionAmt", 0)) if pos else Decimal(0)
        if amt != 0 and (amt > 0) == (t["side"] == "LONG"):
            avg = D(pos.get("entryPrice") or t["entry"]) if pos else Decimal(t["entry"])
            self._set(t["id"], "order filled at once (found the position)", status="ACTIVE",
                      filled_qty=abs(amt), avg_entry=avg, filled_at=_now())
            self._active(self._get(t["id"]), market, "FILLED")
            return
        if wait and (_now() - t["created_at"]).total_seconds() < NOT_FOUND_S:
            return
        self._set(t["id"], "order not found on Tabdeal", status="REJECTED", closed_at=_now())

    def _active(self, t: dict[str, Any], market: str, order_status: str) -> None:
        pos = _first(self.ex.position_risk(market), market)
        amt = D(pos.get("positionAmt", 0)) if pos else Decimal(0)
        if amt == 0:
            return self._ended(t, market, order_status)
        self.live[t["symbol"]] = {
            "position_amt": float(amt),
            "entry_price": fnum(pos.get("entryPrice")) if pos else None,
            "mark": fnum(pos.get("markPrice")) if pos else None,
            "upnl": fnum(pos.get("unRealizedProfit")) if pos else None,
            "liquidation": fnum(pos.get("liquidationPrice")) if pos else None,
            "leverage": fnum(pos.get("leverage")) if pos else None,
            "at": self.clock(),
        }
        if t["position_id"] is None:
            p = self._position(market)
            if p is not None:
                self._set(t["id"], f"position {p.get('id')}", position_id=p.get("id"))
                t = self._get(t["id"])
        if Decimal(t["sltp_qty"]) < Decimal(t["filled_qty"]):
            self._protect(t, market)
        return None

    def _position(self, market: str) -> dict[str, Any] | None:
        return _first(self.ex.positions(market, active=True), market)

    def _protect(self, t: dict[str, Any], market: str) -> None:
        if t["position_id"] is None:
            self._set(t["id"], "stop / target waiting for the position id",
                      last_error="position id not yet known")
            return
        tick = self.instruments[t["symbol"]][0]
        try:
            self.ex.position_sl_tp(t["position_id"], market, wire(t["sl"], tick),
                                   wire(t["tp"], tick), self.cfg.working_type)
        except ExchangeError as e:
            self._set(t["id"], f"stop / target refused: {e}", last_error=str(e))
            return
        self._set(t["id"], f"stop {wire(t['sl'], tick)} / target {wire(t['tp'], tick)} set on"
                  f" {Decimal(t['filled_qty']).normalize()}",
                  sltp_qty=t["filled_qty"], last_error=None)

    def _ended(self, t: dict[str, Any], market: str, order_status: str) -> None:
        """The position is gone: record how it ended."""
        if order_status in ORDER_OPEN:
            self._cancel_rest(t, market)
        self.live.pop(t["symbol"], None)
        exit_price = pnl = None
        for p in _rows(self.ex.positions(market, active=False, limit=10)):
            if t["position_id"] is None or p.get("id") == t["position_id"]:
                exit_price = D(p.get("avgExitPrice") or 0) or None
                pnl = D(p["realizedPnl"]) if p.get("realizedPnl") is not None else None
                break
        reason = t["close_reason"] or self._why(t, exit_price)
        self._set(t["id"], f"closed ({reason}) at {exit_price}, PnL {pnl}", status="CLOSED",
                  exit_price=exit_price, realized_pnl=pnl, close_reason=reason, closed_at=_now())

    @staticmethod
    def _why(t: dict[str, Any], exit_price: Decimal | None) -> str:
        if exit_price is None:
            return "CLOSED_ON_TABDEAL"
        entry, sl, tp = Decimal(t["entry"]), Decimal(t["sl"]), Decimal(t["tp"])
        if abs(exit_price - tp) <= abs(tp - entry) / 4:
            return "TP"
        if abs(exit_price - sl) <= abs(entry - sl) / 4:
            return "SL"
        return "CLOSED_ON_TABDEAL"

    # ---- the poller -----------------------------------------------------------------------------
    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.reconcile()
            except Exception:  # keep polling: the next tick retries
                log.exception("trade reconcile failed")
            stop.wait(self.cfg.poll_s)


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime.now(UTC)
