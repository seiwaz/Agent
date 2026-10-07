"""Backtest of System B on daily bars (docs/TREND_STRATEGY.md).

Timing, with no look-ahead: every signal is decided on the CLOSE of day t from bars up to and
including t (the channels exclude day t itself), and filled at the OPEN of day t + 1. The stop
is a resting order from the moment of the fill: a day whose low reaches it exits at the stop,
or at the open when the day opened below it (gap), minus the slippage allowance. The stop is
checked before the close-based exit, and it also applies on the day of the fill.

Costs: the taker fee on every fill (entries and exits are market orders) and `slippage` as a
fraction of the price, against us, on every fill. Equity is marked at each daily close.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sp2l.core.types import Candle
from sp2l.trend.model import TrendParams

YEAR_DAYS = 365  # crypto trades every day


@dataclass(frozen=True, slots=True)
class Fees:
    fee: float = 0.0  # per fill, fraction of the notional
    slippage: float = 0.0  # per fill, fraction of the price, against us


@dataclass(slots=True)
class Trade:
    entry_i: int  # bar of the first fill
    entry_time: datetime
    entry_price: float  # average fill price over the units
    qty: float
    units: int
    risk: float  # planned loss at the initial stop(s), before costs (1R)
    stop: float  # the stop when the trade ended
    exit_time: datetime | None = None
    exit_price: float | None = None
    reason: str = "OPEN"  # STOP / CHANNEL / OPEN (still held on the last bar)
    pnl: float = 0.0  # net of fees and slippage
    fees: float = 0.0
    bars: int = 0

    @property
    def r(self) -> float:
        return self.pnl / self.risk if self.risk > 0 else 0.0

    @property
    def return_pct(self) -> float:
        basis = self.entry_price * self.qty
        return self.pnl / basis if basis > 0 else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_time": self.entry_time.date().isoformat(),
            "exit_time": None if self.exit_time is None else self.exit_time.date().isoformat(),
            "reason": self.reason,
            "units": self.units,
            "entry_price": round(self.entry_price, 2),
            "exit_price": None if self.exit_price is None else round(self.exit_price, 2),
            "stop": round(self.stop, 2),
            "qty": round(self.qty, 6),
            "pnl": round(self.pnl, 2),
            "fees": round(self.fees, 2),
            "r": round(self.r, 2),
            "return_pct": round(100 * self.return_pct, 2),
            "bars": self.bars,
        }


@dataclass(slots=True)
class Result:
    params: TrendParams
    times: list[datetime]
    close: list[float]
    equity: list[float]  # at each close
    in_market: list[bool]  # a position held at that close
    start: int  # first bar on which a signal could be decided
    trades: list[Trade] = field(default_factory=list)


def wilder_atr(h: Sequence[float], lo: Sequence[float], c: Sequence[float], n: int) -> list[float]:
    """ATR(n), Wilder smoothing; NaN until bar n (the first n true ranges are averaged)."""
    out = [math.nan] * len(c)
    tr = [h[0] - lo[0]] + [
        max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])) for i in range(1, len(c))
    ]
    if len(c) <= n:
        return out
    a = sum(tr[1 : n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(c)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def prior_max(x: Sequence[float], n: int) -> list[float]:
    """max(x[i-n:i]): the previous n bars, excluding bar i; NaN for i < n."""
    return [math.nan] * n + [max(x[i - n : i]) for i in range(n, len(x))]


def prior_min(x: Sequence[float], n: int) -> list[float]:
    return [math.nan] * n + [min(x[i - n : i]) for i in range(n, len(x))]


def sma(x: Sequence[float], n: int) -> list[float]:
    out = [math.nan] * len(x)
    s = 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def run(bars: Sequence[Candle], p: TrendParams, fees: Fees) -> Result:
    if len(bars) <= p.warmup + 1:
        raise ValueError(f"{len(bars)} daily bars; System B needs more than {p.warmup + 1}")
    t = [b.open_time for b in bars]
    o = [float(b.open) for b in bars]
    h = [float(b.high) for b in bars]
    lo = [float(b.low) for b in bars]
    c = [float(b.close) for b in bars]
    atr = wilder_atr(h, lo, c, p.atr_len)
    upper = prior_max(h, p.entry_len)
    lower = prior_min(lo, p.exit_len)
    ma = sma(c, p.regime_ma) if p.regime_ma else [math.nan] * len(c)

    cash, qty = p.initial_equity, 0.0
    units, n_atr, last_fill, stop = 0, 0.0, 0.0, 0.0
    pending: str | None = None  # "entry" / "add" / "exit", filled at the next open
    pending_n = 0.0
    trade: Trade | None = None
    trade_cash_before = 0.0  # cash before the trade's first fill: pnl = cash after - this
    res = Result(p, t, c, [], [], p.warmup)

    def close_out(i: int, px: float, reason: str) -> None:
        nonlocal cash, qty, units
        assert trade is not None
        fill = px * (1 - fees.slippage)
        fee = qty * fill * fees.fee
        cash += qty * fill - fee
        trade.fees += fee
        trade.exit_time, trade.exit_price, trade.reason = t[i], fill, reason
        trade.pnl = cash - trade_cash_before
        trade.bars = i - trade.entry_i
        trade.stop = stop
        res.trades.append(trade)
        qty, units = 0.0, 0

    for i in range(len(bars)):
        # 1. orders decided at yesterday's close fill at today's open
        if pending == "exit" and qty > 0:
            close_out(i, o[i], "CHANNEL")
            trade = None
        elif pending in ("entry", "add"):
            fill = o[i] * (1 + fees.slippage)
            equity = cash + qty * o[i]
            if pending == "entry":
                n_atr = pending_n
            room = max(0.0, p.max_exposure * equity - qty * fill)
            risk_qty = equity * p.risk_pct / (p.stop_atr * n_atr)
            want = risk_qty if p.sizing == "risk" else math.inf
            add = min(want, room / (fill * (1 + fees.fee)))
            if add > 0:
                fee = add * fill * fees.fee
                if trade is None:
                    trade_cash_before = cash
                    trade = Trade(i, t[i], fill, 0.0, 0, 0.0, 0.0)
                trade.entry_price = (trade.entry_price * trade.qty + fill * add) / (trade.qty + add)
                trade.qty += add
                trade.units += 1
                trade.risk += add * p.stop_atr * n_atr
                trade.fees += fee
                cash -= add * fill + fee
                qty += add
                units += 1
                last_fill = fill
                stop = fill - p.stop_atr * n_atr
        pending = None

        # 2. the resting stop, intraday (also on the day of the fill)
        if qty > 0 and lo[i] <= stop:
            close_out(i, min(o[i], stop), "STOP")
            trade = None

        # 3. mark to market at the close
        res.equity.append(cash + qty * c[i])
        res.in_market.append(qty > 0)

        # 4. signals on this close, for tomorrow's open
        if i < p.warmup or i == len(bars) - 1:
            continue
        if qty > 0:
            if c[i] < lower[i]:
                pending = "exit"
            elif units < p.max_units and c[i] >= last_fill + p.add_atr * n_atr:
                pending = "add"
        elif c[i] > upper[i] and atr[i] > 0 and (not p.regime_ma or c[i] > ma[i]):
            pending, pending_n = "entry", atr[i]

    if trade is not None:  # still held: marked at the last close, not closed
        trade.exit_price = c[-1]
        trade.pnl = res.equity[-1] - trade_cash_before
        trade.bars = len(bars) - 1 - trade.entry_i
        trade.stop = stop
        res.trades.append(trade)
    return res


# --- statistics -----------------------------------------------------------------------------


def curve_stats(times: Sequence[datetime], eq: Sequence[float]) -> dict[str, Any]:
    """Return, CAGR, volatility, Sharpe (rf 0), max drawdown of one equity curve."""
    if len(eq) < 2 or eq[0] <= 0:
        return {}
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    sd = math.sqrt(var)
    down = [min(0.0, r) for r in rets]
    dsd = math.sqrt(sum(d * d for d in down) / max(1, len(down) - 1))
    days = (times[-1] - times[0]).total_seconds() / 86400
    total = eq[-1] / eq[0] - 1
    cagr = (eq[-1] / eq[0]) ** (YEAR_DAYS / days) - 1 if days > 0 and eq[-1] > 0 else -1.0
    peak, mdd, mdd_at, peak_at = eq[0], 0.0, times[0], times[0]
    worst_peak = times[0]
    for ti, v in zip(times, eq, strict=True):
        if v > peak:
            peak, peak_at = v, ti
        dd = 1 - v / peak
        if dd > mdd:
            mdd, mdd_at, worst_peak = dd, ti, peak_at
    return {
        "from": times[0].date().isoformat(),
        "to": times[-1].date().isoformat(),
        "days": round(days),
        "total_return_pct": round(100 * total, 1),
        "cagr_pct": round(100 * cagr, 1),
        "vol_pct": round(100 * sd * math.sqrt(YEAR_DAYS), 1),
        "sharpe": round(mean / sd * math.sqrt(YEAR_DAYS), 2) if sd > 0 else None,
        "sortino": round(mean / dsd * math.sqrt(YEAR_DAYS), 2) if dsd > 0 else None,
        "max_drawdown_pct": round(100 * mdd, 1),
        "max_drawdown_peak": worst_peak.date().isoformat(),
        "max_drawdown_trough": mdd_at.date().isoformat(),
        "calmar": round(cagr / mdd, 2) if mdd > 0 else None,
    }


def buy_hold(res: Result, fees: Fees, a: int, b: int) -> list[float]:
    """Bought at the close of bar `a` (fee + slippage), held to bar `b`."""
    q = res.equity[a] / (res.close[a] * (1 + fees.slippage) * (1 + fees.fee))
    return [q * x for x in res.close[a : b + 1]]


def trade_stats(trades: Sequence[Trade]) -> dict[str, Any]:
    closed = [x for x in trades if x.reason != "OPEN"]
    rs = [x.r for x in closed]
    wins = [x for x in closed if x.pnl > 0]
    losses = [x for x in closed if x.pnl <= 0]
    gross_win = sum(x.pnl for x in wins)
    gross_loss = -sum(x.pnl for x in losses)
    return {
        "trades": len(closed),
        "open_trade": any(x.reason == "OPEN" for x in trades),
        "win_rate_pct": round(100 * len(wins) / len(closed), 1) if closed else None,
        "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
        "median_r": round(sorted(rs)[len(rs) // 2], 2) if rs else None,
        "best_r": round(max(rs), 2) if rs else None,
        "worst_r": round(min(rs), 2) if rs else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "avg_win_pct": round(100 * sum(x.return_pct for x in wins) / len(wins), 1)
        if wins
        else None,
        "avg_loss_pct": round(100 * sum(x.return_pct for x in losses) / len(losses), 1)
        if losses
        else None,
        "avg_bars_win": round(sum(x.bars for x in wins) / len(wins), 1) if wins else None,
        "avg_bars_loss": round(sum(x.bars for x in losses) / len(losses), 1) if losses else None,
        "exits": {k: sum(1 for x in closed if x.reason == k) for k in ("STOP", "CHANNEL")},
        "fees_paid": round(sum(x.fees for x in trades), 2),
        "max_losing_streak": _streak([x.pnl <= 0 for x in closed]),
    }


def _streak(flags: Sequence[bool]) -> int:
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0
        best = max(best, cur)
    return best


def yearly(res: Result, bh: Sequence[float]) -> list[dict[str, Any]]:
    """Calendar-year returns of the strategy and buy & hold from `res.start`."""
    out: list[dict[str, Any]] = []
    s = res.start
    times, eq = res.times[s:], res.equity[s:]
    i0 = 0
    for i in range(1, len(times) + 1):
        if i == len(times) or times[i].year != times[i0].year:
            base = max(i0 - 1, 0)
            expo = res.in_market[s + i0 : s + i]
            out.append(
                {
                    "year": times[i0].year,
                    "strategy_pct": round(100 * (eq[i - 1] / eq[base] - 1), 1),
                    "buy_hold_pct": round(100 * (bh[i - 1] / bh[base] - 1), 1),
                    "exposure_pct": round(100 * sum(expo) / len(expo), 0),
                }
            )
            i0 = i
    return out


def summary(res: Result, fees: Fees) -> dict[str, Any]:
    """Whole period, the first 2/3 and the last 1/3, each against buy & hold."""
    s, e = res.start, len(res.equity) - 1
    bh = buy_hold(res, fees, s, e)
    split = s + (e - s) * 2 // 3

    def part(a: int, b: int) -> dict[str, Any]:
        return {
            "strategy": curve_stats(res.times[a : b + 1], res.equity[a : b + 1]),
            "buy_hold": curve_stats(res.times[a : b + 1], bh[a - s : b - s + 1]),
            "exposure_pct": round(100 * sum(res.in_market[a : b + 1]) / (b - a + 1), 1),
            "trades": trade_stats(
                [x for x in res.trades if res.times[a] <= x.entry_time <= res.times[b]]
            ),
        }

    return {
        "params": res.params.as_dict(),
        "params_hash": res.params.digest(),
        "fees": {"fee": fees.fee, "slippage": fees.slippage},
        "bars": len(res.times),
        "first_signal_bar": res.times[s].date().isoformat(),
        "full": part(s, e),
        "first_two_thirds": part(s, split),
        "last_third": part(split, e),
        "yearly": yearly(res, bh),
    }


def grid(
    bars: Sequence[Candle], base: TrendParams, fees: Fees, axes: Mapping[str, Sequence[Any]]
) -> list[dict[str, Any]]:
    """Sensitivity table over parameter axes (for robustness, not for picking the best)."""
    rows: list[dict[str, Any]] = []
    combos: list[dict[str, Any]] = [{}]
    for k, vs in axes.items():
        combos = [{**cmb, k: v} for cmb in combos for v in vs]
    # one common start, so every row covers the same days
    start = max(TrendParams.from_mapping({**base.as_dict(), **cmb}).warmup for cmb in combos)
    for cmb in combos:
        p = TrendParams.from_mapping({**base.as_dict(), **cmb})
        r = run(bars, p, fees)
        st = curve_stats(r.times[start:], r.equity[start:])
        ts = trade_stats([x for x in r.trades if x.entry_time >= r.times[start]])
        rows.append(
            {
                **cmb,
                "cagr_pct": st.get("cagr_pct"),
                "max_drawdown_pct": st.get("max_drawdown_pct"),
                "sharpe": st.get("sharpe"),
                "trades": ts["trades"],
                "win_rate_pct": ts["win_rate_pct"],
                "exposure_pct": round(100 * sum(r.in_market[start:]) / len(r.in_market[start:]), 1),
            }
        )
    return rows
