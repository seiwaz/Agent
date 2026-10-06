# ruff: noqa: E501  (report tables are long by nature)
"""Breakout / confirmation research (pre-registered in docs/research/breakout/plan.md).

H3 CRT (1h, 4h, 1D), H4 IFVG / CISD (1h, 4h), H5 liquidity-run continuation (1h, 4h): 13
variants, matched random baselines, discovery only for steps 1-3. `--holdout` runs the
candidates written by step 3 once and nothing else.

    uv run python -m sp2l.smc.research.breakout            # steps 1-3 (discovery)
    uv run python -m sp2l.smc.research.breakout --holdout  # step 4 (once)
"""

from __future__ import annotations

import argparse
import json
import math
import pickle
import zlib
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np

from sp2l.core.types import Candle, Side
from sp2l.smc.model import Analysis, Costs, SmcParams
from sp2l.smc.research.data import Market
from sp2l.smc.structure import analyze
from sp2l.smc.timeframes import MINUTE, aggregate, length

OUT = Path("docs/research/breakout")
CACHE = OUT / "cache"
HORIZON = {"1h": 100, "4h": 100, "1d": 30}
LOOKBACK = {"1h": 400, "4h": 180, "1d": 120}
HTF = {"1h": "4h", "4h": "1d", "1d": "1w"}
BUF_ATR = 0.2
FRONT_ATR = 0.05
EQ_TOL_ATR = 0.1
N_CTRL = 10
MATCH = 0.25
SEEDS = 100
VARIANTS = (
    "H3 CRT market all", "H3 CRT market HTF-trend", "H3 CRT limit50 all", "H3 CRT limit50 HTF-trend",
    "H4 IFVG market plain", "H4 IFVG market sweep", "H4 CISD market",
    "H4 IFVG retest plain", "H4 IFVG retest sweep", "H4 CISD retest",
    "H5 run all", "H5 run with 1D", "H5 run against 1D",
)


# ---- range queries on M1 -------------------------------------------------------------------
class RMQ:
    """O(1) max (or min) over [a, b) of an array (sparse table)."""

    def __init__(self, x: np.ndarray, op: str) -> None:
        self.f = np.maximum if op == "max" else np.minimum
        self.lv = [x.astype(float)]
        k = 1
        while (1 << k) <= x.size:
            prev = self.lv[-1]
            h = 1 << (k - 1)
            self.lv.append(self.f(prev[:-h], prev[h:]))
            k += 1

    def q(self, a: int, b: int) -> float:
        if b <= a:
            return math.nan
        k = (b - a).bit_length() - 1
        t = self.lv[k]
        return float(self.f(t[a], t[b - (1 << k)]))


# ---- timeframe data ------------------------------------------------------------------------
@dataclass
class TFData:
    tf: str
    bars: list[Candle]
    o: np.ndarray
    h: np.ndarray
    lo: np.ndarray
    c: np.ndarray
    close_ts: np.ndarray
    atr: np.ndarray
    a: Analysis
    end_i: np.ndarray  # M1 index of the minute opening at each close
    hi_conf: np.ndarray = field(default_factory=lambda: np.zeros(0, int))  # swing highs by confirmed idx
    lo_conf: np.ndarray = field(default_factory=lambda: np.zeros(0, int))


def weekly(days: list[Candle]) -> list[Candle]:
    out: list[Candle] = []
    cur: list[Candle] = []
    key = None
    for d in days:
        k = d.open_time - timedelta(days=d.open_time.weekday())
        if k != key:
            if cur and key is not None and len(cur) == 7:
                out.append(Candle(key, cur[0].open, max(x.high for x in cur), min(x.low for x in cur), cur[-1].close, sum((x.volume for x in cur), start=cur[0].volume * 0)))
            cur, key = [], k
        cur.append(d)
    if cur and key is not None and len(cur) == 7:
        out.append(Candle(key, cur[0].open, max(x.high for x in cur), min(x.low for x in cur), cur[-1].close, sum((x.volume for x in cur), start=cur[0].volume * 0)))
    return out


def tfdata(m: Market, tf: str, p: SmcParams, bars: list[Candle] | None = None) -> TFData:
    end = m.bars[-1].open_time + MINUTE
    if bars is None:
        bars = aggregate(m.bars, tf, end)
    ln = 7 * 86400 if tf == "1w" else length(tf).total_seconds()
    a = analyze(bars, "1d" if tf == "1w" else tf, replace(p, fvg_min_atr=Decimal(0)))
    close_ts = np.array([b.open_time.timestamp() + ln for b in bars])
    d = TFData(
        tf, bars,
        np.array([float(b.open) for b in bars]), np.array([float(b.high) for b in bars]),
        np.array([float(b.low) for b in bars]), np.array([float(b.close) for b in bars]),
        close_ts, np.array([float(x) if x is not None else math.nan for x in a.atr]), a,
        np.searchsorted(m.t, close_ts, side="left"),
    )
    d.hi_conf = np.array(sorted((s.confirmed_idx, s.idx) for s in a.swings if s.kind == "HIGH"), dtype=int).reshape(-1, 2)
    d.lo_conf = np.array(sorted((s.confirmed_idx, s.idx) for s in a.swings if s.kind == "LOW"), dtype=int).reshape(-1, 2)
    return d


@dataclass
class MarketData:
    m: Market
    rh: RMQ
    rl: RMQ
    tfs: dict[str, TFData]

    def unswept(self, level: float, up: bool, from_ts: float, to_i: int) -> bool:
        a = int(np.searchsorted(self.m.t, from_ts, side="left"))
        if a >= to_i:
            return True
        return self.rh.q(a, to_i) <= level if up else self.rl.q(a, to_i) >= level

    def trend(self, tf: str, ts: float) -> int:
        d = self.tfs[tf]
        k = int(np.searchsorted(d.close_ts, ts, side="right")) - 1
        return int(d.a.trend[k]) if k >= 0 else 0


def market_data(m: Market, p: SmcParams) -> MarketData:
    tfs = {tf: tfdata(m, tf, p) for tf in ("1h", "4h", "1d")}
    tfs["1w"] = tfdata(m, "1w", p, weekly(tfs["1d"].bars))
    return MarketData(m, RMQ(m.h, "max"), RMQ(m.lo, "min"), tfs)


# ---- liquidity -------------------------------------------------------------------------------
def pools(md: MarketData, tf: str, k: int, to_i: int, ts: float, up: bool, atr: float, singles: bool) -> list[tuple[float, str]]:
    """Unswept liquidity known at ts (M1 checks up to minute to_i, exclusive): same-TF swings
    confirmed by bar k (singles), equal highs / lows (>= 2 swings within EQ_TOL_ATR x ATR),
    previous day / week high or low. up = highs (above price)."""
    d = md.tfs[tf]
    ln = d.close_ts[1] - d.close_ts[0] if d.close_ts.size > 1 else 3600
    conf = d.hi_conf if up else d.lo_conf
    n = int(np.searchsorted(conf[:, 0], k, side="right")) if conf.size else 0
    lv: list[float] = []
    for _, idx in conf[:n]:
        if idx < k - LOOKBACK.get(tf, 400):
            continue
        px = float(d.h[idx] if up else d.lo[idx])
        if md.unswept(px, up, float(d.close_ts[idx]), to_i):
            lv.append(px)
    out: list[tuple[float, str]] = []
    if singles:
        out += [(x, "swing") for x in lv]
    xs = sorted(lv)
    tol = EQ_TOL_ATR * atr
    used = [False] * len(xs)
    for i in range(len(xs)):
        if used[i]:
            continue
        grp = [i]
        j = i
        while j + 1 < len(xs) and xs[j + 1] - xs[j] <= tol:
            j += 1
            grp.append(j)
        if len(grp) >= 2:
            for g in grp:
                used[g] = True
            out.append((xs[grp[-1]] if up else xs[grp[0]], "equal"))
    for name, per in (("1d", "PD"), ("1w", "PW")):
        dd = md.tfs[name]
        kd = int(np.searchsorted(dd.close_ts, ts, side="right")) - 1
        if kd >= 0:
            px = float(dd.h[kd] if up else dd.lo[kd])
            if md.unswept(px, up, float(dd.close_ts[kd]), to_i):
                out.append((px, per + ("H" if up else "L")))
    del ln
    return out


def nearest_beyond(cands: list[tuple[float, str]], d: int, entry: float, front: float, tick: float) -> tuple[float, str] | None:
    best = None
    for lvl, src in cands:
        px = rnd(lvl - d * front, tick, -d)  # rounded towards the entry
        if (px - entry) * d <= 0:
            continue
        if best is None or (px - entry) * d < (best[0] - entry) * d:
            best = (px, src)
    return best


def rnd(v: float, tick: float, d: int) -> float:
    """Round to the tick: d > 0 up, d < 0 down."""
    return (math.ceil(v / tick - 1e-9) if d > 0 else math.floor(v / tick + 1e-9)) * tick


# ---- outcome walk ---------------------------------------------------------------------------------
def _first(mask: np.ndarray) -> int:
    if mask.size == 0:
        return -1
    k = int(mask.argmax())
    return k if mask[k] else -1


def walk(m: Market, i0: int, end_i: int, d: int, entry: float, stop: float, target: float) -> dict[str, Any]:
    """From entry minute i0 to end_i (exclusive). Stop counts in i0, target not; stop first."""
    hi, lo = m.h[i0:end_i], m.lo[i0:end_i]
    r = abs(entry - stop)
    s = _first(lo <= stop if d > 0 else hi >= stop)
    g = _first(hi[1:] >= target if d > 0 else lo[1:] <= target)
    g = g + 1 if g >= 0 else -1
    if s >= 0 and (g < 0 or s <= g):
        x, lvl, kind = s, stop, "SL"
    elif g >= 0:
        x, lvl, kind = g, target, "TP"
    else:
        x, lvl, kind = hi.size - 1, float(m.c[end_i - 1]), "TIME"
    hits = {}
    stop_at = s if s >= 0 else hi.size
    for kk in (1, 2):
        f = _first(hi[1:] >= entry + kk * r if d > 0 else lo[1:] <= entry - kk * r)
        hits[kk] = f >= 0 and f + 1 < stop_at
    upto = stop_at
    best = ((hi[:upto].max() - entry) * d if d > 0 else (entry - lo[:upto].min())) if upto > 0 else 0.0
    worst = 1.0 if s >= 0 else (((entry - lo.min()) if d > 0 else (hi.max() - entry)) / r)
    return {
        "exit_i": i0 + x, "level": lvl, "kind": kind, "gross": (lvl - entry) * d / r,
        "hit1": hits[1], "hit2": hits[2], "tp_first": kind == "TP",
        "mfe": max(0.0, float(best) / r), "mae": max(0.0, float(worst)), "hold_min": x + 1,
    }


# ---- events ----------------------------------------------------------------------------------------
@dataclass
class Ev:
    hyp: str  # H3 / H4 / H5
    mode: str  # market / limit50 / retest
    market: str
    tf: str
    k: int  # signal bar
    t: float  # decision time
    d: int
    entry: float
    stop: float
    target: float
    target_src: str
    ratio: float  # matching key: H3 candle-1 range / ATR, H4 / H5 body / ATR
    stop_atr: float
    flags: dict[str, Any] = field(default_factory=dict)
    entry_i: int = -1  # entry / fill minute (-1: not filled)
    end_i: int = -1  # window end (exclusive)
    status: str = "FILLED"  # FILLED / NOT_PLACED / MISSED_WIN / EXPIRED
    res: dict[str, Any] | None = None
    ctl: list[float] = field(default_factory=list)  # gross R of the matched controls

    @property
    def target_r(self) -> float:
        return abs(self.target - self.entry) / abs(self.entry - self.stop)


def _in_split(m: Market, t: float, end_ts: float, holdout: bool) -> bool:
    split = m.split.timestamp()
    if holdout:
        return t >= split and end_ts <= float(m.t[-1]) + 60
    return end_ts <= split


def _limit(m: Market, ev: Ev, a: int, b: int, end_i: int) -> None:
    """A resting limit at ev.entry over minutes [a, b): fill on touch; a target touched first is a
    missed winner; else expired."""
    hi, lo = m.h[a:b], m.lo[a:b]
    f = _first(lo <= ev.entry if ev.d > 0 else hi >= ev.entry)
    g = _first(hi >= ev.target if ev.d > 0 else lo <= ev.target)
    if f >= 0 and (g < 0 or f <= g):
        ev.entry_i = a + f
        ev.res = walk(m, ev.entry_i, end_i, ev.d, ev.entry, ev.stop, ev.target)
    else:
        ev.status = "MISSED_WIN" if g >= 0 else "EXPIRED"


def crt_events(md: MarketData, tf: str, holdout: bool) -> list[Ev]:
    m, d_ = md.m, md.tfs[tf]
    tick = float(m.tick)
    ln = 86400 if tf == "1d" else length(tf).total_seconds()
    out = []
    for j in range(1, len(d_.bars) - 1):
        atr = d_.atr[j]
        if math.isnan(atr) or atr <= 0:
            continue
        crh, crl = d_.h[j - 1], d_.lo[j - 1]
        hj, lj, cj = d_.h[j], d_.lo[j], d_.c[j]
        up, dn = hj > crh, lj < crl
        if up == dn or not (crl <= cj <= crh):
            continue
        side = -1 if up else 1
        t = float(d_.close_ts[j])
        end_ts = t + HORIZON[tf] * ln
        if not _in_split(m, t, end_ts, holdout):
            continue
        stop = rnd((hj + tick + BUF_ATR * atr) if side < 0 else (lj - tick - BUF_ATR * atr), tick, -side)
        target = rnd((crl + FRONT_ATR * atr) if side < 0 else (crh - FRONT_ATR * atr), tick, side * -1)
        if (target - cj) * side <= 0:
            continue
        i0 = int(d_.end_i[j])
        end_i = int(np.searchsorted(m.t, end_ts, side="left"))
        htf = md.trend(HTF[tf], t)
        base = dict(market=m.symbol, tf=tf, k=j, t=t, d=side, stop=stop, target=target, target_src="CR",
                    ratio=(crh - crl) / atr, flags={"htf": htf * side}, end_i=end_i)
        ev = Ev("H3", "market", entry=cj, stop_atr=abs(cj - stop) / atr, **base)
        ev.entry_i = i0
        ev.res = walk(m, i0, end_i, side, cj, stop, target)
        out.append(ev)
        mid = rnd((hj + lj) / 2, tick, -side)  # away from the market: long down, short up
        lv = Ev("H3", "limit50", entry=mid, stop_atr=abs(mid - stop) / atr, **base)
        if (mid - cj) * side >= 0 or (mid - stop) * side <= 0 or (target - mid) * side <= 0:
            lv.status = "NOT_PLACED"
        else:
            _limit(m, lv, i0, int(np.searchsorted(m.t, t + ln, side="left")), end_i)
        out.append(lv)
    return out


def ifvg_events(md: MarketData, tf: str, holdout: bool) -> list[Ev]:
    m, d_ = md.m, md.tfs[tf]
    a = d_.a
    tick = float(m.tick)
    ln = length(tf).total_seconds()
    out = []
    fvgs = a.fvgs
    by_created: dict[int, list[Any]] = defaultdict(list)
    for z in fvgs:
        by_created[z.created_idx].append(z)
    sweeps = defaultdict(list)
    for s in a.sweeps:
        sweeps[s.direction].append(s.idx)
    for z in fvgs:
        f = z.created_idx
        bull = z.direction is Side.LONG
        lo_b, hi_b = float(z.bottom), float(z.top)
        seg = d_.c[f + 1 : min(f + 1 + LOOKBACK[tf], len(d_.bars))]
        kk = _first(seg < lo_b if bull else seg > hi_b)
        if kk < 0:
            continue
        k = f + 1 + kk
        side = -1 if bull else 1
        atr = d_.atr[k]
        if math.isnan(atr) or atr <= 0:
            continue
        t = float(d_.close_ts[k])
        end_ts = t + HORIZON[tf] * ln
        if not _in_split(m, t, end_ts, holdout):
            continue
        conf = d_.hi_conf if side < 0 else d_.lo_conf
        n = int(np.searchsorted(conf[:, 0], k, side="right")) if conf.size else 0
        if n == 0:
            continue
        sw_idx = int(conf[n - 1, 1])
        sw = d_.h[sw_idx] if side < 0 else d_.lo[sw_idx]
        stop = rnd(sw + side * -1 * (tick + BUF_ATR * atr), tick, -side)
        cj = d_.c[k]
        i0 = int(d_.end_i[k])
        end_i = int(np.searchsorted(m.t, end_ts, side="left"))
        cands = pools(md, tf, k, i0, t, side > 0, atr, singles=True)
        swept = any(k - 12 <= x <= k for x in sweeps[Side.SHORT if side < 0 else Side.LONG])
        cisd = any(
            (nz.direction is (Side.LONG if side > 0 else Side.SHORT))
            and float(nz.bottom) <= hi_b and lo_b <= float(nz.top)
            for nz in by_created.get(k, [])
        )
        body = abs(d_.c[k] - d_.o[k]) / atr
        flags = {"sweep": swept, "cisd": cisd}
        for mode, entry in (("market", cj), ("retest", lo_b if side < 0 else hi_b)):
            if (stop - entry) * side >= 0:
                continue
            tg = nearest_beyond(cands, side, entry, FRONT_ATR * atr, tick)
            if tg is None:
                continue
            ev = Ev("H4", mode, m.symbol, tf, k, t, side, entry, stop, tg[0], tg[1], body,
                    abs(entry - stop) / atr, dict(flags), end_i=end_i)
            if mode == "market":
                ev.entry_i = i0
                ev.res = walk(m, i0, end_i, side, entry, stop, tg[0])
            else:
                _limit(m, ev, i0, int(np.searchsorted(m.t, t + 24 * ln, side="left")), end_i)
            out.append(ev)
    return out


def run_events(md: MarketData, tf: str, holdout: bool) -> list[Ev]:
    m, d_ = md.m, md.tfs[tf]
    tick = float(m.tick)
    ln = length(tf).total_seconds()
    out = []
    for k in range(2, len(d_.bars)):
        atr, atr_prev = d_.atr[k], d_.atr[k - 1]
        if math.isnan(atr) or math.isnan(atr_prev) or atr <= 0:
            continue
        body = d_.c[k] - d_.o[k]
        if abs(body) < atr:
            continue
        side = 1 if body > 0 else -1
        t = float(d_.close_ts[k])
        end_ts = t + HORIZON[tf] * ln
        if not _in_split(m, t, end_ts, holdout):
            continue
        open_i = int(d_.end_i[k - 1])  # the minute bar k opens with
        before = pools(md, tf, k - 1, open_i, t - ln, side > 0, atr_prev, singles=False)
        crossed = [x for x in before if (d_.c[k] - x[0]) * side > 0]
        if not crossed:
            continue
        cj = d_.c[k]
        mid = (d_.h[k] + d_.lo[k]) / 2
        stop = rnd(mid - side * (BUF_ATR * atr + tick), tick, -side)
        if (cj - stop) * side <= 0:
            continue
        i0 = int(d_.end_i[k])
        end_i = int(np.searchsorted(m.t, end_ts, side="left"))
        after = pools(md, tf, k, i0, t, side > 0, atr, singles=False)
        r = abs(cj - stop)
        tg = nearest_beyond(after, side, cj, FRONT_ATR * atr, tick)
        if tg is None or abs(tg[0] - cj) > 4 * r:
            tg = (rnd(cj + side * 2 * r, tick, -side), "2R")
        ev = Ev("H5", "market", m.symbol, tf, k, t, side, cj, stop, tg[0], tg[1], abs(body) / atr,
                r / atr, {"d1": md.trend("1d", t) * side, "pool": [x[1] for x in crossed]}, end_i=end_i)
        ev.entry_i = i0
        ev.res = walk(m, i0, end_i, side, cj, stop, tg[0])
        out.append(ev)
    return out


def variant_of(ev: Ev) -> list[str]:
    v = []
    if ev.hyp == "H3":
        tag = "market" if ev.mode == "market" else "limit50"
        v.append(f"H3 CRT {tag} all")
        if ev.flags["htf"] > 0:
            v.append(f"H3 CRT {tag} HTF-trend")
    elif ev.hyp == "H4":
        tag = ev.mode
        v.append(f"H4 IFVG {tag} plain")
        if ev.flags["sweep"]:
            v.append(f"H4 IFVG {tag} sweep")
        if ev.flags["cisd"]:
            v.append(f"H4 CISD {tag}")
    else:
        v.append("H5 run all")
        if ev.flags["d1"] > 0:
            v.append("H5 run with 1D")
        elif ev.flags["d1"] < 0:
            v.append("H5 run against 1D")
    return v


# ---- matched controls ---------------------------------------------------------------------------
def candidate_pool(md: MarketData, tf: str, hyp: str, holdout: bool) -> tuple[np.ndarray, np.ndarray]:
    """Bars usable as controls: (bar index, matching ratio)."""
    m, d_ = md.m, md.tfs[tf]
    ln = 86400 if tf == "1d" else length(tf).total_seconds()
    ks, rs = [], []
    for k in range(1, len(d_.bars)):
        atr = d_.atr[k]
        if math.isnan(atr) or atr <= 0:
            continue
        t = float(d_.close_ts[k])
        if not _in_split(m, t, t + HORIZON[tf] * ln, holdout):
            continue
        ratio = (d_.h[k - 1] - d_.lo[k - 1]) / atr if hyp == "H3" else abs(d_.c[k] - d_.o[k]) / atr
        ks.append(k)
        rs.append(ratio)
    return np.array(ks, dtype=int), np.array(rs)


def add_controls(md: MarketData, evs: list[Ev], holdout: bool) -> None:
    m = md.m
    pools_: dict[tuple[str, str], tuple[np.ndarray, np.ndarray]] = {}
    for ev in evs:
        if ev.res is None:
            continue
        key = (ev.tf, ev.hyp)
        if key not in pools_:
            pools_[key] = candidate_pool(md, ev.tf, ev.hyp, holdout)
        ks, rs = pools_[key]
        ok = ks[np.abs(rs / ev.ratio - 1) <= MATCH] if ev.ratio > 0 else ks[:0]
        if ok.size == 0:
            continue
        rng = np.random.default_rng(zlib.crc32(f"{ev.market}{ev.tf}{ev.hyp}{ev.mode}{ev.k}".encode()))
        d_ = md.tfs[ev.tf]
        ln = 86400 if ev.tf == "1d" else length(ev.tf).total_seconds()
        for k in rng.choice(ok, size=N_CTRL, replace=True):
            e = float(d_.c[k])
            dist = ev.stop_atr * float(d_.atr[k])
            st = e - ev.d * dist
            tg = e + ev.d * ev.target_r * dist
            t = float(d_.close_ts[k])
            end_i = int(np.searchsorted(m.t, t + HORIZON[ev.tf] * ln, side="left"))
            ev.ctl.append(walk(m, int(d_.end_i[k]), end_i, ev.d, e, st, tg)["gross"])


# ---- statistics ------------------------------------------------------------------------------------
def boot(x: np.ndarray, level: float = 95.0, n: int = 2000, seed: int = 7) -> tuple[float, float, float]:
    if x.size == 0:
        return (math.nan, math.nan, math.nan)
    rng = np.random.default_rng(seed)
    means = x[rng.integers(0, x.size, size=(n, x.size))].mean(axis=1)
    a = (100 - level) / 2
    return float(x.mean()), float(np.percentile(means, a)), float(np.percentile(means, 100 - a))


def net_r(ev: Ev, costs: Costs) -> float:
    assert ev.res is not None
    mk, tk, sp = float(costs.maker_fee), float(costs.taker_fee), float(costs.slippage)
    d = ev.d
    market = ev.mode == "market"
    fill = ev.entry * (1 + d * sp) if market else ev.entry
    fee_in = tk if market else mk
    sl_fill = ev.stop * (1 - d * sp)
    risk = abs(fill - ev.stop) + fill * fee_in + ev.stop * sp + sl_fill * tk
    x = ev.res["level"] * (1 - d * sp)
    return ((x - fill) * d - fill * fee_in - x * tk) / risk


def cost_share(ev: Ev, costs: Costs) -> float:
    mk, tk, sp = float(costs.maker_fee), float(costs.taker_fee), float(costs.slippage)
    fee_in = (tk + sp) if ev.mode == "market" else mk
    c = ev.entry * (fee_in + tk + sp)
    return c / (abs(ev.entry - ev.stop) + c)


def event_summary(evs: list[Ev]) -> dict[str, Any]:
    placed = [e for e in evs if e.status != "NOT_PLACED"]
    filled = [e for e in placed if e.res is not None]
    out: dict[str, Any] = {
        "events": len(evs), "placed": len(placed), "filled": len(filled),
        "fill_rate": len(filled) / len(placed) if placed else math.nan,
        "missed_win": sum(e.status == "MISSED_WIN" for e in placed),
        "expired": sum(e.status == "EXPIRED" for e in placed),
        "not_placed": len(evs) - len(placed),
    }
    if not filled:
        return out
    g = np.array([e.res["gross"] for e in filled])  # type: ignore[index]
    out.update({
        "p_target": float(np.mean([e.res["tp_first"] for e in filled])),  # type: ignore[index]
        "p_1R": float(np.mean([e.res["hit1"] for e in filled])),  # type: ignore[index]
        "p_2R": float(np.mean([e.res["hit2"] for e in filled])),  # type: ignore[index]
        "mfe_med": float(np.median([e.res["mfe"] for e in filled])),  # type: ignore[index]
        "mae_med": float(np.median([e.res["mae"] for e in filled])),  # type: ignore[index]
        "hold_h_med": float(np.median([e.res["hold_min"] for e in filled])) / 60,  # type: ignore[index]
        "target_r_med": float(np.median([e.target_r for e in filled])),
        "gross_mean": boot(g),
    })
    diffs = np.array([e.res["gross"] - np.mean(e.ctl) for e in filled if e.ctl])  # type: ignore[index]
    out["baseline_mean"] = float(np.mean([np.mean(e.ctl) for e in filled if e.ctl])) if diffs.size else math.nan
    out["diff_vs_baseline"] = boot(diffs)
    out["n_diff"] = int(diffs.size)
    return out


def positions(evs: list[Ev]) -> list[Ev]:
    """One position per market at a time, in decision order."""
    out = []
    busy: dict[str, int] = {}
    for e in sorted((e for e in evs if e.res is not None), key=lambda e: (e.t, e.tf)):
        if e.entry_i <= busy.get(e.market, -1):
            continue
        busy[e.market] = int(e.res["exit_i"])  # type: ignore[index]
        out.append(e)
    return out


def trade_stats(tr: list[Ev], costs: Costs, months: float, level: float = 95.0) -> dict[str, Any]:
    if not tr:
        return {"n": 0}
    g = np.array([e.res["gross"] for e in tr])  # type: ignore[index]
    r = np.array([net_r(e, costs) for e in tr])
    eq = np.cumsum(r[np.argsort([e.t for e in tr])])
    dd = float(np.max(np.maximum.accumulate(np.concatenate([[0], eq])) - np.concatenate([[0], eq])))
    loss = -r[r <= 0].sum()

    def by(key: Any) -> dict[str, Any]:
        dd_: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for e, gg, rr in zip(tr, g, r, strict=True):
            dd_[str(key(e))].append((gg, rr))
        return {k: {"n": len(v), "gross": round(sum(x[0] for x in v), 2), "net": round(sum(x[1] for x in v), 2)} for k, v in sorted(dd_.items())}

    return {
        "n": int(r.size), "win": float((r > 0).mean()),
        "gross_mean": boot(g, level), "net_mean": boot(r, level),
        "gross_total": float(g.sum()), "net_total": float(r.sum()),
        "pf": float(r[r > 0].sum() / loss) if loss > 0 else math.inf, "max_dd_r": dd,
        "trades_per_month": r.size / months if months else math.nan,
        "cost_share_1R_med": float(np.median([cost_share(e, costs) for e in tr])),
        "per_market": by(lambda e: e.market), "per_side": by(lambda e: "long" if e.d > 0 else "short"),
        "per_quarter": by(lambda e: f"{datetime.fromtimestamp(e.t, UTC):%Y}-Q{(datetime.fromtimestamp(e.t, UTC).month - 1) // 3 + 1}"),
        "per_tf": by(lambda e: e.tf),
        "exits": dict(sorted({k: sum(1 for e in tr if e.res["kind"] == k) for k in ("TP", "SL", "TIME")}.items())),  # type: ignore[index]
    }


def baseline_beats(tr: list[Ev]) -> tuple[float, float, float]:
    """(variant gross mean, baseline seed-mean average, its 97.5th percentile)."""
    with_ctl = [e for e in tr if e.ctl]
    if not with_ctl:
        return (math.nan, math.nan, math.nan)
    rng = np.random.default_rng(11)
    seeds = [float(np.mean([e.ctl[int(rng.integers(0, len(e.ctl)))] for e in with_ctl])) for _ in range(SEEDS)]
    g = float(np.mean([e.res["gross"] for e in tr]))  # type: ignore[index]
    return g, float(np.mean(seeds)), float(np.percentile(seeds, 97.5))


def is_candidate(s: dict[str, Any], beat: tuple[float, float, float]) -> bool:
    if s.get("n", 0) < 150:
        return False
    pos = sum(1 for v in s["per_market"].values() if v["gross"] > 0)
    return s["gross_mean"][1] > 0 and s["net_mean"][0] > 0 and pos >= 6 and beat[0] > beat[2]


# ---- runner ----------------------------------------------------------------------------------------
def all_events(md: MarketData, holdout: bool) -> list[Ev]:
    evs: list[Ev] = []
    for tf in ("1h", "4h", "1d"):
        evs += crt_events(md, tf, holdout)
    for tf in ("1h", "4h"):
        evs += ifvg_events(md, tf, holdout)
        evs += run_events(md, tf, holdout)
    add_controls(md, evs, holdout)
    return evs


def main() -> None:
    from sp2l.smc.research.momentum import COVERAGE, load_markets

    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout", action="store_true")
    args = ap.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)
    mkts, costs, p = load_markets()
    split = "holdout" if args.holdout else "discovery"
    if args.holdout:
        cands = json.loads((OUT / "candidates.json").read_text())
        if not cands:
            raise SystemExit("no candidates: the holdout is not run and the research line ends (plan §5)")
        if (OUT / "holdout.json").exists():
            raise SystemExit("the holdout was already run once")
    evs: list[Ev] = []
    months = 0.0
    for sym in COVERAGE:
        m = mkts[sym]
        f = CACHE / f"events_{split}_{sym}.pkl"
        if f.exists():
            got = pickle.loads(f.read_bytes())
        else:
            got = all_events(market_data(m, p), args.holdout)
            f.write_bytes(pickle.dumps(got))
        span = (m.split.timestamp() - m.t[0]) if not args.holdout else (m.t[-1] - m.split.timestamp())
        months += span / 86400 / 30.44 / len(COVERAGE)
        print(sym, split, len(got), flush=True)
        evs += got
    groups: dict[str, list[Ev]] = defaultdict(list)
    for e in evs:
        for v in variant_of(e):
            groups[v].append(e)
    if args.holdout:
        res = []
        for v in cands:
            tr = positions(groups[v])
            res.append({"variant": v, **trade_stats(tr, costs, months, 95.0),
                        "gross_mean_90": boot(np.array([e.res["gross"] for e in tr]), 90.0) if tr else None,  # type: ignore[index]
                        "event_study": event_summary(groups[v])})
            g90 = res[-1]["gross_mean_90"]
            res[-1]["validated"] = bool(tr) and res[-1]["net_mean"][0] > 0 and g90[1] > 0
        (OUT / "holdout.json").write_text(json.dumps(res, indent=1, default=str))
        print(json.dumps([(r["variant"], r.get("n"), r.get("validated")) for r in res]))
        return
    step1: dict[str, Any] = {}
    for v in VARIANTS:
        ev = groups.get(v, [])
        row = {"pooled": event_summary(ev)}
        row["per_tf"] = {tf: event_summary([e for e in ev if e.tf == tf]) for tf in sorted({e.tf for e in ev})}
        row["per_market"] = {s: event_summary([e for e in ev if e.market == s]) for s in COVERAGE}
        step1[v] = row
        pr = row["pooled"]
        print("step1", v, pr.get("filled"), pr.get("gross_mean"), pr.get("diff_vs_baseline"), flush=True)
    passers = [v for v in VARIANTS if not math.isnan(step1[v]["pooled"].get("diff_vs_baseline", (math.nan,) * 3)[1])
               and step1[v]["pooled"]["diff_vs_baseline"][1] > 0]
    (OUT / "step1.json").write_text(json.dumps({"variants": step1, "passers": passers, "months": months}, indent=1, default=str))
    print("step 1 passers", passers, flush=True)
    step2 = {}
    for v in passers:
        tr = positions(groups[v])
        s = trade_stats(tr, costs, months)
        beat = baseline_beats(tr)
        s["baseline"] = {"gross_mean": beat[0], "random_mean": beat[1], "random_p97_5": beat[2]}
        s["candidate"] = is_candidate(s, beat)
        step2[v] = s
        print("step2", v, s["n"], s["gross_mean"], s["net_mean"], s["candidate"], flush=True)
    (OUT / "step2.json").write_text(json.dumps(step2, indent=1, default=str))
    cands = sorted((v for v, s in step2.items() if s["candidate"]), key=lambda v: -step2[v]["gross_mean"][1])[:3]
    (OUT / "candidates.json").write_text(json.dumps(cands))
    print("candidates", cands)


if __name__ == "__main__":  # run under the module's own name so cached events unpickle anywhere
    from sp2l.smc.research.breakout import main as _main

    _main()
