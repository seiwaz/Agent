"""Confirmed 2-left/2-right M5 pivots (CTX-03, CTX-04).

Swing High at i: High[i] strictly greater than High[i-2], High[i-1], High[i+1], High[i+2].
Swing Low  at i: Low[i]  strictly less    than Low[i-2],  Low[i-1],  Low[i+1],  Low[i+2].
Any equality -> not a pivot. A pivot is confirmable only once bars i+1 and i+2 are
finalized, so confirmed_index = i + 2.

A bar that satisfies both is reported as DUAL. CE§2 says such data is invalid and must
fail closed; how far that fail-closed reaches is BLOCKER B11, so it is left to the caller.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum

from sp2l.core.series import M5, require_contiguous
from sp2l.core.types import Candle

LEFT = 2
RIGHT = 2


class PivotKind(StrEnum):
    HIGH = "HIGH"
    LOW = "LOW"
    DUAL = "DUAL"


@dataclass(frozen=True, slots=True)
class Pivot:
    index: int
    kind: PivotKind
    high: Decimal
    low: Decimal
    confirmed_index: int

    @property
    def price(self) -> Decimal:
        if self.kind is PivotKind.HIGH:
            return self.high
        if self.kind is PivotKind.LOW:
            return self.low
        raise ValueError("a DUAL pivot has no single price; see BLOCKER B11")


def _is_swing_high(c: Sequence[Candle], i: int) -> bool:
    h = c[i].high
    return all(h > c[j].high for j in (i - 2, i - 1, i + 1, i + 2))


def _is_swing_low(c: Sequence[Candle], i: int) -> bool:
    lo = c[i].low
    return all(lo < c[j].low for j in (i - 2, i - 1, i + 1, i + 2))


def confirmed_pivots(
    candles: Sequence[Candle], through_index: int | None = None, step: timedelta = M5
) -> list[Pivot]:
    """Pivots confirmed using only bars [0 .. through_index] (default: all bars)."""
    require_contiguous(candles, step)
    last = len(candles) - 1 if through_index is None else through_index
    if last >= len(candles):
        raise IndexError("through_index beyond series")
    out: list[Pivot] = []
    for i in range(LEFT, last - RIGHT + 1):
        is_high = _is_swing_high(candles, i)
        is_low = _is_swing_low(candles, i)
        if not (is_high or is_low):
            continue
        kind = (
            PivotKind.DUAL
            if (is_high and is_low)
            else (PivotKind.HIGH if is_high else PivotKind.LOW)
        )
        out.append(Pivot(i, kind, candles[i].high, candles[i].low, i + RIGHT))
    return out


def first_break_index(pivot: Pivot, candles: Sequence[Candle]) -> int | None:
    """First bar after confirmation whose finalized close is strictly beyond the level.

    HIGH: close > level. LOW: close < level. Equality does not break (CE§11).
    """
    if pivot.kind is PivotKind.DUAL:
        raise ValueError("DUAL pivot break semantics are BLOCKER B11")
    level = pivot.price
    for j in range(pivot.confirmed_index + 1, len(candles)):
        close = candles[j].close
        if (pivot.kind is PivotKind.HIGH and close > level) or (
            pivot.kind is PivotKind.LOW and close < level
        ):
            return j
    return None
