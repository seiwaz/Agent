"""Context Engine acceptance tests (CTX-01..18; V5.1 B07, B08, B09, B10, B11)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D
from fractions import Fraction

from sp2l.core.types import Side
from sp2l.indicators.regime import Regime
from sp2l.indicators.trend import Trend
from sp2l.strategy.context.engine import ContextInputs, Reason, evaluate_context
from sp2l.strategy.context.levels import (
    FrozenLevel,
    eligible_breakout_levels,
    select_breakout_level,
)
from tests.conftest import candle
from tests.m5_fixtures import bar_time, eval_time, make_state, pivot_high

ORIGIN = candle(0, "101", "102", "101", "101.5")  # OriginLow 101


def spike(*closes: str, high: str = "140"):
    return [ORIGIN] + [candle(i + 1, c, high, c, c) for i, c in enumerate(closes)]


def inp(state, e1="110", r="2", side=Side.LONG, spike_candles=None, level=None, origin=ORIGIN):
    return ContextInputs(
        side=side,
        eval_time=eval_time(state),
        e1=D(e1),
        r=D(r),
        origin=origin,
        spike_candles=spike_candles or [origin],
        breakout_level=level,
    )


def test_range_middle_inclusive_exact_thirds_reject_only_in_range():
    st = make_state(regime=Regime.RANGE)  # range 100..130
    one_third = evaluate_context(st, inp(st, e1="110"))
    two_thirds = evaluate_context(st, inp(st, e1="120"))
    just_below = evaluate_context(st, inp(st, e1="109.99"))
    assert one_third.range_position_e1 == Fraction(1, 3) and one_third.range_middle_reject
    assert two_thirds.range_middle_reject and Reason.RANGE_MIDDLE in two_thirds.reasons
    assert just_below.range_middle_reject is False
    trend_st = make_state(regime=Regime.TREND)
    assert evaluate_context(trend_st, inp(trend_st, e1="115")).range_middle_reject is False


def test_range_position_not_clamped_for_logic():
    st = make_state(regime=Regime.RANGE)
    snap = evaluate_context(st, inp(st, e1="145"))
    assert snap.range_position_e1 == Fraction(3, 2)
    assert snap.range_middle_reject is False


def test_range_edge_origin_uses_raw_origin_position():
    st = make_state(regime=Regime.RANGE, trend=Trend.NEUTRAL)
    snap = evaluate_context(st, inp(st, e1="99"))  # origin low 101 -> RP 1/30
    assert snap.range_edge_origin is True and Reason.NO_CONTEXT not in snap.reasons


def test_breakout_requires_strict_m1_close_not_wick():
    st = make_state(trend=Trend.BEAR)
    level = FrozenLevel(Side.LONG, D("125"), bar_time(3))
    wick = evaluate_context(st, inp(st, spike_candles=spike("125"), level=level))
    assert wick.breakout_context is False
    assert Reason.HTF_OPPOSITE in wick.reasons
    closed = evaluate_context(st, inp(st, spike_candles=spike("125.01"), level=level))
    assert closed.breakout_context is True and Reason.HTF_OPPOSITE not in closed.reasons


def test_no_valid_context_rejects():
    st = make_state(trend=Trend.NEUTRAL)
    snap = evaluate_context(st, inp(st))
    assert snap.reasons == [Reason.NO_CONTEXT]


def test_htf_alignment_passes_and_is_off_in_range():
    st = make_state(trend=Trend.BULL, regime=Regime.TREND)
    assert evaluate_context(st, inp(st)).passed
    st_range = make_state(trend=Trend.BULL, regime=Regime.RANGE)
    assert evaluate_context(st_range, inp(st_range, e1="140")).htf_alignment is False


def test_room_to_tp_exact_1r_passes_and_infinite_without_obstacle():
    st = make_state(pivots=[pivot_high(5, "112")])
    exact = evaluate_context(st, inp(st, e1="110", r="2"))
    assert exact.room_to_tp_r == 1 and exact.room_pass and exact.passed
    short = evaluate_context(st, inp(st, e1="110.01", r="2"))
    assert short.room_pass is False and Reason.ROOM in short.reasons
    none = evaluate_context(make_state(), inp(make_state()))
    assert none.room_to_tp_infinite and none.room_pass


def test_obstacle_invalidated_by_m5_close_is_ignored():
    st = make_state(pivots=[pivot_high(5, "111", broken=20), pivot_high(8, "111.5")])
    snap = evaluate_context(st, inp(st, e1="110", r="2"))
    assert snap.nearest_obstacle == D("111.5") and not snap.room_pass


def test_b38_only_levels_in_the_e1_to_tp_path_are_obstacles():
    # E1 110, R 2 -> TP 112. 109 is behind E1, 113 is beyond TP: neither is an obstacle.
    st = make_state(pivots=[pivot_high(5, "109"), pivot_high(8, "113")])
    snap = evaluate_context(st, inp(st, e1="110", r="2"))
    assert snap.nearest_obstacle is None and snap.room_to_tp_infinite and snap.room_pass
    at_tp = make_state(pivots=[pivot_high(5, "112")])
    s2 = evaluate_context(at_tp, inp(at_tp, e1="110", r="2"))
    assert s2.nearest_obstacle == D("112") and s2.room_to_tp_r == 1 and s2.room_pass


def test_b38_crossed_breakout_level_back_in_path_counts_again():
    st = make_state(pivots=[pivot_high(5, "111")])
    level = FrozenLevel(Side.LONG, D("111"), bar_time(5))
    crossed = spike("111.01")  # the Spike closed above 111 (BreakoutContext true)
    back = evaluate_context(st, inp(st, e1="110", spike_candles=crossed, level=level))
    assert back.breakout_context and back.nearest_obstacle == D("111") and not back.room_pass
    beyond = evaluate_context(st, inp(st, e1="111.5", spike_candles=crossed, level=level))
    assert beyond.nearest_obstacle is None and beyond.room_pass


def test_b38_short_path():
    from tests.m5_fixtures import pivot_low

    st = make_state(trend=Trend.BEAR, pivots=[pivot_low(5, "108"), pivot_low(8, "107.5")])
    origin = candle(0, "112", "112.5", "111", "111.5")
    snap = evaluate_context(
        st, inp(st, e1="110", r="2", side=Side.SHORT, origin=origin, spike_candles=[origin])
    )
    assert snap.nearest_obstacle == D("108") and snap.room_to_tp_r == 1 and snap.room_pass


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


def test_dual_pivot_usable_as_breakout_level_and_obstacle_b32():
    from sp2l.indicators.m5_state import SegPivot
    from sp2l.indicators.pivots import PivotKind

    dual = SegPivot(4, PivotKind.DUAL, D("104"), D("90"), 6)
    st = make_state(pivots=[dual])
    sp = _spike_at(200, "104.5")
    lvl = select_breakout_level(Side.LONG, _eligible(st, sp), sp)
    assert lvl is not None and lvl.price == D("104")
    snap = evaluate_context(st, inp(st, e1="103", r="2"))
    assert snap.nearest_obstacle == D("104") and not snap.room_pass


def test_liquidity_median_excludes_current_and_equality_passes():
    vols = {k: str(k - 19) for k in range(20, 40)}  # bars 20..39 -> 1..20

    def vol(k):
        return vols.get(k, "1000") if k != 40 else "5.25"

    def trades(k):
        return 1 if k == 40 else 10

    st = make_state(n=41, volume=vol, trades=trades)
    snap = evaluate_context(st, inp(st))
    assert snap.volume_ratio == Fraction("5.25") / Fraction("10.5")
    assert snap.liquidity_status == "PASS"  # 5.25 == 0.5 * 10.5
    st2 = make_state(
        n=41, volume=lambda k: vols.get(k, "1000") if k != 40 else "5.24", trades=trades
    )
    assert evaluate_context(st2, inp(st2)).liquidity_status == Reason.LOW_LIQUIDITY
    st3 = make_state(n=41, volume=lambda k: vols.get(k, "1000") if k != 40 else "5.24")
    assert evaluate_context(st3, inp(st3)).liquidity_status == "PASS"  # AND, not OR


def test_warmup_stale_and_dual_pivot_fail_closed():
    cold = make_state(warmup=150)
    assert evaluate_context(cold, inp(cold)).reasons == [Reason.WARMUP]
    st = make_state()
    stale = ContextInputs(
        Side.LONG, eval_time(st) + timedelta(minutes=5), D("110"), D("2"), ORIGIN, [ORIGIN], None
    )
    assert evaluate_context(st, stale).reasons == [Reason.WARMUP]
    dual = make_state(trend=Trend.INVALID_DUAL_PIVOT)
    assert evaluate_context(dual, inp(dual)).reasons == [Reason.INVALID_DATA]


def test_forming_m5_is_never_used():
    st = make_state()
    mid = ContextInputs(
        Side.LONG, eval_time(st) + timedelta(minutes=3), D("110"), D("2"), ORIGIN, [ORIGIN], None
    )
    snap = evaluate_context(st, mid)
    assert snap.ctx_open_time == bar_time(39)  # the last finalized bar, not the forming one
