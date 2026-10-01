"""Risk Engine (RISK_ENGINE_SPEC V5 rev 5.1).

Worst case = both E1 and E2 fill with equal Q and both exit at the stop:
    per-unit loss u = sum over legs j in {E1, E2} of
        |E_j - X| + E_j * entry_fee_rate + X * exit_fee_rate
    with X = the worst modeled exit price = SL moved against the trade by the configured
    SL slippage allowance.
E2 is the tick-rounded price (rounded toward SL, B23), so D2 is never assumed to be 0.5R.

Quantity (B17): Q_final = floor_step(min(Q_risk_limit, Q_margin_limit)), then recheck
minQty, minNotional, cost-adjusted risk and liquidation safety.
Margin (V5.2 B28, Shadow/provisional, linear USDT; both equal legs reserved):
    required_margin(Q) = Q*E1/leverage + Q*E2/leverage
    Q_margin_limit     = available_margin * leverage / (E1 + E2)
Live does not rely on this theoretical formula until the Tabdeal margin model is
runtime-validated (`margin_model_verified`).
Costs are never hardcoded (B16): the caller passes a CostModel from runtime configuration.

V5.11 net-at-TP rule: a setup is rejected (RISK_NET_TP_NOT_POSITIVE) unless E1 alone, exited
at the single TP, is profitable after costs:
    net_tp_per_unit = |TP - E1| - E1 * entry_fee_rate - TP * exit_fee_rate  > 0
(maker entry, TP exit at the configured exit rate; E2 is not assumed to fill - if it does,
the result only improves). The sign does not depend on Q, so rounding cannot change it.
Evaluated with every Risk evaluation, i.e. before the first E1 submit and on every revision.
"""

from __future__ import annotations

import decimal
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

from sp2l.core.numeric import DECISION_CONTEXT
from sp2l.core.types import Side
from sp2l.strategy.levels import SetupLevels

# V5.8: the strategy's leverage, frozen by spec (market.leverage). Shadow sizing, margin,
# risk, reports, liquidation modelling, UI and counterfactuals all use exactly this value.
# The account's leverage is only READ, for the Live LEVERAGE_MISMATCH blocker; it can never
# change any strategy computation.
STRATEGY_LEVERAGE = 10

RISK_FRACTION = Decimal("0.01")


class Mode(StrEnum):
    BACKTEST = "BACKTEST"
    REPLAY = "REPLAY"
    SHADOW = "SHADOW"
    LIVE = "LIVE"


@dataclass(frozen=True, slots=True)
class CostModel:
    entry_fee_rate: Decimal
    exit_fee_rate: Decimal
    sl_slippage_rate: Decimal
    evidence_id: str | None = None  # runtime-validation evidence; required for Live

    def __post_init__(self) -> None:
        for v in (self.entry_fee_rate, self.exit_fee_rate, self.sl_slippage_rate):
            if v < 0:
                raise ValueError("cost rates must be non-negative")


@dataclass(frozen=True, slots=True)
class ExchangeFilters:
    tick: Decimal
    step: Decimal
    min_qty: Decimal | None
    min_notional: Decimal | None
    verified: bool  # established by runtime probe (B26); precision-derived = False


# liquidation estimate for the modeled E1+E2 exposure at quantity q; None = unverifiable
LiquidationEstimator = Callable[[Decimal], Decimal | None]


class RiskReason:
    COSTS_NOT_CONFIGURED = "RISK_COSTS_NOT_CONFIGURED"
    COSTS_UNVERIFIED = "RISK_COSTS_UNVERIFIED"
    FILTERS_UNVERIFIED = "RISK_FILTERS_UNVERIFIED"
    QTY_ZERO = "RISK_QTY_ZERO"
    QTY_BELOW_MIN = "RISK_QTY_BELOW_MIN"
    NOTIONAL_BELOW_MIN = "RISK_NOTIONAL_BELOW_MIN"
    OVER_BUDGET = "RISK_OVER_BUDGET"
    NET_TP_NOT_POSITIVE = "RISK_NET_TP_NOT_POSITIVE"  # V5.11
    LIQ_UNVERIFIED = "LIQ_UNVERIFIED"
    LIQ_UNSAFE = "RISK_LIQUIDATION_UNSAFE"
    MARGIN_MODEL_UNVERIFIED = "RISK_MARGIN_MODEL_UNVERIFIED"
    WALLET_INVALID = "RISK_WALLET_INVALID"


class RiskDiag:
    MARGIN_CAPPED_QTY = "MARGIN_CAPPED_QTY"
    LIQ_UNVERIFIED = "LIQ_UNVERIFIED"
    FILTERS_PROVISIONAL = "FILTERS_PROVISIONAL"
    COSTS_PROVISIONAL = "COSTS_PROVISIONAL"


@dataclass(frozen=True, slots=True)
class RiskInputs:
    mode: Mode
    levels: SetupLevels
    wallet_balance: Decimal
    available_margin: Decimal
    leverage: int
    costs: CostModel | None
    filters: ExchangeFilters
    liquidation: LiquidationEstimator
    margin_model_verified: bool = False  # Tabdeal margin behaviour runtime-validated (B28)


@dataclass(slots=True)
class RiskResult:
    passed: bool = False
    reasons: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    budget: Decimal | None = None
    loss_per_unit: Decimal | None = None
    q_risk_limit: Decimal | None = None
    q_margin_limit: Decimal | None = None
    qty: Decimal | None = None
    worst_exit_price: Decimal | None = None
    modeled_price_loss: Decimal | None = None  # Q*(D1'+D2') at the worst exit
    modeled_costs: Decimal | None = None
    modeled_worst_loss: Decimal | None = None
    e2_risk: Decimal | None = None  # Q * |E2 - SL|
    initial_margin: Decimal | None = None
    liquidation_price: Decimal | None = None
    net_tp_per_unit: Decimal | None = None  # V5.11: E1-only net at TP after costs, per unit
    net_tp: Decimal | None = None  # Q * net_tp_per_unit


def floor_step(q: Decimal, step: Decimal) -> Decimal:
    with decimal.localcontext(DECISION_CONTEXT):
        return (q / step).to_integral_value(rounding=ROUND_FLOOR) * step


def size_setup(inp: RiskInputs) -> RiskResult:
    res = RiskResult()
    lv = inp.levels
    long = lv.side is Side.LONG
    if inp.wallet_balance <= 0 or inp.available_margin < 0 or inp.leverage <= 0:
        res.reasons.append(RiskReason.WALLET_INVALID)
        return res
    if inp.costs is None:
        res.reasons.append(RiskReason.COSTS_NOT_CONFIGURED)
        return res
    costs = inp.costs
    if costs.evidence_id is None:
        if inp.mode is Mode.LIVE:
            res.reasons.append(RiskReason.COSTS_UNVERIFIED)
            return res
        res.diagnostics.append(RiskDiag.COSTS_PROVISIONAL)
    if inp.mode is Mode.LIVE and not inp.margin_model_verified:
        res.reasons.append(RiskReason.MARGIN_MODEL_UNVERIFIED)
        return res
    if not inp.filters.verified:
        if inp.mode is Mode.LIVE:
            res.reasons.append(RiskReason.FILTERS_UNVERIFIED)
            return res
        res.diagnostics.append(RiskDiag.FILTERS_PROVISIONAL)

    with decimal.localcontext(DECISION_CONTEXT):
        slip = costs.sl_slippage_rate
        x = lv.sl * (1 - slip) if long else lv.sl * (1 + slip)
        res.worst_exit_price = x
        price_loss_u = abs(lv.e1 - x) + abs(lv.e2 - x)
        cost_u = (lv.e1 + lv.e2) * costs.entry_fee_rate + 2 * x * costs.exit_fee_rate
        u = price_loss_u + cost_u
        res.loss_per_unit = u
        res.budget = inp.wallet_balance * RISK_FRACTION
        res.q_risk_limit = res.budget / u
        margin_u = (lv.e1 + lv.e2) / inp.leverage
        res.q_margin_limit = inp.available_margin * inp.leverage / (lv.e1 + lv.e2)
        q_raw = min(res.q_risk_limit, res.q_margin_limit)
        if res.q_margin_limit < res.q_risk_limit:
            res.diagnostics.append(RiskDiag.MARGIN_CAPPED_QTY)
        q = floor_step(q_raw, inp.filters.step)
        res.qty = q
        res.modeled_price_loss = q * price_loss_u
        res.modeled_costs = q * cost_u
        res.modeled_worst_loss = q * u
        res.e2_risk = q * lv.d2
        res.initial_margin = q * margin_u
        net_u = abs(lv.tp - lv.e1) - lv.e1 * costs.entry_fee_rate - lv.tp * costs.exit_fee_rate
        res.net_tp_per_unit = net_u
        res.net_tp = q * net_u

    if res.net_tp_per_unit <= 0:
        res.reasons.append(RiskReason.NET_TP_NOT_POSITIVE)  # V5.11
    if q <= 0:
        res.reasons.append(RiskReason.QTY_ZERO)
        return res
    f = inp.filters
    if f.min_qty is not None and q < f.min_qty:
        res.reasons.append(RiskReason.QTY_BELOW_MIN)
    if f.min_notional is not None and q * min(lv.e1, lv.e2) < f.min_notional:
        res.reasons.append(RiskReason.NOTIONAL_BELOW_MIN)
    assert res.modeled_worst_loss is not None and res.budget is not None
    if res.modeled_worst_loss > res.budget:
        res.reasons.append(RiskReason.OVER_BUDGET)  # cannot happen after floor; asserted

    liq = inp.liquidation(q)
    res.liquidation_price = liq
    if liq is None:
        if inp.mode is Mode.LIVE:
            res.reasons.append(RiskReason.LIQ_UNVERIFIED)
        else:
            res.diagnostics.append(RiskDiag.LIQ_UNVERIFIED)
    else:
        safe = liq <= lv.sl - lv.r if long else liq >= lv.sl + lv.r
        if not safe:
            res.reasons.append(RiskReason.LIQ_UNSAFE)
    res.passed = not res.reasons
    return res


def unverifiable_liquidation(_q: Decimal) -> Decimal | None:
    """Default estimator: the Tabdeal formula is unknown (UNRESOLVED #3)."""
    return None
