"""Regime classification with fixed precedence (CTX-08).

RANGE is evaluated first, then TREND, else TRANSITION. Overlaps are resolved by this
order only; do not reorder.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from sp2l.indicators import Unknown

RANGE_EXPR = "CHOP14 >= 61.8 AND ADX14 < 20"
TREND_EXPR = "CHOP14 <= 38.2 OR ADX14 >= 25"

CHOP_RANGE_MIN = Decimal("61.8")
ADX_RANGE_BELOW = Decimal("20")
CHOP_TREND_MAX = Decimal("38.2")
ADX_TREND_MIN = Decimal("25")


class Regime(StrEnum):
    RANGE = "RANGE"
    TREND = "TREND"
    TRANSITION = "TRANSITION"
    UNKNOWN = "UNKNOWN"


def classify_regime(chop: Decimal | None | Unknown, adx: Decimal | None | Unknown) -> Regime:
    if not isinstance(chop, Decimal) or not isinstance(adx, Decimal):
        return Regime.UNKNOWN
    if chop >= CHOP_RANGE_MIN and adx < ADX_RANGE_BELOW:
        return Regime.RANGE
    if chop <= CHOP_TREND_MAX or adx >= ADX_TREND_MIN:
        return Regime.TREND
    return Regime.TRANSITION
