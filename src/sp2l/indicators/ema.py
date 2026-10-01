"""EMA20_M5 (EXH-02): alpha = 2/(n+1), seeded by the SMA of the first n anchored closes.

Restart stability comes from persisting the recursive state per bar with its anchor and
resuming from it; this function is the from-anchor reference computation.
"""

from __future__ import annotations

import decimal
from collections.abc import Sequence
from decimal import Decimal

from sp2l.core.numeric import DECISION_CONTEXT


def ema_series(values: Sequence[Decimal], period: int = 20) -> list[Decimal | None]:
    out: list[Decimal | None] = [None] * len(values)
    if len(values) < period:
        return out
    with decimal.localcontext(DECISION_CONTEXT):
        alpha = Decimal(2) / Decimal(period + 1)
        ema = sum(values[:period], Decimal(0)) / period
        out[period - 1] = ema
        for i in range(period, len(values)):
            ema = alpha * values[i] + (1 - alpha) * ema
            out[i] = ema
    return out


def ema_step(prev: Decimal, value: Decimal, period: int = 20) -> Decimal:
    """One recursive step, used when resuming from persisted state."""
    with decimal.localcontext(DECISION_CONTEXT):
        alpha = Decimal(2) / Decimal(period + 1)
        return alpha * value + (1 - alpha) * prev
