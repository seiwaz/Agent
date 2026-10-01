"""Exhaustion Gate acceptance tests (EXH-03..12; V5.1 B10, B12, B13)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D
from fractions import Fraction

from sp2l.core.types import Side
from sp2l.indicators.regime import Regime
from sp2l.indicators.trend import Trend
from sp2l.strategy.context.levels import FrozenLevel
from sp2l.strategy.exhaustion.engine import (
    REJECT,
    UNKNOWN,
    ExhaustionInputs,
    Sub,
    evaluate_exhaustion,
)
from tests.conftest import T0, candle
from tests.m5_fixtures import bar_time, eval_time, make_state, pivot_high

ALT_LOW = lambda k: "100" if k % 2 == 0 else "99"  # noqa: E731 - microchannel stays 1


def spike_candles(bo_minute: int | None = None, bo_close: str = "110.5"):
    """Origin at minute 190 (low 105); max High 120 -> extreme 120, range 15 (1.5 ATR)."""
    out = [candle(190, "105.5", "106", "105", "105.5")]
    m = bo_minute if bo_minute is not None else 199
    out.append(candle(m, "110", "120", "109.5", bo_close if bo_minute is not None else "110"))
    return out


def ex(state, candles, level=None, at=None):
    return evaluate_exhaustion(
        state, ExhaustionInputs(Side.LONG, at or eval_time(state), candles, level)
    )


def late_state(age: int, **kw):
    return make_state(
        low=ALT_LOW, trend=lambda k: Trend.BULL if k >= 40 - age else Trend.NEUTRAL, **kw
    )


def test_threshold_equalities_trigger_and_expression_rejects():
    snap = ex(late_state(20), spike_candles())
    assert snap.trend_age_bars == 20 and snap.microchannel_len == 1
    assert snap.stretch_atr == 2 and snap.spike_atr == Fraction(3, 2)
    assert (snap.late_trend, snap.extreme_stretch, snap.climactic_spike) == (True, True, True)
    assert snap.status == "REJECT" and snap.reason == REJECT
    assert snap.sub_reasons == [Sub.LATE_TREND_AGE, Sub.EXTREME_STRETCH, Sub.CLIMACTIC_SPIKE]


def test_trend_age_19_passes():
    snap = ex(late_state(19), spike_candles())
    assert snap.trend_age_bars == 19 and snap.late_trend is False and snap.passed


def test_microchannel_equality_counts():
    snap = ex(make_state(low=lambda k: "100", trend=Trend.NEUTRAL), spike_candles())
    assert snap.microchannel_len == 40 and snap.late_trend
    assert Sub.LONG_MICROCHANNEL in snap.sub_reasons


def test_outer_edge_without_climactic_still_rejects():
    # extreme 120 of 20-bar range 99..121 -> RP20 = 21/22 >= 0.9; spike range 1 ATR (not climactic)
    st = late_state(20, high=lambda k: "121")
    candles = [candle(190, "110", "111", "110", "110.5"), candle(199, "115", "120", "115", "118")]
    snap = ex(st, candles)
    assert snap.climactic_spike is False and snap.range_position_20 == Fraction(21, 22)
    assert snap.at_outer_edge and snap.status == "REJECT"


def test_rp20_zero_range_fails_closed():
    snap = ex(make_state(high=lambda k: "100", low=lambda k: "100"), spike_candles())
    assert snap.status == "UNKNOWN" and snap.reason == UNKNOWN


def test_opposing_swing_infinite_and_boundary():
    assert ex(late_state(20), spike_candles()).opposing_swing_infinite
    st = late_state(20, pivots=[pivot_high(10, "125")])
    snap = ex(st, spike_candles())
    assert (
        snap.opposing_swing_distance_atr == Fraction(1, 2)
        and Sub.OPPOSING_SWING_NEAR in snap.sub_reasons
    )


def _fresh(bo_minute: int, prior_regime: Regime, at=None):
    level = FrozenLevel(Side.LONG, D("110"), bar_time(20))
    st = late_state(20, regime=lambda k: prior_regime if k < 39 or at else Regime.TREND)
    return ex(st, spike_candles(bo_minute), level, at)


def test_fresh_breakout_age_0_and_1_with_prior_transition_is_exempt():
    age0 = _fresh(197, Regime.TRANSITION)
    assert age0.breakout_age_m5 == 0 and age0.fresh_breakout_exception and age0.passed
    age1 = _fresh(192, Regime.RANGE)
    assert age1.breakout_age_m5 == 1 and age1.fresh_breakout_exception


def test_fresh_breakout_age_2_is_not_exempt():
    snap = _fresh(187, Regime.TRANSITION)
    assert snap.breakout_age_m5 == 2 and not snap.fresh_breakout_exception
    assert snap.status == "REJECT"


def test_fresh_breakout_uses_regime_strictly_before_breakout_close():
    # breakout M1 closes 12:... exactly on bar 38's close boundary? use minute 194 -> close 195,
    # which equals bar 38 close; the regime used must be bar 37 (close 190 < 195).
    level = FrozenLevel(Side.LONG, D("110"), bar_time(20))

    def regime(k):
        return Regime.TREND if k == 37 else Regime.TRANSITION

    st = late_state(20, regime=regime)
    snap = ex(st, spike_candles(194), level)
    assert snap.previous_regime_at_breakout is Regime.TREND
    assert not snap.fresh_breakout_exception


def test_forming_bucket_breakout_has_age_0():
    at = T0 + timedelta(minutes=202)
    level = FrozenLevel(Side.LONG, D("110"), bar_time(20))
    st = late_state(20, regime=Regime.TRANSITION)
    snap = ex(st, spike_candles(201), level, at)
    assert snap.breakout_age_m5 == 0 and snap.fresh_breakout_exception


def test_b38_opposing_candidates_strictly_beyond_spike_extreme():
    # extreme 120: a level at 119.2 (crossed, behind) is not a candidate; 121 is
    st = late_state(
        20, pivots=[pivot_high(10, "119.2"), pivot_high(12, "121")], regime=Regime.TREND
    )
    snap = ex(st, spike_candles(197, bo_close="119.5"))
    assert snap.opposing_swing_level == D("121")
    st2 = late_state(20, pivots=[pivot_high(10, "120")], regime=Regime.TREND)
    assert ex(st2, spike_candles()).opposing_swing_infinite  # equal to extreme: not strictly above


# ---- V6.0: Exhaustion is advisory at the gates level --------------------------------------


def _gates(state):  # type: ignore[no-untyped-def]
    from sp2l.engine.gates import M5Gates
    from sp2l.strategy.risk.engine import (
        CostModel,
        ExchangeFilters,
        Mode,
        unverifiable_liquidation,
    )

    return M5Gates(
        state,
        Mode.SHADOW,
        10,
        CostModel(D("0.0008"), D("0.00095"), D("0.000198")),
        ExchangeFilters(D("0.1"), D("0.001"), None, None, False),
        lambda: D(100),
        lambda: D(100),
        unverifiable_liquidation,
    )


def _evaluate(state, candles):  # type: ignore[no-untyped-def]
    from sp2l.strategy.levels import compute_levels
    from sp2l.strategy.spike import Spike

    sp = Spike(Side.LONG, list(candles))
    lv = compute_levels(Side.LONG, sp.origin, sp.last, D("0.1"))
    return _gates(state).evaluate(
        side=Side.LONG,
        eval_time=eval_time(state),
        levels=lv,
        spike=sp,
        frozen=None,
        with_risk=False,
    )


def test_gates_never_reject_on_exhaustion_and_record_advisory_would_reject():
    st = late_state(20)
    raw = ex(st, spike_candles())
    assert raw.status == "REJECT"  # evaluate_exhaustion itself is unchanged
    res = _evaluate(st, spike_candles())
    assert res.exhaustion is not None and res.exhaustion.passed
    assert res.exhaustion.reason is None
    assert res.exhaustion.sub_reasons[:3] == [
        Sub.LATE_TREND_AGE,
        Sub.EXTREME_STRETCH,
        Sub.CLIMACTIC_SPIKE,
    ]
    assert (
        "ADVISORY_WOULD_REJECT" in res.exhaustion.sub_reasons
        and REJECT in res.exhaustion.sub_reasons
    )
    assert res.failed_state() is None or res.failed_state().value != "REJECTED_EXHAUSTION"
    assert REJECT not in res.reasons()


def test_gates_turn_unknown_exhaustion_into_advisory_unknown():
    st = make_state(high=lambda k: "100", low=lambda k: "100")
    res = _evaluate(st, spike_candles())
    assert res.exhaustion is not None and res.exhaustion.passed
    assert (
        "ADVISORY_UNKNOWN" in res.exhaustion.sub_reasons and UNKNOWN in res.exhaustion.sub_reasons
    )
