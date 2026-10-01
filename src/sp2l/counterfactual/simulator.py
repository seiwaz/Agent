"""Counterfactual simulation of rejected candidates (SHD-06; V5.1 B20).

For every candidate rejected by Context or Exhaustion, a parallel non-executing
simulation continues canonical base SP2L causally (including repricing on extension) with
the same frozen levels and fill rules, on its own ShadowBroker and with unit quantity.
It never creates exchange orders, never reserves Shadow capital and never feeds back into
runtime decisions. Results are reported in R and always labelled COUNTERFACTUAL.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sp2l.engine.gates import BaseSp2lGates
from sp2l.engine.model import ExitKind
from sp2l.engine.setup_machine import MachineConfig, SetupMachine
from sp2l.execution.shadow_broker import ShadowBroker
from sp2l.marketdata.m1_builder import M1Result
from sp2l.strategy.risk.engine import CostModel

LABEL = "COUNTERFACTUAL"
# cf-2 (2026-09-29): the creation minute is no longer fed twice (see ShadowSymbolEngine.on_m1).
# cf-1 froze the counterfactual Spike at creation, so its E1 never followed extensions.
SIMULATOR_VERSION = "cf-2"


@dataclass(frozen=True, slots=True)
class CounterfactualOutcome:
    candidate_id: str
    rejection_stage: str
    label: str
    outcome: str  # TP / SL / EXPIRED_NO_FILL / EXPIRED_UNARMED / OPEN / AMBIGUOUS / ERROR
    result_r_gross: Decimal | None
    result_r_net: Decimal | None
    e2_filled: bool


class CounterfactualRun:
    def __init__(
        self,
        original: SetupMachine,
        rejection_stage: str,
        costs: CostModel,
        eval_time: datetime,
        last_trade: Decimal | None,
    ) -> None:
        self.candidate_id = original.cfg.setup_id
        self.rejection_stage = rejection_stage
        self.broker = ShadowBroker(costs)
        cfg = MachineConfig(
            setup_id=f"cf-{original.cfg.setup_id}",
            tick=original.cfg.tick,
            fill_window_candles=original.cfg.fill_window_candles,
            protection_attempts=original.cfg.protection_attempts,
            cancel_wait_closes=original.cfg.cancel_wait_closes,
        )
        spike = copy.deepcopy(original.spike)
        self.machine = SetupMachine(cfg, self.broker, BaseSp2lGates(), spike, original.frozen)
        self.machine.eligible_levels = original.eligible_levels
        if original.ever_armed and original.pb_minute is not None:
            # rejected after PullbackStart: E1 was frozen and resting; it continues unchanged
            self.machine.resume_resting_e1(
                original.levels, Decimal(1), eval_time, original.pb_minute
            )
        else:
            # rejected before any fill/pullback: base SP2L re-arms at the current LastSpike
            self.machine.start(eval_time, last_trade)

    ambiguous = False  # set when a restart interrupted the simulation (coverage gap)
    simulator_version = SIMULATOR_VERSION

    @classmethod
    def restored(
        cls,
        candidate_id: str,
        stage: str,
        machine: SetupMachine,
        broker: ShadowBroker,
        simulator_version: str = SIMULATOR_VERSION,
    ) -> CounterfactualRun:
        run = cls.__new__(cls)
        run.simulator_version = simulator_version
        run.candidate_id = candidate_id
        run.rejection_stage = stage
        run.broker = broker
        run.machine = machine
        run.ambiguous = False
        return run

    @property
    def done(self) -> bool:
        return self.machine.terminal or self.ambiguous

    def on_trade(self, price: Decimal, ts: datetime) -> None:
        if self.done:
            return
        self.machine.on_trade(price, ts)
        fills, exits = self.broker.on_trade(price, ts)
        for f in fills:
            self.machine.on_fill(f)
        for e in exits:
            self.machine.on_exit(e)

    def on_m1_close(self, m1: M1Result, eval_time: datetime, last: Decimal | None) -> None:
        if not self.done:
            self.machine.on_m1_close(m1, eval_time, last)

    def outcome(self) -> CounterfactualOutcome:
        m = self.machine
        r = m.levels.r
        gross = net = None
        kind = str(m.state)
        if self.ambiguous and not m.terminal:
            kind = "AMBIGUOUS"
        elif m.exits:
            ex = m.exits[-1]
            kind = str(ex.kind) if ex.kind is not ExitKind.EMERGENCY else "ERROR"
            gross = self.broker.realized_pnl / r
            net = (self.broker.realized_pnl - self.broker.fees) / r
        if not m.terminal and not self.ambiguous:
            kind = "OPEN"
        return CounterfactualOutcome(
            self.candidate_id,
            self.rejection_stage,
            LABEL,
            kind,
            gross,
            net,
            m.e2_filled > 0,
        )
