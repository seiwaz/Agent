"""RoomToTP hypothesis probe — ANALYSIS ONLY; it can never influence a runtime decision.

Evidence collection for hypothesis B ("breakout-consumed obstacle", 2026-09-29 study). For a
candidate rejected at creation with ROOM_TO_TP_INSUFFICIENT, the probe watches exactly ONE
next finalized M1 and records whether the SAME Spike, still valid, closed strictly through
the exact blocking swing (Long: Close > level; Short: Close < level; no wick, no equality),
with no touch of the original E1 (PullbackStart) and no data gap. If so it re-runs the full
Context / Exhaustion / Risk evaluation at that minute with only that level removed from the
obstacles, applies B15, and then simulates ordinary SP2L execution (E1, PullbackStart,
FillWindow, E2, SL/TP) on its own ShadowBroker with the real cost model, like a
counterfactual. It never reserves the market slot, never touches the Shadow wallet or
broker, and is never checkpointed (a probe in flight at a restart is simply not recorded).
"""

from __future__ import annotations

import copy
import dataclasses
from datetime import datetime
from decimal import Decimal
from typing import Any

from sp2l.core.types import Side
from sp2l.engine.model import ExitKind
from sp2l.engine.setup_machine import MachineConfig, SetupMachine
from sp2l.execution.shadow_broker import ShadowBroker
from sp2l.marketdata.m1_builder import M1Result
from sp2l.strategy.risk.engine import CostModel

PROBE_VERSION = "room-b1"
ROOM_REASON = "ROOM_TO_TP_INSUFFICIENT"


def execution_breakdown(m: SetupMachine, costs: CostModel) -> dict[str, Any]:
    """Actual simulated execution of one setup, decomposed into price result and costs.

    initial trade risk (1R, USDT) = E1 filled qty x |E1 - SL|. Slippage = the SL fill's
    distance beyond the SL price (Shadow fills at the worse of SL and the trigger trade).
    gross = the result at the nominal TP/SL prices; net = gross - fees - slippage.
    """
    lv = m.levels
    long = m.side is Side.LONG
    e1 = [f for f in m.fills if "-E1-" in f.client_order_id]
    e2 = [f for f in m.fills if "-E2-" in f.client_order_id]
    q1 = sum((f.qty for f in e1), Decimal(0))
    q2 = sum((f.qty for f in e2), Decimal(0))
    entry_notional = sum((f.price * f.qty for f in e1 + e2), Decimal(0))
    entry_fee = sum((f.fee for f in e1 + e2), Decimal(0))
    ex = m.exits[-1] if m.exits else None
    r_usdt = q1 * lv.r if q1 else None
    out: dict[str, Any] = {
        "e1": str(lv.e1),
        "e2": str(lv.e2),
        "sl": str(lv.sl),
        "tp": str(lv.tp),
        "r_price": str(lv.r),
        "r_pct": float(lv.r / lv.e1 * 100),
        "r_bps": float(lv.r / lv.e1 * 10000),
        "e1_filled_qty": str(q1),
        "e2_filled_qty": str(q2),
        "e2_state": "FILLED" if q2 else ("SUBMITTED" if m.e2_id else "NOT_SUBMITTED"),
        "entry_notional": str(entry_notional),
        "entry_fee": str(entry_fee),
        "exit": str(ex.kind) if ex else None,
        "r_usdt": str(r_usdt) if r_usdt is not None else None,
    }
    if ex is None or not r_usdt:
        return out
    nominal = lv.tp if ex.kind is ExitKind.TP else (lv.sl if ex.kind is ExitKind.SL else ex.price)
    slip = abs(ex.price - nominal) * ex.qty if ex.kind is ExitKind.SL else Decimal(0)
    gross_nominal = sum(
        ((nominal - f.price) if long else (f.price - nominal)) * f.qty for f in e1 + e2
    )
    exit_fee = ex.fee
    costs_total = entry_fee + exit_fee + slip
    out.update(
        {
            "exit_price": str(ex.price),
            "exit_notional": str(ex.price * ex.qty),
            "exit_fee": str(exit_fee),
            "slippage_usdt": str(slip),
            "total_cost_usdt": str(costs_total),
            "gross_usdt": str(gross_nominal),
            "net_usdt": str(gross_nominal - costs_total),
            "entry_fee_r": float(entry_fee / r_usdt),
            "exit_fee_r": float(exit_fee / r_usdt),
            "slippage_r": float(slip / r_usdt),
            "cost_r": float(costs_total / r_usdt),
            "gross_r": float(gross_nominal / r_usdt),
            "net_r": float((gross_nominal - costs_total) / r_usdt),
            "exit_ts": ex.ts.isoformat(),
        }
    )
    return out


class RoomProbe:
    def __init__(self, original: SetupMachine, gates: Any, costs: CostModel, ts: datetime) -> None:
        ctx = original.evaluations[0][2].context if original.evaluations else None
        assert ctx is not None and ctx.nearest_obstacle is not None
        self.key = original.cfg.setup_id
        self.side = original.side
        self.long = original.side is Side.LONG
        self.created = ts
        self.level: Decimal = ctx.nearest_obstacle
        self.orig = original.levels
        self.spike = copy.deepcopy(original.spike)
        self.eligible = original.eligible_levels
        self.gates = dataclasses.replace(gates, consumed_obstacles=frozenset({self.level}))
        self.costs = costs
        self.cfg = original.cfg
        self.touched_at: datetime | None = None
        self.stage = "WAITING"  # WAITING -> RUNNING -> DONE
        self.result: str | None = None
        self.machine: SetupMachine | None = None
        self.broker: ShadowBroker | None = None
        self.info: dict[str, Any] = {
            "probe_version": PROBE_VERSION,
            "candidate_key": self.key,
            "side": str(self.side),
            "created_at": ts.isoformat(),
            "blocking_swing": str(self.level),
            "distance_usdt": str(abs(self.level - self.orig.e1)),
            "distance_r": float(abs(self.level - self.orig.e1) / self.orig.r),
            "room_r": None if ctx.room_to_tp_r is None else float(ctx.room_to_tp_r),
            "original": {
                "e1": str(self.orig.e1),
                "sl": str(self.orig.sl),
                "tp": str(self.orig.tp),
                "r": str(self.orig.r),
            },
            "original_reasons": list(ctx.reasons),
        }

    @property
    def done(self) -> bool:
        return self.stage == "DONE"

    def _finish(self, result: str) -> None:
        self.stage, self.result = "DONE", result

    def abort(self, reason: str) -> None:
        if not self.done:
            if self.broker is not None and self.machine is not None:
                orders, _ = self.machine.exposure()
                self.broker.discard_unknowable(orders)
            self._finish(reason)

    def on_trade(self, price: Decimal, ts: datetime) -> None:
        if self.stage == "WAITING":
            e1 = self.orig.e1
            if self.touched_at is None and (price <= e1 if self.long else price >= e1):
                self.touched_at = ts  # PullbackStart of the original E1 during the wait
        elif self.stage == "RUNNING":
            assert self.machine is not None and self.broker is not None
            self.machine.on_trade(price, ts)
            fills, exits = self.broker.on_trade(price, ts)
            for f in fills:
                self.machine.on_fill(f)
            for e in exits:
                self.machine.on_exit(e)
            if self.machine.terminal:
                self._finish(str(self.machine.state))

    def on_m1(
        self, m1: M1Result, eval_time: datetime, last_trade: Decimal | None, slot_free: bool
    ) -> None:
        if self.stage == "RUNNING":
            assert self.machine is not None
            self.machine.on_m1_close(m1, eval_time, last_trade)
            if self.machine.terminal:
                self._finish(str(self.machine.state))
            return
        if self.stage != "WAITING":
            return
        c = m1.candle
        alive = self.spike.try_extend(m1)
        broke = c is not None and (c.close > self.level if self.long else c.close < self.level)
        self.info.update(
            {
                "wait_minute": m1.open_time.isoformat(),
                "wait_close": str(c.close) if c else None,
                "same_spike_alive": alive,
                "closed_through": broke,
                "pullback_during_wait": self.touched_at is not None,
                "slot_free_at_decision": slot_free,
            }
        )
        if not alive:
            self._finish("RUN_ENDED")
            return
        if self.touched_at is not None:
            self._finish("PULLBACK_STARTED")
            return
        if not broke:
            self._finish("NO_CLOSE_THROUGH_OBSTACLE")
            return
        self.info["breakout_ts"] = eval_time.isoformat()
        self.broker = ShadowBroker(self.costs)
        cfg = MachineConfig(
            setup_id=f"probe-{self.key}",
            tick=self.cfg.tick,
            fill_window_candles=self.cfg.fill_window_candles,
            protection_attempts=self.cfg.protection_attempts,
            cancel_wait_closes=self.cfg.cancel_wait_closes,
        )
        m = SetupMachine(cfg, self.broker, self.gates, self.spike, None)
        m.eligible_levels = self.eligible
        m.start(eval_time, last_trade)  # full Context/Exhaustion/Risk + B15 at this minute
        self.machine = m
        res = m.evaluations[0][2] if m.evaluations else None
        self.info["reevaluation"] = {
            "e1": str(m.levels.e1),
            "sl": str(m.levels.sl),
            "tp": str(m.levels.tp),
            "r": str(m.levels.r),
            "context": res.context.status if res and res.context else None,
            "context_reasons": list(res.context.reasons) if res and res.context else [],
            "breakout_context": res.context.breakout_context if res and res.context else None,
            "htf_alignment": res.context.htf_alignment if res and res.context else None,
            "range_edge_origin": res.context.range_edge_origin if res and res.context else None,
            "liquidity": res.context.liquidity_status if res and res.context else None,
            "obstacle": (
                str(res.context.nearest_obstacle)
                if res and res.context and res.context.nearest_obstacle is not None
                else None
            ),
            "room_r": (
                float(res.context.room_to_tp_r)
                if res and res.context and res.context.room_to_tp_r is not None
                else None
            ),
            "exhaustion": res.exhaustion.status if res and res.exhaustion else None,
            "risk": res.risk.passed if res and res.risk else None,
            "b15_safe": m.ever_armed,
        }
        if m.terminal:
            self._finish(str(m.state))
        elif not m.ever_armed:
            self._finish("B15_NOT_SAFE")  # must be market-safe at that minute
        else:
            self.stage = "RUNNING"

    def record(self) -> dict[str, Any]:
        out = dict(self.info)
        out["result"] = self.result
        m = self.machine
        if m is not None:
            out["qualified"] = m.ever_armed
            out["filled"] = m.e1_filled > 0
            out["pullback_minute"] = m.pb_minute.isoformat() if m.pb_minute else None
            out["execution"] = execution_breakdown(m, self.costs)
        else:
            out["qualified"] = False
            out["filled"] = False
        return out
