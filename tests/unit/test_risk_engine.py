"""Risk engine (RSK-01..04; V5.1 B16, B17, B18, B23, B26, PROVISIONAL B28)."""

from __future__ import annotations

from decimal import Decimal as D

from hypothesis import given
from hypothesis import strategies as st

from sp2l.core.numeric import div
from sp2l.core.types import Side
from sp2l.strategy.levels import compute_levels
from sp2l.strategy.risk.engine import (
    CostModel,
    ExchangeFilters,
    Mode,
    RiskDiag,
    RiskInputs,
    RiskReason,
    size_setup,
    unverifiable_liquidation,
)
from sp2l.strategy.submit_guard import e1_submit_safe
from tests.conftest import candle

TICK = D("0.1")
ZERO_COSTS = CostModel(D(0), D(0), D(0), evidence_id="test")
FILTERS = ExchangeFilters(TICK, D("0.001"), D("0.001"), D("5"), verified=True)


def levels(e1_low="60000", origin_low="59900.1"):
    origin = candle(0, origin_low, str(D(origin_low) + 10), origin_low, origin_low)
    last = candle(3, e1_low, str(D(e1_low) + 50), e1_low, e1_low)
    return compute_levels(Side.LONG, origin, last, TICK)  # SL = origin_low - 0.1


def run(
    lv,
    *,
    wallet="100",
    margin="100",
    mode=Mode.SHADOW,
    costs=ZERO_COSTS,
    filters=FILTERS,
    liq=unverifiable_liquidation,
    margin_verified=None,
):
    # Live tests assume a validated margin model unless they test that gate explicitly (B28)
    verified = mode is Mode.LIVE if margin_verified is None else margin_verified
    return size_setup(RiskInputs(mode, lv, D(wallet), D(margin), 10, costs, filters, liq, verified))


def test_zero_cost_sizing_uses_rounded_e2():
    lv = levels()  # E1 60000, SL 59900, D1 100, E2 59950 (on tick), D2 50
    r = run(lv, margin="100000")
    assert r.budget == D("1") and r.loss_per_unit == D("150")
    assert r.qty == D("0.006")  # floor(1/150 = 0.00666) to 0.001
    assert r.modeled_worst_loss == D("0.900") and r.passed
    assert RiskDiag.LIQ_UNVERIFIED in r.diagnostics  # shadow continues (B18)


def test_e2_off_tick_rounds_toward_sl_and_risk_uses_it():
    lv = levels(origin_low="59900.2")  # SL 59900.1 -> D1 99.9, mid 59950.05 -> E2 59950.0
    assert lv.e2 == D("59950.0") and lv.d2 == D("49.9")
    r = run(lv, margin="100000")
    assert r.loss_per_unit == D("149.8")


def test_margin_cap_b17_b28_exposes_diagnostic_and_keeps_qty_positive():
    lv = levels()
    r = run(lv, margin="20")  # B28: 20 * 10 / (60000 + 59950) = 0.001667
    assert RiskDiag.MARGIN_CAPPED_QTY in r.diagnostics
    assert r.qty == D("0.001") and r.passed
    assert r.q_margin_limit == div(D(200), D(119950))
    assert r.initial_margin == D("0.001") * D(119950) / 10  # both legs reserved


def test_live_rejects_unvalidated_margin_model_b28():
    lv = levels()
    r = run(lv, margin="100000", mode=Mode.LIVE, margin_verified=False)
    assert r.reasons == [RiskReason.MARGIN_MODEL_UNVERIFIED]


def test_below_min_qty_rejects():
    lv = levels()
    r = run(lv, margin="5")
    assert r.qty == D("0") and RiskReason.QTY_ZERO in r.reasons
    r2 = run(lv, margin="100000", filters=ExchangeFilters(TICK, D("0.001"), D("0.01"), None, True))
    assert RiskReason.QTY_BELOW_MIN in r2.reasons


def test_costs_reduce_qty_and_are_never_defaulted():
    lv = levels()
    costs = CostModel(D("0.0004"), D("0.0006"), D("0.0005"), evidence_id="tier-x")
    r = run(lv, margin="100000", costs=costs)
    assert r.loss_per_unit is not None and r.loss_per_unit > D("150")
    assert r.modeled_worst_loss is not None and r.modeled_worst_loss <= r.budget
    assert RiskReason.COSTS_NOT_CONFIGURED in run(lv, costs=None).reasons


def test_live_requires_verified_costs_filters_and_liquidation():
    lv = levels()
    unverified = CostModel(D(0), D(0), D(0), evidence_id=None)
    assert RiskReason.COSTS_UNVERIFIED in run(lv, mode=Mode.LIVE, costs=unverified).reasons
    provisional = ExchangeFilters(TICK, D("0.001"), None, None, verified=False)
    assert RiskReason.FILTERS_UNVERIFIED in run(lv, mode=Mode.LIVE, filters=provisional).reasons
    shadow = run(lv, margin="100000", filters=provisional)
    assert shadow.passed and RiskDiag.FILTERS_PROVISIONAL in shadow.diagnostics
    assert RiskReason.LIQ_UNVERIFIED in run(lv, margin="100000", mode=Mode.LIVE).reasons


def test_liquidation_must_be_1r_beyond_sl():
    lv = levels()  # SL 59900, R 100 -> liq must be <= 59800
    ok = run(lv, margin="100000", liq=lambda q: D("59800"))
    bad = run(lv, margin="100000", liq=lambda q: D("59800.1"))
    assert ok.passed and RiskReason.LIQ_UNSAFE in bad.reasons


@given(st.integers(1, 5000), st.integers(1, 3000), st.integers(10, 100000))
def test_property_worst_loss_never_exceeds_one_percent(d1_ticks, e1_offset, wallet):
    e1 = D(50000) + D(e1_offset)
    origin_low = e1 - D(d1_ticks) * TICK + TICK
    lv = levels(str(e1), str(origin_low))
    costs = CostModel(D("0.0004"), D("0.0006"), D("0.001"), evidence_id="x")
    r = run(
        lv,
        wallet=str(wallet),
        margin=str(wallet),
        costs=costs,
        filters=ExchangeFilters(TICK, D("0.000001"), None, None, True),
    )
    if r.qty and r.qty > 0:
        assert r.modeled_worst_loss is not None and r.budget is not None
        assert r.modeled_worst_loss <= r.budget


def test_submit_guard_b15_strict():
    assert e1_submit_safe(Side.LONG, D("100"), D("100.1"))
    assert not e1_submit_safe(Side.LONG, D("100"), D("100"))
    assert e1_submit_safe(Side.SHORT, D("100"), D("99.9"))
    assert not e1_submit_safe(Side.SHORT, D("100"), D("100"))
    assert not e1_submit_safe(Side.LONG, D("100"), None)
