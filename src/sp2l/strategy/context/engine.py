"""M5 Context Engine (CONTEXT_ENGINE_SPEC V5 rev 5.1).

`evaluate_context` is pure: it reads the M5 segment state at the context bar and the
setup's frozen geometry and returns a snapshot with every raw value, every gate boolean
and ordered reason codes. Nothing here is recomputed by the frontend (UI-02).

Ratios are compared exactly with Fraction; nothing is clamped for logic.
Reason order (primary = first) follows spec section order.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from fractions import Fraction

from sp2l.core.numeric import median_even
from sp2l.core.types import Candle, Side
from sp2l.indicators.m5_state import M5Bar, M5State
from sp2l.indicators.regime import Regime
from sp2l.indicators.trend import Trend
from sp2l.strategy.context.levels import (
    FrozenLevel,
    breakout_start,
    nearest_beyond,
    opposing_levels,
    room_obstacles,
)

RANGE_BARS = 14
LIQ_LOOKBACK = 20
ONE_THIRD = Fraction(1, 3)
TWO_THIRDS = Fraction(2, 3)
ROOM_MIN_R = Fraction(1)
LIQ_RATIO = Fraction(1, 2)
LIQ_BASIS_BOTH = "VOLUME_AND_TRADE_COUNT"
LIQ_BASIS_VOLUME_ONLY = "VOLUME_ONLY"  # V5.10: trade count unknown, volume decides if not low


class Reason:
    WARMUP = "CONTEXT_UNKNOWN_WARMUP"
    INVALID_DATA = "CONTEXT_INVALID_DATA"
    RANGE_MIDDLE = "RANGE_MIDDLE"
    HTF_OPPOSITE = "HTF_OPPOSITE_NO_BREAKOUT"
    NO_CONTEXT = "NO_VALID_CONTEXT"
    ROOM = "ROOM_TO_TP_INSUFFICIENT"
    LOW_LIQUIDITY = "LOW_LIQUIDITY"
    LIQUIDITY_UNKNOWN = "LIQUIDITY_UNKNOWN"


@dataclass(frozen=True, slots=True)
class ContextInputs:
    side: Side
    eval_time: datetime  # M1 close time of the evaluation
    e1: Decimal
    r: Decimal
    origin: Candle
    spike_candles: Sequence[Candle]  # Origin..LastSpikeCandle, finalized M1
    breakout_level: FrozenLevel | None
    # ANALYSIS ONLY (RoomProbe, hypothesis B): exact levels removed from the RoomToTP
    # obstacles. The runtime never sets it; empty = the unchanged B38 rule.
    consumed_obstacles: frozenset[Decimal] = frozenset()


@dataclass(slots=True)
class ContextSnapshot:
    status: str = "UNKNOWN"  # PASS / REJECT / UNKNOWN
    reasons: list[str] = field(default_factory=list)
    ctx_open_time: datetime | None = None
    warm: bool = False
    regime: Regime | None = None
    trend: Trend | None = None
    chop14: Decimal | None = None
    adx14: Decimal | None = None
    range_high: Decimal | None = None
    range_low: Decimal | None = None
    range_position_e1: Fraction | None = None
    range_position_origin: Fraction | None = None
    range_middle_reject: bool | None = None
    latest_swing_high: Decimal | None = None
    latest_swing_low: Decimal | None = None
    breakout_level: Decimal | None = None
    breakout_context: bool = False
    breakout_start_open_time: datetime | None = None
    htf_alignment: bool | None = None
    range_edge_origin: bool | None = None
    htf_opposite_no_breakout: bool | None = None
    nearest_obstacle: Decimal | None = None
    room_to_tp_r: Fraction | None = None
    room_to_tp_infinite: bool | None = None
    room_pass: bool | None = None
    volume_ratio: Fraction | None = None
    tradecount_ratio: Fraction | None = None
    liquidity_status: str | None = None
    liquidity_basis: str | None = None  # V5.10: which inputs decided the Liquidity gate
    # V5.8 readiness split (execution certainty vs indicator continuity)
    price_context_ready: bool = False
    liquidity_context_ready: bool = False

    @property
    def primary_reason(self) -> str | None:
        return self.reasons[0] if self.reasons else None

    @property
    def passed(self) -> bool:
        return self.status == "PASS"


def context_bar(state: M5State, eval_time: datetime) -> M5Bar | None:
    """Latest finalized M5 with close_time <= eval_time, only if it is the segment head.

    If the M5 bar that should be the latest is missing, the segment is broken and there is
    no usable context bar (fail closed).
    """
    bar = state.last
    if bar is None or bar.close_time > eval_time:
        return None
    if eval_time - bar.close_time >= bar.close_time - bar.open_time:
        return None  # a newer M5 should have closed; the series is stale/broken
    return bar


def range_position(price: Decimal, low: Decimal, high: Decimal) -> Fraction:
    return Fraction(price - low) / Fraction(high - low)


def evaluate_context(state: M5State, inp: ContextInputs) -> ContextSnapshot:
    snap = ContextSnapshot()
    long = inp.side is Side.LONG

    # §8 BreakoutContext is M1-based and always reportable
    snap.breakout_level = inp.breakout_level.price if inp.breakout_level else None
    bo = breakout_start(inp.breakout_level, inp.spike_candles)
    snap.breakout_context = bo is not None
    snap.breakout_start_open_time = bo.open_time if bo else None

    bar = context_bar(state, inp.eval_time)
    seg = state.segment
    if bar is None or seg is None:
        snap.reasons.append(Reason.WARMUP)
        return snap
    k = bar.index
    snap.ctx_open_time = bar.open_time
    snap.warm = state.warm
    snap.price_context_ready = state.price_ready
    snap.regime = bar.regime
    snap.trend = bar.trend
    snap.chop14 = bar.chop14 if isinstance(bar.chop14, Decimal) else None
    snap.adx14 = bar.adx.adx if isinstance(bar.adx.adx, Decimal) else None

    highs = [p for p in seg.pivots if p.confirmed_index <= k and p.has_high]
    lows = [p for p in seg.pivots if p.confirmed_index <= k and p.has_low]
    snap.latest_swing_high = highs[-1].high if highs else None
    snap.latest_swing_low = lows[-1].low if lows else None

    window = seg.bars[max(0, k - RANGE_BARS + 1) : k + 1]
    if len(window) == RANGE_BARS:
        snap.range_high = max(b.candle.high for b in window)
        snap.range_low = min(b.candle.low for b in window)

    if not state.warm:
        snap.reasons.append(Reason.WARMUP)
    elif (
        bar.regime is Regime.UNKNOWN
        or bar.trend is Trend.INVALID_DUAL_PIVOT
        or snap.range_high is None
        or snap.range_low is None
        or snap.range_high == snap.range_low
    ):
        snap.reasons.append(Reason.INVALID_DATA)
    if snap.reasons:
        _liquidity(snap, seg.bars, k)
        return snap
    assert snap.range_high is not None and snap.range_low is not None

    # §7 Range position (raw, never clamped for logic)
    origin_ref = inp.origin.low if long else inp.origin.high
    snap.range_position_e1 = range_position(inp.e1, snap.range_low, snap.range_high)
    snap.range_position_origin = range_position(origin_ref, snap.range_low, snap.range_high)
    is_range = bar.regime is Regime.RANGE
    snap.range_middle_reject = is_range and ONE_THIRD <= snap.range_position_e1 <= TWO_THIRDS
    if long:
        snap.range_edge_origin = is_range and snap.range_position_origin <= ONE_THIRD
    else:
        snap.range_edge_origin = is_range and snap.range_position_origin >= TWO_THIRDS

    # §9 HTF
    aligned = Trend.BULL if long else Trend.BEAR
    opposite = Trend.BEAR if long else Trend.BULL
    snap.htf_alignment = bar.trend is aligned and not is_range
    snap.htf_opposite_no_breakout = bar.trend is opposite and not snap.breakout_context

    # §11 RoomToTP: nearest valid level in the E1 -> TP path (B38; B10 reworded)
    tp = inp.e1 + inp.r if long else inp.e1 - inp.r
    obstacles = room_obstacles(inp.side, opposing_levels(inp.side, seg.pivots, k), inp.e1, tp)
    if inp.consumed_obstacles:
        obstacles = [x for x in obstacles if x not in inp.consumed_obstacles]
    nearest = nearest_beyond(inp.side, obstacles, inp.e1)
    snap.nearest_obstacle = nearest
    if nearest is None:
        snap.room_to_tp_infinite = True
        snap.room_pass = True
    else:
        room = (nearest - inp.e1) if long else (inp.e1 - nearest)
        snap.room_to_tp_infinite = False
        snap.room_to_tp_r = Fraction(room) / Fraction(inp.r)
        snap.room_pass = snap.room_to_tp_r >= ROOM_MIN_R

    _liquidity(snap, seg.bars, k)

    # reasons in spec section order
    if snap.range_middle_reject:
        snap.reasons.append(Reason.RANGE_MIDDLE)
    if snap.htf_opposite_no_breakout:
        snap.reasons.append(Reason.HTF_OPPOSITE)
    if not (snap.breakout_context or snap.htf_alignment or snap.range_edge_origin):
        snap.reasons.append(Reason.NO_CONTEXT)
    if not snap.room_pass:
        snap.reasons.append(Reason.ROOM)
    if snap.liquidity_status != "PASS":
        snap.reasons.append(snap.liquidity_status or Reason.LIQUIDITY_UNKNOWN)
    snap.status = "REJECT" if snap.reasons else "PASS"
    return snap


def _liquidity(snap: ContextSnapshot, bars: Sequence[M5Bar], k: int) -> None:
    """§12: L = context bar; reference = exactly 20 bars before L (L excluded).
    V5.8: a bar with an UNKNOWN trade count (Tabdeal chart history) is never invented.
    V5.10: the reject is a conjunction, so an unknown trade count only matters when volume is
    low: (low volume = false) AND unknown = false -> PASS, decided by volume alone; (low
    volume = true) AND unknown = unknown -> LIQUIDITY_UNKNOWN. Formula and thresholds are
    unchanged."""
    if k < LIQ_LOOKBACK:
        snap.liquidity_status = Reason.LIQUIDITY_UNKNOWN
        return
    ref = bars[k - LIQ_LOOKBACK : k]
    lc = bars[k].candle
    med_v = median_even([b.candle.volume for b in ref])
    snap.volume_ratio = Fraction(lc.volume) / Fraction(med_v) if med_v > 0 else None
    low_v = Fraction(lc.volume) < LIQ_RATIO * Fraction(med_v)
    counts = [b.candle.trade_count for b in ref]
    if lc.trade_count is None or any(n is None for n in counts):
        snap.liquidity_basis = LIQ_BASIS_VOLUME_ONLY
        snap.liquidity_status = Reason.LIQUIDITY_UNKNOWN if low_v else "PASS"
        return
    snap.liquidity_context_ready = True
    snap.liquidity_basis = LIQ_BASIS_BOTH
    med_n = median_even([Decimal(n) for n in counts if n is not None])
    snap.tradecount_ratio = Fraction(lc.trade_count) / Fraction(med_n) if med_n > 0 else None
    low_n = Fraction(lc.trade_count) < LIQ_RATIO * Fraction(med_n)
    snap.liquidity_status = Reason.LOW_LIQUIDITY if (low_v and low_n) else "PASS"
