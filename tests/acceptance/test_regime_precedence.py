"""AT: Regime precedence RANGE before TREND; exact boundaries (CTX-08)."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from sp2l.indicators import UNKNOWN
from sp2l.indicators.regime import Regime, classify_regime


@pytest.mark.parametrize(
    ("chop", "adx", "expected"),
    [
        ("61.8", "19.999", Regime.RANGE),  # equality on CHOP counts for RANGE
        ("61.8", "20", Regime.TRANSITION),  # ADX < 20 is strict
        ("61.79", "10", Regime.TRANSITION),
        ("38.2", "10", Regime.TREND),  # CHOP <= 38.2 inclusive
        ("38.21", "24.99", Regime.TRANSITION),
        ("50", "25", Regime.TREND),  # ADX >= 25 inclusive
        ("70", "30", Regime.TREND),  # CHOP high but ADX not < 20 -> not RANGE
    ],
)
def test_regime_boundaries(chop, adx, expected):
    assert classify_regime(D(chop), D(adx)) is expected


def test_unknown_or_warmup_inputs_fail_closed():
    assert classify_regime(UNKNOWN, D("10")) is Regime.UNKNOWN
    assert classify_regime(D("50"), None) is Regime.UNKNOWN
