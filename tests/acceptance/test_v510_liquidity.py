"""V5.10 Liquidity measure with an unknown trade count (DECISIONS V5.10).

V6.0: Liquidity is INFORMATIONAL - it is still measured and recorded exactly as below, but it
never appears in the Context reasons and never affects the result.

The reject is `Volume < 0.5*median AND TradeCount < 0.5*median`. With a trade count unknown
(Tabdeal chart-history repair), a not-low volume already makes the conjunction false (PASS,
decided by volume alone); a low volume leaves it undecided (LIQUIDITY_UNKNOWN). No trade
count is ever invented and LOW_LIQUIDITY is only decided with both values known.
"""

from __future__ import annotations

from decimal import Decimal as D
from fractions import Fraction

import pytest

from sp2l.core.types import Side
from sp2l.strategy.context.engine import (
    LIQ_BASIS_BOTH,
    LIQ_BASIS_VOLUME_ONLY,
    ContextInputs,
    Reason,
    evaluate_context,
)
from sp2l.strategy.risk.engine import CostModel
from tests.conftest import candle
from tests.m5_fixtures import eval_time, make_state

COSTS = CostModel(D("0.0008"), D("0.00095"), D("0.000198"))
ORIGIN = candle(0, "101", "102", "101", "101.5")
REF = {k: str(k - 19) for k in range(20, 40)}  # bars 20..39 -> volumes 1..20, median 10.5


def snap_for(current_volume: str, trades):  # type: ignore[no-untyped-def]
    st = make_state(
        n=41, volume=lambda k: REF.get(k, "1000") if k != 40 else current_volume, trades=trades
    )
    return evaluate_context(
        st,
        ContextInputs(
            side=Side.LONG,
            eval_time=eval_time(st),
            e1=D("110"),
            r=D("2"),
            origin=ORIGIN,
            spike_candles=[ORIGIN],
            breakout_level=None,
            costs=COSTS,
        ),
    )


def reference_unknown(k: int) -> int | None:
    return None if k == 25 else (1 if k == 40 else 10)  # one history-repaired reference bar


def context_bar_unknown(k: int) -> int | None:
    return None if k == 40 else 10  # the context bar itself was repaired


@pytest.mark.parametrize("trades", [reference_unknown, context_bar_unknown])
def test_unknown_trade_count_passes_on_volume_when_volume_not_low(trades):  # type: ignore[no-untyped-def]
    snap = snap_for("8", trades)
    assert snap.liquidity_status == "PASS"
    assert snap.liquidity_basis == LIQ_BASIS_VOLUME_ONLY
    assert snap.tradecount_ratio is None  # never invented
    assert snap.volume_ratio == Fraction(8) / Fraction("10.5")
    assert not snap.liquidity_context_ready
    assert Reason.LIQUIDITY_UNKNOWN not in snap.reasons and snap.passed


@pytest.mark.parametrize("trades", [reference_unknown, context_bar_unknown])
def test_unknown_trade_count_volume_equality_passes(trades):  # type: ignore[no-untyped-def]
    assert snap_for("5.25", trades).liquidity_status == "PASS"  # 5.25 == 0.5 * 10.5


@pytest.mark.parametrize("trades", [reference_unknown, context_bar_unknown])
def test_unknown_trade_count_with_low_volume_stays_unknown(trades):  # type: ignore[no-untyped-def]
    snap = snap_for("5.24", trades)
    assert snap.liquidity_status == Reason.LIQUIDITY_UNKNOWN  # never LOW_LIQUIDITY
    assert snap.liquidity_basis == LIQ_BASIS_VOLUME_ONLY
    assert Reason.LIQUIDITY_UNKNOWN not in snap.reasons and snap.passed  # V6.0: info only


def test_known_trade_counts_keep_the_v58_result():
    def low_count(k: int) -> int:
        return 1 if k == 40 else 10

    both_low = snap_for("5.24", low_count)
    assert both_low.liquidity_status == Reason.LOW_LIQUIDITY
    assert both_low.liquidity_basis == LIQ_BASIS_BOTH and both_low.liquidity_context_ready
    assert snap_for("5.24", lambda k: 10).liquidity_status == "PASS"  # AND, not OR
    assert snap_for("5.25", low_count).liquidity_status == "PASS"


def test_fewer_than_20_reference_bars_stays_unknown():
    st = make_state(n=20, warmup=10)
    snap = evaluate_context(
        st,
        ContextInputs(
            side=Side.LONG,
            eval_time=eval_time(st),
            e1=D("110"),
            r=D("2"),
            origin=ORIGIN,
            spike_candles=[ORIGIN],
            breakout_level=None,
            costs=COSTS,
        ),
    )
    assert snap.liquidity_status == Reason.LIQUIDITY_UNKNOWN
    assert snap.liquidity_basis is None


def test_v6_liquidity_never_rejects_but_is_recorded():
    def low_count(k: int) -> int:
        return 1 if k == 40 else 10

    for vol, trades in (
        ("5.24", low_count),
        ("5.24", reference_unknown),
        ("8", context_bar_unknown),
    ):
        snap = snap_for(vol, trades)
        assert snap.liquidity_status is not None  # still measured
        assert not {Reason.LOW_LIQUIDITY, Reason.LIQUIDITY_UNKNOWN} & set(snap.reasons)
        assert snap.passed
