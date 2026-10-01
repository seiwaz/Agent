"""History bootstrap: fill the canonical M1 series from Tabdeal's own futures chart feed.

Source: the same public, read-only endpoint as tier 3 (tabdeal_public.chart_history, the
TradingView datafeed of Tabdeal's futures page). One request returns the whole range at
resolution 1 (no paging). Bars follow the continuity model (open = previous close, H/L include
it) and carry NO trade count; the bar of the current minute is still forming.

This module is pure: it turns one chart response into the minutes to store. The database
step (runtime/reconcile.bootstrap_history) validates the response against our own live
canonical minutes before anything is written, and fails closed.

Minutes the chart leaves out (it omits minutes without trades) - decided 2026-09-30:
- a run of 1..`max_fill_gap` missing minutes between two known minutes becomes a synthetic
  no-trade minute (B31 semantics: O=H=L=C = previous close, volume 0, trade count 0);
- a longer run stays missing (DATA_GAP: its M5 bucket has no bar and the series breaks there).
Minutes already stored are never touched. Bootstrap minutes are OHLCV only: never used for
fills, PullbackStart, SL/TP order or intrabar chronology (CANDLE_HISTORY_REPAIR semantics).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sp2l.core.types import Candle
from sp2l.marketdata.history import parse_bars

MINUTE = timedelta(minutes=1)


@dataclass(slots=True)
class BootstrapPlan:
    candles: dict[datetime, Candle] = field(default_factory=dict)  # chart bars to store
    synthetic: dict[datetime, Candle] = field(default_factory=dict)  # filled short holes
    gaps: list[tuple[datetime, datetime]] = field(default_factory=list)  # left as DATA_GAP
    dropped_forming: int = 0
    chart_first: datetime | None = None
    chart_last: datetime | None = None

    @property
    def minutes(self) -> dict[datetime, Candle]:
        return {**self.candles, **self.synthetic}


def final_bars(
    raw: Iterable[Mapping[str, Any]], requested_at: datetime, settle: timedelta
) -> tuple[dict[datetime, Candle], int] | None:
    """Chart bars -> candles, without the bar still forming at request time.
    None if any bar is malformed (the whole response is then rejected)."""
    bars = parse_bars(raw)
    if bars is None:
        return None
    final = {t: c for t, c in bars.items() if t + MINUTE + settle <= requested_at}
    return final, len(bars) - len(final)


def plan_bootstrap(
    raw: Iterable[Mapping[str, Any]],
    start: datetime,
    end: datetime,
    stored_close: Mapping[datetime, Decimal],
    requested_at: datetime,
    *,
    settle: timedelta = timedelta(seconds=5),
    max_fill_gap: int = 2,
) -> BootstrapPlan | None:
    """Minutes to store in [start, end) that are not stored yet (`stored_close`: every stored
    minute near the range -> its close). None if the response is malformed."""
    got = final_bars(raw, requested_at, settle)
    if got is None:
        return None
    bars, dropped = got
    plan = BootstrapPlan(dropped_forming=dropped)
    inside = sorted(t for t in bars if start <= t < end)
    if not inside:
        return plan
    plan.chart_first, plan.chart_last = inside[0], inside[-1]

    def close_of(t: datetime) -> Decimal | None:
        if t in bars:
            return bars[t].close
        return stored_close.get(t)

    t = max(start, plan.chart_first)  # nothing exists before the market's first bar
    while t < end:
        if t in stored_close:
            t += MINUTE
            continue
        if t in bars:
            plan.candles[t] = bars[t]
            t += MINUTE
            continue
        run_start = t  # a run of minutes neither in the chart nor stored
        while t < end and t not in bars and t not in stored_close:
            t += MINUTE
        run = int((t - run_start) / MINUTE)
        prev = close_of(run_start - MINUTE)
        closed = t < end  # a known minute follows the run
        if closed and prev is not None and run <= max_fill_gap:
            for i in range(run):
                m = run_start + i * MINUTE
                plan.synthetic[m] = Candle(m, prev, prev, prev, prev, Decimal(0), 0, synthetic=True)
        else:
            plan.gaps.append((run_start, t - MINUTE))
    return plan
