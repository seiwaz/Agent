"""Context Engine V6.0 (CONTEXT_ENGINE_SPEC V6.0; owner decision 2026-10-01).

The source strategy (poursamadi.com/sp2l-strategy) names three "best conditions" for a Spike.
A setup's Context PASSES iff

    D  NetTP:       |TP - E1| - E1*entry_fee_rate - TP*exit_fee_rate > 0   (zero rejects)
    AND at least one of
    A  LevelBreak:  a Spike M1 close strictly beyond a confirmed M5 swing level that existed
                    at Spike origin (B27 machinery, unchanged; wick/equality do not count)
    B  ChannelEdge: raw RangePosition of the Origin reference in the last 14 finalized M5 bars
                    (incl. the context bar) <= 1/3 (Long, OriginLow) / >= 2/3 (Short, OriginHigh)
    C  HTFAligned:  the M5 trend is BULL (Long) / BEAR (Short)

D needs no M5 data: it is evaluated FIRST, even during warmup (missing costs -> fail closed,
CONTEXT_NET_TP_UNKNOWN). A, B and C need a warm M5 segment (>= the configured warmup bars;
otherwise CONTEXT_UNKNOWN_WARMUP, fail closed). Reasons, in order:
CONTEXT_NET_TP_NOT_POSITIVE | CONTEXT_NET_TP_UNKNOWN, CONTEXT_UNKNOWN_WARMUP, NO_VALID_CONTEXT.

Everything the V5 engine measured (regime ADX/CHOP, RangeMiddle, HTF-opposite, RoomToTP,
Liquidity) is still computed and recorded INFORMATIONALLY: it never appears in `reasons` and
never affects `status`. `evaluate_context` is pure; ratios are exact Fractions, never clamped.
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
from sp2l.strategy.risk.engine import CostModel

RANGE_BARS = 14
LIQ_LOOKBACK = 20
ONE_THIRD = Fraction(1, 3)
TWO_THIRDS = Fraction(2, 3)
ROOM_MIN_R = Fraction(1)  # informational since V6.0 (was the RoomToTP gate)
LIQ_RATIO = Fraction(1, 2)  # informational since V6.0 (was the Liquidity gate)
LIQ_BASIS_BOTH = "VOLUME_AND_TRADE_COUNT"
LIQ_BASIS_VOLUME_ONLY = "VOLUME_ONLY"  # V5.10: trade count unknown, volume decides if not low


class Reason:
    # V6.0 gating reasons (in this order)
    NET_TP_NOT_POSITIVE = "CONTEXT_NET_TP_NOT_POSITIVE"
    NET_TP_UNKNOWN = "CONTEXT_NET_TP_UNKNOWN"
    WARMUP = "CONTEXT_UNKNOWN_WARMUP"
    NO_CONTEXT = "NO_VALID_CONTEXT"
    # V5 codes: no longer emitted by evaluate_context (history rows and informational flags)
    INVALID_DATA = "CONTEXT_INVALID_DATA"
    RANGE_MIDDLE = "RANGE_MIDDLE"
    HTF_OPPOSITE = "HTF_OPPOSITE_NO_BREAKOUT"
    ROOM = "ROOM_TO_TP_INSUFFICIENT"
    LOW_LIQUIDITY = "LOW_LIQUIDITY"
    LIQUIDITY_UNKNOWN = "LIQUIDITY_UNKNOWN"


UNKNOWN_REASONS = frozenset({Reason.NET_TP_UNKNOWN, Reason.WARMUP})


@dataclass(frozen=True, slots=True)
class ContextInputs:
    side: Side
    eval_time: datetime  # M1 close time of the evaluation
    e1: Decimal
    r: Decimal
    origin: Candle
    spike_candles: Sequence[Candle]  # Origin..LastSpikeCandle, finalized M1
    breakout_level: FrozenLevel | None
    # ANALYSIS ONLY (RoomProbe): exact levels removed from the informational RoomToTP
    # obstacles. The runtime never sets it.
    consumed_obstacles: frozenset[Decimal] = frozenset()
    # V6.0: entry/exit fee rates for the NetTP condition (runtime config; never hardcoded).
    # None -> CONTEXT_NET_TP_UNKNOWN (fail closed).
    costs: CostModel | None = None


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
    range_middle_reject: bool | None = None  # informational since V6.0
    latest_swing_high: Decimal | None = None
    latest_swing_low: Decimal | None = None
    breakout_level: Decimal | None = None
    breakout_context: bool = False  # A LevelBreak
    breakout_start_open_time: datetime | None = None
    htf_alignment: bool | None = None  # C HTFAligned
    range_edge_origin: bool | None = None  # B ChannelEdge
    htf_opposite_no_breakout: bool | None = None  # informational since V6.0
    nearest_obstacle: Decimal | None = None  # informational since V6.0
    room_to_tp_r: Fraction | None = None
    room_to_tp_infinite: bool | None = None
    room_pass: bool | None = None
    volume_ratio: Fraction | None = None  # informational since V6.0
    tradecount_ratio: Fraction | None = None
    liquidity_status: str | None = None
    liquidity_basis: str | None = None  # V5.10: which inputs decided the Liquidity measure
    # V5.8 readiness split (execution certainty vs indicator continuity)
    price_context_ready: bool = False
    liquidity_context_ready: bool = False
    # V6.0
    tp: Decimal | None = None
    net_tp_per_unit: Decimal | None = None  # D: E1-only net at TP after costs, per unit
    net_tp_positive: bool | None = None  # None = costs unknown

    @property
    def primary_reason(self) -> str | None:
        return self.reasons[0] if self.reasons else None

    @property
    def passed(self) -> bool:
        return self.status == "PASS"

    # V6.0 names of the three conditions (stored under their V5 field names)
    @property
    def level_break(self) -> bool:
        return self.breakout_context

    @property
    def channel_edge(self) -> bool | None:
        return self.range_edge_origin

    @property
    def htf_aligned(self) -> bool | None:
        return self.htf_alignment


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


def net_tp(side: Side, e1: Decimal, r: Decimal, costs: CostModel) -> tuple[Decimal, Decimal]:
    """(TP, E1-only net at TP per unit after entry and exit fees) - same formula as Risk V5.11."""
    tp = e1 + r if side is Side.LONG else e1 - r
    return tp, abs(tp - e1) - e1 * costs.entry_fee_rate - tp * costs.exit_fee_rate


def evaluate_context(state: M5State, inp: ContextInputs) -> ContextSnapshot:
    snap = ContextSnapshot()
    long = inp.side is Side.LONG
    reasons: list[str] = []

    # D NetTP: first, needs no M5 data
    snap.tp = inp.e1 + inp.r if long else inp.e1 - inp.r
    if inp.costs is None:
        reasons.append(Reason.NET_TP_UNKNOWN)
    else:
        snap.tp, snap.net_tp_per_unit = net_tp(inp.side, inp.e1, inp.r, inp.costs)
        snap.net_tp_positive = snap.net_tp_per_unit > 0
        if not snap.net_tp_positive:
            reasons.append(Reason.NET_TP_NOT_POSITIVE)

    # A LevelBreak: M1-based, always reportable (B27 machinery unchanged)
    snap.breakout_level = inp.breakout_level.price if inp.breakout_level else None
    bo = breakout_start(inp.breakout_level, inp.spike_candles)
    snap.breakout_context = bo is not None
    snap.breakout_start_open_time = bo.open_time if bo else None

    bar = context_bar(state, inp.eval_time)
    seg = state.segment
    if bar is None or seg is None or not state.warm:
        if bar is not None and seg is not None:
            _informational(snap, state, bar, inp, long)
        reasons.append(Reason.WARMUP)
        return _finish(snap, reasons)
    _informational(snap, state, bar, inp, long)

    # B ChannelEdge (no regime requirement; a flat range -> false, not a global reject)
    if snap.range_position_origin is not None:
        rp = snap.range_position_origin
        snap.range_edge_origin = rp <= ONE_THIRD if long else rp >= TWO_THIRDS
    else:
        snap.range_edge_origin = False
    # C HTFAligned (no regime requirement; NEUTRAL / INVALID_DUAL_PIVOT -> false)
    snap.htf_alignment = bar.trend is (Trend.BULL if long else Trend.BEAR)

    if not (snap.breakout_context or snap.range_edge_origin or snap.htf_alignment):
        reasons.append(Reason.NO_CONTEXT)
    return _finish(snap, reasons)


def _finish(snap: ContextSnapshot, reasons: list[str]) -> ContextSnapshot:
    snap.reasons = reasons
    if not reasons:
        snap.status = "PASS"
    elif set(reasons) <= UNKNOWN_REASONS:
        snap.status = "UNKNOWN"  # undecidable: fail closed
    else:
        snap.status = "REJECT"
    return snap


def _informational(
    snap: ContextSnapshot, state: M5State, bar: M5Bar, inp: ContextInputs, long: bool
) -> None:
    """Everything the V5 gates measured, recorded for analytics/UI only (never a reason)."""
    seg = state.segment
    assert seg is not None
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
    if (
        snap.range_high is not None
        and snap.range_low is not None
        and snap.range_high != snap.range_low
    ):
        origin_ref = inp.origin.low if long else inp.origin.high
        snap.range_position_e1 = range_position(inp.e1, snap.range_low, snap.range_high)
        snap.range_position_origin = range_position(origin_ref, snap.range_low, snap.range_high)
        snap.range_middle_reject = (
            bar.regime is Regime.RANGE and ONE_THIRD <= snap.range_position_e1 <= TWO_THIRDS
        )

    opposite = Trend.BEAR if long else Trend.BULL
    snap.htf_opposite_no_breakout = bar.trend is opposite and not snap.breakout_context

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
