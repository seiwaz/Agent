"""Diagnostic replay of the backtest with switchable execution readings.

`simulate` re-runs the orders of `backtest.orders` with the same capacity rule as
`backtest.run`, walking each order through M1 bars with `walk`. With the default options
(`fill="touch"`, `stop_first=True`, `tp_in_fill=False`) it reproduces the lifecycle exactly
(tests/smc/test_diagnostics.py); the options are the alternative readings to measure:

- fill: "touch" = a long limit fills when low <= entry (the engine's rule); "through" = only
  when low < entry (price traded beyond the resting order).
- stop_first: a minute that reached both the stop and the target is read as the stop (engine)
  or as the target.
- tp_in_fill: the target may also be credited in the fill minute.

Every walked trade records MFE / MAE (in price R = stop distance), whether its fill was a bare
touch, and the R it would have had under the other intrabar readings. Nothing here changes
strategy logic; the context managers below patch module state only inside a `with` block.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

from sp2l.core.types import Candle, Side
from sp2l.smc import strategy, timeframes
from sp2l.smc.backtest import orders
from sp2l.smc.lifecycle import State, Tracked, risk_unit
from sp2l.smc.model import Analysis, Costs, Setup, SmcParams
from sp2l.smc.strategy import last_closed, net_r, tracked
from sp2l.smc.timeframes import MINUTE


@dataclass(slots=True)
class Walked:
    setup: Setup
    t: Tracked
    touch_only: bool = False  # filled on a minute whose extreme only equalled the entry
    both_minute: bool = False  # a minute reached stop and target; read as `stop_first` says
    alt_both_r: Decimal | None = None  # R had that minute been read the other way
    tp_in_fill_minute: bool = False  # the fill minute also reached the target
    alt_fill_r: Decimal | None = None  # R had the fill-minute target been credited
    mfe_r: Decimal | None = None  # best excursion after the fill, in stop distances
    mae_r: Decimal | None = None  # worst excursion after the fill, in stop distances
    first_reach: dict[str, bool] | None = None  # losers: +1R / +1.5R / +2R before the stop

    @property
    def gross_r(self) -> Decimal | None:
        """Price R of the exit before any cost: move / stop distance."""
        t = self.t
        if t.exit_price is None:
            return None
        sgn = 1 if t.side is Side.LONG else -1
        return (t.exit_price - t.entry) * sgn / abs(t.entry - (t.sl0 or t.sl))


def _r_at(t: Tracked, price: Decimal, costs: Costs) -> Decimal:
    return t.net_per_unit(price, costs) / t.risk


def _close(t: Tracked, state: State, price: Decimal, at: datetime, costs: Costs) -> None:
    from sp2l.smc.lifecycle import _close as close

    close(t, state, price, at, costs)


def walk(
    s: Setup,
    bars: Sequence[Candle],
    i0: int,
    p: SmcParams,
    costs: Costs,
    *,
    fill: str = "touch",
    stop_first: bool = True,
    tp_in_fill: bool = False,
) -> Walked:
    t = tracked(s, p, costs)
    w = Walked(s, t)
    long = t.side is Side.LONG
    one_r = abs(t.entry - t.sl)
    hi_seen: Decimal | None = None
    lo_seen: Decimal | None = None
    marks = {"1R": Decimal(1), "1.5R": Decimal("1.5"), "2R": Decimal(2)}
    reached = dict.fromkeys(marks, False)
    i = i0
    while i < len(bars) and t.active:
        b = bars[i]
        i += 1
        end = b.open_time + MINUTE
        t.last_m1 = b.open_time
        stop = b.low <= t.sl if long else b.high >= t.sl
        tgt = b.high >= t.tp if long else b.low <= t.tp
        if t.state is State.PENDING:
            through = b.low < t.entry if long else b.high > t.entry
            touch = b.low <= t.entry if long else b.high >= t.entry
            if through if fill == "through" else touch:
                t.state, t.filled_at = State.OPEN, b.open_time
                w.touch_only = touch and not through
                hi_seen, lo_seen = b.high, b.low
                if tgt:
                    w.tp_in_fill_minute = True
                    w.alt_fill_r = _r_at(t, t.tp, costs)
                if stop:
                    slip = t.sl * costs.slippage
                    _close(t, State.SL, t.sl - slip if long else t.sl + slip, end, costs)
                elif tgt and tp_in_fill:
                    _close(t, State.TP, t.tp, end, costs)
                continue
            if tgt:
                t.state, t.closed_at = State.MISSED, end
            elif b.open_time >= t.created_at + timedelta(minutes=p.pending_expiry_min):
                t.state, t.closed_at = State.EXPIRED, end
            continue
        hi_seen = b.high if hi_seen is None else max(hi_seen, b.high)
        lo_seen = b.low if lo_seen is None else min(lo_seen, b.low)
        fav = (hi_seen - t.entry) if long else (t.entry - lo_seen)
        if not stop:
            for k, m in marks.items():
                if fav >= m * one_r:
                    reached[k] = True
        slip = t.sl * costs.slippage
        stop_px = t.sl - slip if long else t.sl + slip
        if stop and tgt:
            w.both_minute = True
            if stop_first:
                w.alt_both_r = _r_at(t, t.tp, costs)
                _close(t, State.SL, stop_px, end, costs)
            else:
                w.alt_both_r = _r_at(t, stop_px, costs)
                _close(t, State.TP, t.tp, end, costs)
            continue
        if stop:
            _close(t, State.SL, stop_px, end, costs)
            continue
        if tgt:
            _close(t, State.TP, t.tp, end, costs)
            continue
        assert t.filled_at is not None
        held = b.open_time - t.filled_at
        if t.time_stop_min > 0 and held >= timedelta(minutes=t.time_stop_min):
            _close(t, State.TIME_STOP, b.close, end, costs)
        elif held >= timedelta(minutes=p.max_hold_min):
            _close(t, State.TIMEOUT, b.close, end, costs)
    if t.filled_at is not None and hi_seen is not None and lo_seen is not None:
        w.mfe_r = ((hi_seen - t.entry) if long else (t.entry - lo_seen)) / one_r
        w.mae_r = ((t.entry - lo_seen) if long else (hi_seen - t.entry)) / one_r
        w.first_reach = reached
    return w


def simulate(
    ctx: dict[str, Analysis],
    p: SmcParams,
    costs: Costs,
    *,
    setups: Sequence[Setup] | None = None,
    fill: str = "touch",
    stop_first: bool = True,
    tp_in_fill: bool = False,
) -> tuple[list[Setup], list[Walked]]:
    """backtest.run with switchable readings: (all evaluated orders, walked trades)."""
    found = list(setups) if setups is not None else orders(ctx, p, costs)
    bars = ctx["1m"].bars
    index = {c.open_time: i for i, c in enumerate(bars)}
    out: list[Walked] = []
    busy: list[datetime] = []
    for s in sorted((s for s in found if s.accepted), key=lambda s: s.created_at):
        busy = [b for b in busy if b > s.created_at]
        if len(busy) >= p.max_active:
            continue
        i0 = index.get(s.created_at)
        if i0 is None:
            continue
        w = walk(s, bars, i0, p, costs, fill=fill, stop_first=stop_first, tp_in_fill=tp_in_fill)
        busy.append(w.t.closed_at or bars[-1].open_time + MINUTE)
        out.append(w)
    return found, out


def with_stop_buffer(
    s: Setup, ctx: Mapping[str, Analysis], p: SmcParams, costs: Costs, atr_mult: Decimal
) -> Setup:
    """The same order with its stop moved `atr_mult` x ATR(zone TF) further away (diagnostic):
    the target stays; the net R and the LOW_NET_RR reason are recomputed."""
    if s.entry is None or s.sl is None or s.tp is None or atr_mult == 0:
        return s
    za = ctx[p.zone_tf]
    atr = za.atr[last_closed(za, s.created_at)] or Decimal(0)
    long = s.direction is Side.LONG
    raw = s.sl - atr_mult * atr if long else s.sl + atr_mult * atr
    sl = (raw / p.tick).to_integral_value(
        rounding=ROUND_FLOOR if long else "ROUND_CEILING"
    ) * p.tick
    ru = risk_unit(s.direction, s.entry, sl, costs, market=s.market)
    tp = replace(s.tp, net_r=net_r(s.direction, s.entry, s.tp.price, ru, costs, s.market))
    reasons = [r for r in s.reasons if r != "LOW_NET_RR"]
    if p.min_net_rr > 0 and tp.net_r < p.min_net_rr:
        reasons.append("LOW_NET_RR")
    return replace(s, sl=sl, tp=tp, reasons=tuple(reasons), accepted=not reasons)


@contextlib.contextmanager
def utc_grid() -> Iterator[None]:
    """1h / 4h bars on the UTC grid (hh:00) instead of Tehran's (hh:30), inside the block."""
    saved = dict(timeframes.TIMEFRAMES)
    timeframes.TIMEFRAMES["1h"] = (timedelta(hours=1), timedelta(0))
    timeframes.TIMEFRAMES["4h"] = (timedelta(hours=4), timedelta(0))
    try:
        yield
    finally:
        timeframes.TIMEFRAMES.clear()
        timeframes.TIMEFRAMES.update(saved)


@contextlib.contextmanager
def bias_ignored(record: dict[str, int]) -> Iterator[None]:
    """Every setup passes the bias check inside the block; `record[key]` gets the real bias
    (+1 / -1 / 0) seen at evaluation, so results can be split with / against it."""
    real_trend, real_map = strategy.trend_at, strategy.TREND

    def trend(a: Analysis | None, t: datetime) -> int:
        return 2

    real_eval = strategy.evaluate

    def evaluate(s, ctx, p, costs, t, **kw):  # type: ignore[no-untyped-def]
        record[s.key] = real_trend(ctx.get(p.bias_tf), t)
        return real_eval(s, ctx, p, costs, t, **kw)

    import sp2l.smc.backtest as bt

    bt_eval = getattr(bt, "evaluate")  # noqa: B009 - the name the backtest module calls
    setattr(strategy, "trend_at", trend)  # noqa: B010
    strategy.TREND = {Side.LONG: 2, Side.SHORT: 2}
    setattr(bt, "evaluate", evaluate)  # noqa: B010
    try:
        yield
    finally:
        setattr(strategy, "trend_at", real_trend)  # noqa: B010
        strategy.TREND = real_map
        setattr(bt, "evaluate", bt_eval)  # noqa: B010
