"""Directional runs, Spike confirmation, Origin and extension (SPK-01..04; V5.1 B02-B04, B29).

A directional run (V5.2 B29) for a side is a maximal sequence of consecutive, real
(non-synthetic) finalized M1 candles where each continues the previous one (SEQ-01/02).
Long ends at the first M1 with Low[n] < Low[n-1]; that violating candle seeds the next
run. A synthetic no-trade minute or a DATA_GAP also ends the run and cannot seed one
(B31: synthetic candles are ineligible for P-Gap, Spike, Origin and sequence).

- A P-Gap counts only if the triple (i-1, i, i+1) lies inside one run, i.e. it satisfies the
  directional sequence itself (B04). V5.12: no P-Gap quality filter - every strict geometric
  P-Gap qualifies. pgap.assess_pgap still MEASURES the V5.9 impulse/gap values for the record
  and Diagnostics, but they never reject anything.
- Only the first P-Gap of a run may create a candidate. Any later P-Gap of the
  same run is log-only. If the first qualifying one arrives while capacity is busy or data
  is not usable, the run is spent (B03).
- Origin = the first candle of the run; this is exactly the backtracking of SPK-02, which
  stops at the first violation or missing candle. It is frozen at confirmation.
- Extension (B02): while the setup is pre-PullbackStart, every finalized M1 that continues
  the sequence from LastSpikeCandle becomes the new LastSpikeCandle. A synthetic minute
  or a DATA_GAP ends the sequence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sp2l.core.types import Candle, Side
from sp2l.marketdata.m1_builder import MINUTE, M1Result
from sp2l.strategy.pgap import PGapQuality, assess_pgap, detect_pgap
from sp2l.strategy.sequence import continues


@dataclass(frozen=True, slots=True)
class PGapSignal:
    side: Side
    left: Candle
    middle: Candle
    right: Candle
    origin: Candle
    run_candles: tuple[Candle, ...]  # origin .. right
    first_in_run: bool  # first P-Gap of the run (V5.12: every geometric P-Gap qualifies)
    quality: PGapQuality


@dataclass(slots=True)
class _Run:
    candles: list[Candle] = field(default_factory=list)
    spent: bool = False


class RunTracker:
    """Feeds on every M1 result and reports P-Gaps with their run context, per side."""

    def __init__(self, tick: Decimal) -> None:
        self.tick = tick
        self._runs: dict[Side, _Run] = {Side.LONG: _Run(), Side.SHORT: _Run()}
        self._last: Candle | None = None

    def on_m1(self, m1: M1Result) -> list[PGapSignal]:
        if not m1.tradeable or m1.candle is None:
            self._runs = {Side.LONG: _Run(), Side.SHORT: _Run()}
            self._last = None
            return []
        c = m1.candle
        contiguous = self._last is not None and c.open_time - self._last.open_time == MINUTE
        signals: list[PGapSignal] = []
        for side, run in self._runs.items():
            if run.candles and contiguous and continues(side, run.candles[-1], c):
                run.candles.append(c)
            else:
                self._runs[side] = run = _Run([c])
            if len(run.candles) >= 3:
                left, middle, right = run.candles[-3:]
                if detect_pgap(left, middle, right) is side:
                    q = assess_pgap(side, left, middle, right, self.tick)
                    qualifying = True  # V5.12: quality is measured and recorded, never a gate
                    signals.append(
                        PGapSignal(
                            side,
                            left,
                            middle,
                            right,
                            run.candles[0],
                            tuple(run.candles),
                            first_in_run=qualifying and not run.spent,
                            quality=q,
                        )
                    )
                    if qualifying:
                        run.spent = True
        self._last = c
        return signals


@dataclass(slots=True)
class Spike:
    """The frozen Origin plus the Spike candles Origin..LastSpikeCandle of one setup."""

    side: Side
    candles: list[Candle]
    extendable: bool = True

    @property
    def origin(self) -> Candle:
        return self.candles[0]

    @property
    def last(self) -> Candle:
        return self.candles[-1]

    def try_extend(self, m1: M1Result) -> bool:
        """B02: extend by one finalized M1 if it continues the sequence. Pre-PullbackStart only."""
        if not self.extendable:
            return False
        c = m1.candle
        if (
            not m1.tradeable
            or c is None
            or c.open_time - self.last.open_time != MINUTE
            or not continues(self.side, self.last, c)
        ):
            self.extendable = False
            return False
        self.candles.append(c)
        return True

    def freeze(self) -> None:
        self.extendable = False

    @property
    def extreme(self) -> Decimal:
        """SpikeExtreme (EXH §5): max High (Long) / min Low (Short) through LastSpikeCandle."""
        if self.side is Side.LONG:
            return max(c.high for c in self.candles)
        return min(c.low for c in self.candles)

    @property
    def range(self) -> Decimal:
        """SpikeRange_M1 (EXH §6) over Origin..LastSpikeCandle."""
        return max(c.high for c in self.candles) - min(c.low for c in self.candles)
