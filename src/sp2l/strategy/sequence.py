"""Directional continuation (SEQ-01..03). Candle colour is never consulted."""

from __future__ import annotations

from sp2l.core.types import Candle, Side

LONG_EXPR = "low[current] >= low[previous]"
SHORT_EXPR = "high[current] <= high[previous]"


def continues(side: Side, previous: Candle, current: Candle) -> bool:
    if side is Side.LONG:
        return current.low >= previous.low
    return current.high <= previous.high
