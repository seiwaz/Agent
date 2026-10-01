"""Swing levels used by Context and Exhaustion (CE§8, CE§11, EG§8; B09, B27, B32, B38).

- "Unbroken" for a level means no finalized M5 close strictly beyond it after its
  confirmation, up to and including the evaluation (context) bar.
- BreakoutContext (V5.3 B27 + clarification):
  * EligibleBreakoutLevels are frozen once at setup creation: confirmed M5 pivot levels
    (Long: Swing Highs, Short: Swing Lows, dual-pivot High/Low included per B32) whose
    pivot was confirmed no later than SpikeOriginCandle.open_time and that were still
    unbroken (no finalized M5 close strictly beyond) at that time. Pivots confirmed later
    are never eligible for the setup.
  * BreakoutLevel = the highest crossed eligible Swing High (Long) / the lowest crossed
    eligible Swing Low (Short), crossed = a finalized Spike M1 close strictly beyond.
    Never selected by distance from Origin, E1 or last trade.
  * It may become true, and advance to a farther level, during zero-fill pre-Pullback
    extension; it freezes permanently at PullbackStart or the first E1 fill.
- A confirmed DUAL pivot bar supplies its High as resistance / Long breakout level and its
  Low as support / Short breakout level (V5.2 B32).
- Obstacles are path-relative only (V5.4 B38, B10 reworded); no level is removed because
  the Spike crossed it:
    RoomToTP       Long: E1 < level <= TP     Short: TP <= level < E1
    OpposingSwing  Long: level > SpikeExtreme Short: level < SpikeExtreme
  Levels must be confirmed and not invalidated by a finalized M5 close.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sp2l.core.types import Candle, Side
from sp2l.indicators.m5_state import M5Bar, SegPivot


@dataclass(frozen=True, slots=True)
class FrozenLevel:
    """BreakoutContext level for one setup, frozen at Spike confirmation."""

    side: Side
    price: Decimal
    pivot_open_time: datetime


def _unbroken_high(p: SegPivot, ctx_index: int) -> bool:
    return p.high_broken_index is None or p.high_broken_index > ctx_index


def _unbroken_low(p: SegPivot, ctx_index: int) -> bool:
    return p.low_broken_index is None or p.low_broken_index > ctx_index


@dataclass(frozen=True, slots=True)
class EligibleLevel:
    price: Decimal
    pivot_open_time: datetime


def eligible_breakout_levels(
    side: Side,
    origin_open: datetime,
    pivots: Sequence[SegPivot],
    bars: Sequence[M5Bar],
) -> tuple[EligibleLevel, ...]:
    """The setup's frozen EligibleBreakoutLevels (B27 clarification)."""
    long = side is Side.LONG
    out: list[EligibleLevel] = []
    for p in pivots:
        if bars[p.confirmed_index].close_time > origin_open:
            continue  # confirmed after SpikeOrigin: never eligible for this setup
        if long and p.has_high:
            broken, price = p.high_broken_index, p.high
        elif not long and p.has_low:
            broken, price = p.low_broken_index, p.low
        else:
            continue
        if broken is not None and bars[broken].close_time <= origin_open:
            continue  # already broken at SpikeOrigin open time
        out.append(EligibleLevel(price, bars[p.index].open_time))
    return tuple(out)


def select_breakout_level(
    side: Side,
    eligible: Sequence[EligibleLevel],
    spike_candles: Sequence[Candle],
) -> FrozenLevel | None:
    """Highest crossed eligible Swing High (Long) / lowest crossed Swing Low (Short)."""
    long = side is Side.LONG
    crossed = [
        e
        for e in eligible
        if any((c.close > e.price) if long else (c.close < e.price) for c in spike_candles)
    ]
    if not crossed:
        return None
    best = (
        max(crossed, key=lambda e: (e.price, e.pivot_open_time))
        if long
        else min(crossed, key=lambda e: (e.price, e.pivot_open_time))
    )
    return FrozenLevel(side, best.price, best.pivot_open_time)


def breakout_start(level: FrozenLevel | None, spike_candles: Iterable[Candle]) -> Candle | None:
    """First Spike M1 candle whose close is strictly beyond the frozen level (CE§8, EG§10)."""
    if level is None:
        return None
    for c in spike_candles:
        if (level.side is Side.LONG and c.close > level.price) or (
            level.side is Side.SHORT and c.close < level.price
        ):
            return c
    return None


def opposing_levels(
    side: Side,
    pivots: Sequence[SegPivot],
    ctx_index: int,
) -> list[Decimal]:
    """Confirmed, M5-valid levels on the opposing side of the trade.

    Long: Swing High prices (incl. DUAL). Short: Swing Low prices (incl. DUAL).
    """
    out: list[Decimal] = []
    for p in pivots:
        if p.confirmed_index > ctx_index:
            continue
        if side is Side.LONG and p.has_high and _unbroken_high(p, ctx_index):
            price = p.high
        elif side is Side.SHORT and p.has_low and _unbroken_low(p, ctx_index):
            price = p.low
        else:
            continue
        out.append(price)
    return out


def room_obstacles(
    side: Side, levels: Iterable[Decimal], e1: Decimal, tp: Decimal
) -> list[Decimal]:
    """B38: levels in the E1 -> TP path. Long: E1 < level <= TP. Short: TP <= level < E1."""
    if side is Side.LONG:
        return [x for x in levels if e1 < x <= tp]
    return [x for x in levels if tp <= x < e1]


def nearest_beyond(side: Side, levels: Iterable[Decimal], ref: Decimal) -> Decimal | None:
    """Nearest level strictly above `ref` (Long) / strictly below `ref` (Short); None = +INF."""
    if side is Side.LONG:
        above = [x for x in levels if x > ref]
        return min(above) if above else None
    below = [x for x in levels if x < ref]
    return max(below) if below else None
