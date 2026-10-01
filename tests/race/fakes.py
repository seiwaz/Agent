"""Scripted exchange port and gate evaluator for state-machine and race tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal as D

from sp2l.core.types import Side
from sp2l.engine.model import ExitEvent, ExitKind, FillEvent, OrderSnapshot, OrderStatus
from sp2l.engine.setup_machine import GateResult, MachineConfig, SetupMachine
from sp2l.marketdata.m1_builder import M1Result, M1Status
from sp2l.strategy.context.engine import ContextSnapshot
from sp2l.strategy.exhaustion.engine import ExhaustionSnapshot
from sp2l.strategy.levels import SetupLevels
from sp2l.strategy.risk.engine import RiskResult
from sp2l.strategy.spike import Spike
from tests.conftest import T0, candle

TICK = D("0.1")


@dataclass
class FakeOrder:
    cid: str
    side: Side
    price: D
    qty: D
    executed: D = D(0)
    status: OrderStatus = OrderStatus.NEW


@dataclass
class FakePort:
    orders: dict[str, FakeOrder] = field(default_factory=dict)
    submits: list[tuple[str, D, D]] = field(default_factory=list)
    pos: D = D(0)
    # behaviours consumed per cancel call: "ok", "pending", ("fill", qty), ("position", qty)
    cancel_script: list[object] = field(default_factory=list)
    protection_script: list[bool] = field(default_factory=list)
    protections: list[tuple[D, D]] = field(default_factory=list)
    emergency_closes: int = 0

    def submit_limit(self, cid, side, price, qty, ts):
        self.submits.append((cid, price, qty))
        self.orders[cid] = FakeOrder(cid, side, price, qty)
        return self.query(cid)

    def cancel(self, cid, ts):
        o = self.orders[cid]
        how = self.cancel_script.pop(0) if self.cancel_script else "ok"
        if how == "pending":
            o.status = OrderStatus.PENDING_CANCEL
        elif isinstance(how, tuple) and how[0] == "fill":
            self.fill(cid, how[1])
            if o.status is not OrderStatus.FILLED:
                o.status = OrderStatus.CANCELED
        elif isinstance(how, tuple) and how[0] == "position":
            self.pos += how[1]
            o.status = OrderStatus.CANCELED
        elif o.status in (
            OrderStatus.NEW,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.PENDING_CANCEL,
        ):
            o.status = OrderStatus.CANCELED
        return self.query(cid)

    def query(self, cid):
        o = self.orders[cid]
        return OrderSnapshot(cid, o.status, o.price, o.qty, o.executed)

    def position_qty(self):
        return self.pos

    def place_protection(self, side, sl, tp, ts):
        ok = self.protection_script.pop(0) if self.protection_script else True
        if ok:
            self.protections.append((sl, tp))
        return ok

    def emergency_close(self, ts):
        self.emergency_closes += 1
        qty, self.pos = abs(self.pos), D(0)
        return ExitEvent(ExitKind.EMERGENCY, ts, D("100"), qty, D(0)) if qty else None

    # test helpers -------------------------------------------------------------------
    def fill(self, cid: str, qty: D) -> FillEvent:
        o = self.orders[cid]
        o.executed += qty
        o.status = OrderStatus.FILLED if o.executed >= o.qty else OrderStatus.PARTIALLY_FILLED
        self.pos += qty if o.side is Side.LONG else -qty
        return FillEvent(cid, T0, o.price, qty, D(0), o.executed, o.qty)

    def active_e1(self) -> list[str]:
        return [
            c
            for c, o in self.orders.items()
            if "-E1-" in c
            and o.status
            in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED, OrderStatus.PENDING_CANCEL)
        ]


def gate(ok: bool = True, stage: str = "context", qty: str = "1") -> GateResult:
    ctx = ContextSnapshot(
        status="PASS" if ok or stage != "context" else "REJECT",
        reasons=[] if ok or stage != "context" else ["NO_VALID_CONTEXT"],
    )
    exh = ExhaustionSnapshot(
        status="PASS" if ok or stage != "exhaustion" else "REJECT",
        reason=None if ok or stage != "exhaustion" else "EXHAUSTION_RISK",
    )
    risk = RiskResult(
        passed=ok or stage != "risk",
        qty=D(qty),
        reasons=[] if ok or stage != "risk" else ["RISK_QTY_BELOW_MIN"],
    )
    return GateResult(ctx, exh, risk)


@dataclass
class FakeGates:
    decide: Callable[[int, SetupLevels, bool], GateResult] = lambda i, lv, r: gate()
    calls: list[tuple[SetupLevels, bool]] = field(default_factory=list)
    e2_ok: bool = True

    def evaluate(self, *, side, eval_time, levels, spike, frozen, with_risk):
        self.calls.append((levels, with_risk))
        res = self.decide(len(self.calls) - 1, levels, with_risk)
        if not with_risk:
            res = GateResult(res.context, res.exhaustion, None)
        return res

    def e2_allowed(self):
        return self.e2_ok

    def eligible_breakout_levels(self, side, spike):
        return self.eligible

    eligible: tuple = ()


def base_spike() -> Spike:
    """Long spike: Origin low 99 (SL 98.9), LastSpike low 101 (E1 101), R 2.1."""
    return Spike(
        Side.LONG,
        [
            candle(0, "99.5", "100", "99", "99.8"),
            candle(1, "100", "101", "99.5", "100.8"),
            candle(2, "101.5", "103", "101", "102.5"),
        ],
    )


def m1(i: int, o: str, h: str, lo: str, c: str) -> M1Result:
    return M1Result(T0 + timedelta(minutes=i), M1Status.OK, candle(i, o, h, lo, c))


def at(i: int, sec: int = 0) -> datetime:
    return T0 + timedelta(minutes=i, seconds=sec)


def machine(
    port: FakePort | None = None, gates: FakeGates | None = None, **cfg
) -> tuple[SetupMachine, FakePort, FakeGates]:
    port = port or FakePort()
    gates = gates or FakeGates()
    m = SetupMachine(
        MachineConfig(setup_id="s1", tick=TICK, **cfg), port, gates, base_spike(), None
    )
    return m, port, gates
