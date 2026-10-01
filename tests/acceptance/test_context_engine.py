"""Context Engine V6.0 acceptance tests (CONTEXT_ENGINE_SPEC V6.0; owner decision 2026-10-01).

PASS iff NetTP > 0 AND (A LevelBreak OR B ChannelEdge OR C HTFAligned). Regime, RangeMiddle,
HTF-opposite, RoomToTP and Liquidity are informational only. Breakout-level selection (B27,
B32, B38 geometry) is unchanged and still tested here.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D
from fractions import Fraction

import pytest

from sp2l.core.types import Side
from sp2l.indicators.regime import Regime
from sp2l.indicators.trend import Trend
from sp2l.strategy.context.engine import ContextInputs, Reason, evaluate_context
from sp2l.strategy.context.levels import (
    FrozenLevel,
    eligible_breakout_levels,
    select_breakout_level,
)
from sp2l.strategy.risk.engine import CostModel
from tests.conftest import candle
from tests.m5_fixtures import bar_time, eval_time, make_state, pivot_high, pivot_low

COSTS = CostModel(D("0.0008"), D("0.00095"), D("0.000198"))
ORIGIN = candle(0, "101", "102", "101", "101.5")  # OriginLow 101: RP 1/30 in 100..130 -> B true
MID = candle(0, "115", "116", "115", "115.5")  # OriginLow 115: RP 1/2 -> B false (Long)


def spike(*closes: str, high: str = "140", origin=ORIGIN):  # type: ignore[no-untyped-def]
    return [origin] + [candle(i + 1, c, high, c, c) for i, c in enumerate(closes)]


def inp(  # type: ignore[no-untyped-def]
    state, e1="110", r="2", side=Side.LONG, spike_candles=None, level=None, origin=ORIGIN,
    costs=COSTS,
):  # fmt: skip
    return ContextInputs(
        side=side,
        eval_time=eval_time(state),
        e1=D(e1),
        r=D(r),
        origin=origin,
        spike_candles=spike_candles or [origin],
        breakout_level=level,
        costs=costs,
    )


def none_state(**kw):  # type: ignore[no-untyped-def]
    """No trend alignment (NEUTRAL); with a mid-range Origin and no level, A/B/C are all false."""
    return make_state(trend=Trend.NEUTRAL, **kw)


# ---- the rule: any one of A / B / C, plus a positive net ------------------------------------


def test_level_break_alone_passes():
    st = none_state()
    level = FrozenLevel(Side.LONG, D("125"), bar_time(3))
    snap = evaluate_context(
        st, inp(st, origin=MID, spike_candles=spike("125.01", origin=MID), level=level)
    )
    assert snap.passed and snap.level_break and not snap.channel_edge and not snap.htf_aligned


def test_channel_edge_alone_passes():
    st = none_state()
    snap = evaluate_context(st, inp(st))  # OriginLow 101 -> RP 1/30
    assert snap.passed and snap.channel_edge and not snap.level_break and not snap.htf_aligned


def test_htf_aligned_alone_passes():
    st = make_state(trend=Trend.BULL)
    snap = evaluate_context(st, inp(st, origin=MID))
    assert snap.passed and snap.htf_aligned and not snap.channel_edge and not snap.level_break


def test_no_condition_is_no_valid_context():
    st = none_state()
    snap = evaluate_context(st, inp(st, origin=MID))
    assert snap.status == "REJECT" and snap.reasons == [Reason.NO_CONTEXT]


# ---- D NetTP ----------------------------------------------------------------------------


def test_net_tp_exactly_zero_rejects_and_tiny_positive_passes():
    st = make_state(low=lambda k: "90")
    zero = CostModel(D("0.01"), D(0), D(0))  # 1 - 100*0.01 = 0
    snap = evaluate_context(st, inp(st, e1="100", r="1", costs=zero))
    assert snap.net_tp_per_unit == 0 and snap.net_tp_positive is False
    assert snap.reasons == [Reason.NET_TP_NOT_POSITIVE] and snap.status == "REJECT"
    tiny = CostModel(D("0.00999"), D(0), D(0))
    ok = evaluate_context(st, inp(st, e1="100", r="1", costs=tiny))
    assert ok.net_tp_per_unit == D("0.001") and ok.passed


def test_net_tp_formula_matches_risk_v511_and_short_side():
    st = make_state(trend=Trend.BEAR)
    snap = evaluate_context(st, inp(st, e1="110", r="2", side=Side.SHORT, origin=MID))
    assert snap.tp == D("108")
    assert snap.net_tp_per_unit == D("2") - D("110") * D("0.0008") - D("108") * D("0.00095")
    assert snap.passed and snap.htf_aligned


def test_costs_missing_fail_closed():
    st = make_state()
    snap = evaluate_context(st, inp(st, costs=None))
    assert snap.reasons == [Reason.NET_TP_UNKNOWN] and snap.status == "UNKNOWN"
    assert snap.net_tp_per_unit is None and snap.net_tp_positive is None


def test_net_tp_is_evaluated_while_unwarm_and_ordered_first():
    cold = make_state(warmup=1000)
    snap = evaluate_context(cold, inp(cold))
    assert snap.net_tp_positive is True and snap.reasons == [Reason.WARMUP]
    bad = evaluate_context(cold, inp(cold, costs=CostModel(D("0.05"), D(0), D(0))))
    assert bad.reasons == [Reason.NET_TP_NOT_POSITIVE, Reason.WARMUP]
    assert bad.status == "REJECT"


# ---- A LevelBreak -----------------------------------------------------------------------


def test_level_break_needs_a_strict_m1_close_not_wick_or_equality():
    st = none_state()
    level = FrozenLevel(Side.LONG, D("125"), bar_time(3))
    wick = evaluate_context(
        st, inp(st, origin=MID, spike_candles=spike("124", high="140", origin=MID), level=level)
    )
    eq = evaluate_context(
        st, inp(st, origin=MID, spike_candles=spike("125", origin=MID), level=level)
    )
    assert not wick.level_break and not eq.level_break
    assert wick.reasons == eq.reasons == [Reason.NO_CONTEXT]


# ---- B ChannelEdge ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("side", "origin_ref", "expected"),
    [
        (Side.LONG, "110", True),  # RP exactly 1/3 (inclusive)
        (Side.LONG, "110.01", False),
        (Side.LONG, "95", True),  # raw RP -1/6: below the range, never clamped
        (Side.SHORT, "120", True),  # RP exactly 2/3 (inclusive)
        (Side.SHORT, "119.99", False),
        (Side.SHORT, "135", True),  # raw RP 7/6
    ],
)
def test_channel_edge_inclusive_thirds_raw_position(side, origin_ref, expected):  # type: ignore[no-untyped-def]
    st = none_state()
    ref = D(origin_ref)
    origin = (
        candle(0, origin_ref, str(ref + 1), origin_ref, origin_ref)
        if side is Side.LONG
        else candle(0, origin_ref, origin_ref, str(ref - 1), origin_ref)
    )
    e1, r = ("125", "2") if side is Side.LONG else ("105", "2")
    snap = evaluate_context(st, inp(st, e1=e1, r=r, side=side, origin=origin))
    assert snap.channel_edge is expected and snap.passed is expected
    rp = (Fraction(ref) - 100) / 30
    assert snap.range_position_origin == rp


@pytest.mark.parametrize("regime", [Regime.TREND, Regime.RANGE, Regime.TRANSITION])
def test_channel_edge_and_htf_aligned_work_in_any_regime(regime):  # type: ignore[no-untyped-def]
    edge = make_state(trend=Trend.NEUTRAL, regime=regime)
    assert evaluate_context(edge, inp(edge)).channel_edge
    trend = make_state(trend=Trend.BULL, regime=regime)
    snap = evaluate_context(trend, inp(trend, origin=MID))
    assert snap.htf_aligned and snap.passed


def test_flat_range_makes_b_false_not_a_global_reject():
    st = make_state(trend=Trend.BULL, high=lambda k: "100", low=lambda k: "100")
    snap = evaluate_context(st, inp(st, e1="110", origin=MID))
    assert snap.channel_edge is False and snap.passed  # C still passes


# ---- C HTFAligned -----------------------------------------------------------------------


def test_opposite_trend_with_breakout_passes_and_alone_is_no_valid_context():
    st = make_state(trend=Trend.BEAR)
    level = FrozenLevel(Side.LONG, D("125"), bar_time(3))
    broke = evaluate_context(
        st, inp(st, origin=MID, spike_candles=spike("125.5", origin=MID), level=level)
    )
    assert broke.passed and broke.htf_opposite_no_breakout is False
    alone = evaluate_context(st, inp(st, origin=MID))
    assert alone.reasons == [Reason.NO_CONTEXT] and alone.htf_opposite_no_breakout is True


def test_dual_pivot_trend_makes_c_false_but_a_and_b_still_pass():
    st = make_state(trend=Trend.INVALID_DUAL_PIVOT)
    edge = evaluate_context(st, inp(st))
    assert edge.htf_aligned is False and edge.channel_edge and edge.passed
    level = FrozenLevel(Side.LONG, D("125"), bar_time(3))
    brk = evaluate_context(
        st, inp(st, origin=MID, spike_candles=spike("126", origin=MID), level=level)
    )
    assert brk.passed and brk.level_break
    none = evaluate_context(st, inp(st, origin=MID))
    assert none.reasons == [Reason.NO_CONTEXT]


# ---- warmup and timing ------------------------------------------------------------------


def test_warm_boundary_at_30_bars():
    from sp2l.indicators.m5_state import DEFAULT_WARMUP

    assert DEFAULT_WARMUP == 30
    cold = make_state(n=29, warmup=DEFAULT_WARMUP)
    snap = evaluate_context(cold, inp(cold))
    assert snap.reasons == [Reason.WARMUP] and snap.status == "UNKNOWN"
    warm = make_state(n=30, warmup=DEFAULT_WARMUP)
    assert evaluate_context(warm, inp(warm)).passed


def test_stale_series_is_warmup_and_forming_m5_is_never_used():
    st = make_state()
    stale = ContextInputs(
        Side.LONG, eval_time(st) + timedelta(minutes=5), D("110"), D("2"), ORIGIN, [ORIGIN], None,
        costs=COSTS,
    )  # fmt: skip
    assert evaluate_context(st, stale).reasons == [Reason.WARMUP]
    mid = ContextInputs(
        Side.LONG, eval_time(st) + timedelta(minutes=3), D("110"), D("2"), ORIGIN, [ORIGIN], None,
        costs=COSTS,
    )  # fmt: skip
    snap = evaluate_context(st, mid)
    assert snap.ctx_open_time == bar_time(39)  # the last finalized bar, not the forming one


# ---- former hard rejects are informational only -----------------------------------------


def test_middle_of_range_no_longer_rejects_but_is_recorded():
    st = make_state(regime=Regime.RANGE, trend=Trend.NEUTRAL)
    snap = evaluate_context(st, inp(st, e1="110"))  # E1 RP exactly 1/3, regime RANGE
    assert snap.range_middle_reject is True and snap.passed  # B (origin edge) holds
    assert Reason.RANGE_MIDDLE not in snap.reasons


def test_room_to_tp_below_1r_no_longer_rejects_but_is_recorded():
    st = make_state(pivots=[pivot_high(5, "111")])
    snap = evaluate_context(st, inp(st, e1="110", r="2"))
    assert snap.room_to_tp_r == Fraction(1, 2) and snap.room_pass is False
    assert snap.passed and Reason.ROOM not in snap.reasons


def test_low_liquidity_no_longer_rejects_but_is_recorded():
    vols = {k: str(k - 19) for k in range(20, 40)}
    st = make_state(n=41, volume=lambda k: vols.get(k, "1000") if k != 40 else "1",
                    trades=lambda k: 1 if k == 40 else 10)  # fmt: skip
    snap = evaluate_context(st, inp(st))
    assert snap.liquidity_status == Reason.LOW_LIQUIDITY and snap.passed
    assert Reason.LOW_LIQUIDITY not in snap.reasons


def test_regime_is_informational():
    for regime in (Regime.RANGE, Regime.TREND, Regime.TRANSITION):
        st = make_state(regime=regime)
        snap = evaluate_context(st, inp(st))
        assert snap.regime is regime and snap.passed


def test_room_measure_geometry_unchanged_b38():
    # E1 110, R 2 -> TP 112. 109 is behind E1, 113 is beyond TP: neither is an obstacle.
    st = make_state(pivots=[pivot_high(5, "109"), pivot_high(8, "113")])
    snap = evaluate_context(st, inp(st, e1="110", r="2"))
    assert snap.nearest_obstacle is None and snap.room_to_tp_infinite
    at_tp = make_state(pivots=[pivot_high(5, "112")])
    s2 = evaluate_context(at_tp, inp(at_tp, e1="110", r="2"))
    assert s2.nearest_obstacle == D("112") and s2.room_to_tp_r == 1 and s2.room_pass
    inv = make_state(pivots=[pivot_high(5, "111", broken=20), pivot_high(8, "111.5")])
    s3 = evaluate_context(inv, inp(inv, e1="110", r="2"))
    assert s3.nearest_obstacle == D("111.5")
    sh = make_state(trend=Trend.BEAR, pivots=[pivot_low(5, "108"), pivot_low(8, "107.5")])
    o = candle(0, "112", "112.5", "111", "111.5")
    s4 = evaluate_context(sh, inp(sh, e1="110", r="2", side=Side.SHORT, origin=o))
    assert s4.nearest_obstacle == D("108") and s4.room_to_tp_r == 1


# ---- B27 breakout-level selection (levels.py, unchanged) ---------------------------------


def _spike_at(minute: int, *closes: str):
    """Spike M1 candles whose Origin opens at T0 + minute (M5 bar k closes at 5k+5)."""
    origin = candle(minute, "101", "102", "101", "101.5")
    return [origin] + [candle(minute + i + 1, c, "140", c, c) for i, c in enumerate(closes)]


def _eligible(st, spike_candles, side=Side.LONG):
    seg = st.segment
    assert seg is not None
    return eligible_breakout_levels(side, spike_candles[0].open_time, seg.pivots, seg.bars)


def test_b27_highest_crossed_eligible_swing_high():
    pivots = [pivot_high(3, "105"), pivot_high(9, "108"), pivot_high(12, "111")]
    st = make_state(pivots=pivots)
    sp = _spike_at(200, "109")
    elig = _eligible(st, sp)
    assert [e.price for e in elig] == [D("105"), D("108"), D("111")]
    lvl = select_breakout_level(Side.LONG, elig, sp)
    assert lvl is not None and lvl.price == D("108")  # highest crossed, not nearest
    assert select_breakout_level(Side.LONG, elig, _spike_at(200, "105")) is None  # equality


def test_b27_eligibility_confirmed_before_origin_and_unbroken_then():
    pivots = [
        pivot_high(3, "105", broken=10),  # broken (bar 10 closes at 55 min) before origin
        pivot_high(6, "107", broken=39),  # broken after origin: still eligible
        pivot_high(37, "109"),  # confirmed at bar 39 (close 200 min) > origin 195: never
    ]
    st = make_state(pivots=pivots)
    sp = _spike_at(195, "120")
    assert [e.price for e in _eligible(st, sp)] == [D("107")]


def test_b27_short_lowest_crossed_swing_low():
    from tests.m5_fixtures import pivot_low

    st = make_state(pivots=[pivot_low(3, "95"), pivot_low(6, "92"), pivot_low(9, "89")])
    sp = [candle(200, "100", "100", "99", "99.5"), candle(201, "91", "91.5", "90", "91")]
    elig = _eligible(st, sp, Side.SHORT)
    lvl = select_breakout_level(Side.SHORT, elig, sp)
    assert lvl is not None and lvl.price == D("92")


def test_dual_pivot_usable_as_breakout_level_b32():
    from sp2l.indicators.m5_state import SegPivot
    from sp2l.indicators.pivots import PivotKind

    dual = SegPivot(4, PivotKind.DUAL, D("104"), D("90"), 6)
    st = make_state(pivots=[dual])
    sp = _spike_at(200, "104.5")
    lvl = select_breakout_level(Side.LONG, _eligible(st, sp), sp)
    assert lvl is not None and lvl.price == D("104")
    snap = evaluate_context(st, inp(st, e1="103", r="2"))
    assert snap.nearest_obstacle == D("104")  # informational obstacle
