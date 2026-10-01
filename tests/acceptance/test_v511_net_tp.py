"""V5.11 acceptance: a setup is never armed unless a win at TP is profitable after costs.

net_tp_per_unit = |TP - E1| - E1*entry_fee_rate - TP*exit_fee_rate, E1 only; <= 0 rejects with
RISK_NET_TP_NOT_POSITIVE (DECISIONS V5.11).
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from sp2l.core.types import Side
from sp2l.engine.model import SetupState as S
from sp2l.engine.setup_machine import GateResult
from sp2l.strategy.levels import compute_levels
from sp2l.strategy.risk.engine import (
    CostModel,
    ExchangeFilters,
    Mode,
    RiskInputs,
    RiskReason,
    size_setup,
    unverifiable_liquidation,
)
from tests.conftest import candle
from tests.race.fakes import FakeGates, at, gate, m1, machine

TICK = D("0.1")
FILTERS = ExchangeFilters(TICK, D("0.00001"), None, None, verified=True)
TABDEAL = CostModel(D("0.0008"), D("0.00095"), D("0.000198"), evidence_id="test")


def levels(e1_low: str, origin_low: str, side: Side = Side.LONG):  # type: ignore[no-untyped-def]
    if side is Side.LONG:
        origin = candle(0, origin_low, str(D(origin_low) + 10), origin_low, origin_low)
        last = candle(3, e1_low, str(D(e1_low) + 50), e1_low, e1_low)
    else:  # e1_low / origin_low are the highs here
        origin = candle(0, origin_low, origin_low, str(D(origin_low) - 10), origin_low)
        last = candle(3, e1_low, e1_low, str(D(e1_low) - 50), e1_low)
    return compute_levels(side, origin, last, TICK)


def risk(lv, costs=TABDEAL, wallet="100"):  # type: ignore[no-untyped-def]
    return size_setup(
        RiskInputs(
            Mode.SHADOW, lv, D(wallet), D(wallet), 10, costs, FILTERS, unverifiable_liquidation
        )
    )


def test_net_at_tp_is_exact_per_unit_e1_only():
    lv = levels("50000", "49900.1")  # SL 49900, R 100, TP 50100
    r = risk(lv, CostModel(D("0.001"), D("0.0005"), D(0), evidence_id="t"))
    assert r.net_tp_per_unit == D(100) - D(50000) * D("0.001") - D(50100) * D("0.0005")
    assert r.net_tp == r.qty * r.net_tp_per_unit
    assert r.passed


@pytest.mark.parametrize(
    ("entry", "passes"),
    [("0.00199", True), ("0.002", False), ("0.00201", False)],  # 50000*0.002 = R exactly
)
def test_zero_net_rejects_and_positive_passes(entry, passes):  # type: ignore[no-untyped-def]
    lv = levels("50000", "49900.1")
    r = risk(lv, CostModel(D(entry), D(0), D(0), evidence_id="t"))
    assert (RiskReason.NET_TP_NOT_POSITIVE not in r.reasons) is passes
    assert r.passed is passes


def test_real_tabdeal_rates_floor_is_about_17_5_bps():
    # 29 Sep 09:11 Tehran: R 145.1 on E1 83095 (17.46 bps) reached TP and closed at -0.0008
    below = risk(levels("83095.0", "82950.0"))
    assert below.net_tp_per_unit < 0 and below.reasons == [RiskReason.NET_TP_NOT_POSITIVE]
    above = risk(levels("83095.0", "82949.1"))  # R 146.0 (17.57 bps)
    assert above.net_tp_per_unit > 0 and above.passed


def test_short_side_uses_the_same_rule():
    # Short: E1 = LastSpike high, SL = OriginHigh + tick, TP = E1 - R
    below = risk(levels("83095.0", "83240.0", Side.SHORT))  # R 145.1
    above = risk(levels("83095.0", "83240.9", Side.SHORT))  # R 146.0
    assert RiskReason.NET_TP_NOT_POSITIVE in below.reasons
    assert above.passed


def test_independent_of_wallet_and_quantity_rounding():
    lv = levels("83095.0", "82950.0")
    for wallet in ("10", "100", "100000"):
        assert risk(lv, wallet=wallet).reasons == [RiskReason.NET_TP_NOT_POSITIVE]


def test_zero_costs_never_trigger():
    r = risk(levels("83095.0", "83094.9"), CostModel(D(0), D(0), D(0), evidence_id="t"))
    assert r.net_tp_per_unit == D("0.2") and RiskReason.NET_TP_NOT_POSITIVE not in r.reasons


# ---- the setup machine: nothing is armed; every revision is re-checked ------------------------


def real_risk(costs: CostModel) -> FakeGates:
    def decide(i: int, lv, with_risk: bool) -> GateResult:  # type: ignore[no-untyped-def]
        g = gate()
        return GateResult(g.context, g.exhaustion, risk(lv, costs, wallet="1000"))

    return FakeGates(decide=decide)


def test_machine_rejects_before_any_order_when_a_win_would_lose():
    # base spike: E1 101, SL 98.9, R 2.1, TP 103.1 -> net 2.1 - 1.0403 - 1.06193 < 0
    gates = real_risk(CostModel(D("0.0103"), D("0.0103"), D(0), evidence_id="t"))
    m, port, _ = machine(gates=gates)
    m.start(at(3), last_trade=D("102.5"))
    assert m.state is S.REJECTED_RISK and m.primary_reason == RiskReason.NET_TP_NOT_POSITIVE
    assert port.submits == []


def test_machine_rechecks_the_rule_on_every_revision():
    # R 2.1 passes at 1 % costs (net 0.059); the extension to E1 102 is re-evaluated with risk.
    # An extension only widens R (E1 moves away from the fixed SL), so a passing setup keeps
    # passing; the check still runs on every revision.
    costs = CostModel(D("0.01"), D("0.01"), D(0), evidence_id="t")
    m, port, g = machine(gates=real_risk(costs))
    m.start(at(3), last_trade=D("102.5"))
    assert m.state is S.E1_PENDING
    m.on_m1_close(m1(3, "102.5", "104", "102", "103.5"), at(4), D("103.5"))
    assert m.state is S.E1_PENDING and port.submits[-1][1] == D("102")
    assert [with_risk for _, with_risk in g.calls] == [True, True]
    nets = [risk(lv, costs).net_tp_per_unit for lv, _ in g.calls]
    assert nets[0] == D("0.059") and nets[1] > nets[0]
