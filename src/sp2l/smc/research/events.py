"""Event study: what price does after the first touch of a zone, against random levels.

Zones come from the engine's own structure code (`structure.analyze`) on one timeframe:
OB_last (last opposite candle), OB_extreme (extreme candle), FVG, OB_adjFVG (OB with the FVG
right after it), OB_adjFVG_sweep (that, after a sweep within 12 bars before the break) and
candle_adjFVG (any opposite candle followed by an adjacent FVG, no break of structure).

An event is the FIRST minute after the zone is known that trades into it (entry = the near
edge, stop = one tick beyond the far edge, 1R = their distance). From that minute on, on M1:
did price reach +1R / +2R / +3R before -1R (a minute reaching both counts as the stop; no
target in the touch minute), the MFE / MAE in R and the hold time. `second` gives the same for
the second touch (price first moved 0.5R away, then came back, the stop untouched).

Each event gets a random control level: same market and timeframe, a random time within
+-7 days, the same distance below (long) / above (short) the price and the same width, both
in ATR of the timeframe at that time; it is measured the same way. All before costs.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

from sp2l.smc.model import Analysis, SmcParams
from sp2l.smc.research.data import Market
from sp2l.smc.structure import analyze
from sp2l.smc.timeframes import MINUTE, aggregate, length

HORIZON_BARS = 100  # outcome window after the touch, in bars of the zone timeframe
CONTROL_DAYS = 7
SWEEP_MAX_BARS = 12


@dataclass
class RZone:
    kind: str
    direction: int  # +1 long (demand) / -1 short (supply)
    top: float
    bottom: float
    known: float  # epoch s: when the zone is tradeable (its last bar closed)
    born: float  # epoch s: open of the zone candle
    atr: float  # ATR(tf) when known
    close: float  # last close when known
    sweep_wick: float | None = None

    @property
    def entry(self) -> float:
        return self.top if self.direction > 0 else self.bottom


@dataclass
class Event:
    market: str
    tf: str
    kind: str
    direction: int
    touch: float  # epoch s of the touch minute
    split: str  # discovery / holdout
    r: float  # 1R in price
    hit: dict[int, bool]
    mfe: float
    mae: float
    hold_min: float
    bias4h: int  # +1 aligned / -1 against / 0 undefined
    bias1h: int
    discount: bool
    control: dict[str, Any] | None = None  # the same measures for the random level
    second: dict[str, Any] | None = None  # the second touch of the zone
    attrs: dict[str, Any] = field(default_factory=dict)


def _first(mask: np.ndarray) -> int:
    if mask.size == 0:
        return -1
    k = int(mask.argmax())
    return k if mask[k] else -1


def outcome(
    m: Market, i: int, direction: int, entry: float, stop: float, horizon: int
) -> dict[str, Any] | None:
    """The measures from touch minute i (None if the window is cut by the data's end)."""
    end = i + horizon
    if end > len(m.t):
        return None
    hi, lo = m.h[i:end], m.lo[i:end]
    r = abs(entry - stop)
    if r <= 0:
        return None
    adverse = lo <= stop if direction > 0 else hi >= stop
    s = _first(adverse)
    stop_at = s if s >= 0 else horizon
    hit: dict[int, bool] = {}
    t1 = -1
    for k in (1, 2, 3):
        lvl = entry + direction * k * r
        fav = (hi[1:] >= lvl) if direction > 0 else (lo[1:] <= lvl)
        f = _first(fav)
        f = f + 1 if f >= 0 else -1
        hit[k] = f >= 0 and f < stop_at
        if k == 1:
            t1 = f if hit[k] else -1
    upto = stop_at if s >= 0 else horizon  # MFE before the stop minute (order unknown in it)
    if upto > 0:
        best = (hi[:upto].max() - entry) if direction > 0 else (entry - lo[:upto].min())
    else:
        best = 0.0
    worst = 1.0 if s >= 0 else (((entry - lo.min()) if direction > 0 else (hi.max() - entry)) / r)
    return {
        "hit": hit,
        "mfe": max(0.0, float(best) / r),
        "mae": max(0.0, float(worst)),
        "hold_min": float(t1 if t1 >= 0 else stop_at),
    }


def _touch(m: Market, i0: int, i1: int, direction: int, entry: float) -> int:
    seg = m.lo[i0:i1] <= entry if direction > 0 else m.h[i0:i1] >= entry
    k = _first(seg)
    return i0 + k if k >= 0 else -1


def zones(m: Market, tf: str, p: SmcParams) -> tuple[list[RZone], Analysis]:
    """Every zone of the six kinds on one timeframe."""
    end = m.bars[-1].open_time + MINUTE
    bars = aggregate(m.bars, tf, end)
    base = replace(
        p,
        ob_require_fvg=False,
        ob_min_atr=type(p.ob_min_atr)(0),
        fvg_min_atr=type(p.fvg_min_atr)(0),
    )
    a = analyze(bars, tf, replace(base, ob_rule="last_opposite"))
    ax = analyze(bars, tf, replace(base, ob_rule="extreme"))
    ln = length(tf).total_seconds()
    ts = [b.open_time.timestamp() for b in bars]
    atr = [float(x) if x is not None else 0.0 for x in a.atr]

    def mk(
        kind: str, d: int, top: Any, bot: Any, known_i: int, born_i: int, wick: Any = None
    ) -> RZone:
        return RZone(
            kind,
            d,
            float(top),
            float(bot),
            ts[known_i] + ln,
            ts[born_i],
            atr[known_i],
            float(bars[known_i].close),
            None if wick is None else float(wick),
        )

    out: list[RZone] = []
    events = {e.id: e for e in a.events}
    for z in a.order_blocks:
        d = 1 if z.direction.value == "LONG" else -1
        if not atr[z.created_idx]:
            continue
        out.append(mk("OB_last", d, z.top, z.bottom, z.created_idx, z.idx))
        j = z.idx
        if j + 2 < len(bars):
            gap = bars[j].high < bars[j + 2].low if d > 0 else bars[j + 2].high < bars[j].low
            if gap:
                k = max(z.created_idx, j + 2)
                out.append(mk("OB_adjFVG", d, z.top, z.bottom, k, j))
                ev = events.get(z.event_id or "")
                if ev is not None:
                    sw = [
                        s
                        for s in a.sweeps
                        if s.direction is z.direction
                        and s.idx <= j
                        and s.idx >= ev.break_idx - SWEEP_MAX_BARS
                    ]
                    if sw:
                        out.append(mk("OB_adjFVG_sweep", d, z.top, z.bottom, k, j, sw[-1].wick))
    for z in ax.order_blocks:
        if atr[z.created_idx]:
            d = 1 if z.direction.value == "LONG" else -1
            out.append(mk("OB_extreme", d, z.top, z.bottom, z.created_idx, z.idx))
    for z in a.fvgs:
        if atr[z.created_idx]:
            d = 1 if z.direction.value == "LONG" else -1
            out.append(mk("FVG", d, z.top, z.bottom, z.created_idx, z.idx - 1))
    for j in range(len(bars) - 2):
        b0, b2 = bars[j], bars[j + 2]
        if not atr[j + 2]:
            continue
        if b0.close < b0.open and b2.low > b0.high:
            out.append(mk("candle_adjFVG", 1, b0.high, b0.low, j + 2, j))
        elif b0.close > b0.open and b2.high < b0.low:
            out.append(mk("candle_adjFVG", -1, b0.high, b0.low, j + 2, j))
    return out, a


class Trend:
    """Bias of a timeframe at any time (its trend after the last closed bar)."""

    def __init__(self, a: Analysis) -> None:
        ln = length(a.tf).total_seconds()
        self.close = np.array([b.open_time.timestamp() + ln for b in a.bars])
        self.trend = np.array(a.trend)

    def at(self, ts: float) -> int:
        k = int(np.searchsorted(self.close, ts, side="right")) - 1
        return int(self.trend[k]) if k >= 0 else 0


def study(
    m: Market, tf: str, p: SmcParams, bias4h: Trend, bias1h: Trend, seed: int = 1
) -> list[Event]:
    zs, a = zones(m, tf, p)
    rnd = random.Random(seed)
    ln_min = int(length(tf).total_seconds() // 60)
    horizon = HORIZON_BARS * ln_min
    window = p.lookback(tf) * ln_min  # a zone expires after the timeframe's lookback
    tick = float(m.tick)
    atr_close = np.array([b.open_time.timestamp() + length(tf).total_seconds() for b in a.bars])
    atr = np.array([float(x) if x is not None else 0.0 for x in a.atr])
    split_ts = m.split.timestamp()
    out: list[Event] = []
    for z in zs:
        i0 = m.index_at(z.known)
        i = _touch(m, i0, min(i0 + window, len(m.t)), z.direction, z.entry)
        if i < 0:
            continue
        stop = (z.bottom - tick) if z.direction > 0 else (z.top + tick)
        o = outcome(m, i, z.direction, z.entry, stop, horizon)
        if o is None:
            continue
        touch = float(m.t[i])
        # discount / premium of the entry inside sweep wick (else far edge) -> leg extreme
        ib = m.index_at(z.born)
        start = (
            z.sweep_wick if z.sweep_wick is not None else (z.bottom if z.direction > 0 else z.top)
        )
        ext = float(m.h[ib:i].max() if z.direction > 0 else m.lo[ib:i].min()) if i > ib else z.entry
        mid = (start + ext) / 2
        disc = z.entry <= mid if z.direction > 0 else z.entry >= mid
        ev = Event(
            m.symbol,
            tf,
            z.kind,
            z.direction,
            touch,
            "discovery" if touch < split_ts else "holdout",
            abs(z.entry - stop),
            o["hit"],
            o["mfe"],
            o["mae"],
            o["hold_min"],
            bias4h.at(touch) * z.direction,
            bias1h.at(touch) * z.direction,
            disc,
        )
        # the second touch: price went 0.5R away, came back, the stop untouched meanwhile
        r = abs(z.entry - stop)
        away = (
            m.h[i + 1 : i + horizon] >= z.entry + 0.5 * r
            if z.direction > 0
            else m.lo[i + 1 : i + horizon] <= z.entry - 0.5 * r
        )
        ka = _first(away)
        if ka >= 0:
            ja = i + 1 + ka
            stop_seg = m.lo[i:ja] <= stop if z.direction > 0 else m.h[i:ja] >= stop
            if _first(stop_seg) < 0:
                j2 = _touch(m, ja + 1, min(ja + 1 + window, len(m.t)), z.direction, z.entry)
                if j2 >= 0:
                    ev.second = outcome(m, j2, z.direction, z.entry, stop, horizon)
        # the random control level
        dist = z.direction * (z.close - z.entry) / z.atr if z.atr else -1
        width = (z.top - z.bottom) / z.atr if z.atr else 0
        if dist >= 0 and width > 0:
            lo_ts = max(float(m.t[0]) + 30 * 86400, z.known - CONTROL_DAYS * 86400)
            hi_ts = min(float(m.t[-1]) - horizon * 60 - window * 60, z.known + CONTROL_DAYS * 86400)
            if ev.split == "discovery":
                hi_ts = min(hi_ts, split_ts)
            else:
                lo_ts = max(lo_ts, split_ts)
            if hi_ts > lo_ts:
                tc = rnd.uniform(lo_ts, hi_ts)
                ic = m.index_at(tc)
                k = int(np.searchsorted(atr_close, tc, side="right")) - 1
                ac = atr[k] if k >= 0 else 0.0
                if ic > 0 and ac > 0:
                    pc = float(m.c[ic - 1])
                    e_c = pc - z.direction * dist * ac
                    far = e_c - z.direction * width * ac
                    st_c = far - z.direction * tick
                    jc = _touch(m, ic, min(ic + window, len(m.t)), z.direction, e_c)
                    if jc >= 0:
                        ev.control = outcome(m, jc, z.direction, e_c, st_c, horizon)
        out.append(ev)
    return out


def bootstrap_diff(
    z: np.ndarray, c: np.ndarray, n: int = 2000, seed: int = 7
) -> tuple[float, float, float]:
    """mean(z - c) over paired observations with a 95 % bootstrap interval."""
    d = z.astype(float) - c.astype(float)
    if d.size == 0:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, d.size, size=(n, d.size))
    means = d[idx].mean(axis=1)
    return float(d.mean()), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))
