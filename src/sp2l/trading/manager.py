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

Positions opened directly on Tabdeal (no open trade here for that market) are adopted by the
poller (`origin` TABDEAL): followed like the others — size, average entry, live PnL, its end —
with the stop / target Tabdeal reports for it when it reports one. `set_sltp()` sets or moves
the stop / target of any active trade (the chart's position drawing, dragged).

Safety: one open trade per market (one-way mode, position-level SL/TP), refused while the market
already has a position or open orders on Tabdeal; leverage and margin are capped by the config;
the stop must sit before the estimated cross-margin liquidation; an entry remainder still resting
when the position ends is canceled (it would open a new, unprotected position).
"""

from __future__ import annotations

import contextlib
import json
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
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
    max_leverage: int = 100
    max_margin_usdt: float = 50.0
    working_type: str = "MARK_PRICE"
    poll_s: float = 3.0
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


SL_KEYS = ("slPrice", "stopLossPrice", "stopLoss", "sl_price", "sl")
TP_KEYS = ("tpPrice", "takeProfitPrice", "takeProfit", "tp_price", "tp")


def _price(rows: list[dict[str, Any] | None], keys: tuple[str, ...]) -> Decimal | None:
    """A stop / target price Tabdeal reports on a position (field names vary), if any."""
    for r in rows:
        for k in keys:
            v = (r or {}).get(k)
            try:
                d = Decimal(str(v)) if v not in (None, "") else None
            except InvalidOperation:
                d = None
            if d is not None and d.is_finite() and d > 0:
                return d
    return None


EARLIEST = 1_483_228_800  # 2017-01-01: no Tabdeal position opened before this


def epoch(v: Any, now: float) -> float | None:
    """Epoch seconds from a Tabdeal time in s, ms or µs (the unit varies by endpoint), or None
    if it is missing or not a plausible time (before 2017, or more than a day ahead)."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    for scale in (1.0, 1e3, 1e6, 1e9):
        if EARLIEST <= x / scale <= now + 86400:
            return x / scale
    return None


def _rows(rows: Any) -> list[dict[str, Any]]:
    if isinstance(rows, dict):
        rows = rows.get("data") or rows.get("orders") or rows.get("result") or []
    return [r for r in rows or [] if isinstance(r, dict)]


COLUMNS = (
    "id, symbol, side, status, leverage, margin_usdt, qty, entry, sl, tp, client_id, order_id,"
    " position_id, filled_qty, avg_entry, sltp_qty, exit_price, realized_pnl, close_reason,"
    " drawing_id, last_error, events, created_at, filled_at, closed_at, updated_at, origin"
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
        w = self.wallet()
        busy = self._busy(symbol, market)
        return {
            "symbol": symbol,
            "available_usdt": float(w["available"]),
            "available_estimated": w["estimated"],
            "wallet_usdt": float(w["wallet"]),
            "busy": busy,
            **self.cfg.public(),
        }

    def wallet(self) -> dict[str, Any]:
        """The futures wallet in USDT: {wallet, available, estimated}.

        Tabdeal has been seen to report availableBalance 0 with no position open and a funded
        wallet (2026-09-26), so a non-positive one is replaced by an estimate: wallet balance +
        unrealized PnL - the initial margin of the open positions. The exchange still has the last
        word when the order is placed."""
        u = self._usdt()
        wallet = D(u.get("crossWalletBalance") or u.get("walletBalance") or u.get("balance") or 0)
        avail = D(u.get("availableBalance") or 0)
        if avail > 0:
            return {"wallet": wallet, "available": avail, "estimated": False}
        used = Decimal(0)
        for market in (ws_market(s) for s in self.instruments):
            pos = _first(self.ex.position_risk(market), market)
            amt = D(pos.get("positionAmt", 0)) if pos else Decimal(0)
            if pos is not None and amt != 0:
                lev = D(pos.get("leverage") or 1) or Decimal(1)
                used += abs(amt) * D(pos.get("entryPrice") or 0) / lev
        est = wallet + D(u.get("crossUnPnl") or 0) - used
        return {"wallet": wallet, "available": max(est, Decimal(0)), "estimated": True}

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
        out["protected"] = bool(filled > 0 and Decimal(r["sltp_qty"] or 0) >= filled
                                and (r["sl"] is not None or r["tp"] is not None))
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
            w = self.wallet()
            avail, wallet = w["available"], w["wallet"]
            if notional / leverage > avail:
                raise TradeError(
                    f"needs {notional / leverage:.2f} USDT margin; {avail:.2f} available"
                    + (" (estimated)" if w["estimated"] else "")
                    + f" in the futures wallet ({wallet:.2f} USDT)"
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
            step_name = "setting the leverage"
            try:
                self.ex.set_leverage(market, leverage)
                self._set(tid, f"leverage set to {leverage}")
                step_name = "placing the order"
                o = self.ex.limit_order(market, "BUY" if side == "LONG" else "SELL",
                                        wire(qty, step), wire(entry, tick), client_id)
            except ExchangeError as e:
                if e.status is None and step_name == "placing the order":
                    # the order may or may not exist: look for it
                    self._set(tid, f"order outcome unknown ({e})", last_error=str(e))
                    self._settle(tid)
                else:
                    why = f"{step_name}: {e}" + _hint(e)
                    self._set(tid, f"rejected while {why}", status="REJECTED", last_error=why,
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

    def set_sltp(self, tid: int, sl: Any, tp: Any) -> dict[str, Any]:
        """Set or move the stop / target of an active trade on Tabdeal."""
        with self._lock:
            t = self._get(tid)
            if t["status"] != "ACTIVE":
                raise TradeError(f"trade {tid} is {t['status']}: only an active trade")
            tick = self.instruments[t["symbol"]][0]
            nsl = None if sl in (None, "") else near(D(sl), tick)
            ntp = None if tp in (None, "") else near(D(tp), tick)
            if nsl is None and ntp is None:
                raise TradeError("give a stop, a target or both")
            live = self.live.get(t["symbol"]) or {}
            ref = D(live["mark"]) if live.get("mark") else Decimal(t["avg_entry"] or t["entry"])
            long = t["side"] == "LONG"
            if nsl is not None and (nsl >= ref if long else nsl <= ref):
                raise TradeError(f"the stop must be {'below' if long else 'above'} the price {ref}")
            if ntp is not None and (ntp <= ref if long else ntp >= ref):
                raise TradeError(
                    f"the target must be {'above' if long else 'below'} the price {ref}")
            market = ws_market(t["symbol"])
            if t["position_id"] is None:
                p = self._position(market)
                if p is None:
                    raise TradeError("Tabdeal reports no open position for this trade")
                self._set(tid, f"position {p.get('id')}", position_id=p.get("id"))
            old = (t["sl"], t["tp"])
            self._set(tid, f"stop / target requested: {nsl} / {ntp}", sl=nsl, tp=ntp)
            try:
                self._protect(self._get(tid), market)
            except TradeError:
                self._set(tid, None, sl=old[0], tp=old[1])  # Tabdeal keeps the previous ones
                raise
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
        with self._lock:
            errors = []
            for tid in self._open_ids():
                try:
                    self._reconcile_one(tid)
                except ExchangeError as e:
                    errors.append(str(e))
                    log.warning("trade %s: reconcile failed: %s", tid, e)
            for symbol in self.instruments:
                if self._open_ids(symbol):
                    continue
                self.live.pop(symbol, None)
                try:
                    self._adopt(symbol)
                except ExchangeError as e:
                    errors.append(str(e))
                    log.warning("%s: position check failed: %s", symbol, e)
            self.poll_error = errors[0] if errors else None

    def _adopt(self, symbol: str) -> int | None:
        """A position opened directly on Tabdeal: follow it as a trade from now on."""
        market = ws_market(symbol)
        pos = _first(self.ex.position_risk(market), market)
        amt = D(pos.get("positionAmt", 0)) if pos else Decimal(0)
        if pos is None or amt == 0:
            return None
        p = self._position(market) or {}
        entry = D(pos.get("entryPrice") or p.get("entryPrice") or 0)
        lev = int(D(pos.get("leverage") or 1)) or 1
        sl, tp = _price([pos, p], SL_KEYS), _price([pos, p], TP_KEYS)
        opened = epoch(p.get("createdTime") or p.get("updateTime") or pos.get("updateTime"),
                       self.clock())
        side = "LONG" if amt > 0 else "SHORT"
        qty = abs(amt)
        pid = p.get("id")
        from datetime import UTC, datetime

        at = datetime.fromtimestamp(opened, UTC) if opened is not None else _now()
        protected = qty if (sl is not None or tp is not None) else Decimal(0)
        def n(v: Decimal | None) -> str:
            return "—" if v is None else f"{v.normalize():f}"

        ev = f"adopted from Tabdeal: {side} {n(qty)} @ {n(entry)} x{lev}" + (
            f", stop {n(sl)} / target {n(tp)}" if protected else ", no stop / target reported"
        )
        with self.db.begin() as c:
            tid = int(
                c.execute(
                    text(
                        "INSERT INTO manual_trades (symbol, side, status, leverage, margin_usdt,"
                        " qty, entry, sl, tp, client_id, position_id, filled_qty, avg_entry,"
                        " sltp_qty, origin, created_at, filled_at, events) VALUES (:s, :side,"
                        " 'ACTIVE', :lev, :m, :q, :e, :sl, :tp, :cid, :pid, :q, :e, :prot,"
                        " 'TABDEAL', :at, :at, CAST(:ev AS jsonb)) RETURNING id"
                    ),
                    {"s": symbol, "side": side, "lev": lev, "m": qty * entry / lev, "q": qty,
                     "e": entry, "sl": sl, "tp": tp,
                     "cid": f"tabdeal-{pid or secrets.token_hex(6)}-{int(self.clock())}",
                     "pid": pid, "prot": protected, "at": at,
                     "ev": json.dumps([{"at": self.clock(), "event": ev}])},
                ).scalar_one()
            )
        log.info("trade %s: %s", tid, ev)
        self._active(self._get(tid), market, "FILLED")
        return tid

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
        if t["order_id"] is None:  # adopted from Tabdeal: only the position to follow
            return self._active(t, market, "FILLED")
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
        if amt == 0 or (amt > 0) != (t["side"] == "LONG"):  # closed (or flipped on Tabdeal)
            return self._ended(t, market, order_status)
        if t["origin"] == "TABDEAL" and t["filled_at"] is not None and (
            t["filled_at"].timestamp() < EARLIEST
        ):  # adopted before times in seconds were understood (stored as 1970)
            prow = self._position(market) or {}
            opened = epoch(prow.get("createdTime") or prow.get("updateTime"), self.clock())
            from datetime import UTC, datetime

            at = datetime.fromtimestamp(opened, UTC) if opened is not None else t["created_at"]
            if at.timestamp() < EARLIEST:
                at = _now()
            self._set(t["id"], f"opening time corrected to {at.isoformat()}", filled_at=at,
                      created_at=at)
            t = self._get(t["id"])
        if t["origin"] == "TABDEAL" and abs(amt) != Decimal(t["filled_qty"]):
            entry = D(pos.get("entryPrice") or t["avg_entry"]) if pos else Decimal(t["entry"])
            self._set(t["id"], f"size on Tabdeal now {abs(amt).normalize()} @ {entry}",
                      filled_qty=abs(amt), qty=abs(amt), avg_entry=entry)
            t = self._get(t["id"])
        self.live[t["symbol"]] = {
            "position_amt": float(amt),
            "entry_price": fnum(pos.get("entryPrice")) if pos else None,
            "mark": fnum(pos.get("markPrice")) if pos else None,
            "upnl": fnum(pos.get("unRealizedProfit")) if pos else None,
            "liquidation": fnum(pos.get("liquidationPrice")) if pos else None,
            "leverage": fnum(pos.get("leverage")) if pos else None,
            "at": self.clock(),
            # Tabdeal's own row, shown in the trade's details to compare with its app
            "raw": {k: v for k, v in (pos or {}).items() if isinstance(v, (str, int, float, bool))},
        }
        if t["position_id"] is None:
            p = self._position(market)
            if p is not None:
                self._set(t["id"], f"position {p.get('id')}", position_id=p.get("id"))
                t = self._get(t["id"])
        if t["sl"] is None and t["tp"] is None:  # adopted unprotected: did it get one on Tabdeal?
            sl, tp = _price([pos], SL_KEYS), _price([pos], TP_KEYS)
            if sl is not None or tp is not None:
                self._set(t["id"], f"stop {sl} / target {tp} reported by Tabdeal", sl=sl, tp=tp,
                          sltp_qty=t["filled_qty"])
            return None
        if Decimal(t["sltp_qty"]) < Decimal(t["filled_qty"]):
            with contextlib.suppress(TradeError):  # logged on the trade; the next poll retries
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
        sl = None if t["sl"] is None else wire(t["sl"], tick)
        tp = None if t["tp"] is None else wire(t["tp"], tick)
        try:
            self.ex.position_sl_tp(t["position_id"], market, sl, tp, self.cfg.working_type)
        except ExchangeError as e:
            self._set(t["id"], f"stop / target refused: {e}", last_error=str(e))
            raise TradeError(f"Tabdeal refused the stop / target: {e}") from e
        self._set(t["id"], f"stop {sl} / target {tp} set on"
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
        entry = Decimal(t["avg_entry"] or t["entry"])
        if t["tp"] is not None and abs(exit_price - t["tp"]) <= abs(t["tp"] - entry) / 4:
            return "TP"
        if t["sl"] is not None and abs(exit_price - t["sl"]) <= abs(entry - t["sl"]) / 4:
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


def _hint(e: ExchangeError) -> str:
    """What to check when Tabdeal refuses a trading call."""
    msg = str(e).lower()
    if e.status in (401, 403) or "denied" in msg or "permission" in msg or "ip" in msg.split():
        return (" — check the Tabdeal API key: it must allow futures trading (not read-only),"
                " and if it is limited to IP addresses, the server's IP must be on its list")
    if "signature" in msg or "timestamp" in msg or "recvwindow" in msg:
        return " — the request signature / clock was refused: check the key, secret and server time"
    return ""


def check_key(client: Any, market: str) -> list[tuple[str, bool, str]]:
    """Can this API key trade? Reads (balance, leverage), then sets the market's leverage to the
    value it already has: nothing changes, no order is placed, but Tabdeal answers whether the
    key may make changes. [(step, ok, detail)]."""
    out: list[tuple[str, bool, str]] = []
    try:
        rows = _rows(client.balance())
        usdt = next((r for r in rows if str(r.get("asset", "")).upper() == "USDT"), {})
        out.append(("read the futures wallet", True,
                    f"USDT wallet {usdt.get('crossWalletBalance') or usdt.get('walletBalance')}"
                    f", available {usdt.get('availableBalance')}"))
    except ExchangeError as e:
        out.append(("read the futures wallet", False, str(e) + _hint(e)))
        return out
    try:
        lev = client.get_leverage(market)
        cur = int(D((lev or {}).get("leverage")))
        out.append((f"read the leverage of {market}", True, f"{cur}x"))
    except (ExchangeError, TradeError, TypeError, ValueError) as e:
        detail = str(e) + (_hint(e) if isinstance(e, ExchangeError) else "")
        out.append((f"read the leverage of {market}", False, detail))
        return out
    try:
        client.set_leverage(market, cur)
        out.append((f"set the leverage of {market} (same value, {cur}x)", True,
                    "the key may trade"))
    except ExchangeError as e:
        out.append((f"set the leverage of {market} (same value, {cur}x)", False,
                    str(e) + _hint(e)))
    return out


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime.now(UTC)
