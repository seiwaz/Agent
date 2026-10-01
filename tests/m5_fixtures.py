"""Hand-built M5 segment states so Context/Exhaustion boundaries can be hit exactly."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from decimal import Decimal as D

from sp2l.core.types import Candle
from sp2l.indicators.m5_state import M5Bar, M5State, Segment, SegPivot
from sp2l.indicators.pivots import PivotKind
from sp2l.indicators.regime import Regime
from sp2l.indicators.trend import Trend
from sp2l.indicators.wilder import AdxPoint
from tests.conftest import T0

M5 = timedelta(minutes=5)


def bar_time(k: int) -> datetime:
    return T0 + M5 * k


def make_state(
    n: int = 40,
    *,
    high: Callable[[int], str] = lambda k: "130",
    low: Callable[[int], str] = lambda k: "100",
    regime: Callable[[int], Regime] | Regime = Regime.TREND,
    trend: Callable[[int], Trend] | Trend = Trend.BULL,
    atr: str = "10",
    ema: str = "100",
    volume: Callable[[int], str] = lambda k: "10",
    trades: Callable[[int], int] = lambda k: 10,
    close: Callable[[int], str] | None = None,
    pivots: Sequence[SegPivot] = (),
    warmup: int = 1,
) -> M5State:
    st = M5State(warmup_bars=warmup)
    seg = Segment(anchor_open_time=T0)
    for k in range(n):
        h, lo = D(high(k)), D(low(k))
        cl = D(close(k)) if close else (h + lo) / 2
        c = Candle(bar_time(k), cl, h, lo, cl, D(volume(k)), trades(k))
        rg = regime(k) if callable(regime) else regime
        tr = trend(k) if callable(trend) else trend
        adx = AdxPoint(None, None, None, None, None, None, D("30"))
        seg.bars.append(M5Bar(k, c, D(atr), adx, D(ema), D("50"), rg, tr))
    seg.pivots = list(pivots)
    st.segment = seg
    return st


def eval_time(state: M5State) -> datetime:
    assert state.last is not None
    return state.last.close_time


def pivot_high(index: int, price: str, broken: int | None = None) -> SegPivot:
    return SegPivot(index, PivotKind.HIGH, D(price), D("0"), index + 2, high_broken_index=broken)


def pivot_low(index: int, price: str, broken: int | None = None) -> SegPivot:
    return SegPivot(index, PivotKind.LOW, D("1e9"), D(price), index + 2, low_broken_index=broken)
