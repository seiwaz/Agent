# ruff: noqa: E501  (report tables are long by nature)
"""Momentum research (pre-registered in docs/research/momentum/plan.md; research only).

Steps 1-3 read the DISCOVERY split only; the holdout is read only by `--holdout`, which runs the
candidates written by step 3 and nothing else.

    uv run python -m sp2l.smc.research.momentum            # steps 1-3 (discovery)
    uv run python -m sp2l.smc.research.momentum --holdout  # step 4 (once)

Reused unchanged: markets / split / loader of the edge study, `events.zones` (zone kinds, known
times), `events.outcome` (event rules), the control-level method of `events.controls` (here every
control is kept, with its own momentum state), `events.Trend` (structural 4h bias), the Tabdeal
level-1 costs.
"""

from __future__ import annotations

import argparse
import json
import math
import pickle
import random
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from sp2l.smc.model import Costs, SmcParams
from sp2l.smc.research.data import Market
from sp2l.smc.research.events import CONTROL_DAYS, HORIZON_BARS, Trend, _touch, outcome, zones
from sp2l.smc.structure import analyze, atr_series
from sp2l.smc.timeframes import MINUTE, aggregate, length

OUT = Path("docs/research/momentum")
CACHE = OUT / "cache"
# the edge study's markets, retention (days) and end (scripts/research_edge.py)
COVERAGE = {
    "BTCUSDT": 296, "XRPUSDT": 296, "ETHUSDT": 296, "SOLUSDT": 253, "DOGEUSDT": 253,
    "ADAUSDT": 253, "BNBUSDT": 202, "LTCUSDT": 197, "AVAXUSDT": 167,
}
END = datetime(2026, 10, 5, 11, 40, tzinfo=UTC)
KINDS = ("FVG", "OB_extreme", "OB_last")
TFS = ("1h", "4h")
STATES = ("M1", "M2", "M3", "B4")
FAST = ("M1", "M2", "M3")
N_CTRL = 20
EXITS = ("fixed_2R", "fixed_3R", "chandelier_2", "chandelier_3", "momentum_flip", "time_12", "time_24")
SAFETY_BARS = 100
STOP_BUFFER_ATR = 0.2
RANDOM_SEEDS = 100


# ---- momentum states -------------------------------------------------------------------
def ema(xs: np.ndarray, n: int) -> np.ndarray:
    """EMA seeded with the SMA of the first n values (NaN before)."""
    out = np.full(xs.size, np.nan)
    if xs.size < n:
        return out
    a = 2.0 / (n + 1)
    out[n - 1] = xs[:n].mean()
    for k in range(n, xs.size):
        out[k] = a * xs[k] + (1 - a) * out[k - 1]
    return out


class Momentum:
    """M1 / M2 / M3 / B4 of one market at any time T (only data closed by T)."""

    def __init__(self, m: Market, p: SmcParams) -> None:
        self.m = m
        end = m.bars[-1].open_time + MINUTE
        h1 = aggregate(m.bars, "1h", end)
        self.h1_close = np.array([b.open_time.timestamp() + 3600 for b in h1])
        c = np.array([float(b.close) for b in h1])
        hi = np.array([float(b.high) for b in h1])
        lo = np.array([float(b.low) for b in h1])
        e = ema(c, 20)
        s2 = np.zeros(c.size, dtype=int)
        for k in range(6, c.size):
            if not (np.isnan(e[k]) or np.isnan(e[k - 6])):
                s2[k] = int(np.sign(e[k] - e[k - 6]))
        self.m2 = s2
        atr = [float(x) if x is not None else math.nan for x in atr_series(h1, p.atr_len)]
        disp = np.zeros(c.size, dtype=int)
        last = 0
        for j in range(1, c.size):
            a = atr[j - 1]
            if not math.isnan(a):
                if c[j] > hi[j - 1] + a:
                    last = 1
                elif c[j] < lo[j - 1] - a:
                    last = -1
            disp[j] = last
        self.m3 = disp
        self.b4 = Trend(analyze(aggregate(m.bars, "4h", end), "4h", p))

    def _h1(self, ts: np.ndarray) -> np.ndarray:
        return np.searchsorted(self.h1_close, ts, side="right") - 1

    def m1(self, i: np.ndarray) -> np.ndarray:
        """Sign of the 24 h return up to the open of minute index i."""
        t, c = self.m.t, self.m.c
        prev = i - 1
        ok = prev >= 0
        prev = np.where(ok, prev, 0)
        ref = np.searchsorted(t, t[prev] - 86400, side="right") - 1
        good = ok & (ref >= 0) & (t[prev] - 86400 >= t[0])
        return np.where(good, np.sign(c[prev] - c[np.maximum(ref, 0)]), 0).astype(int)

    def state(self, name: str, i: np.ndarray) -> np.ndarray:
        """State at the open of minute index i (array)."""
        i = np.asarray(i)
        ts = self.m.t[np.minimum(i, self.m.t.size - 1)]
        if name == "M1":
            return self.m1(i)
        if name == "B4":
            return np.array([self.b4.at(float(x)) for x in np.atleast_1d(ts)], dtype=int)
        k = self._h1(ts)
        arr = self.m2 if name == "M2" else self.m3
        return np.where(k >= 0, arr[np.maximum(k, 0)], 0).astype(int)

    def at_ts(self, name: str, ts: float) -> int:
        """State using data closed by epoch ts (ts on a minute boundary)."""
        i = int(np.searchsorted(self.m.t, ts, side="left"))
        if name == "M1":
            j = i  # the minute opening at ts (its previous minute closed at ts)
            if j >= self.m.t.size:
                j = self.m.t.size  # beyond the data: use the last close
                prev = j - 1
                ref = int(np.searchsorted(self.m.t, self.m.t[prev] - 86400, side="right")) - 1
                return int(np.sign(self.m.c[prev] - self.m.c[ref])) if ref >= 0 else 0
            return int(self.m1(np.array([j]))[0])
        if name == "B4":
            return self.b4.at(ts)
        k = int(np.searchsorted(self.h1_close, ts, side="right")) - 1
        arr = self.m2 if name == "M2" else self.m3
        return int(arr[k]) if k >= 0 else 0


# ---- event study with per-control states ----------------------------------------------------
@dataclass
class Ctl:
    i: int  # touch minute
    level: float
    width: float  # in price
    atr: float
    hit: dict[int, bool]
    mfe: float
    mae: float
    align: dict[str, int]


@dataclass
class Ev:
    market: str
    tf: str
    kind: str
    d: int
    i: int  # touch minute index
    known: float
    entry: float
    far: float  # far edge
    atr: float
    hit: dict[int, bool]
    mfe: float
    mae: float
    align: dict[str, int]
    ctl: list[Ctl] = field(default_factory=list)


def controls_each(
    m: Market, z: Any, split_ts: float, horizon: int, window: int, tick: float,
    atr_close: np.ndarray, atr: np.ndarray, rnd: random.Random, n: int, discovery: bool = True,
) -> list[tuple[int, float, float, float, dict[str, Any]]]:
    """The edge study's random control levels (`events.controls` logic), every one kept:
    (touch minute, level, width in price, ATR, outcome)."""
    dist = z.direction * (z.close - z.entry) / z.atr if z.atr else -1
    width = (z.top - z.bottom) / z.atr if z.atr else 0
    if dist < 0 or width <= 0:
        return []
    lo_ts = max(float(m.t[0]) + 30 * 86400, z.known - CONTROL_DAYS * 86400)
    hi_ts = min(float(m.t[-1]) - horizon * 60 - window * 60, z.known + CONTROL_DAYS * 86400)
    if discovery:
        hi_ts = min(hi_ts, split_ts)
    else:
        lo_ts = max(lo_ts, split_ts)
    if hi_ts <= lo_ts:
        return []
    got = []
    for _ in range(n):
        tc = rnd.uniform(lo_ts, hi_ts)
        ic = m.index_at(tc)
        k = int(np.searchsorted(atr_close, tc, side="right")) - 1
        ac = atr[k] if k >= 0 else 0.0
        if ic <= 0 or ac <= 0:
            continue
        e_c = float(m.c[ic - 1]) - z.direction * dist * ac
        st_c = e_c - z.direction * width * ac - z.direction * tick
        jc = _touch(m, ic, min(ic + window, len(m.t)), z.direction, e_c)
        if jc >= 0 and (o := outcome(m, jc, z.direction, e_c, st_c, horizon)) is not None:
            got.append((jc, e_c, width * ac, ac, o))
    return got


def study(m: Market, tf: str, p: SmcParams, mom: Momentum, holdout: bool = False) -> list[Ev]:
    """First touches of FVG / OB_extreme / OB_last on `tf` in ONE split (discovery unless
    `holdout`), each with N_CTRL controls and every state's alignment."""
    zs, a = zones(m, tf, p)
    rnd = random.Random(zlib.crc32(f"{m.symbol}{tf}{'H' if holdout else 'D'}".encode()))
    ln_min = int(length(tf).total_seconds() // 60)
    horizon = HORIZON_BARS * ln_min
    window = p.lookback(tf) * ln_min
    tick = float(m.tick)
    atr_close = np.array([b.open_time.timestamp() + length(tf).total_seconds() for b in a.bars])
    atr = np.array([float(x) if x is not None else 0.0 for x in a.atr])
    split_ts = m.split.timestamp()
    out: list[Ev] = []
    for z in zs:
        if z.kind not in KINDS:
            continue
        i0 = m.index_at(z.known)
        i = _touch(m, i0, min(i0 + window, len(m.t)), z.direction, z.entry)
        if i < 0 or ((m.t[i] >= split_ts) != holdout):
            continue  # the other split is never looked at
        stop = (z.bottom - tick) if z.direction > 0 else (z.top + tick)
        o = outcome(m, i, z.direction, z.entry, stop, horizon)
        if o is None:
            continue
        al = {s: int(mom.state(s, np.array([i]))[0]) * z.direction for s in STATES}
        far = z.bottom if z.direction > 0 else z.top
        ev = Ev(m.symbol, tf, z.kind, z.direction, i, z.known, z.entry, far, z.atr,
                o["hit"], o["mfe"], o["mae"], al)
        for jc, lvl, w, ac, oc in controls_each(
            m, z, split_ts, horizon, window, tick, atr_close, atr, rnd, N_CTRL, not holdout
        ):
            cal = {s: int(mom.state(s, np.array([jc]))[0]) * z.direction for s in STATES}
            ev.ctl.append(Ctl(jc, lvl, w, ac, oc["hit"], oc["mfe"], oc["mae"], cal))
        out.append(ev)
    return out


def boot_mean(x: np.ndarray, n: int = 2000, seed: int = 7) -> tuple[float, float, float]:
    if x.size == 0:
        return (math.nan, math.nan, math.nan)
    rng = np.random.default_rng(seed)
    means = x[rng.integers(0, x.size, size=(n, x.size))].mean(axis=1)
    return float(x.mean()), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def boot_diff_unpaired(a: np.ndarray, b: np.ndarray, n: int = 2000, seed: int = 9) -> tuple[float, float, float]:
    if a.size == 0 or b.size == 0:
        return (math.nan, math.nan, math.nan)
    rng = np.random.default_rng(seed)
    ma = a[rng.integers(0, a.size, size=(n, a.size))].mean(axis=1)
    mb = b[rng.integers(0, b.size, size=(n, b.size))].mean(axis=1)
    d = ma - mb
    return float(a.mean() - b.mean()), float(np.percentile(d, 2.5)), float(np.percentile(d, 97.5))


def momentum_effect(evs: list[Ev], s: str, k: int) -> tuple[float, float, float, int]:
    """(a) per event: mean of its aligned controls - mean of its against controls."""
    xs = []
    for e in evs:
        al = [c.hit[k] for c in e.ctl if c.align[s] > 0]
        ag = [c.hit[k] for c in e.ctl if c.align[s] < 0]
        if al and ag:
            xs.append(np.mean(al) - np.mean(ag))
    arr = np.array(xs, dtype=float)
    return (*boot_mean(arr), arr.size)


def zone_effect(evs: list[Ev], s: str, k: int) -> tuple[float, float, float, int]:
    """(b) per aligned event: its hit - mean of its aligned controls."""
    xs = []
    for e in evs:
        if e.align[s] <= 0:
            continue
        al = [c.hit[k] for c in e.ctl if c.align[s] > 0]
        if al:
            xs.append(float(e.hit[k]) - np.mean(al))
    arr = np.array(xs, dtype=float)
    return (*boot_mean(arr), arr.size)


def cell_table(evs: list[Ev]) -> list[dict[str, Any]]:
    rows = []
    for tf in TFS:
        for kind in KINDS:
            sub = [e for e in evs if e.tf == tf and e.kind == kind]
            for s in STATES:
                al = [e for e in sub if e.align[s] > 0]
                ag = [e for e in sub if e.align[s] < 0]
                row: dict[str, Any] = {"tf": tf, "kind": kind, "state": s, "n": len(sub),
                                       "n_aligned": len(al), "n_against": len(ag)}
                for k in (1, 2, 3):
                    row[f"p{k}_aligned"] = boot_mean(np.array([e.hit[k] for e in al], dtype=float))
                    row[f"p{k}_against"] = boot_mean(np.array([e.hit[k] for e in ag], dtype=float))
                    row[f"a{k}"] = momentum_effect(sub, s, k)
                    row[f"b{k}"] = zone_effect(sub, s, k)
                    ctl_al = [c.hit[k] for e in sub for c in e.ctl if c.align[s] > 0]
                    row[f"r{k}_aligned"] = float(np.mean(ctl_al)) if ctl_al else math.nan
                row["mfe_aligned"] = float(np.median([e.mfe for e in al])) if al else math.nan
                row["mae_aligned"] = float(np.median([e.mae for e in al])) if al else math.nan
                row["mfe_against"] = float(np.median([e.mfe for e in ag])) if ag else math.nan
                row["mae_against"] = float(np.median([e.mae for e in ag])) if ag else math.nan
                rows.append(row)
    return rows


def h2_table(evs: list[Ev]) -> list[dict[str, Any]]:
    rows = []
    for tf in TFS:
        sub = [e for e in evs if e.tf == tf]
        for s in STATES:
            row: dict[str, Any] = {"tf": tf, "state": s}
            for k in (1, 2, 3):
                row[f"a{k}"] = momentum_effect(sub, s, k)
                al = [c.hit[k] for e in sub for c in e.ctl if c.align[s] > 0]
                ag = [c.hit[k] for e in sub for c in e.ctl if c.align[s] < 0]
                row[f"r{k}_aligned"] = float(np.mean(al)) if al else math.nan
                row[f"r{k}_against"] = float(np.mean(ag)) if ag else math.nan
                row[f"n_ctl_aligned_{k}"] = len(al)
            rows.append(row)
    return rows


def passers(cells: list[dict[str, Any]], h2: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Step-1 pass rule of the plan (§3), at most 4 by the largest lower CI bound."""
    got = []
    for r in cells:
        if r["state"] not in FAST or r["n_aligned"] < 100:
            continue
        lows = [r[f"b{k}"][1] for k in (1, 2) if not math.isnan(r[f"b{k}"][1])]
        if lows and max(lows) > 0:
            got.append({"type": "zone", "tf": r["tf"], "kind": r["kind"], "state": r["state"], "low": max(lows)})
    for r in h2:
        if r["state"] not in FAST:
            continue
        lows = [r[f"a{k}"][1] for k in (1, 2) if not math.isnan(r[f"a{k}"][1])]
        if lows and max(lows) > 0:
            got.append({"type": "H2", "tf": r["tf"], "kind": "random", "state": r["state"], "low": max(lows)})
    return sorted(got, key=lambda x: -x["low"])[:4]


def agreement(mkts: dict[str, Market], moms: dict[str, Momentum]) -> dict[str, Any]:
    """B4 vs M1-M3 at every discovery 1h close; delay from a momentum switch to a B4 flip."""
    agree: dict[str, list[int]] = defaultdict(list)
    delays: dict[str, list[float]] = defaultdict(list)
    none: dict[str, int] = defaultdict(int)
    flips = 0
    for sym, m in mkts.items():
        mom = moms[sym]
        grid = mom.h1_close[(mom.h1_close > m.t[0] + 2 * 86400) & (mom.h1_close <= m.split.timestamp())]
        st = {s: np.array([mom.at_ts(s, float(t)) for t in grid]) for s in STATES}
        for s in FAST:
            both = (st[s] != 0) & (st["B4"] != 0)
            agree[s] += list((st[s][both] == st["B4"][both]).astype(int))
        b = st["B4"]
        for g in range(1, grid.size):
            if b[g] != 0 and b[g] != b[g - 1]:
                flips += 1
                d = b[g]
                for s in FAST:
                    x = st[s]
                    sw = [h for h in range(max(1, g - 30 * 24), g + 1) if x[h] == d and x[h - 1] != d]
                    if sw:
                        delays[s].append((grid[g] - grid[sw[-1]]) / 3600)
                    else:
                        none[s] += 1
    out: dict[str, Any] = {"b4_flips": flips}
    for s in FAST:
        a = np.array(agree[s])
        dl = np.array(delays[s])
        out[s] = {
            "agreement": float(a.mean()) if a.size else math.nan,
            "hours_both_defined": int(a.size),
            "delay_h_median": float(np.median(dl)) if dl.size else math.nan,
            "delay_h_q1_q3": [float(np.percentile(dl, 25)), float(np.percentile(dl, 75))] if dl.size else None,
            "no_switch_within_30d": none[s],
        }
    return out


# ---- step 2: exits ---------------------------------------------------------------------------
class ZoneBars:
    """Close times and ATR of the zone timeframe's bars, and the M1 index each closes at."""

    def __init__(self, m: Market, tf: str, p: SmcParams) -> None:
        end = m.bars[-1].open_time + MINUTE
        bars = aggregate(m.bars, tf, end)
        self.close = np.array([b.open_time.timestamp() + length(tf).total_seconds() for b in bars])
        self.atr = np.array([float(x) if x is not None else math.nan for x in atr_series(bars, p.atr_len)])
        # index of the first minute opening at/after each close = exclusive end of the bar
        self.end_i = np.searchsorted(m.t, self.close, side="left")


@dataclass
class Trade:
    market: str
    i: int  # fill minute
    d: int
    entry: float
    stop: float
    exit_i: int = -1
    level: float = math.nan
    kind: str = "OPEN"


def _first(mask: np.ndarray) -> int:
    if mask.size == 0:
        return -1
    k = int(mask.argmax())
    return k if mask[k] else -1


def simulate(
    m: Market, i: int, d: int, entry: float, stop: float, rule: str, zb: ZoneBars,
    mom: Momentum, state: str,
) -> tuple[int, float, str]:
    """Walk one filled trade from fill minute i. Returns (exit minute, exit level, kind).

    The stop (or trail) and a fixed target are checked minute by minute after the fill minute
    (stop first in a shared minute); the trail ratchets and the time / safety exits fire at
    zone-TF closes; the momentum exit is checked at 1h closes. Market exits at a close use the
    close of the last minute of that bar."""
    hi, lo, c = m.h, m.lo, m.c
    if (lo[i] <= stop) if d > 0 else (hi[i] >= stop):
        return i, stop, "SL"
    r = abs(entry - stop)
    tp = entry + d * (2 if rule == "fixed_2R" else 3) * r if rule.startswith("fixed") else math.nan
    k_ch = float(rule.split("_")[1]) if rule.startswith("chandelier") else 0.0
    n_time = int(rule.split("_")[1]) if rule.startswith("time") else SAFETY_BARS
    b0 = int(np.searchsorted(zb.close, m.t[i] + 60, side="left"))
    last_b = b0 + n_time - 1
    avail = int(np.searchsorted(zb.close, m.t[-1] + 60, side="right")) - 1  # last bar fully in the data
    last_b = min(last_b, avail)
    if last_b < b0:
        return -1, math.nan, "OPEN"
    cps: list[tuple[int, int, int]] = [(int(zb.end_i[b]), 1, b - b0 + 1) for b in range(b0, last_b + 1)]
    if rule == "momentum_flip":
        h = int(np.searchsorted(mom.h1_close, m.t[i] + 60, side="left"))
        while h < mom.h1_close.size and mom.h1_close[h] <= zb.close[last_b]:
            cps.append((int(np.searchsorted(m.t, mom.h1_close[h], side="left")), 0, h))
            h += 1
    cps.sort()
    trail = stop
    best = hi[i] if d > 0 else lo[i]
    start = i + 1
    for end, kind, payload in cps:
        if end > start:
            seg = slice(start, end)
            st = _first(lo[seg] <= trail if d > 0 else hi[seg] >= trail)
            tg = _first(hi[seg] >= tp if d > 0 else lo[seg] <= tp) if not math.isnan(tp) else -1
            if st >= 0 and (tg < 0 or st <= tg):
                return start + st, trail, "SL" if trail == stop else "TRAIL"
            if tg >= 0:
                return start + tg, tp, "TP"
            best = max(best, float(hi[seg].max())) if d > 0 else min(best, float(lo[seg].min()))
            start = end
        if end - 1 <= i:
            continue
        if kind == 0:  # a 1h close: the entry's own state against the trade -> exit at market
            if mom.at_ts(state, float(mom.h1_close[payload])) * d < 0:
                return end - 1, float(c[end - 1]), "FLIP"
            continue
        b = b0 + payload - 1
        if k_ch > 0 and not math.isnan(zb.atr[b]):
            cand = best - d * k_ch * zb.atr[b]
            trail = max(trail, cand) if d > 0 else min(trail, cand)
        if payload == n_time:
            return end - 1, float(c[end - 1]), "TIME" if rule.startswith("time") else "SAFETY"
    return -1, math.nan, "OPEN"


def price(t: Trade, costs: Costs) -> tuple[float, float]:
    """(gross in stop distances, net R): maker entry, every exit at market (taker + slippage)."""
    mk, tk, sp = float(costs.maker_fee), float(costs.taker_fee), float(costs.slippage)
    d, e, sl = t.d, t.entry, t.stop
    dist = abs(e - sl)
    sl_fill = sl - d * sl * sp
    risk = dist + e * mk + sl * sp + sl_fill * tk
    x = t.level - d * t.level * sp
    net = (x - e) * d - e * mk - x * tk
    return (t.level - e) * d / dist, net / risk


def round_away(v: float, tick: float, d: int) -> float:
    return (math.floor(v / tick) if d > 0 else math.ceil(v / tick)) * tick


def entries(evs: list[Ev], cand: dict[str, Any], tick_of: dict[str, float]) -> list[tuple[str, int, int, float, float]]:
    """(market, fill minute, side, entry, structural stop) of one entry set, in time order."""
    s = cand["state"]
    out = []
    for e in evs:
        if e.tf != cand["tf"]:
            continue
        if cand["type"] == "zone":
            if e.kind != cand["kind"] or e.align[s] <= 0:
                continue
            stop = round_away(e.far - e.d * STOP_BUFFER_ATR * e.atr, tick_of[e.market], e.d)
            out.append((e.market, e.i, e.d, e.entry, stop))
        else:
            c = next((x for x in e.ctl if x.align[s] > 0), None)
            if c is None:
                continue
            far = c.level - e.d * c.width
            stop = round_away(far - e.d * STOP_BUFFER_ATR * c.atr, tick_of[e.market], e.d)
            out.append((e.market, c.i, e.d, c.level, stop))
    return sorted(out, key=lambda x: (x[0], x[1]))


def run_trades(
    rows: list[tuple[str, int, int, float, float]], rule: str, mkts: dict[str, Market],
    zbs: dict[str, ZoneBars], moms: dict[str, Momentum], state: str,
) -> list[Trade]:
    """One position per market at a time, in touch order."""
    out = []
    busy: dict[str, int] = {}
    for sym, i, d, e, st in rows:
        if busy.get(sym, -1) >= i or (e - st) * d <= 0:
            continue
        m = mkts[sym]
        x, lvl, kind = simulate(m, i, d, e, st, rule, zbs[sym], moms[sym], state)
        t = Trade(sym, i, d, e, st, x, lvl, kind)
        busy[sym] = x if x >= 0 else len(m.t)
        out.append(t)
    return out


def random_aligned(
    trades: list[Trade], rule: str, mkts: dict[str, Market], zbs: dict[str, ZoneBars],
    moms: dict[str, Momentum], state: str, seed: int, holdout: bool = False,
) -> list[Trade]:
    """Per trade: a random minute of the same market and split at which `state` is aligned with
    the trade's side; entry at its open (maker), the same stop distance in %, the same exit."""
    rng = np.random.default_rng(seed)
    out = []
    for t in trades:
        m = mkts[t.market]
        pool = _aligned_pool(m, moms[t.market], state, t.d, holdout)
        if pool.size == 0:
            continue
        i = int(pool[rng.integers(0, pool.size)])
        e = float(m.o[i])
        st = e * (1 - t.d * abs(t.entry - t.stop) / t.entry)
        x, lvl, kind = simulate(m, i, t.d, e, st, rule, zbs[t.market], moms[t.market], state)
        out.append(Trade(t.market, i, t.d, e, st, x, lvl, kind))
    return out


_POOLS: dict[tuple[str, str, int, bool], np.ndarray] = {}


def _aligned_pool(m: Market, mom: Momentum, state: str, d: int, holdout: bool) -> np.ndarray:
    key = (m.symbol, state, d, holdout)
    if key not in _POOLS:
        split_i = m.index_at(m.split.timestamp())
        lo_i, hi_i = (split_i, len(m.t) - 1) if holdout else (m.index_at(m.t[0] + 30 * 86400), split_i)
        idx = np.arange(lo_i, max(lo_i, hi_i - SAFETY_BARS * 240), 7)  # every 7th minute
        st = mom.state(state, idx) if state != "B4" else np.zeros(idx.size, dtype=int)
        _POOLS[key] = idx[st == d]
    return _POOLS[key]


def tstats(trades: list[Trade], costs: Costs, seed: int = 3) -> dict[str, Any]:
    rows = [price(t, costs) for t in trades if t.kind != "OPEN"]
    if not rows:
        return {"n": 0}
    g = np.array([x[0] for x in rows])
    r = np.array([x[1] for x in rows])
    gm = boot_mean(g, seed=seed)
    nm = boot_mean(r, seed=seed + 1)
    loss = -r[r <= 0].sum()
    return {
        "n": int(r.size), "win": float((r > 0).mean()),
        "gross_avg": gm[0], "gross_ci": [gm[1], gm[2]], "gross_total": float(g.sum()),
        "net_avg": nm[0], "net_ci": [nm[1], nm[2]], "net_total": float(r.sum()),
        "pf": float(r[r > 0].sum() / loss) if loss > 0 else math.inf,
        "exits": dict(sorted({k: sum(1 for t in trades if t.kind == k) for k in {t.kind for t in trades}}.items())),
    }


def per_market(trades: list[Trade], costs: Costs) -> dict[str, float]:
    g: dict[str, float] = defaultdict(float)
    for t in trades:
        if t.kind != "OPEN":
            g[t.market] += price(t, costs)[0]
    return dict(g)


def cost_share(trades: list[Trade], costs: Costs) -> float:
    rt = float(costs.maker_fee + costs.taker_fee + costs.slippage)
    xs = [rt * t.entry / (abs(t.entry - t.stop) + rt * t.entry) for t in trades]
    return float(np.median(xs)) if xs else math.nan


# ---- runner ------------------------------------------------------------------------------
def load_markets() -> tuple[dict[str, Market], Costs, SmcParams]:
    from sqlalchemy import create_engine

    from sp2l.config import RuntimeConfig
    from sp2l.smc.research.data import load

    cfg = RuntimeConfig.load(Path("config/runtime.yaml"))
    db = create_engine(cfg.database_url)
    mk = {}
    for sym in COVERAGE:
        f = CACHE / f"market_{sym}.pkl"
        if f.exists():
            mk[sym] = pickle.loads(f.read_bytes())
            continue
        tick = cfg.instrument(sym)[0] if sym in cfg.symbols else None
        mk[sym] = load(db, sym, COVERAGE[sym] + 5, tick, upto=END)
        f.write_bytes(pickle.dumps(mk[sym]))
    return mk, cfg.costs(), cfg.smc_params()


def evaluate_variant(
    cand: dict[str, Any], rule: str, evs: list[Ev], mkts: dict[str, Market],
    zbs: dict[str, dict[str, ZoneBars]], moms: dict[str, Momentum], costs: Costs, holdout: bool,
) -> dict[str, Any]:
    ticks = {s: float(m.tick) for s, m in mkts.items()}
    rows = entries(evs, cand, ticks)
    tr = run_trades(rows, rule, mkts, {s: zbs[s][cand["tf"]] for s in mkts}, moms, cand["state"])
    st = tstats(tr, costs)
    pm = per_market(tr, costs)
    seeds = []
    for sd in range(RANDOM_SEEDS):
        rt = random_aligned(tr, rule, mkts, {s: zbs[s][cand["tf"]] for s in mkts}, moms, cand["state"],
                            seed=zlib.crc32(f"{cand}{rule}{sd}".encode()), holdout=holdout)
        rs = [price(t, costs)[0] for t in rt if t.kind != "OPEN"]
        if rs:
            seeds.append(float(np.mean(rs)))
    rnd = np.array(seeds)
    return {
        "entry_set": f"{cand['type']} {cand['tf']} {cand['kind']} {cand['state']}", "exit": rule,
        **st, "markets_gross_pos": sum(1 for v in pm.values() if v > 0), "markets": len(pm),
        "per_market_gross": pm, "cost_share_1R": cost_share(tr, costs),
        "random_gross_mean": float(rnd.mean()) if rnd.size else math.nan,
        "random_gross_p97_5": float(np.percentile(rnd, 97.5)) if rnd.size else math.nan,
    }


def is_candidate(v: dict[str, Any]) -> bool:
    return (
        v.get("n", 0) >= 150
        and v["gross_ci"][0] > 0
        and v["markets_gross_pos"] >= 6
        and v["gross_avg"] > v["random_gross_p97_5"]
        and v["net_avg"] > 0
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout", action="store_true", help="step 4: the candidates on the holdout, once")
    args = ap.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)
    mkts, costs, p = load_markets()
    moms = {s: Momentum(m, p) for s, m in mkts.items()}
    zbs = {s: {tf: ZoneBars(m, tf, p) for tf in TFS} for s, m in mkts.items()}
    split = "holdout" if args.holdout else "discovery"
    evs: list[Ev] = []
    for sym, m in mkts.items():
        f = CACHE / f"events_{split}_{sym}.pkl"
        if f.exists():
            got = pickle.loads(f.read_bytes())
        else:
            got = []
            for tf in TFS:
                got += study(m, tf, p, moms[sym], holdout=args.holdout)
            f.write_bytes(pickle.dumps(got))
        print(sym, split, "events", len(got), flush=True)
        evs += got
    if args.holdout:
        cands = json.loads((OUT / "candidates.json").read_text())
        if not cands:
            raise SystemExit("no candidates: the holdout is not run (plan §5)")
        if (OUT / "holdout.json").exists():
            raise SystemExit("the holdout was already run once (holdout.json exists)")
        res = [evaluate_variant(c["cand"], c["exit"], evs, mkts, zbs, moms, costs, True) for c in cands]
        (OUT / "holdout.json").write_text(json.dumps(res, indent=1, default=str))
        print(json.dumps(res, indent=1, default=str))
        return
    cells = cell_table(evs)
    h2 = h2_table(evs)
    agr = agreement(mkts, moms)
    ps = passers(cells, h2)
    (OUT / "step1.json").write_text(json.dumps({"cells": cells, "h2": h2, "agreement": agr, "passers": ps,
                                                "events": len(evs)}, indent=1, default=str))
    print("step 1 passers", ps, flush=True)
    variants = []
    for cand in ps:
        for rule in EXITS:
            v = evaluate_variant(cand, rule, evs, mkts, zbs, moms, costs, False)
            v["cand"] = cand
            variants.append(v)
            print(v["entry_set"], rule, v.get("n"), round(v.get("gross_avg", math.nan), 3),
                  round(v.get("net_avg", math.nan), 3), flush=True)
    (OUT / "step2.json").write_text(json.dumps({"variants_run": len(variants), "variants": variants}, indent=1, default=str))
    cands = sorted((v for v in variants if is_candidate(v)), key=lambda v: -v["gross_ci"][0])[:3]
    (OUT / "candidates.json").write_text(json.dumps([{"cand": v["cand"], "exit": v["exit"]} for v in cands], indent=1))
    print("candidates", [(v["entry_set"], v["exit"]) for v in cands])


if __name__ == "__main__":
    main()
