"""Exhaustion Gate (EXHAUSTION_GATE_SPEC V5 rev 5.1).

Evaluated after Context and before every initial or replacement E1 submit, using only
finalized data available at evaluation time. Boundary equality triggers a condition.

Reject EXHAUSTION_RISK iff
    LateTrend AND ExtremeStretch AND (ClimacticSpike OR AtOuterEdge)
    AND NOT FreshBreakoutException.
If a required input is unavailable (warmup, ATR == 0, zero range) -> EXHAUSTION_UNKNOWN,
which fails closed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from fractions import Fraction

from sp2l.core.types import Candle, Side
from sp2l.indicators.m5_state import M5State
from sp2l.indicators.regime import Regime
from sp2l.indicators.trend import Trend
from sp2l.marketdata.m1_builder import MINUTE
from sp2l.marketdata.m5_aggregator import M5_STEP, m5_bucket
from sp2l.strategy.context.engine import context_bar
from sp2l.strategy.context.levels import (
    FrozenLevel,
    breakout_start,
    nearest_beyond,
    opposing_levels,
)

TREND_AGE_MIN = 20
MICROCHANNEL_MIN = 8
STRETCH_MIN = Fraction(2)
SPIKE_ATR_MIN = Fraction(3, 2)
RP20_LONG_MIN = Fraction(9, 10)
RP20_SHORT_MAX = Fraction(1, 10)
OPPOSING_MAX = Fraction(1, 2)
RP_BARS = 20
FRESH_MAX_AGE = 1


class Sub:
    LATE_TREND_AGE = "LATE_TREND_AGE"
    LONG_MICROCHANNEL = "LONG_MICROCHANNEL"
    SHORT_MICROCHANNEL = "SHORT_MICROCHANNEL"
    EXTREME_STRETCH = "EXTREME_STRETCH"
    CLIMACTIC_SPIKE = "CLIMACTIC_SPIKE"
    OUTER_EDGE_20 = "OUTER_EDGE_20"
    OPPOSING_SWING_NEAR = "OPPOSING_SWING_NEAR"
    FRESH_BREAKOUT_EXCEPTION = "FRESH_BREAKOUT_EXCEPTION"


REJECT = "EXHAUSTION_RISK"
UNKNOWN = "EXHAUSTION_UNKNOWN"


@dataclass(frozen=True, slots=True)
class ExhaustionInputs:
    side: Side
    eval_time: datetime
    spike_candles: Sequence[Candle]
    breakout_level: FrozenLevel | None


@dataclass(slots=True)
class ExhaustionSnapshot:
    status: str = "UNKNOWN"
    reason: str | None = None
    sub_reasons: list[str] = field(default_factory=list)
    ctx_open_time: datetime | None = None
    trend_age_bars: int | None = None
    microchannel_len: int | None = None
    ema20_m5: Decimal | None = None
    atr14_m5: Decimal | None = None
    spike_extreme: Decimal | None = None
    stretch_atr: Fraction | None = None
    spike_atr: Fraction | None = None
    range_position_20: Fraction | None = None
    opposing_swing_level: Decimal | None = None
    opposing_swing_distance_atr: Fraction | None = None
    opposing_swing_infinite: bool | None = None
    late_trend: bool | None = None
    extreme_stretch: bool | None = None
    climactic_spike: bool | None = None
    at_outer_edge: bool | None = None
    fresh_breakout_exception: bool | None = None
    previous_regime_at_breakout: Regime | None = None
    breakout_start_m5_open_time: datetime | None = None
    breakout_age_m5: int | None = None

    @property
    def passed(self) -> bool:
        return self.status == "PASS"


def evaluate_exhaustion(state: M5State, inp: ExhaustionInputs) -> ExhaustionSnapshot:
    snap = ExhaustionSnapshot()
    long = inp.side is Side.LONG
    seg = state.segment
    bar = context_bar(state, inp.eval_time)
    candles = list(inp.spike_candles)
    snap.spike_extreme = max(c.high for c in candles) if long else min(c.low for c in candles)
    spike_range = max(c.high for c in candles) - min(c.low for c in candles)
    if bar is None or seg is None or not state.warm:
        snap.reason = UNKNOWN
        return snap
    k = bar.index
    snap.ctx_open_time = bar.open_time
    snap.atr14_m5 = bar.atr14
    snap.ema20_m5 = bar.ema20

    # §3 TrendAgeBars / §4 MicrochannelLen
    direction = Trend.BULL if long else Trend.BEAR
    snap.trend_age_bars = state.trend_age(direction, k)
    n = 1
    j = k
    while j > 0:
        cur, prev = seg.bars[j].candle, seg.bars[j - 1].candle
        if (long and cur.low >= prev.low) or (not long and cur.high <= prev.high):
            n += 1
            j -= 1
        else:
            break
    snap.microchannel_len = n

    # §7 RangePosition20 (includes the context bar, B12)
    rp_ok = k + 1 >= RP_BARS
    if rp_ok:
        window = seg.bars[k - RP_BARS + 1 : k + 1]
        hh = max(b.candle.high for b in window)
        ll = min(b.candle.low for b in window)
        rp_ok = hh != ll
        if rp_ok:
            snap.range_position_20 = Fraction(snap.spike_extreme - ll) / Fraction(hh - ll)

    atr, ema = bar.atr14, bar.ema20
    if atr is None or ema is None or atr == 0 or not rp_ok:
        snap.reason = UNKNOWN
        return snap

    # §5 / §6
    snap.stretch_atr = Fraction(abs(snap.spike_extreme - ema)) / Fraction(atr)
    snap.spike_atr = Fraction(spike_range) / Fraction(atr)

    # §8 OpposingSwingDistance: nearest valid level strictly beyond SpikeExtreme (B38)
    bo = breakout_start(inp.breakout_level, candles)
    level = nearest_beyond(inp.side, opposing_levels(inp.side, seg.pivots, k), snap.spike_extreme)
    snap.opposing_swing_level = level
    snap.opposing_swing_infinite = level is None
    if level is not None:
        dist = (level - snap.spike_extreme) if long else (snap.spike_extreme - level)
        snap.opposing_swing_distance_atr = Fraction(dist) / Fraction(atr)

    # §9 conditions (equality triggers)
    late_age = snap.trend_age_bars >= TREND_AGE_MIN
    late_micro = snap.microchannel_len >= MICROCHANNEL_MIN
    snap.late_trend = late_age or late_micro
    snap.extreme_stretch = snap.stretch_atr >= STRETCH_MIN
    snap.climactic_spike = snap.spike_atr >= SPIKE_ATR_MIN
    assert snap.range_position_20 is not None
    edge_rp = (
        snap.range_position_20 >= RP20_LONG_MIN
        if long
        else snap.range_position_20 <= RP20_SHORT_MAX
    )
    edge_opp = (
        snap.opposing_swing_distance_atr is not None
        and snap.opposing_swing_distance_atr <= OPPOSING_MAX
    )
    snap.at_outer_edge = edge_rp or edge_opp

    # §10 FreshBreakoutException (B13: bucket by M1 open_time; forming bucket = age 0)
    snap.fresh_breakout_exception = False
    if bo is not None:
        bucket = m5_bucket(bo.open_time)
        snap.breakout_start_m5_open_time = bucket
        age = int((bar.open_time - bucket) / M5_STEP)
        snap.breakout_age_m5 = max(age, 0)
        bo_close = bo.open_time + MINUTE
        prior = [b for b in seg.bars[: k + 1] if b.close_time < bo_close]
        prev_regime = prior[-1].regime if prior else None
        if prior and prior[-1].close_time + M5_STEP < bo_close:
            prev_regime = None  # the bar immediately before is not in this segment
        snap.previous_regime_at_breakout = prev_regime
        snap.fresh_breakout_exception = (
            prev_regime in (Regime.RANGE, Regime.TRANSITION)
            and snap.breakout_age_m5 <= FRESH_MAX_AGE
        )

    if late_age:
        snap.sub_reasons.append(Sub.LATE_TREND_AGE)
    if late_micro:
        snap.sub_reasons.append(Sub.LONG_MICROCHANNEL if long else Sub.SHORT_MICROCHANNEL)
    if snap.extreme_stretch:
        snap.sub_reasons.append(Sub.EXTREME_STRETCH)
    if snap.climactic_spike:
        snap.sub_reasons.append(Sub.CLIMACTIC_SPIKE)
    if edge_rp:
        snap.sub_reasons.append(Sub.OUTER_EDGE_20)
    if edge_opp:
        snap.sub_reasons.append(Sub.OPPOSING_SWING_NEAR)
    if snap.fresh_breakout_exception:
        snap.sub_reasons.append(Sub.FRESH_BREAKOUT_EXCEPTION)

    reject = (
        snap.late_trend
        and snap.extreme_stretch
        and (snap.climactic_spike or snap.at_outer_edge)
        and not snap.fresh_breakout_exception
    )
    snap.status = "REJECT" if reject else "PASS"
    snap.reason = REJECT if reject else None
    return snap
