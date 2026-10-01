"""Incremental (one-bar-at-a-time) ATR14, ADX14, EMA20 and CHOP14.

Each class reproduces the batch reference in wilder.py / ema.py / chop.py exactly
(tests/golden/test_incremental.py), so a live or replay engine never recomputes the full
history and a restart can resume from persisted state.
"""

from __future__ import annotations

import decimal
from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from sp2l.core.numeric import DECISION_CONTEXT
from sp2l.core.types import Candle
from sp2l.indicators import UNKNOWN, Unknown
from sp2l.indicators.wilder import AdxPoint, directional_movement, true_range

Value = Decimal | None | Unknown


class AtrState:
    def __init__(self, period: int = 14) -> None:
        self.period = period
        self._prev: Candle | None = None
        self._seed: list[Decimal] = []
        self.value: Decimal | None = None
        self.last_tr: Decimal | None = None

    def update(self, c: Candle) -> Decimal | None:
        with decimal.localcontext(DECISION_CONTEXT):
            if self._prev is None:
                self._prev = c
                return None
            tr = true_range(self._prev.close, c)
            self.last_tr = tr
            self._prev = c
            if self.value is None:
                self._seed.append(tr)
                if len(self._seed) == self.period:
                    self.value = sum(self._seed, Decimal(0)) / self.period
                return self.value
            self.value = (self.value * (self.period - 1) + tr) / self.period
            return self.value


class EmaState:
    def __init__(self, period: int = 20) -> None:
        self.period = period
        self._seed: list[Decimal] = []
        self.value: Decimal | None = None

    def update(self, x: Decimal) -> Decimal | None:
        with decimal.localcontext(DECISION_CONTEXT):
            if self.value is None:
                self._seed.append(x)
                if len(self._seed) == self.period:
                    self.value = sum(self._seed, Decimal(0)) / self.period
                return self.value
            alpha = Decimal(2) / Decimal(self.period + 1)
            self.value = alpha * x + (1 - alpha) * self.value
            return self.value


class AdxState:
    """TA-Lib seeding (V5.1 B06+): sums seeded with n-1 values."""

    def __init__(self, period: int = 14) -> None:
        self.period = period
        self._prev: Candle | None = None
        self._n = 0  # number of bars seen
        self._s_tr = Decimal(0)
        self._s_pdm = Decimal(0)
        self._s_mdm = Decimal(0)
        self._dx_window: list[Decimal] = []
        self._adx: Value = None
        self._poisoned = False
        self.point = AdxPoint(None, None, None, None, None, None, None)

    def update(self, c: Candle) -> AdxPoint:
        n = self.period
        i = self._n
        self._n += 1
        prev = self._prev
        self._prev = c
        if prev is None:
            return self.point
        with decimal.localcontext(DECISION_CONTEXT):
            pdm, mdm = directional_movement(prev, c)
            tr = true_range(prev.close, c)
            if i <= n - 1:
                self._s_tr += tr
                self._s_pdm += pdm
                self._s_mdm += mdm
                return self.point
            self._s_tr = self._s_tr - self._s_tr / n + tr
            self._s_pdm = self._s_pdm - self._s_pdm / n + pdm
            self._s_mdm = self._s_mdm - self._s_mdm / n + mdm
            plus_di: Value
            minus_di: Value
            dx: Value
            if self._s_tr == 0:
                plus_di = minus_di = dx = UNKNOWN
            else:
                plus_di = Decimal(100) * self._s_pdm / self._s_tr
                minus_di = Decimal(100) * self._s_mdm / self._s_tr
                di_sum = plus_di + minus_di
                dx = UNKNOWN if di_sum == 0 else Decimal(100) * abs(plus_di - minus_di) / di_sum
            if isinstance(dx, Unknown):
                self._poisoned = True
            if self._poisoned:
                self._adx = UNKNOWN
            else:
                assert isinstance(dx, Decimal)
                if len(self._dx_window) < n:
                    self._dx_window.append(dx)
                    if len(self._dx_window) == n:
                        self._adx = sum(self._dx_window, Decimal(0)) / n
                else:
                    assert isinstance(self._adx, Decimal)
                    self._adx = (self._adx * (n - 1) + dx) / n
            self.point = AdxPoint(
                self._s_tr, self._s_pdm, self._s_mdm, plus_di, minus_di, dx, self._adx
            )
            return self.point


@dataclass(slots=True)
class _ChopBar:
    tr: Decimal
    high: Decimal
    low: Decimal


class ChopState:
    def __init__(self, period: int = 14) -> None:
        self.period = period
        self._prev: Candle | None = None
        self._window: deque[_ChopBar] = deque(maxlen=period)
        self.value: Value = None

    def update(self, c: Candle) -> Value:
        prev = self._prev
        self._prev = c
        if prev is None:
            return None
        self._window.append(_ChopBar(true_range(prev.close, c), c.high, c.low))
        if len(self._window) < self.period:
            return None
        with decimal.localcontext(DECISION_CONTEXT):
            hh = max(b.high for b in self._window)
            ll = min(b.low for b in self._window)
            if hh == ll:
                self.value = UNKNOWN
                return self.value
            tr_sum = sum((b.tr for b in self._window), Decimal(0))
            self.value = Decimal(100) * (tr_sum / (hh - ll)).log10() / Decimal(self.period).log10()
            return self.value
