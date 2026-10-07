"""A stand-in for Tabdeal futures (the TabdealTrade calls the trade manager makes): one-way
positions, LIMIT orders that fill when the test says so, position-level SL / TP, market close.
Answers are shaped like docs.tabdeal.org's examples (strings for numbers)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sp2l.trading.client import ExchangeError


class FakeExchange:
    def __init__(self, wallet: str = "1000") -> None:
        self.wallet = Decimal(wallet)
        self.calls: list[tuple[str, Any]] = []
        self.orders: dict[int, dict[str, Any]] = {}
        self.leverage: dict[str, int] = {}
        self.position: dict[str, dict[str, Any]] = {}  # market -> active position
        self.closed: list[dict[str, Any]] = []
        self.mark: dict[str, Decimal] = {}
        self.fail: dict[str, ExchangeError] = {}  # call name -> the error it raises once
        self.lost_reply = False  # place the next order but answer with a timeout
        self.reports_sltp = False  # positionRisk carries the stop / target (undocumented)
        self._ids = 5000

    def _call(self, name: str, args: Any) -> None:
        self.calls.append((name, args))
        if name in self.fail:
            raise self.fail.pop(name)

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]

    # ---- reads ----------------------------------------------------------------------------------
    def balance(self) -> Any:
        self._call("balance", None)
        used = sum(
            (abs(p["amt"]) * p["entry"] / p["lev"] for p in self.position.values()), Decimal(0)
        ) + sum(
            ((o["qty"] - o["exec"]) * o["price"] / self.leverage.get(o["market"], 1)
             for o in self.orders.values() if o["status"] in ("NEW", "PARTIALLY_FILLED")),
            Decimal(0),
        )
        w = str(self.wallet)
        return [{"asset": "USDT", "balance": w, "crossWalletBalance": w,
                 "availableBalance": str(self.wallet - used)}]

    def position_risk(self, market: str) -> Any:
        self._call("position_risk", market)
        p = self.position.get(market)
        if not p:
            return []
        mark = self.mark.get(market, p["entry"])
        return [{"symbol": market, "positionAmt": str(p["amt"]), "entryPrice": str(p["entry"]),
                 "markPrice": str(mark), "unRealizedProfit": str((mark - p["entry"]) * p["amt"]),
                 "liquidationPrice": "1", "leverage": str(p["lev"]), "marginType": "cross",
                 "positionSide": "BOTH",
                 **({"slPrice": str(p.get("sl", 0)), "tpPrice": str(p.get("tp", 0))}
                    if self.reports_sltp else {})}]

    def positions(self, market: str, active: bool, limit: int = 20) -> Any:
        self._call("positions", (market, active))
        if active:
            p = self.position.get(market)
            return [self._row(p, "ACTIVE")] if p else []
        done = [p for p in reversed(self.closed) if p["market"] == market]
        return [self._row(p, "CLOSED") for p in done][:limit]

    @staticmethod
    def _row(p: dict[str, Any], status: str) -> dict[str, Any]:
        return {"id": p["id"], "symbol": p["market"], "side": "BUY" if p["amt"] > 0 else "SELL",
                "positionAmt": str(abs(p["amt"])), "entryPrice": str(p["entry"]),
                "avgExitPrice": str(p.get("exit", 0)), "realizedPnl": str(p.get("pnl", 0)),
                "status": status, "createdTime": p.get("created", 1_700_000_000_000)}

    def order(self, market: str, order_id: int) -> Any:
        self._call("order", order_id)
        o = self.orders.get(order_id)
        if o is None:
            raise ExchangeError("Order does not exist", -2013, 400)
        return self._order_row(o)

    @staticmethod
    def _order_row(o: dict[str, Any]) -> dict[str, Any]:
        return {"orderId": o["id"], "symbol": o["market"], "status": o["status"],
                "clientOrderId": o["cid"], "price": str(o["price"]),
                "avgPrice": str(o["price"] if o["exec"] else 0), "origQty": str(o["qty"]),
                "executedQty": str(o["exec"]), "cumQty": str(o["exec"]), "side": o["side"],
                "type": "LIMIT", "positionSide": "BOTH"}

    def open_orders(self, market: str) -> Any:
        self._call("open_orders", market)
        return [self._order_row(o) for o in self.orders.values()
                if o["market"] == market and o["status"] in ("NEW", "PARTIALLY_FILLED")]

    # ---- changes --------------------------------------------------------------------------------
    def set_leverage(self, market: str, leverage: int) -> Any:
        self._call("set_leverage", (market, leverage))
        self.leverage[market] = leverage
        return {"leverage": leverage, "symbol": market}

    def limit_order(self, market: str, side: str, qty: str, price: str, client_id: str) -> Any:
        self._call("limit_order", (market, side, qty, price, client_id))
        self._ids += 1
        o = {"id": self._ids, "market": market, "side": side, "qty": Decimal(qty),
             "price": Decimal(price), "exec": Decimal(0), "status": "NEW", "cid": client_id}
        self.orders[o["id"]] = o
        if self.lost_reply:
            self.lost_reply = False
            raise ExchangeError("exchange unreachable: TimeoutError")
        return self._order_row(o)

    def cancel(self, market: str, order_id: int) -> Any:
        self._call("cancel", order_id)
        o = self.orders[order_id]
        if o["status"] not in ("NEW", "PARTIALLY_FILLED"):
            raise ExchangeError("Unknown order sent.", -2011, 400)
        o["status"] = "CANCELED"
        return self._order_row(o)

    def position_sl_tp(self, position_id: int, market: str, sl: str | None, tp: str | None,
                       wt: str) -> Any:
        self._call("position_sl_tp", (position_id, market, sl, tp, wt))
        p = self.position.get(market)
        if not p or p["id"] != position_id:
            raise ExchangeError("position not found", 1300, 400)
        if sl is not None:
            p["sl"] = Decimal(sl)
        if tp is not None:
            p["tp"] = Decimal(tp)
        p["sltp_amt"] = abs(p["amt"])
        return {"msg": "success"}

    def close_position(self, market: str) -> Any:
        self._call("close_position", market)
        if market not in self.position:
            raise ExchangeError("no open position", 1301, 400)
        self.end(market, self.mark.get(market, self.position[market]["entry"]))
        return {"msg": "success"}

    # ---- what the market does (test controls) ---------------------------------------------------
    def fill(self, order_id: int, qty: str | None = None) -> None:
        """Fill (part of) an order at its price; the position grows (one-way mode)."""
        o = self.orders[order_id]
        q = Decimal(qty) if qty is not None else o["qty"] - o["exec"]
        o["exec"] += q
        o["status"] = "FILLED" if o["exec"] >= o["qty"] else "PARTIALLY_FILLED"
        signed = q if o["side"] == "BUY" else -q
        p = self.position.get(o["market"])
        if p is None:
            self._ids += 1
            p = {"id": self._ids, "market": o["market"], "amt": Decimal(0), "entry": o["price"],
                 "lev": self.leverage.get(o["market"], 1)}
            self.position[o["market"]] = p
        p["entry"] = (p["entry"] * abs(p["amt"]) + o["price"] * q) / (abs(p["amt"]) + q)
        p["amt"] += signed

    def manual(self, market: str, amt: str, price: str, lev: int = 10,
               sl: str | None = None, tp: str | None = None) -> dict[str, Any]:
        """A position opened directly in Tabdeal's app (amt < 0: short)."""
        self._ids += 1
        p = {"id": self._ids, "market": market, "amt": Decimal(amt), "entry": Decimal(price),
             "lev": lev, "created": 1_790_000_000_000}
        if sl is not None:
            p["sl"] = Decimal(sl)
        if tp is not None:
            p["tp"] = Decimal(tp)
        self.position[market] = p
        return p

    def end(self, market: str, price: Decimal | str) -> None:
        """The position closes at `price` (its stop, its target, or a close on Tabdeal)."""
        p = self.position.pop(market)
        p["exit"] = Decimal(price)
        p["pnl"] = (p["exit"] - p["entry"]) * p["amt"]
        self.wallet += p["pnl"]
        self.closed.append(p)
