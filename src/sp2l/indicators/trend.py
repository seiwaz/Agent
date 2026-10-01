"""M5 Trend from the last two confirmed swing highs and lows (CTX-05)."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from enum import StrEnum

# Expression text as written in spec/SP2L_RULES.yaml context.trend (drift-checked in tests).
BULL_EXPR = "SH2>SH1 AND SL2>SL1"
BEAR_EXPR = "SH2<SH1 AND SL2<SL1"


class Trend(StrEnum):
    BULL = "BULL"
    BEAR = "BEAR"
    NEUTRAL = "NEUTRAL"
    NEUTRAL_INSUFFICIENT_STRUCTURE = "NEUTRAL_INSUFFICIENT_STRUCTURE"
    INVALID_DUAL_PIVOT = "INVALID_DUAL_PIVOT"  # V5.1 B11: fail closed


def classify_trend(swing_highs: Sequence[Decimal], swing_lows: Sequence[Decimal]) -> Trend:
    """Inputs are confirmed pivot prices in confirmation order (oldest first)."""
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return Trend.NEUTRAL_INSUFFICIENT_STRUCTURE
    sh1, sh2 = swing_highs[-2], swing_highs[-1]
    sl1, sl2 = swing_lows[-2], swing_lows[-1]
    if sh2 > sh1 and sl2 > sl1:
        return Trend.BULL
    if sh2 < sh1 and sl2 < sl1:
        return Trend.BEAR
    return Trend.NEUTRAL
