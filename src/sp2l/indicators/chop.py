"""CHOP14 (CTX-07): 100 * log10(sum(TR_n) / (HH_n - LL_n)) / log10(n).

The window is the n bars ending at the context bar (inclusive). TR needs the previous
close, so the first value is at bar n. HH == LL -> UNKNOWN (fail closed).
"""

from __future__ import annotations

import decimal
from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal

from sp2l.core.numeric import DECISION_CONTEXT
from sp2l.core.series import M5
from sp2l.core.types import Candle
from sp2l.indicators import UNKNOWN, Unknown
from sp2l.indicators.wilder import tr_series


def chop_series(
    candles: Sequence[Candle], period: int = 14, step: timedelta = M5
) -> list[Decimal | None | Unknown]:
    trs = tr_series(candles, step)
    out: list[Decimal | None | Unknown] = [None] * len(candles)
    with decimal.localcontext(DECISION_CONTEXT):
        log_n = Decimal(period).log10()
        for i in range(period, len(candles)):
            window = candles[i - period + 1 : i + 1]
            hh = max(c.high for c in window)
            ll = min(c.low for c in window)
            if hh == ll:
                out[i] = UNKNOWN
                continue
            tr_sum = Decimal(0)
            for t in trs[i - period + 1 : i + 1]:
                assert t is not None
                tr_sum += t
            out[i] = Decimal(100) * (tr_sum / (hh - ll)).log10() / log_n
    return out
