"""Gate evaluators used by the SetupMachine.

M5Gates evaluates Context, Exhaustion and (optionally) Risk from the live M5 state. All
metrics are computed even when an earlier stage rejects, so every candidate record is
complete. The terminal state follows stage order (Context -> Exhaustion -> Risk).

BaseSp2lGates is the canonical base SP2L with no Context/Exhaustion gates, used only for
counterfactual simulation (B20). Its unit quantity makes results naturally expressed in R.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sp2l.core.types import Side
from sp2l.engine.setup_machine import GateResult
from sp2l.indicators.m5_state import M5State
from sp2l.strategy.context.engine import ContextInputs, evaluate_context
from sp2l.strategy.context.levels import EligibleLevel, FrozenLevel, eligible_breakout_levels
from sp2l.strategy.exhaustion.engine import ExhaustionInputs, evaluate_exhaustion
from sp2l.strategy.levels import SetupLevels
from sp2l.strategy.risk.engine import (
    CostModel,
    ExchangeFilters,
    LiquidationEstimator,
    Mode,
    RiskInputs,
    RiskResult,
    size_setup,
)
from sp2l.strategy.spike import Spike


@dataclass(slots=True)
class M5Gates:
    m5: M5State
    mode: Mode
    leverage: int
    costs: CostModel | None
    filters: ExchangeFilters
    wallet_balance: Callable[[], Decimal]
    available_margin: Callable[[], Decimal]
    liquidation: LiquidationEstimator
    margin_model_verified: bool = False
    e2_semantics_validated: bool = False
    consumed_obstacles: frozenset[Decimal] = frozenset()  # analysis probes only (never runtime)

    def evaluate(
        self,
        *,
        side: Side,
        eval_time: datetime,
        levels: SetupLevels,
        spike: Spike,
        frozen: FrozenLevel | None,
        with_risk: bool,
    ) -> GateResult:
        ctx = evaluate_context(
            self.m5,
            ContextInputs(
                side,
                eval_time,
                levels.e1,
                levels.r,
                spike.origin,
                spike.candles,
                frozen,
                self.consumed_obstacles,
            ),
        )
        exh = evaluate_exhaustion(self.m5, ExhaustionInputs(side, eval_time, spike.candles, frozen))
        risk = None
        if with_risk:
            risk = size_setup(
                RiskInputs(
                    self.mode,
                    levels,
                    self.wallet_balance(),
                    self.available_margin(),
                    self.leverage,
                    self.costs,
                    self.filters,
                    self.liquidation,
                    self.margin_model_verified,
                )
            )
        return GateResult(ctx, exh, risk)

    def e2_allowed(self) -> bool:
        return self.mode is not Mode.LIVE or self.e2_semantics_validated

    def eligible_breakout_levels(self, side: Side, spike: Spike) -> tuple[EligibleLevel, ...]:
        seg = self.m5.segment
        if seg is None:
            return ()
        return eligible_breakout_levels(side, spike.origin.open_time, seg.pivots, seg.bars)


class BaseSp2lGates:
    """Counterfactual only: no Context/Exhaustion gates, unit quantity (results in R)."""

    def evaluate(
        self,
        *,
        side: Side,
        eval_time: datetime,
        levels: SetupLevels,
        spike: Spike,
        frozen: FrozenLevel | None,
        with_risk: bool,
    ) -> GateResult:
        risk = RiskResult(passed=True, qty=Decimal(1)) if with_risk else None
        return GateResult(None, None, risk)

    def e2_allowed(self) -> bool:
        return True

    def eligible_breakout_levels(self, side: Side, spike: Spike) -> tuple[EligibleLevel, ...]:
        return ()
