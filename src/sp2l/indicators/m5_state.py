"""Incremental M5 context state over one continuous, anchored segment.

- The segment starts at an anchor (fixed per symbol). A DATA_GAP (or a silent hole) ends
  it; the next OK bar starts a new anchor and every recursive indicator restarts
  (V5.1 B06, B25; V5.2 B31).
- B33: bars with synthetic no-trade minutes are valid for indicators, range, liquidity and
  (if they contain at least one real trade) pivots. An all-synthetic bar can never be a
  pivot centre; it still acts as a neighbour for other bars (PROVISIONAL B37).
- `warm` becomes true only once the segment holds `warmup_bars` (150) finalized bars.
- Pivots are confirmed two bars after the pivot bar. Their M5-close break index is tracked.
- Trend per bar uses the last two confirmed swing highs and lows. A DUAL pivot among the
  pivots used makes the trend INVALID_DUAL_PIVOT (fail closed, B11).
- The trend history supports TrendAgeBars.
- V5.8 readiness is split: PRICE_CONTEXT_READY = the segment holds 150 price bars (TABDEAL
  HISTORY bars count: they carry OHLCV); LIQUIDITY_CONTEXT_READY = the context bar and the 20
  bars before it all have a KNOWN trade count (history bars do not; nothing is invented).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from fractions import Fraction

from sp2l.core.types import Candle
from sp2l.indicators import Unknown
from sp2l.indicators.incremental import AdxState, AtrState, ChopState, EmaState
from sp2l.indicators.pivots import PivotKind
from sp2l.indicators.regime import Regime, classify_regime
from sp2l.indicators.trend import Trend, classify_trend
from sp2l.indicators.wilder import AdxPoint
from sp2l.marketdata.m5_aggregator import M5_STEP, M5Result, M5Status

DEFAULT_WARMUP = 150


@dataclass(slots=True)
class SegPivot:
    """A confirmed pivot inside the current segment (indices are segment indices)."""

    index: int
    kind: PivotKind
    high: Decimal
    low: Decimal
    confirmed_index: int
    high_broken_index: int | None = None  # first M5 close strictly above `high`
    low_broken_index: int | None = None  # first M5 close strictly below `low`

    @property
    def has_high(self) -> bool:
        return self.kind in (PivotKind.HIGH, PivotKind.DUAL)

    @property
    def has_low(self) -> bool:
        return self.kind in (PivotKind.LOW, PivotKind.DUAL)


@dataclass(frozen=True, slots=True)
class M5Bar:
    index: int
    candle: Candle
    atr14: Decimal | None
    adx: AdxPoint
    ema20: Decimal | None
    chop14: Decimal | None | Unknown
    regime: Regime
    trend: Trend
    synthetic_m1_count: int = 0  # B33 quality fields

    @property
    def synthetic_fraction(self) -> Fraction:
        return Fraction(self.synthetic_m1_count, 5)

    @property
    def real_trade_count(self) -> int | None:
        return self.candle.trade_count

    @property
    def all_synthetic(self) -> bool:
        return self.candle.synthetic

    @property
    def open_time(self) -> datetime:
        return self.candle.open_time

    @property
    def close_time(self) -> datetime:
        return self.candle.open_time + M5_STEP


@dataclass(slots=True)
class Segment:
    anchor_open_time: datetime
    bars: list[M5Bar] = field(default_factory=list)
    pivots: list[SegPivot] = field(default_factory=list)


class M5State:
    def __init__(self, warmup_bars: int = DEFAULT_WARMUP) -> None:
        self.warmup_bars = warmup_bars
        self.segment: Segment | None = None
        self.anchors: list[datetime] = []
        self.breaks: list[tuple[datetime, M5Status]] = []
        self._reset_indicators()

    def _reset_indicators(self) -> None:
        self._atr = AtrState(14)
        self._adx = AdxState(14)
        self._ema = EmaState(20)
        self._chop = ChopState(14)

    @property
    def warm(self) -> bool:
        return self.segment is not None and len(self.segment.bars) >= self.warmup_bars

    @property
    def price_ready(self) -> bool:
        """PRICE_CONTEXT_READY (the 150-bar warmup; unchanged)."""
        return self.warm

    LIQ_BARS = 21  # context bar L + the 20 reference bars before it (CE §12)

    def liquidity_bars_missing(self) -> int:
        """M5 bars still needed until LIQUIDITY_CONTEXT_READY (0 = ready)."""
        seg = self.segment
        if seg is None:
            return self.LIQ_BARS
        n = len(seg.bars)
        unknown = [i for i, b in enumerate(seg.bars) if b.candle.trade_count is None]
        need_len = max(0, self.LIQ_BARS - n)
        need_after = max(0, unknown[-1] + self.LIQ_BARS - (n - 1)) if unknown else 0
        return max(need_len, need_after)

    @property
    def liquidity_ready(self) -> bool:
        return self.liquidity_bars_missing() == 0

    @property
    def last(self) -> M5Bar | None:
        if self.segment is None or not self.segment.bars:
            return None
        return self.segment.bars[-1]

    def rebuild(self, history: list[M5Result]) -> None:
        """Re-derive the segment from contiguous stored bars after a validated late repair
        healed a break (exactly what priming a new session does; nothing is invented)."""
        self.segment = None
        self._reset_indicators()
        for r in history:
            self.add(r)

    def add(self, result: M5Result) -> M5Bar | None:
        if result.status is not M5Status.OK or result.candle is None:
            self.breaks.append((result.open_time, result.status))
            self.segment = None
            self._reset_indicators()
            return None
        c = result.candle
        if self.segment is not None:
            expected = self.segment.bars[-1].open_time + M5_STEP
            if c.open_time != expected:
                # a silent hole is a break, never bridged
                self.breaks.append((expected, M5Status.MISSING))
                self.segment = None
                self._reset_indicators()
        if self.segment is None:
            self.segment = Segment(anchor_open_time=c.open_time)
            self.anchors.append(c.open_time)
        seg = self.segment
        k = len(seg.bars)
        atr = self._atr.update(c)
        adx = self._adx.update(c)
        ema = self._ema.update(c.close)
        chop = self._chop.update(c)
        candles = [b.candle for b in seg.bars] + [c]
        self._confirm_pivot(seg, candles, k)
        self._update_breaks(seg, c, k)
        trend = self._trend_at(seg, k)
        regime = classify_regime(chop, adx.adx)
        bar = M5Bar(k, c, atr, adx, ema, chop, regime, trend, result.synthetic_m1_count)
        seg.bars.append(bar)
        return bar

    @staticmethod
    def _confirm_pivot(seg: Segment, candles: list[Candle], k: int) -> None:
        i = k - 2
        if i < 2 or candles[i].synthetic:  # B33: an all-synthetic bar is never a pivot
            return
        h, lo = candles[i].high, candles[i].low
        nb = (i - 2, i - 1, i + 1, i + 2)
        is_high = all(h > candles[j].high for j in nb)
        is_low = all(lo < candles[j].low for j in nb)
        if not (is_high or is_low):
            return
        kind = (
            PivotKind.DUAL if is_high and is_low else (PivotKind.HIGH if is_high else PivotKind.LOW)
        )
        seg.pivots.append(SegPivot(i, kind, h, lo, k))

    @staticmethod
    def _update_breaks(seg: Segment, c: Candle, k: int) -> None:
        for p in seg.pivots:
            if k <= p.confirmed_index:
                continue
            if p.has_high and p.high_broken_index is None and c.close > p.high:
                p.high_broken_index = k
            if p.has_low and p.low_broken_index is None and c.close < p.low:
                p.low_broken_index = k

    @staticmethod
    def _trend_at(seg: Segment, k: int) -> Trend:
        confirmed = [p for p in seg.pivots if p.confirmed_index <= k]
        highs = [p for p in confirmed if p.has_high]
        lows = [p for p in confirmed if p.has_low]
        used = highs[-2:] + lows[-2:]
        if len(highs) >= 2 and len(lows) >= 2 and any(p.kind is PivotKind.DUAL for p in used):
            return Trend.INVALID_DUAL_PIVOT
        return classify_trend([p.high for p in highs], [p.low for p in lows])

    # ---- queries used by the Context / Exhaustion engines -------------------------

    def bar_at_close(self, close_time: datetime) -> M5Bar | None:
        """The segment bar whose close_time equals `close_time`, if present."""
        seg = self.segment
        if seg is None or not seg.bars:
            return None
        k = int((close_time - M5_STEP - seg.anchor_open_time) / M5_STEP)
        if 0 <= k < len(seg.bars) and seg.bars[k].close_time == close_time:
            return seg.bars[k]
        return None

    def trend_age(self, direction: Trend, ctx_index: int) -> int:
        """EXH-03: bars from the change into `direction` through ctx inclusive; 0 if not aligned."""
        seg = self.segment
        if seg is None or seg.bars[ctx_index].trend is not direction:
            return 0
        k = ctx_index
        while k > 0 and seg.bars[k - 1].trend is direction:
            k -= 1
        return ctx_index - k + 1
