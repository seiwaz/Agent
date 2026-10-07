"""Ensemble trend following with volatility targeting, on one coin or a rotating coin universe
(docs/trend/plan3.md; after Zarattini, Pagani & Barbon, "Catching Crypto Trends", 2025).

Per coin and per lookback L: long from the close that is >= the highest close of the previous
L days, with a trailing stop = max(previous stop, midpoint of the highest and lowest close of
the last L days, today included); flat from a close below the stop. The signal is the mean of
the lookback states (0 .. 1). Target weight = signal x min(target_vol / sigma, 1) / slots, with
sigma the annualized std of the last `vol_len` daily returns; the gross target is capped.

Timing, with no look-ahead: targets are decided on the close of day t and traded at the open of
day t + 1, only when they differ from the current weight by more than `band` (a target of 0
always closes the position). Costs (fee + slippage) apply to the traded notional. A coin whose
series ends (delisting, a gap) is sold at its last close. Spot: long only, no borrowing; buys are
scaled down when cash is short.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from sp2l.core.types import Candle

LOOKBACKS = (5, 10, 20, 30, 60, 90, 150, 250, 360)
YEAR_DAYS = 365


@dataclass(frozen=True, slots=True)
class EnsembleParams:
    lookbacks: tuple[int, ...] = LOOKBACKS
    target_vol: float = 0.25
    vol_len: int = 30
    slots: int = 1  # each coin's weight is divided by this (a portfolio of 10: 10)
    gross_cap: float = 1.0
    band: float = 0.05
    universe_size: int = 0  # 0 = every series is eligible every day
    min_history: int = 60  # days of history before a coin can enter the universe
    volume_len: int = 30  # days of dollar volume for the universe ranking
    cash_yield: float = 0.0  # per year, on positive cash (descriptive overlay)
    initial_equity: float = 10_000.0


@dataclass(slots=True)
class Series:
    sym: str
    t: list[datetime]
    o: np.ndarray
    c: np.ndarray
    dollar_vol: np.ndarray
    index: dict[datetime, int]


def split_series(sym: str, bars: Sequence[Candle], max_gap_days: int = 7) -> list[Series]:
    """One series per run of days without a gap longer than `max_gap_days` (a delisting or a
    relaunch under the same ticker ends the asset; what follows is a new one)."""
    runs: list[list[Candle]] = []
    for b in bars:
        if runs and b.open_time - runs[-1][-1].open_time <= timedelta(days=max_gap_days):
            runs[-1].append(b)
        else:
            runs.append([b])
    out = []
    for k, run_ in enumerate(runs):
        name = sym if k == 0 else f"{sym}#{k + 1}"
        c = np.array([float(b.close) for b in run_])
        out.append(
            Series(
                name,
                [b.open_time for b in run_],
                np.array([float(b.open) for b in run_]),
                c,
                c * np.array([float(b.volume) for b in run_]),
                {b.open_time: i for i, b in enumerate(run_)},
            )
        )
    return out


def ensemble_signal(c: np.ndarray, lookbacks: Sequence[int]) -> np.ndarray:
    """Mean lookback state after each close (0 .. 1); a lookback is flat until it has L closes
    before the current one."""
    n = len(c)
    total = np.zeros(n)
    for L in lookbacks:
        if n <= L:
            continue
        prev_hi = np.full(n, np.nan)
        prev_hi[L:] = sliding_window_view(c, L).max(axis=1)[:-1]  # closes i-L .. i-1
        win = sliding_window_view(c, L)  # closes i-L+1 .. i, for i >= L-1
        mid = np.full(n, np.nan)
        mid[L - 1 :] = (win.max(axis=1) + win.min(axis=1)) / 2
        state, stop = False, -math.inf
        for i in range(L, n):
            if state:
                stop = max(stop, mid[i])
                if c[i] < stop:
                    state = False
            elif c[i] >= prev_hi[i]:
                state, stop = True, mid[i]
            total[i] += state
    return total / len(lookbacks)


def realized_vol(c: np.ndarray, n: int) -> np.ndarray:
    """Annualized std of the last n daily close-to-close returns (NaN before n returns)."""
    out = np.full(len(c), np.nan)
    if len(c) <= n:
        return out
    r = c[1:] / c[:-1] - 1
    sd = sliding_window_view(r, n).std(axis=1, ddof=1)
    out[n:] = sd * math.sqrt(YEAR_DAYS)
    return out


@dataclass(slots=True)
class EnsembleResult:
    times: list[datetime]
    equity: list[float]
    gross: list[float]  # gross weight at each close
    trades: int = 0
    traded: float = 0.0  # notional
    costs: float = 0.0
    cash_yield: float = 0.0
    forced_exits: list[tuple[str, datetime]] = field(default_factory=list)
    members: dict[datetime, list[str]] = field(default_factory=dict)  # universe per month


def run_ensemble(
    series: Sequence[Series], p: EnsembleParams, fee: float, slippage: float
) -> EnsembleResult:
    cost_rate = fee + slippage
    sig = {s.sym: ensemble_signal(s.c, p.lookbacks) for s in series}
    vol = {s.sym: realized_vol(s.c, p.vol_len) for s in series}
    by_sym = {s.sym: s for s in series}
    days = sorted({t for s in series for t in s.t})
    cash = p.initial_equity
    qty: dict[str, float] = {}
    targets: dict[str, float] = {}
    members: list[str] = []
    res = EnsembleResult([], [], [])
    prev_month: tuple[int, int] | None = None

    def last_close(sym: str, d: datetime) -> float:
        s = by_sym[sym]
        i = s.index.get(d)
        return float(s.c[i] if i is not None else s.c[-1])

    for d in days:
        # 1. a held coin with no bar today: its series ended -> sold at its last close
        for sym in [k for k in qty if d not in by_sym[k].index]:
            px = float(by_sym[sym].c[-1])
            cash += qty[sym] * px - qty[sym] * px * cost_rate
            res.costs += qty[sym] * px * cost_rate
            res.traded += qty[sym] * px
            res.trades += 1
            res.forced_exits.append((sym, d))
            del qty[sym]
        # 2. targets decided at yesterday's close trade at today's open
        if targets or qty:
            opens = {
                k: float(by_sym[k].o[by_sym[k].index[d]])
                for k in set(targets) | set(qty)
                if d in by_sym[k].index
            }
            eq = cash + sum(q * opens[k] for k, q in qty.items())
            orders: dict[str, float] = {}
            for sym in opens:
                tgt = targets.get(sym, 0.0)
                cur = qty.get(sym, 0.0) * opens[sym] / eq if eq > 0 else 0.0
                if (tgt == 0 and cur > 0) or abs(tgt - cur) > p.band:
                    orders[sym] = (tgt - cur) * eq  # notional to buy (+) / sell (-)
            for sym, n in sorted(orders.items(), key=lambda kv: kv[1]):  # sells first
                if n > 0:
                    n = min(n, max(0.0, cash) / (1 + cost_rate))
                    if n <= 0:
                        continue
                q = n / opens[sym]
                if targets.get(sym, 0.0) == 0:
                    q = -qty.get(sym, 0.0)
                    n = q * opens[sym]
                cash -= n + abs(n) * cost_rate
                res.costs += abs(n) * cost_rate
                res.traded += abs(n)
                res.trades += 1
                qty[sym] = qty.get(sym, 0.0) + q
                if qty[sym] <= 1e-12:
                    del qty[sym]
        # 3. idle-cash yield, then mark to market at the close
        if p.cash_yield and cash > 0:
            y = cash * p.cash_yield / YEAR_DAYS
            cash += y
            res.cash_yield += y
        held = sum(q * last_close(k, d) for k, q in qty.items())
        eq = cash + held
        res.times.append(d)
        res.equity.append(max(eq, 1e-9))
        res.gross.append(held / eq if eq > 0 else 0.0)
        # 4. universe (first day of each month) and targets for tomorrow's open
        live = [s for s in series if d in s.index]
        if p.universe_size:
            if prev_month != (d.year, d.month):
                ranked = []
                for s in live:
                    i = s.index[d]
                    if i + 1 >= max(p.min_history, p.volume_len):
                        ranked.append(
                            (float(s.dollar_vol[i + 1 - p.volume_len : i + 1].mean()), s.sym)
                        )
                members = [sym for _, sym in sorted(ranked, reverse=True)[: p.universe_size]]
                res.members[d] = members
            elig = [by_sym[k] for k in members if d in by_sym[k].index]
        else:
            elig = live
        prev_month = (d.year, d.month)
        targets = {}
        for s in elig:
            i = s.index[d]
            sv, vv = sig[s.sym][i], vol[s.sym][i]
            if sv > 0 and vv > 0 and not math.isnan(vv):
                targets[s.sym] = sv * min(p.target_vol / vv, 1.0) / p.slots
        tot = sum(targets.values())
        if tot > p.gross_cap:
            targets = {k: w * p.gross_cap / tot for k, w in targets.items()}
    return res


def summarize(res: EnsembleResult, a: datetime, b: datetime) -> dict[str, Any]:
    """Statistics of the equity curve between two dates (inclusive)."""
    from sp2l.trend.backtest import curve_stats

    idx = [i for i, t in enumerate(res.times) if a <= t <= b]
    if len(idx) < 2:
        return {}
    i0, i1 = idx[0], idx[-1]
    st = curve_stats(res.times[i0 : i1 + 1], res.equity[i0 : i1 + 1])
    st["exposure_pct"] = round(100 * float(np.mean(res.gross[i0 : i1 + 1])), 1)
    return st


def as_dict(p: EnsembleParams) -> dict[str, Any]:
    return {k: getattr(p, k) for k in p.__dataclass_fields__}


def series_from(bars: Mapping[str, Sequence[Candle]]) -> list[Series]:
    return [s for sym, b in sorted(bars.items()) for s in split_series(sym, b)]
