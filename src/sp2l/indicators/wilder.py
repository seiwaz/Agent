"""Wilder True Range, ATR14 and ADX14 (CTX-06, EXH-01).

Seeding (plan §7, textbook Wilder):
- TR exists from bar 1 (it needs the previous close); bar 0 has no TR.
- ATR_first at bar n = mean(TR_1..TR_n); then ATR_i = ((n-1)*ATR_{i-1} + TR_i) / n.
- ADX (canonical = TA-Lib, V5.1 B06/B06+): Wilder running sums for TR, +DM, -DM are
  seeded with sum(x_1..x_{n-1}); from bar n on S_i = S_{i-1} - S_{i-1}/n + x_i. DI/DX from
  bar n. ADX_first at bar 2n-1 = mean(DX_n..DX_{2n-1}); then ADX_i = ((n-1)*ADX_{i-1} + DX_i)/n.
  Verified bit-for-bit against TA-Lib 0.8 in tests/golden. There is no alternative
  implementation and no runtime switch.
"""

from __future__ import annotations

import decimal
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from sp2l.core.numeric import DECISION_CONTEXT
from sp2l.core.series import M5, require_contiguous
from sp2l.core.types import Candle
from sp2l.indicators import UNKNOWN, Unknown

Value = Decimal | None | Unknown


def true_range(prev_close: Decimal, c: Candle) -> Decimal:
    return max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))


def directional_movement(prev: Candle, cur: Candle) -> tuple[Decimal, Decimal]:
    """(+DM, -DM). Ties and non-positive moves give 0 (standard Wilder)."""
    up = cur.high - prev.high
    down = prev.low - cur.low
    plus_dm = up if (up > down and up > 0) else Decimal(0)
    minus_dm = down if (down > up and down > 0) else Decimal(0)
    return plus_dm, minus_dm


def tr_series(candles: Sequence[Candle], step: timedelta = M5) -> list[Decimal | None]:
    require_contiguous(candles, step)
    out: list[Decimal | None] = [None] * len(candles)
    for i in range(1, len(candles)):
        out[i] = true_range(candles[i - 1].close, candles[i])
    return out


def atr_series(
    candles: Sequence[Candle], period: int = 14, step: timedelta = M5
) -> list[Decimal | None]:
    trs = tr_series(candles, step)
    out: list[Decimal | None] = [None] * len(candles)
    if len(candles) <= period:
        return out
    with decimal.localcontext(DECISION_CONTEXT):
        seed_trs = [t for t in trs[1 : period + 1] if t is not None]
        atr = sum(seed_trs, Decimal(0)) / period
        out[period] = atr
        for i in range(period + 1, len(candles)):
            tr = trs[i]
            assert tr is not None
            atr = (atr * (period - 1) + tr) / period
            out[i] = atr
    return out


@dataclass(frozen=True, slots=True)
class AdxPoint:
    tr_s: Decimal | None
    plus_dm_s: Decimal | None
    minus_dm_s: Decimal | None
    plus_di: Value
    minus_di: Value
    dx: Value
    adx: Value


_EMPTY = AdxPoint(None, None, None, None, None, None, None)


def adx_series(
    candles: Sequence[Candle],
    period: int = 14,
    step: timedelta = M5,
) -> list[AdxPoint]:
    require_contiguous(candles, step)
    n = len(candles)
    out: list[AdxPoint] = [_EMPTY] * n
    if n <= period:
        return out
    with decimal.localcontext(DECISION_CONTEXT):
        s_tr = s_pdm = s_mdm = Decimal(0)
        seed_last = period - 1  # TA-Lib seeding
        for i in range(1, seed_last + 1):
            pdm, mdm = directional_movement(candles[i - 1], candles[i])
            s_tr += true_range(candles[i - 1].close, candles[i])
            s_pdm += pdm
            s_mdm += mdm

        dx_window: list[Decimal] = []
        adx: Value = None
        poisoned = False
        for i in range(period, n):
            if i > seed_last:
                pdm, mdm = directional_movement(candles[i - 1], candles[i])
                s_tr = s_tr - s_tr / period + true_range(candles[i - 1].close, candles[i])
                s_pdm = s_pdm - s_pdm / period + pdm
                s_mdm = s_mdm - s_mdm / period + mdm

            plus_di: Value
            minus_di: Value
            dx: Value
            if s_tr == 0:
                plus_di = minus_di = dx = UNKNOWN
            else:
                plus_di = Decimal(100) * s_pdm / s_tr
                minus_di = Decimal(100) * s_mdm / s_tr
                di_sum = plus_di + minus_di
                dx = UNKNOWN if di_sum == 0 else Decimal(100) * abs(plus_di - minus_di) / di_sum

            if isinstance(dx, Unknown):
                poisoned = True
            if poisoned:
                adx = UNKNOWN
            else:
                assert isinstance(dx, Decimal)
                if len(dx_window) < period:
                    dx_window.append(dx)
                    if len(dx_window) == period:
                        adx = sum(dx_window, Decimal(0)) / period
                else:
                    assert isinstance(adx, Decimal)
                    adx = (adx * (period - 1) + dx) / period
            out[i] = AdxPoint(s_tr, s_pdm, s_mdm, plus_di, minus_di, dx, adx)
    return out
