"""The four playbook strategies on closed 1h bars, long and short, and their simulation.

Every rule is decided at the close of a bar from that bar and earlier ones only (pivots once
confirmed, the 4h EMA 200 from the last CLOSED 4h bar); orders act from the next bar. One setup
at a time per strategy (pending or open), as the guide's "one unit".

Fills and exits, conservatively:
- market entry (Donchian): the next bar's open; stop entry: the stop price, or the open when the
  bar gaps through it; limit entry: the limit, or the open when it gaps through it
- on the fill bar only the stop is checked (a target there is not counted)
- stop and target in one bar: the stop; a gap through the stop exits at the open
- costs: Tabdeal's fees from the config (taker + slippage allowance for market / stop orders and
  stop exits, maker for limits and targets) and funding at every funding time held
"""

from __future__ import annotations

import bisect
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sp2l.playbook import indicators as ind

H1, H4 = 3600, 14400


@dataclass(frozen=True)
class Params:
    htf_filter: bool = True  # close above (long) / below (short) the last closed 4h EMA 200
    min_stop: float = 0.007  # stop distance, fraction of the entry: outside [min, max] no trade
    max_stop: float = 0.03
    # 1 previous-high breakout, Donchian 26-bar exit, long only (docs/PLAYBOOK.md): a bullish
    # candle whose body crosses the previous high (the highest unbroken swing high of the last
    # prev_high_bars bars); it replaced the 48-bar high, which drops when an old high leaves it
    high_piv: int = 5  # swing-high strength (bars each side), as the chart's structure labels
    prev_high_bars: int = 240
    exit_len: int = 26
    n_len: int = 20
    n_mult: float = 2.0
    # 2 EMA 50 pullback
    ema_len: int = 50
    adx_min: float = 20.0
    pull_len: int = 15
    pull_atr: float = 2.0
    slope_bars: int = 5
    ema_rr: float = 3.0
    stop_valid: int = 3  # bars a stop entry rests (EMA 50, VWAP)
    # 3 liquidity sweep + structure + FVG / OB
    piv: int = 3  # pivot strength (bars each side) of equal lows / highs and the last swing
    eq_tol_atr: float = 0.25
    liq_bars: int = 72  # equal lows / highs looked for in the last liq_bars bars
    sweep_within: int = 12  # the displacement comes at most this many bars after the sweep
    disp_atr: float = 1.5
    fvg_atr: float = 0.5
    sl_atr: float = 0.1
    smc_min_rr: float = 2.0
    smc_valid: int = 24  # bars the limit rests (the guide sets none: our choice)
    tp_back: int = 24  # the target: the extreme of the move into the equal lows / highs
    # 4 anchored VWAP pullback
    anchor_window: int = 120
    anchor_age: int = 12
    anchor_move_atr: float = 3.0  # the decline (rally) into the anchor, in ATR
    strength_bars: int = 15
    vwap_rr: float = 2.0


@dataclass(frozen=True)
class Costs:
    maker: float = 0.0
    taker: float = 0.0
    slippage: float = 0.0
    funding_rate: float = 0.0
    funding_h: int = 8


Check = dict[str, Any]  # {"label", "ok", "value", "unit": price | x (a ratio) | % | time}


def check(label: str, ok: bool | None, value: Any = None, unit: str = "price") -> Check:
    return {"label": label, "ok": ok, "value": value, "unit": unit}


@dataclass
class Setup:
    strategy: str
    side: str
    signal_i: int
    kind: str  # market | stop | limit
    entry: float  # order price (market: the signal close, an estimate)
    sl: float
    tp: float | None
    valid: int = 1
    status: str = "pending"  # pending, open, tp, sl, exit, expired, cancelled, missed, rejected
    reason: str = ""
    cancel: Callable[[int], bool] | None = None
    marks: dict[str, Any] = field(default_factory=dict)
    n: float = 0.0  # Donchian: N at the signal (the stop is set from the fill)
    fill_i: int | None = None
    fill: float | None = None
    exit_i: int | None = None
    exit_price: float | None = None
    exit_next: bool = False
    end_i: int | None = None

    @property
    def d(self) -> int:
        return 1 if self.side == "LONG" else -1


class Ctx:
    """Bars and indicators of one market; ema4[i] = EMA 200 of the 4h bars closed by the close
    of 1h bar i."""

    def __init__(self, bars: Sequence[dict[str, Any]], htf: Sequence[dict[str, Any]], p: Params,
                 tf_s: int = H1, htf_s: int = H4) -> None:
        self.p, self.tf_s = p, tf_s
        self.t = [int(b["t"]) for b in bars]
        self.o = [float(b["o"]) for b in bars]
        self.h = [float(b["h"]) for b in bars]
        self.l = [float(b["l"]) for b in bars]
        self.c = [float(b["c"]) for b in bars]
        self.v = [float(b.get("v") or 0.0) for b in bars]
        self.n = len(self.c)
        h, lo, c = self.h, self.l, self.c
        self.atr14 = ind.atr(h, lo, c, 14)
        self.atr20 = ind.atr(h, lo, c, p.n_len)
        self.ema50 = ind.ema(c, p.ema_len)
        self.adx = ind.adx(h, lo, c, 14)
        self.prev_hi = ind.previous_highs(h, c, p.high_piv, p.prev_high_bars)
        self.hi_out, self.lo_out = ind.prev_max(h, p.exit_len), ind.prev_min(lo, p.exit_len)
        self.ph, self.pl = ind.pivots(h, p.piv, True), ind.pivots(lo, p.piv, False)
        # the 4h EMA 200, from closed 4h bars only
        t4 = [int(b["t"]) for b in htf]
        e4 = ind.ema([float(b["c"]) for b in htf], 200)
        self.ema4: list[float | None] = []
        k = -1
        for t in self.t:
            while k + 1 < len(t4) and t4[k + 1] + htf_s <= t + tf_s:
                k += 1
            self.ema4.append(e4[k] if k >= 0 else None)
        # cumulative (typical price x volume) and volume for anchored VWAPs
        self.cpv, self.cv = [0.0], [0.0]
        for i in range(self.n):
            vol = self.v[i] if self.v[i] > 0 else 1.0  # no volume: equal weights
            self.cpv.append(self.cpv[-1] + (h[i] + lo[i] + c[i]) / 3 * vol)
            self.cv.append(self.cv[-1] + vol)
        self.memo: dict[tuple[int, int], Any] = {}
        self.anchor = {1: [self._anchor(i, 1) for i in range(self.n)],
                       -1: [self._anchor(i, -1) for i in range(self.n)]}

    def vwap(self, a: int, i: int) -> float:
        return (self.cpv[i + 1] - self.cpv[a]) / (self.cv[i + 1] - self.cv[a])

    def _anchor(self, i: int, d: int) -> int | None:
        """The swing low (long) / high (short) the VWAP is anchored at, as known at bar i: the
        extreme of bars [i - window, i - age] after a move of >= anchor_move_atr ATR into it,
        not exceeded since."""
        p = self.p
        lo, hi = max(0, i - p.anchor_window), i - p.anchor_age
        if hi - lo < 10:
            return None
        xs = self.l if d > 0 else self.h
        a = min(range(lo, hi + 1), key=lambda j: d * xs[j])
        if any(d * (xs[j] - xs[a]) <= 0 for j in range(a + 1, i + 1)):
            return None
        atr = self.atr14[a]
        if atr is None or a < 1:
            return None
        before = range(max(0, a - 48), a)
        far = max(self.h[j] for j in before) if d > 0 else min(self.l[j] for j in before)
        return a if d * (far - xs[a]) >= p.anchor_move_atr * atr else None


# ---- the strategies ---------------------------------------------------------------------------
def _side(d: int) -> str:
    return "LONG" if d > 0 else "SHORT"


def _htf(x: Ctx, i: int, d: int) -> Check:
    e = x.ema4[i]
    if not x.p.htf_filter:
        return check("4h EMA 200 filter (off)", True, e)
    ok = e is not None and d * (x.c[i] - e) > 0
    return check(f"Close {'above' if d > 0 else 'below'} the 4h EMA 200", ok, e)


def _stop_range(x: Ctx, entry: float, sl: float) -> Check:
    f = abs(entry - sl) / entry if entry else 0.0
    ok = x.p.min_stop <= f <= x.p.max_stop
    return check(f"Stop {f * 100:.2f}% within {x.p.min_stop * 100:g}–{x.p.max_stop * 100:g}%",
                 ok, round(f * 100, 3), "%")


def _finish(x: Ctx, name: str, i: int, d: int, checks: list[Check], core: int,
            build: Callable[[], Setup] | None) -> tuple[list[Check], Setup | None]:
    """checks[:core] are the pattern; the rest are filters. The pattern without a filter is a
    rejected setup (shown, never traded)."""
    if build is None or not all(c["ok"] for c in checks[:core]):
        return checks, None
    s = build()
    failed = [c["label"] for c in checks[core:] if not c["ok"]]
    if failed:
        s.status, s.reason = "rejected", "; ".join(failed)
    return checks, s


def donchian(x: Ctx, i: int, d: int) -> tuple[list[Check], Setup | None]:
    """Long only: a bullish candle whose body crosses the previous high (open at or below it,
    close above it). Entry at the next open, stop 2N, exit on a close below the 26-bar low."""
    p = x.p
    prev = x.prev_hi[i]
    n = x.atr20[i]
    if d < 0:
        return [], None
    if n is None:
        return [check("Not enough bars yet", False)], None
    if prev is None:
        return [check(f"A previous high (an unbroken swing high of the last {p.prev_high_bars} "
                      "bars)", False)], None
    lvl, at = prev
    o, c = x.o[i], x.c[i]
    sl = c - p.n_mult * n
    checks = [
        check("A bullish candle whose body crosses the previous high", c > o and o <= lvl < c,
              lvl),
        _htf(x, i, d),
        _stop_range(x, c, sl),
    ]

    def build() -> Setup:
        return Setup("donchian", _side(d), i, "market", c, sl, None, 1, n=n,
                     marks={"level": lvl, "level_t": x.t[at], "n": n})

    return _finish(x, "donchian", i, d, checks, 1, build)


def donchian_exit(x: Ctx, j: int, d: int) -> bool:
    lvl = x.lo_out[j] if d > 0 else x.hi_out[j]
    return lvl is not None and d * (x.c[j] - lvl) < 0


def ema_pullback(x: Ctx, i: int, d: int) -> tuple[list[Check], Setup | None]:
    p = x.p
    e, a, adx, e4 = x.ema50[i], x.atr14[i], x.adx[i], x.ema4[i]
    if e is None or a is None or adx is None or i < max(p.pull_len, p.slope_bars, 3):
        return [check("Not enough bars yet", False)], None
    e5 = x.ema50[i - p.slope_bars]
    o, h, lo, c = x.o[i], x.h[i], x.l[i], x.c[i]
    if d > 0:
        pull = max(x.h[i - p.pull_len:i]) - lo
        touch = lo <= e and c > e and c > o
        entry = h
        sl = min(x.l[i - 2:i + 1]) - 0.5 * a
    else:
        pull = h - min(x.l[i - p.pull_len:i])
        touch = h >= e and c < e and c < o
        entry = lo
        sl = max(x.h[i - 2:i + 1]) + 0.5 * a
    tp = entry + d * p.ema_rr * abs(entry - sl)
    word = "above" if d > 0 else "below"
    checks = [
        check(f"EMA {p.ema_len} {word} the 4h EMA 200", e4 is not None and d * (e - e4) > 0, e),
        check(f"EMA {p.ema_len} {'rising' if d > 0 else 'falling'} ({p.slope_bars} bars)",
              e5 is not None and d * (e - e5) > 0, e5),
        check(f"ADX(14) > {p.adx_min:g}", adx > p.adx_min, round(adx, 1), "x"),
        check(f"Pullback >= {p.pull_atr:g} ATR from the {p.pull_len}-bar "
              f"{'high' if d > 0 else 'low'}", pull >= p.pull_atr * a, round(pull / a, 2), "x"),
        check(f"Signal candle: {'low' if d > 0 else 'high'} reaches EMA {p.ema_len}, "
              f"{'bullish' if d > 0 else 'bearish'} close {word} it", touch, e),
        _htf(x, i, d),
        _stop_range(x, entry, sl),
    ]

    def build() -> Setup:
        def cancel(j: int) -> bool:
            return d * (x.c[j] - (lo if d > 0 else h)) < 0

        return Setup("ema", _side(d), i, "stop", entry, sl, tp, p.stop_valid, cancel=cancel,
                     marks={"ema": e, "cancel": lo if d > 0 else h})

    return _finish(x, "ema", i, d, checks, 5, build)


def _equal(x: Ctx, s: int, d: int) -> tuple[int, int, float] | None:
    """Equal lows (long) / highs (short) unswept before bar s and swept by s: (a, b, level)."""
    p = x.p
    pv = x.pl if d > 0 else x.ph
    xs = x.l if d > 0 else x.h
    atr = x.atr14[s]
    if atr is None:
        return None
    hi = bisect.bisect_right(pv, s - 1 - p.piv) - 1  # pivots confirmed by bar s - 1
    lo = bisect.bisect_left(pv, s - p.liq_bars)
    cand = pv[lo:hi + 1]
    for bi in range(len(cand) - 1, 0, -1):
        b = cand[bi]
        for ai in range(bi - 1, -1, -1):
            a = cand[ai]
            if abs(xs[a] - xs[b]) > p.eq_tol_atr * atr:
                continue
            level = min(xs[a], xs[b]) if d > 0 else max(xs[a], xs[b])
            inner = max(xs[a], xs[b]) if d > 0 else min(xs[a], xs[b])
            if any(d * (xs[j] - level) < 0 for j in range(a + 1, s)):
                continue  # swept before
            if d * (xs[s] - level) < 0 and d * (x.c[s] - inner) > 0:
                return a, b, level
    return None


def _liquidity(x: Ctx, i: int, d: int) -> float | None:
    """The nearest unswept equal lows / highs at bar i (shown while waiting for a sweep)."""
    p = x.p
    pv = x.pl if d > 0 else x.ph
    xs = x.l if d > 0 else x.h
    atr = x.atr14[i]
    if atr is None:
        return None
    hi = bisect.bisect_right(pv, i - p.piv) - 1
    lo = bisect.bisect_left(pv, i - p.liq_bars)
    cand = pv[lo:hi + 1]
    for bi in range(len(cand) - 1, 0, -1):
        for ai in range(bi - 1, -1, -1):
            a, b = cand[ai], cand[bi]
            if abs(xs[a] - xs[b]) <= p.eq_tol_atr * atr:
                level = min(xs[a], xs[b]) if d > 0 else max(xs[a], xs[b])
                if all(d * (xs[j] - level) >= 0 for j in range(a + 1, i + 1)):
                    return level
    return None


def _sweep_at(x: Ctx, s: int, d: int) -> tuple[int, int, float] | None:
    key = (s, d)
    if key not in x.memo:
        x.memo[key] = _equal(x, s, d)
    found: tuple[int, int, float] | None = x.memo[key]
    return found


def smc(x: Ctx, i: int, d: int) -> tuple[list[Check], Setup | None]:
    """Equal lows swept, then within sweep_within bars a close beyond the swing high standing at
    the sweep (BOS), the move carrying a displacement candle (body >= disp_atr ATR) with an FVG
    (>= fvg_atr ATR) around it. Known at bar i when its last part completes there."""
    p = x.p
    if i < max(p.liq_bars // 2, 30) or x.atr14[i] is None:
        return [check("Not enough bars yet", False)], None
    o, c, h, lo = x.o, x.c, x.h, x.l
    word = "lows" if d > 0 else "highs"
    pv = x.ph if d > 0 else x.pl
    liq = _liquidity(x, i, d)
    best: tuple[int, ...] | None = None
    seen: dict[str, float | int | None] = {"sweep": None, "bos": None, "disp": None}
    for s in range(i - 1, max(i - p.sweep_within - 1, 1) - 1, -1):
        eq = _sweep_at(x, s, d)
        if eq is None:
            continue
        seen["sweep"] = seen["sweep"] or eq[2]
        k = bisect.bisect_right(pv, s - p.piv) - 1  # the swing standing at the sweep
        if k < 0:
            continue
        q = pv[k]
        lvl = h[q] if d > 0 else lo[q]
        brk = next((j for j in range(s + 1, min(s + p.sweep_within, i) + 1)
                    if d * (c[j] - lvl) > 0), None)
        if brk is None:
            continue
        seen["bos"] = seen["bos"] or lvl
        disp = None
        for j in range(s + 1, min(brk, i - 1) + 1):  # the displacement and its FVG (j + 1 <= i)
            a = x.atr14[j] or 0.0
            gap = (lo[j + 1] - h[j - 1]) if d > 0 else (lo[j - 1] - h[j + 1])
            if d * (c[j] - o[j]) >= p.disp_atr * a and gap >= p.fvg_atr * a:
                disp = j
                break
        if disp is None:
            continue
        seen["disp"] = seen["disp"] or disp
        if max(brk, disp + 1) == i:  # completes at this bar
            best = (s, *eq[:2], q, brk, disp)
            break
    checks: list[Check] = [
        check(f"Unswept equal {word} (liquidity)", liq is not None or seen["sweep"] is not None,
              liq if liq is not None else seen["sweep"]),
        check(f"Equal {word} swept: wick beyond, close back inside (last {p.sweep_within} bars)",
              seen["sweep"] is not None, seen["sweep"]),
        check(f"Close beyond the swing {'high' if d > 0 else 'low'} after the sweep (BOS)",
              seen["bos"] is not None, seen["bos"]),
        check(f"Displacement: body >= {p.disp_atr:g} ATR with an FVG >= {p.fvg_atr:g} ATR",
              seen["disp"] is not None),
    ]
    ob = None
    if best is not None:
        s, ea, _eb, q, brk, disp = best
        for j in range(disp - 1, s - 1, -1):
            if d * (c[j] - o[j]) < 0:  # the last opposite candle before the move
                ob = j
                break
    checks.append(check("Order block: the last opposite candle before the move", ob is not None))
    if best is None or ob is None:
        checks.append(_htf(x, i, d))
        return checks, None
    s, ea, _eb, q, brk, disp = best
    level = _sweep_at(x, s, d)[2]  # type: ignore[index]
    top, bottom = max(o[ob], c[ob]), min(o[ob], c[ob])
    entry = top if d > 0 else bottom
    ext = min(lo[s:i + 1]) if d > 0 else max(h[s:i + 1])
    sl = ext - d * p.sl_atr * (x.atr14[s] or x.atr14[i] or 0.0)
    back = range(max(0, ea - p.tp_back), s)
    tp = max(h[j] for j in back) if d > 0 else min(lo[j] for j in back)
    risk = d * (entry - sl)
    rr = d * (tp - entry) / risk if risk > 0 else 0.0
    checks += [
        _htf(x, i, d),
        check(f"R:R >= {p.smc_min_rr:g} (target: the {'high' if d > 0 else 'low'} before the "
              f"equal {word})", rr >= p.smc_min_rr, round(rr, 2), "x"),
        _stop_range(x, entry, sl),
    ]
    lvl = h[q] if d > 0 else lo[q]
    fvg = sorted((h[disp - 1], lo[disp + 1]) if d > 0 else (lo[disp - 1], h[disp + 1]))

    def build() -> Setup:
        def cancel(j: int) -> bool:
            return d * (x.c[j] - sl) < 0

        return Setup("smc", _side(d), i, "limit", entry, sl, tp, p.smc_valid, cancel=cancel,
                     marks={"eq": [x.t[ea], x.t[s], level], "sweep": [x.t[s], ext],
                            "bos": [x.t[q], x.t[brk], lvl],
                            "fvg": [x.t[disp - 1], x.t[disp + 1], *fvg],
                            "ob": [x.t[ob], top, bottom]})

    return _finish(x, "smc", i, d, checks, 5, build)


def avwap(x: Ctx, i: int, d: int) -> tuple[list[Check], Setup | None]:
    p = x.p
    a = x.anchor[d][i]
    A = x.atr14[i]
    word = "low" if d > 0 else "high"
    if a is None or A is None or i - a < max(p.anchor_age, 5):
        return [check(f"Anchor: a swing {word} after a >= {p.anchor_move_atr:g} ATR move, "
                      f"{p.anchor_age}+ bars old, not exceeded since", False)], None
    v = x.vwap(a, i)
    v5 = x.vwap(a, i - 5)
    o, h, lo, c = x.o[i], x.h[i], x.l[i], x.c[i]
    strong = any(d * (x.c[j] - x.vwap(a, j)) > (x.atr14[j] or 0)
                 for j in range(max(a, i - p.strength_bars), i))
    if d > 0:
        touch = lo <= v + 0.5 * A and c > o and c > v
        entry, sl = h, min(lo, v) - 0.5 * A
    else:
        touch = h >= v - 0.5 * A and c < o and c < v
        entry, sl = lo, max(h, v) + 0.5 * A
    tp = entry + d * p.vwap_rr * abs(entry - sl)
    checks = [
        check(f"Anchor: swing {word} {i - a} bars ago", True, x.t[a], "time"),
        check(f"VWAP {'rising' if d > 0 else 'falling'} (5 bars) and close "
              f"{'above' if d > 0 else 'below'} it", d * (v - v5) > 0 and d * (c - v) > 0, v),
        check(f"A close > 1 ATR {'above' if d > 0 else 'below'} the VWAP in the last "
              f"{p.strength_bars} bars", strong),
        check(f"Signal candle: {'low' if d > 0 else 'high'} within 0.5 ATR of the VWAP, "
              f"{'bullish' if d > 0 else 'bearish'} close {'above' if d > 0 else 'below'} it",
              touch, v),
        _htf(x, i, d),
        _stop_range(x, entry, sl),
    ]

    def build() -> Setup:
        def cancel(j: int) -> bool:
            return d * (x.c[j] - x.vwap(a, j)) < 0 or d * (x.c[j] - (lo if d > 0 else h)) < 0

        return Setup("avwap", _side(d), i, "stop", entry, sl, tp, p.stop_valid, cancel=cancel,
                     marks={"anchor": [x.t[a], x.l[a] if d > 0 else x.h[a]], "vwap": v})

    return _finish(x, "avwap", i, d, checks, 4, build)


@dataclass(frozen=True)
class Strategy:
    id: str
    label: str
    detect: Callable[[Ctx, int, int], tuple[list[Check], Setup | None]]
    exit_rule: Callable[[Ctx, int, int], bool] | None = None
    target: str = ""
    sides: tuple[int, ...] = (1, -1)  # 1 long, -1 short


STRATEGIES: dict[str, Strategy] = {
    "donchian": Strategy("donchian", "Previous-high breakout, 26-bar exit (long only)", donchian,
                         donchian_exit, "a close below the 26-bar low (or the 2N stop)", (1,)),
    "ema": Strategy("ema", "EMA 50 pullback (4h EMA 200 + ADX)", ema_pullback, None, "3R"),
    "smc": Strategy("smc", "Liquidity sweep + BOS + FVG / OB", smc, None,
                    "the high / low before the equal lows / highs (>= 2R)"),
    "avwap": Strategy("avwap", "Anchored VWAP pullback", avwap, None, "2R"),
}


# ---- simulation -------------------------------------------------------------------------------
def _fill(s: Setup, x: Ctx, j: int, price: float, st: Strategy) -> None:
    s.status, s.fill_i, s.fill = "open", j, price
    if s.strategy == "donchian":  # the stop follows the actual entry: entry - 2N
        s.sl = price - s.d * x.p.n_mult * s.n
    d = s.d
    if (d > 0 and x.l[j] <= s.sl) or (d < 0 and x.h[j] >= s.sl):
        # the stop on the fill bar (a target there is not counted)
        _exit(s, j, s.sl if d * (price - s.sl) > 0 else price, "sl")
    elif st.exit_rule is not None and st.exit_rule(x, j, d):
        s.exit_next = True


def _exit(s: Setup, j: int, price: float, why: str) -> None:
    s.status, s.exit_i, s.exit_price, s.end_i = why, j, price, j


def _end(s: Setup, j: int, why: str) -> None:
    s.status, s.end_i = why, j


def step(s: Setup, x: Ctx, j: int, st: Strategy) -> None:
    """Bar j for a pending or open setup."""
    d, o, h, lo = s.d, x.o[j], x.h[j], x.l[j]
    if s.status == "pending":
        if s.kind == "market":
            _fill(s, x, j, o, st)
        elif s.kind == "stop":
            if (d > 0 and h >= s.entry) or (d < 0 and lo <= s.entry):
                _fill(s, x, j, max(o, s.entry) if d > 0 else min(o, s.entry), st)
            elif s.cancel is not None and s.cancel(j):
                _end(s, j, "cancelled")
            elif j - s.signal_i >= s.valid:
                _end(s, j, "expired")
        else:  # limit
            if d * (o - s.sl) <= 0:
                _end(s, j, "cancelled")  # opened beyond the stop: the idea is void
            elif (d > 0 and lo <= s.entry) or (d < 0 and h >= s.entry):
                _fill(s, x, j, min(o, s.entry) if d > 0 else max(o, s.entry), st)
            elif s.tp is not None and ((d > 0 and h >= s.tp) or (d < 0 and lo <= s.tp)):
                _end(s, j, "missed")  # the target came first: no trade, no loss
            elif s.cancel is not None and s.cancel(j):
                _end(s, j, "cancelled")
            elif j - s.signal_i >= s.valid:
                _end(s, j, "expired")
        return
    if s.status != "open":
        return
    if s.exit_next:
        _exit(s, j, o, "exit")
        return
    if d * (o - s.sl) <= 0:
        _exit(s, j, o, "sl")  # gapped through the stop
    elif (d > 0 and lo <= s.sl) or (d < 0 and h >= s.sl):
        _exit(s, j, s.sl, "sl")
    elif s.tp is not None and d * (o - s.tp) >= 0:
        _exit(s, j, o, "tp")
    elif s.tp is not None and ((d > 0 and h >= s.tp) or (d < 0 and lo <= s.tp)):
        _exit(s, j, s.tp, "tp")
    elif st.exit_rule is not None and st.exit_rule(x, j, d):
        s.exit_next = True


def _funding_times(t0: int, t1: int, every_h: int) -> int:
    if every_h <= 0 or t1 <= t0:
        return 0
    k = every_h * 3600
    return t1 // k - t0 // k


def outcome(s: Setup, x: Ctx, costs: Costs) -> dict[str, Any]:
    """R (to the stop at the fill) gross and after costs; an open trade at the last close."""
    if s.fill is None or s.fill_i is None:
        return {"r": None, "net_r": None, "cost_r": None}
    risk = abs(s.fill - s.sl)
    if risk <= 0:
        return {"r": None, "net_r": None, "cost_r": None}
    last = x.c[-1]
    px = s.exit_price if s.exit_price is not None else last
    r = s.d * (px - s.fill) / risk
    fee_in = costs.maker if s.kind == "limit" else costs.taker + costs.slippage
    fee_out = (costs.maker if s.status == "tp" else costs.taker + costs.slippage)
    t1 = x.t[s.exit_i] if s.exit_i is not None else x.t[-1] + x.tf_s
    fund = costs.funding_rate * _funding_times(x.t[s.fill_i], t1, costs.funding_h) * s.d
    cost_r = (fee_in + fee_out + fund) / (risk / s.fill)
    return {"r": round(r, 3), "net_r": round(r - cost_r, 3), "cost_r": round(cost_r, 3)}


def run(x: Ctx, st: Strategy, costs: Costs, start: int = 0) -> list[Setup]:
    """Every setup of the strategy over bars [start, n): one at a time."""
    out: list[Setup] = []
    active: Setup | None = None
    last_rejected: dict[str, int] = {}
    for i in range(x.n):
        if active is not None:
            step(active, x, i, st)
            if active.status not in ("pending", "open"):
                out.append(active)
                active = None
        if active is not None or i < start:
            continue
        for d in st.sides:
            _, s = st.detect(x, i, d)
            if s is None:
                continue
            if s.status == "rejected":
                if last_rejected.get(s.side) != i - 1:  # once per run of the same pattern
                    s.end_i = i
                    out.append(s)
                last_rejected[s.side] = i
                continue
            active = s
            break
    if active is not None:
        out.append(active)
    return out


def stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    done = [r for r in rows if r["status"] in ("tp", "sl", "exit") and r["net_r"] is not None]
    rs = [r["net_r"] for r in done]
    wins = [v for v in rs if v > 0]
    losses = [v for v in rs if v <= 0]
    eq = peak = dd = 0.0
    for v in rs:
        eq += v
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    count = {k: sum(1 for r in rows if r["status"] == k)
             for k in ("expired", "cancelled", "missed", "rejected")}
    side = {s: [r["net_r"] for r in done if r["side"] == s] for s in ("LONG", "SHORT")}
    return {
        "trades": len(done),
        "wins": len(wins),
        "win_rate": round(len(wins) / len(done), 4) if done else None,
        "total_r": round(sum(rs), 2),
        "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
        "gross_avg_r": round(sum(r["r"] for r in done) / len(done), 3) if done else None,
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0
        else None,
        "max_dd_r": round(dd, 2),
        "best_r": round(max(rs), 2) if rs else None,
        "worst_r": round(min(rs), 2) if rs else None,
        "long": {"trades": len(side["LONG"]), "total_r": round(sum(side["LONG"]), 2)},
        "short": {"trades": len(side["SHORT"]), "total_r": round(sum(side["SHORT"]), 2)},
        **count,
    }
