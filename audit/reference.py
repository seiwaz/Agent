"""F1. Independent, slow re-implementation of the structure rules, written from the rule text
(docs/SMC_STRATEGY.md and the docstring of sp2l/smc/structure.py), not from the code: plain
loops, floats are NOT used (Decimal like the engine) so differences are rule differences.

Compared object by object with `sp2l.smc.structure.analyze` on the full history of BTC and XRP
(5m, 15m, 1h, 4h): swings, BOS / CHoCH, fair value gaps, order blocks (with the gap right
after them) and liquidity sweeps.

Usage: uv run python -m audit.reference -> docs/audit/reference.json
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from audit.common import MAIN, OUT, m1, params
from sp2l.core.types import Candle
from sp2l.smc.model import SmcParams
from sp2l.smc.structure import analyze
from sp2l.smc.timeframes import MINUTE, aggregate


def wilder_atr(bars: list[Candle], n: int) -> list[Decimal | None]:
    out: list[Decimal | None] = []
    trs: list[Decimal] = []
    for i, b in enumerate(bars):
        if i == 0:
            tr = b.high - b.low
        else:
            pc = bars[i - 1].close
            tr = max(b.high - b.low, abs(b.high - pc), abs(b.low - pc))
        trs.append(tr)
        if i + 1 < n:
            out.append(None)
        elif i + 1 == n:
            out.append(sum(trs, start=Decimal(0)) / n)
        else:
            prev = out[-1]
            assert prev is not None
            out.append((prev * (n - 1) + tr) / n)
    return out


def reference(bars: list[Candle], p: SmcParams, lookback: int) -> dict[str, Any]:
    n = p.swing_len
    atr = wilder_atr(bars, p.atr_len)
    swings: list[tuple[str, int, Decimal, int]] = []  # kind, pivot, price, known at
    events: list[tuple[str, str, Decimal, int, int]] = []  # kind, dir, level, level idx, bar
    fvgs: list[tuple[str, Decimal, Decimal, int]] = []  # dir, bottom, top, known at
    obs: list[tuple[str, int, Decimal, Decimal, int, tuple[Decimal, Decimal]]] = []
    sweeps: list[tuple[str, int, Decimal]] = []
    last_hi: tuple[int, Decimal] | None = None
    last_lo: tuple[int, Decimal] | None = None
    trend = 0
    pend: list[tuple[int, int, str, int]] = []  # (known at, candle, dir, event index)
    # resting liquidity: confirmed swings not yet traded through
    lows: list[tuple[int, Decimal]] = []
    highs: list[tuple[int, Decimal]] = []

    def gap_after(j: int, d: str) -> tuple[Decimal, Decimal] | None:
        if j + 2 >= len(bars):
            return None
        a, c = bars[j], bars[j + 2]
        if d == "LONG" and c.low > a.high:
            return (a.high, c.low)
        if d == "SHORT" and c.high < a.low:
            return (c.high, a.low)
        return None

    for i, b in enumerate(bars):
        # 1) sweeps of liquidity confirmed before this bar
        tol = p.eq_tol_atr * atr[i] if atr[i] is not None else Decimal(0)
        for d, pool in (("LONG", lows), ("SHORT", highs)):
            pool[:] = [x for x in pool if x[0] >= i - lookback]
            if d == "LONG":
                taken = [x for x in pool if b.low < x[1]]
            else:
                taken = [x for x in pool if b.high > x[1]]
            if not taken:
                continue
            for x in taken:
                pool.remove(x)
            deepest = min(x[1] for x in taken) if d == "LONG" else max(x[1] for x in taken)
            rest_equal = any(
                (x[1] >= deepest - tol) if d == "LONG" else (x[1] <= deepest + tol) for x in pool
            )
            closed_back = b.close > deepest if d == "LONG" else b.close < deepest
            if closed_back and not rest_equal:
                sweeps.append((d, i, deepest))
        # 2) a pivot n bars back is confirmed on this close
        pv = i - n
        if pv >= n:
            h = bars[pv].high
            if all(h > bars[pv - k].high for k in range(1, n + 1)) and all(
                h >= bars[pv + k].high for k in range(1, n + 1)
            ):
                swings.append(("HIGH", pv, h, i))
                last_hi = (pv, h)
                highs.append((pv, h))
            lo = bars[pv].low
            if all(lo < bars[pv - k].low for k in range(1, n + 1)) and all(
                lo <= bars[pv + k].low for k in range(1, n + 1)
            ):
                swings.append(("LOW", pv, lo, i))
                last_lo = (pv, lo)
                lows.append((pv, lo))
        # 3) fair value gap completed by this bar
        if i >= 2:
            a, c = bars[i - 2], bars[i]
            g = None
            if c.low > a.high:
                g = ("LONG", a.high, c.low)
            elif c.high < a.low:
                g = ("SHORT", c.high, a.low)
            if g is not None and (atr[i] is None or g[2] - g[1] >= p.fvg_min_atr * atr[i]):
                fvgs.append((g[0], g[1], g[2], i))
        # 4) order blocks waiting for the gap right after them
        for w in [w for w in pend if w[0] == i]:
            pend.remove(w)
            g2 = gap_after(w[1], w[2])
            if g2 is not None:
                obs.append((w[2], w[1], bars[w[1]].low, bars[w[1]].high, i, g2))
        # 5) a close beyond the last unbroken swing
        brk = None
        if last_hi is not None and b.close > last_hi[1]:
            brk = ("LONG", last_hi)
            last_hi = None
        elif last_lo is not None and b.close < last_lo[1]:
            brk = ("SHORT", last_lo)
            last_lo = None
        if brk is not None:
            d, (lidx, lvl) = brk
            want = 1 if d == "LONG" else -1
            kind = "CHOCH" if trend == -want else "BOS"
            trend = want
            events.append((kind, d, lvl, lidx, i))
            # the order block: last opposite-colour candle after the broken swing
            cands = [
                k
                for k in range(max(lidx + 1, i - p.ob_lookback), i)
                if (bars[k].close < bars[k].open if d == "LONG" else bars[k].close > bars[k].open)
            ]
            if cands:
                j = cands[-1]
                big = atr[i] is None or bars[j].high - bars[j].low >= p.ob_min_atr * atr[i]
                if big:
                    if j + 2 > i:
                        pend.append((j + 2, j, d, len(events) - 1))
                    else:
                        g2 = gap_after(j, d)
                        if g2 is not None:
                            obs.append((d, j, bars[j].low, bars[j].high, i, g2))
    return {"swings": swings, "events": events, "fvgs": fvgs, "obs": obs, "sweeps": sweeps}


def engine(bars: list[Candle], tf: str, p: SmcParams) -> dict[str, Any]:
    a = analyze(bars, tf, p)
    return {
        "swings": [(s.kind, s.idx, s.price, s.confirmed_idx) for s in a.swings],
        "events": [(e.kind, e.direction.value, e.level, e.level_idx, e.break_idx) for e in a.events],
        "fvgs": [(z.direction.value, z.bottom, z.top, z.created_idx) for z in a.fvgs],
        "obs": [(z.direction.value, z.idx, z.bottom, z.top, z.created_idx, z.gap) for z in a.order_blocks],
        "sweeps": [(s.direction.value, s.idx, s.level) for s in a.sweeps],
    }


def main() -> None:
    out: dict[str, Any] = {}
    for sym in MAIN:
        bars1 = m1(sym)
        p = params(sym)
        up = bars1[-1].open_time + MINUTE
        for tf in ("5m", "15m", "1h", "4h"):
            bars = aggregate(bars1, tf, up, p.htf_grid)
            r, e = reference(bars, p, p.lookback(tf)), engine(bars, tf, p)
            row = {}
            for k in r:
                rs, es = set(r[k]), set(e[k])
                row[k] = {
                    "reference": len(rs),
                    "engine": len(es),
                    "only_reference": len(rs - es),
                    "only_engine": len(es - rs),
                    "examples": [str(x) for x in sorted(rs ^ es, key=str)[:4]],
                }
            out[f"{sym}:{tf}"] = row
            print(sym, tf, {k: (v["reference"], v["only_reference"], v["only_engine"]) for k, v in row.items()}, flush=True)
    (OUT / "reference.json").write_text(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
