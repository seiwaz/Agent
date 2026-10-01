"""E1, SL, R, the single TP and the E2 reference price (E1-01, SLTP-01/02, E2-01).

There is exactly one TP. No TP2, partial TP, AB=CD or trailing target exists anywhere.

E2 = (E1 + SL) / 2 rounded to the tick grid toward SL (V5.1 B23): Long rounds down,
Short rounds up. E2 risk D2 = abs(E2 - SL) is taken from the rounded price and is never
assumed to be 0.5R.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from sp2l.core.numeric import div, is_on_tick
from sp2l.core.types import Candle, Side


def e1_price(side: Side, last_spike_candle: Candle) -> Decimal:
    return last_spike_candle.low if side is Side.LONG else last_spike_candle.high


def stop_loss(side: Side, origin: Candle, tick: Decimal) -> Decimal:
    if tick <= 0:
        raise ValueError("tick must be positive")
    return origin.low - tick if side is Side.LONG else origin.high + tick


def risk_distance(e1: Decimal, sl: Decimal) -> Decimal:
    return abs(e1 - sl)


def take_profit(side: Side, e1: Decimal, sl: Decimal) -> Decimal:
    r = risk_distance(e1, sl)
    return e1 + r if side is Side.LONG else e1 - r


def e2_midpoint(e1: Decimal, sl: Decimal) -> Decimal:
    """Exact midpoint; Decimal division by 2 of finite decimals is always exact."""
    return (e1 + sl) / 2


def e2_price(side: Side, e1: Decimal, sl: Decimal, tick: Decimal) -> Decimal:
    """Midpoint rounded to the tick grid toward SL (B23)."""
    if tick <= 0:
        raise ValueError("tick must be positive")
    mid = e2_midpoint(e1, sl)
    steps = div(mid, tick)
    whole = steps.to_integral_value(rounding=ROUND_FLOOR if side is Side.LONG else ROUND_CEILING)
    return whole * tick


@dataclass(frozen=True, slots=True)
class SetupLevels:
    side: Side
    e1: Decimal
    sl: Decimal
    r: Decimal
    tp: Decimal
    e2_mid: Decimal
    e2: Decimal
    d2: Decimal


def compute_levels(side: Side, origin: Candle, last_spike: Candle, tick: Decimal) -> SetupLevels:
    e1 = e1_price(side, last_spike)
    sl = stop_loss(side, origin, tick)
    for price in (e1, sl):
        if not is_on_tick(price, tick):
            raise ValueError(f"{price} is off the {tick} tick grid")
    if (side is Side.LONG and not e1 > sl) or (side is Side.SHORT and not e1 < sl):
        raise ValueError(f"E1 {e1} is not on the profit side of SL {sl}")
    e2 = e2_price(side, e1, sl, tick)
    return SetupLevels(
        side=side,
        e1=e1,
        sl=sl,
        r=risk_distance(e1, sl),
        tp=take_profit(side, e1, sl),
        e2_mid=e2_midpoint(e1, sl),
        e2=e2,
        d2=abs(e2 - sl),
    )
