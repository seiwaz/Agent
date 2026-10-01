"""Causal Shadow exchange simulator (SHD-03/04; V5.1 B19, B24).

Fill model:
- An order can only fill from trades processed after it was submitted (no retroactive fill).
- A resting entry limit fills its FULL quantity once a raw trade prints strictly through
  the limit (BUY: trade < price; SELL: trade > price), at the limit price. A touch is not a
  fill. No partial fills are fabricated.
- Protection is position-level, as on Tabdeal (positionSlTp, CONTRACT_PRICE = last trade):
  - SL (Long) triggers on a trade <= SL and fills at the worse of SL and that trade;
  - the single TP fills on a trade strictly through TP (a resting limit) at the TP price.
- Within one trade, entries are processed before exits, so a gap through E2 and SL counts
  both (the conservative path).
Fees come from the configured CostModel (never hardcoded).
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sp2l.core.numeric import DECISION_CONTEXT
from sp2l.core.types import Side
from sp2l.engine.model import (
    ExitEvent,
    ExitKind,
    FillEvent,
    OrderSnapshot,
    OrderStatus,
)
from sp2l.strategy.risk.engine import CostModel


@dataclass(slots=True)
class _Order:
    client_order_id: str
    side: Side  # position side this entry increases
    price: Decimal
    qty: Decimal
    executed: Decimal
    status: OrderStatus
    submitted_after_trade: int  # trade sequence number at submission (causality audit)


@dataclass(slots=True)
class _Protection:
    side: Side
    sl: Decimal
    tp: Decimal


class ShadowBroker:
    def __init__(self, costs: CostModel, *, protection_fails: int = 0) -> None:
        self.costs = costs
        self._orders: dict[str, _Order] = {}
        self._position = Decimal(0)  # signed
        self._entry_cost = Decimal(0)  # sum(price*qty) of the open position
        self._protection: _Protection | None = None
        self._protection_fails = protection_fails  # test hook for B21
        self.last_price: Decimal | None = None
        self.realized_pnl = Decimal(0)
        self.fees = Decimal(0)
        self.trade_seq = 0
        # (client_order_id, filling trade seq, trade seq at submission) - E1-09 audit
        self.fill_audit: list[tuple[str, int, int]] = []

    # ---- ExecutionPort ----------------------------------------------------------------

    def submit_limit(
        self, client_order_id: str, side: Side, price: Decimal, qty: Decimal, ts: datetime
    ) -> OrderSnapshot:
        if client_order_id in self._orders:
            return self._snap(self._orders[client_order_id])  # idempotent (TAB-02)
        o = _Order(client_order_id, side, price, qty, Decimal(0), OrderStatus.NEW, self.trade_seq)
        self._orders[client_order_id] = o
        return self._snap(o)

    def cancel(self, client_order_id: str, ts: datetime) -> OrderSnapshot:
        o = self._orders[client_order_id]
        if o.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED):
            o.status = OrderStatus.CANCELED
        return self._snap(o)

    def query(self, client_order_id: str) -> OrderSnapshot:
        return self._snap(self._orders[client_order_id])

    def position_qty(self) -> Decimal:
        return self._position

    def place_protection(self, side: Side, sl: Decimal, tp: Decimal, ts: datetime) -> bool:
        if self._protection_fails > 0:
            self._protection_fails -= 1
            return False
        self._protection = _Protection(side, sl, tp)
        return True

    def emergency_close(self, ts: datetime) -> ExitEvent | None:
        if self._position == 0 or self.last_price is None:
            return None
        return self._exit(ExitKind.EMERGENCY, ts, self.last_price)

    def discard_unknowable(self, client_order_ids: list[str]) -> Decimal:
        """V5.5 B39: a DATA_GAP overlapped live exposure. Resting simulated orders and the
        simulated position are dropped WITHOUT a fabricated close, fill or PnL (fees already
        charged on real observed fills stay). Returns the discarded position quantity."""
        for cid in client_order_ids:
            o = self._orders.get(cid)
            if o is not None and o.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED):
                o.status = OrderStatus.CANCELED
        discarded = self._position
        self._position = Decimal(0)
        self._entry_cost = Decimal(0)
        self._protection = None
        return discarded

    # ---- market data -----------------------------------------------------------------

    def on_trade(self, price: Decimal, ts: datetime) -> tuple[list[FillEvent], list[ExitEvent]]:
        self.last_price = price
        self.trade_seq += 1
        fills: list[FillEvent] = []
        for o in self._orders.values():
            if o.status is not OrderStatus.NEW:
                continue
            through = price < o.price if o.side is Side.LONG else price > o.price
            if not through:
                continue
            if o.submitted_after_trade >= self.trade_seq:  # pragma: no cover - invariant
                raise AssertionError("retroactive fill")
            self.fill_audit.append((o.client_order_id, self.trade_seq, o.submitted_after_trade))
            fill_qty = o.qty - o.executed
            o.executed = o.qty
            o.status = OrderStatus.FILLED
            fee = self._fee(o.price * fill_qty, self.costs.entry_fee_rate)
            signed = fill_qty if o.side is Side.LONG else -fill_qty
            self._position += signed
            self._entry_cost += o.price * fill_qty
            fills.append(
                FillEvent(o.client_order_id, ts, o.price, fill_qty, fee, o.executed, o.qty)
            )
        exits: list[ExitEvent] = []
        p = self._protection
        if p is not None and self._position != 0:
            long = p.side is Side.LONG
            sl_hit = price <= p.sl if long else price >= p.sl
            tp_hit = price > p.tp if long else price < p.tp
            if sl_hit:
                fill = min(price, p.sl) if long else max(price, p.sl)
                exits.append(self._exit(ExitKind.SL, ts, fill))
            elif tp_hit:
                exits.append(self._exit(ExitKind.TP, ts, p.tp))
        return fills, exits

    # ---- internals -------------------------------------------------------------------

    def _fee(self, notional: Decimal, rate: Decimal) -> Decimal:
        with decimal.localcontext(DECISION_CONTEXT):
            fee = notional * rate
        self.fees += fee
        return fee

    def _exit(self, kind: ExitKind, ts: datetime, price: Decimal) -> ExitEvent:
        qty = abs(self._position)
        long = self._position > 0
        with decimal.localcontext(DECISION_CONTEXT):
            pnl = (price * qty - self._entry_cost) if long else (self._entry_cost - price * qty)
        fee = self._fee(price * qty, self.costs.exit_fee_rate)
        self.realized_pnl += pnl
        self._position = Decimal(0)
        self._entry_cost = Decimal(0)
        self._protection = None
        return ExitEvent(kind, ts, price, qty, fee, pnl)

    @staticmethod
    def _snap(o: _Order) -> OrderSnapshot:
        return OrderSnapshot(o.client_order_id, o.status, o.price, o.qty, o.executed)
