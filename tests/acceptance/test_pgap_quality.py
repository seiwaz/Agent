"""V5.9 Core P-Gap qualification: strong impulse C2 + strong gap (exact boundaries).

C1 = i-1, C2 = i (impulse), C3 = i+1. tick = 0.1. Thresholds frozen: body/range >= 0.60,
gap >= 0.15 * body(C2), gap >= 2 ticks. Equality passes.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D
from fractions import Fraction

from sp2l.core.types import Candle, Side
from sp2l.marketdata.m1_builder import M1Result, M1Status
from sp2l.strategy.pgap import (
    BODY_TOO_WEAK,
    GAP_SMALL_TICKS,
    GAP_SMALL_VS_BODY,
    WRONG_DIRECTION,
    assess_pgap,
    detect_pgap,
)
from sp2l.strategy.spike import RunTracker
from tests.conftest import T0

TICK = D("0.1")


def c(i: int, o: str, h: str, lo: str, cl: str) -> Candle:
    return Candle(T0 + timedelta(minutes=i), D(o), D(h), D(lo), D(cl), D(1), 5)


def m(x: Candle) -> M1Result:
    return M1Result(x.open_time, M1Status.OK, x)


def long_triple(c2: tuple[str, str, str, str], c3_low: str, c1_high: str = "99.6"):
    c1 = c(0, "99.5", c1_high, "99.4", "99.5")
    c2c = c(1, *c2)
    c3 = c(2, c3_low, str(D(c3_low) + 1), c3_low, str(D(c3_low) + D("0.5")))
    assert detect_pgap(c1, c2c, c3) is Side.LONG
    return c1, c2c, c3


# ---- impulse ------------------------------------------------------------------------------


def test_body_ratio_exactly_060_passes():
    c1, c2, c3 = long_triple(("100", "100.8", "99.8", "100.6"), "100")  # body .6 / range 1
    q = assess_pgap(Side.LONG, c1, c2, c3, TICK)
    assert q.impulse_body_ratio == Fraction(3, 5) and q.strong_impulse_pass
    assert q.final_pgap_pass and q.reasons == ()
    assert (q.impulse_upper_shadow, q.impulse_lower_shadow) == (D("0.2"), D("0.2"))


def test_body_ratio_just_below_060_fails():
    c1, c2, c3 = long_triple(("100", "100.8", "99.8", "100.59"), "100")  # .59 / 1
    q = assess_pgap(Side.LONG, c1, c2, c3, TICK)
    assert not q.strong_impulse_pass and q.reasons[0] == BODY_TOO_WEAK
    assert not q.final_pgap_pass


def test_doji_fails():
    c1, c2, c3 = long_triple(("100.3", "100.8", "99.8", "100.3"), "100")
    q = assess_pgap(Side.LONG, c1, c2, c3, TICK)
    assert q.impulse_direction == "DOJI"
    assert q.reasons[:2] == (WRONG_DIRECTION, BODY_TOO_WEAK) and not q.final_pgap_pass


def test_long_with_bearish_impulse_fails():
    c1, c2, c3 = long_triple(("100.7", "100.8", "99.8", "100"), "100")  # strong but bearish
    q = assess_pgap(Side.LONG, c1, c2, c3, TICK)
    assert q.impulse_direction == "BEARISH" and q.reasons == (WRONG_DIRECTION,)
    assert not q.final_pgap_pass


def test_short_with_bullish_impulse_fails_and_bearish_passes():
    c1 = c(0, "100.5", "100.6", "100.4", "100.5")
    c3 = c(2, "99.5", "100", "99", "99.2")  # high 100 < low(C1) 100.4: gap 0.4
    bull = c(1, "100", "100.3", "99.9", "100.3")
    bear = c(1, "100.3", "100.4", "99.9", "100")
    assert detect_pgap(c1, bull, c3) is Side.SHORT
    qb = assess_pgap(Side.SHORT, c1, bull, c3, TICK)
    assert qb.reasons[0] == WRONG_DIRECTION and not qb.final_pgap_pass
    qs = assess_pgap(Side.SHORT, c1, bear, c3, TICK)
    assert qs.final_pgap_pass and qs.gap_size == D("0.4") and qs.impulse_direction == "BEARISH"


def test_zero_range_impulse_fails():
    c1, _, c3 = long_triple(("100", "100.8", "99.8", "100.6"), "100")
    flat = c(1, "100", "100", "100", "100")
    q = assess_pgap(Side.LONG, c1, flat, c3, TICK)
    assert q.impulse_body_ratio is None and BODY_TOO_WEAK in q.reasons


# ---- gap ----------------------------------------------------------------------------------


def test_gap_body_ratio_exactly_015_passes():
    # body 2.0 (range 2.3): 0.15 * 2 = 0.3 = gap (3 ticks)
    c1, c2, c3 = long_triple(("100", "102.2", "99.9", "102"), "99.9", c1_high="99.6")
    q = assess_pgap(Side.LONG, c1, c2, c3, TICK)
    assert q.gap_size == D("0.3") and q.gap_body_ratio == Fraction(3, 20)
    assert q.strong_gap_pass and q.final_pgap_pass


def test_gap_body_ratio_just_below_015_fails():
    c1, c2, c3 = long_triple(("100", "102.2", "99.9", "102"), "99.89", c1_high="99.6")
    q = assess_pgap(Side.LONG, c1, c2, c3, TICK)
    assert q.gap_size == D("0.29") and GAP_SMALL_VS_BODY in q.reasons
    assert not q.strong_gap_pass and q.strong_impulse_pass


def test_gap_exactly_two_ticks_passes():
    # body 1.0 -> relative minimum 0.15; gap 0.2 = exactly 2 ticks
    c1, c2, c3 = long_triple(("100", "101.1", "99.9", "101"), "99.8", c1_high="99.6")
    q = assess_pgap(Side.LONG, c1, c2, c3, TICK)
    assert q.gap_size_ticks == 2 and q.strong_gap_pass and q.final_pgap_pass


def test_gap_below_two_ticks_fails():
    # body 0.5 -> relative minimum 0.075 passes; gap 0.1 = 1 tick fails
    c1, c2, c3 = long_triple(("100", "100.55", "99.75", "100.5"), "99.7", c1_high="99.6")
    q = assess_pgap(Side.LONG, c1, c2, c3, TICK)
    assert q.gap_size_ticks == 1 and q.reasons == (GAP_SMALL_TICKS,)
    assert not q.final_pgap_pass


# ---- V5.12: quality is measured, never a gate; the first P-Gap spends the run ----------------


def test_weak_geometric_pgap_qualifies_and_spends_the_run():
    rt = RunTracker(TICK)
    seq = [
        c(0, "100", "101", "99", "100"),
        c(1, "100", "102", "99.5", "101"),  # weak impulse: body 1 / range 2.5
        c(2, "102", "104", "101.5", "103"),  # geometric P-Gap #1 (101.5 > 101): WEAK, still valid
        c(3, "101.6", "106", "101.5", "105.9"),  # C2 of #2: strong (4.3/4.5)
        c(4, "106.3", "108", "106.2", "107"),  # P-Gap #2: low 106.2 > high[2] 104, gap 2.2
    ]
    sigs = [s for x in seq for s in rt.on_m1(m(x)) if s.side is Side.LONG]
    assert len(sigs) == 2
    weak, strong = sigs
    assert not weak.quality.final_pgap_pass and weak.quality.primary_reason == BODY_TOO_WEAK
    assert weak.first_in_run  # measured weak, but it is the first P-Gap of the run
    assert strong.quality.final_pgap_pass and not strong.first_in_run  # run already spent
    assert weak.origin.open_time == T0


def test_doji_and_small_gap_pgaps_qualify_too():
    rt = RunTracker(TICK)
    seq = [
        c(0, "100", "101", "99", "100"),
        c(1, "100.5", "102", "99.5", "100.5"),  # doji impulse
        c(2, "102", "103", "101.05", "102.5"),  # gap 0.05 = 0.5 tick, far below 15 % of body 0
    ]
    (sig,) = [s for x in seq for s in rt.on_m1(m(x)) if s.side is Side.LONG]
    assert not sig.quality.final_pgap_pass and len(sig.quality.reasons) >= 2
    assert sig.first_in_run


def test_equality_is_still_not_a_pgap():
    rt = RunTracker(TICK)
    seq = [c(0, "100", "101", "99", "100"), c(1, "100", "102", "99.5", "101.9"),
           c(2, "102", "103", "101", "102.5")]  # low[2] == high[0]: no P-Gap  # fmt: skip
    assert [s for x in seq for s in rt.on_m1(m(x))] == []


def test_non_impulse_spike_candles_may_have_any_colour():
    """SEQ-03 still holds: only C2 of the P-Gap has a colour requirement."""
    rt = RunTracker(TICK)
    seq = [
        c(0, "101", "101.2", "99", "99.5"),  # bearish origin
        c(1, "99.6", "102", "99.5", "101.9"),  # C2 bullish, strong
        c(2, "103.5", "104", "102", "102.5"),  # C3 bearish: low 102 > high[0] 101.2
        c(3, "103", "103.5", "102.1", "102.2"),  # bearish, still continues (low >= low)
    ]
    sigs = [s for x in seq for s in rt.on_m1(m(x)) if s.side is Side.LONG]
    good = [s for s in sigs if s.quality.final_pgap_pass]
    assert len(good) == 1 and good[0].first_in_run  # bearish C1/C3 do not matter
    assert good[0].left.close < good[0].left.open and good[0].right.close < good[0].right.open


def test_engine_still_records_the_measured_quality_but_never_rejects_on_it():
    from sp2l.engine.symbol_engine import ShadowSymbolEngine
    from sp2l.strategy.risk.engine import CostModel, ExchangeFilters

    eng = ShadowSymbolEngine(
        "BTCUSDT",
        tick=TICK,
        costs=CostModel(D("0.0008"), D("0.00095"), D("0.000198")),
        filters=ExchangeFilters(TICK, D("0.001"), None, None, False),
    )
    for x in (
        c(0, "100", "101", "99", "100"),
        c(1, "100", "102", "99.5", "101"),
        c(2, "102", "104", "101.5", "103"),
    ):
        eng.on_m1(m(x))
    (log,) = [p for p in eng.pgaps if p.side == "LONG"]
    # not promoted only because the engine is still warming up; never for its quality
    assert log.reason == "DATA_WARMUP"
    q = log.quality
    assert q is not None and q["impulse_body_ratio"] == "0.4" and q["gap_size"] == "0.5"
    assert q["final_pgap_pass"] is False and q["failure_reasons"] == [BODY_TOO_WEAK]
    assert q["enforced"] is False
