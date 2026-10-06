"""B. Look-ahead and repainting.

B1 swing confirmation delay per timeframe (confirmed_idx - idx must equal swing_len).
B4 repainting replay: the structure analysis is re-run on every prefix bars[:k+1] (bar by bar)
   and its swings, breaks, sweeps, zones (boundaries, gap, known-at bar, tested / mitigated
   state as of k), trend and ATR are compared with the one full-history analysis.
B5 truncation: the whole backtest (context, setups, orders, trades) on M1 data cut at K random
   points must equal the full run for everything decided before the cut.
LW live-window dependence: the live engine analyses only the last `lookback_<tf>` bars of each
   timeframe at every step; the backtest analyses the full history once. For every setup the
   backtest evaluated, the same decision is recomputed from a live-style window context.

Usage: uv run python -m audit.lookahead [--quick] -> docs/audit/lookahead.json
"""

from __future__ import annotations

import json
import random
import sys
import time
from datetime import datetime
from statistics import mean
from typing import Any

from audit.common import CACHE, MAIN, NOFILTER, OUT, SMC22, costs, m1, params
from sp2l.core.types import Candle
from sp2l.smc.backtest import build_context, run
from sp2l.smc.context import needed_tfs
from sp2l.smc.model import Analysis, SmcParams
from sp2l.smc.strategy import (
    ARMED_BEFORE,
    evaluate_order,
    find_arming,
    order_for,
    zone_setups,
)
from sp2l.smc.structure import analyze
from sp2l.smc.timeframes import MINUTE, aggregate, bucket_start, length

VARIANTS = {"current": {}, "nofilter": NOFILTER, "smc22": SMC22}


# ---- B1 ---------------------------------------------------------------------------------
def swing_delay(a: Analysis, p: SmcParams) -> dict[str, Any]:
    d = [s.confirmed_idx - s.idx for s in a.swings]
    hrs = [
        (a.bars[s.confirmed_idx].open_time - a.bars[s.idx].open_time).total_seconds() / 3600
        for s in a.swings
    ]
    return {
        "swings": len(d),
        "bars_delay_all_equal_swing_len": all(x == p.swing_len for x in d),
        "min_bars": min(d),
        "max_bars": max(d),
        "mean_hours_pivot_close_to_known": round(mean(hrs), 3),
    }


# ---- B4 ---------------------------------------------------------------------------------
def _zone_key(z: Any) -> tuple[Any, ...]:
    return (z.id, z.kind, z.top, z.bottom, z.gap, z.gap_idx, z.created_idx, z.event_id)


def snapshot_diff(full: Analysis, snap: Analysis, k: int) -> Counter_t:
    out: dict[str, int] = {}

    def cmp(name: str, f: list[Any], s: list[Any]) -> None:
        fs, ss = set(f), set(s)
        out[f"{name}_early"] = out.get(f"{name}_early", 0) + len(fs - ss)  # in full, not live
        out[f"{name}_late"] = out.get(f"{name}_late", 0) + len(ss - fs)  # live, gone later

    cmp("swings", [x for x in full.swings if x.confirmed_idx <= k], snap.swings)
    cmp("events", [x for x in full.events if x.break_idx <= k], snap.events)
    cmp("sweeps", [x for x in full.sweeps if x.idx <= k], snap.sweeps)
    fz = [z for z in full.zones if z.created_idx <= k]
    cmp("zones", [_zone_key(z) for z in fz], [_zone_key(z) for z in snap.zones])
    sz = {z.id + z.kind: z for z in snap.zones}
    st = 0
    for z in fz:
        o = sz.get(z.id + z.kind)
        if o is None:
            continue
        tf_ = z.tested_idx if z.tested_idx is not None and z.tested_idx <= k else None
        mf = z.mitigated_idx if z.mitigated_idx is not None and z.mitigated_idx <= k else None
        if (tf_, mf) != (o.tested_idx, o.mitigated_idx):
            st += 1
    out["zone_state"] = st
    out["trend"] = sum(1 for i in range(k + 1) if full.trend[i] != snap.trend[i])
    out["atr"] = sum(1 for i in range(k + 1) if full.atr[i] != snap.atr[i])
    out["event_ob"] = sum(
        1
        for e, z in snap.event_ob.items()
        if e not in full.event_ob or _zone_key(full.event_ob[e]) != _zone_key(z)
    )
    return out


Counter_t = dict[str, int]


def replay(bars: list[Candle], tf: str, p: SmcParams, steps: int) -> dict[str, Any]:
    full = analyze(bars, tf, p)
    tot: dict[str, int] = {}
    first = max(p.atr_len + 2 * p.swing_len, len(bars) - steps)
    t0 = time.time()
    for k in range(first, len(bars)):
        d = snapshot_diff(full, analyze(bars[: k + 1], tf, p), k)
        for key, v in d.items():
            tot[key] = tot.get(key, 0) + v
    return {
        "bars": len(bars),
        "steps_replayed": len(bars) - first,
        "from": bars[first].open_time.isoformat(),
        "differences": tot,
        "seconds": round(time.time() - t0, 1),
    }


# ---- B5 ---------------------------------------------------------------------------------
def _setup_view(s: Any) -> tuple[Any, ...]:
    return (
        s.key, s.created_at, s.accepted, s.reasons, s.entry, s.sl,
        None if s.tp is None else (s.tp.price, s.tp.source, s.tp.level), s.bias, s.market,
    )


def _trade_view(s: Any, t: Any) -> tuple[Any, ...]:
    return (s.key, t.state.value, t.filled_at, t.closed_at, t.exit_price, t.result_r)


def truncation(m: list[Candle], p: SmcParams, cuts: int, seed: int) -> dict[str, Any]:
    c = costs()
    full = run(m, p, c)
    rng = random.Random(seed)
    lo = len(m) // 3  # enough warmup for the 4h bias
    points = sorted(rng.sample(range(lo, len(m)), cuts))
    res = {"cuts": len(points), "setups_compared": 0, "trades_compared": 0, "diffs": []}
    for cut in points:
        end = m[cut - 1].open_time + MINUTE
        part = run(m[:cut], p, c)
        a = [_setup_view(s) for s in part["setups"] if s.created_at < end - MINUTE]
        b = [_setup_view(s) for s in full["setups"] if s.created_at < end - MINUTE]
        # a trade is final before the cut if it closed before the cut
        ta = [_trade_view(s, t) for s, t in part["trades"] if t.closed_at and t.closed_at <= end]
        tb = [_trade_view(s, t) for s, t in full["trades"] if t.closed_at and t.closed_at <= end]
        res["setups_compared"] += len(b)
        res["trades_compared"] += len(tb)
        if a != b or ta != tb:
            res["diffs"].append(
                {
                    "cut": end.isoformat(),
                    "setups_only_truncated": [str(x) for x in set(a) - set(b)][:3],
                    "setups_only_full": [str(x) for x in set(b) - set(a)][:3],
                    "trades_only_truncated": [str(x) for x in set(ta) - set(tb)][:3],
                    "trades_only_full": [str(x) for x in set(tb) - set(ta)][:3],
                }
            )
    return res


# ---- live window -------------------------------------------------------------------------
class Windows:
    """Live-style context at any time: the last lookback_<tf> CLOSED bars of each timeframe
    (as `ContextBuilder` / `load_bars` serve them), analysed from scratch."""

    def __init__(self, m: list[Candle], p: SmcParams) -> None:
        self.p = p
        up = m[-1].open_time + MINUTE
        self.m = m
        self.tfs = needed_tfs(p)
        self.agg = {tf: aggregate(m, tf, up, p.htf_grid) for tf in self.tfs if tf != "1m"}

    def ctx(self, upto: datetime) -> dict[str, Analysis]:
        out = {}
        for tf in self.tfs:
            n = self.p.lookback(tf)
            if tf == "1m":
                hi = _bisect(self.m, upto)
                bars = self.m[max(0, hi - n) : hi]
            else:
                end = bucket_start(upto, tf, self.p.htf_grid)  # forming bucket: excluded
                start = end - length(tf) * n
                xs = self.agg[tf]
                bars = xs[_bisect(xs, start) : _bisect(xs, end)]
            out[tf] = analyze(bars, tf, self.p)
        return out


def _bisect(xs: list[Candle], t: datetime) -> int:
    lo, hi = 0, len(xs)
    while lo < hi:
        mid = (lo + hi) // 2
        if xs[mid].open_time < t:
            lo = mid + 1
        else:
            hi = mid
    return lo


def live_window(m: list[Candle], p: SmcParams) -> dict[str, Any]:
    """Every backtest decision recomputed from the live engine's window context at the minute
    the live runner would make it (limit: after the arming minute closed / at the
    confirmation; reject: when decided)."""
    c = costs()
    res = run(m, p, c)
    w = Windows(m, p)
    rows = []
    for s in res["setups"]:
        limit = not s.market and s.reasons not in (("NO_CONFIRM",), ("ZONE_INVALID",))
        upto = s.created_at + MINUTE if limit else s.created_at  # runner: `known`
        ctx = w.ctx(upto)
        za = ctx[p.zone_tf]
        live = next((z for z in zone_setups(za, p) if z.key == s.key), None)
        if live is None:
            rows.append((s, None, "SETUP_NOT_IN_LIVE_WINDOW"))
            continue
        armed = find_arming(live, za, len(za.bars) - 1, ctx["1m"].bars)
        if armed is None or armed == ARMED_BEFORE:
            rows.append((s, None, f"ARMING_{'BEFORE' if armed == ARMED_BEFORE else 'NONE'}"))
            continue
        order = order_for(live, ctx, p, armed, ctx["1m"].bars)
        if order is None:
            rows.append((s, None, "NO_ORDER_YET"))
            continue
        ls = evaluate_order(live, ctx, p, c, order)
        rows.append((s, ls, None))
    same = [r for r in rows if r[1] is not None and _setup_view(r[0]) == _setup_view(r[1])]
    diff = [r for r in rows if r not in same]
    acc_b = sum(1 for r in rows if r[0].accepted)
    acc_l = sum(1 for r in rows if r[1] is not None and r[1].accepted)
    flips = [
        {
            "key": r[0].key,
            "at": r[0].created_at.isoformat(),
            "backtest": [r[0].accepted, list(r[0].reasons), str(r[0].entry), str(r[0].sl)],
            "live": r[2] if r[1] is None else [r[1].accepted, list(r[1].reasons), str(r[1].entry), str(r[1].sl)],
        }
        for r in diff
    ]
    # bias trend: full history vs a 180-bar window, at every 4h close
    a_full = build_context(m, p)[p.bias_tf]
    n = p.lookback(p.bias_tf)
    bars = a_full.bars
    mism = tot = 0
    for k in range(n, len(bars)):
        win = analyze(bars[k - n + 1 : k + 1], p.bias_tf, p)
        tot += 1
        mism += win.trend[-1] != a_full.trend[k]
    return {
        "setups": len(rows),
        "identical": len(same),
        "different": len(diff),
        "accepted_backtest": acc_b,
        "accepted_live_window": acc_l,
        "differences": flips[:40],
        "bias_trend_mismatch": {"bars": tot, "mismatch": mism, "rate": round(mism / tot, 4) if tot else None},
    }


def main() -> None:
    quick = "--quick" in sys.argv
    out: dict[str, Any] = {}
    for sym in MAIN:
        m = m1(sym)
        p = params(sym)
        ctx = build_context(m, p)
        r: dict[str, Any] = {"B1": {tf: swing_delay(a, p) for tf, a in ctx.items() if tf != "1m"}}
        print(sym, "B1", r["B1"], flush=True)
        steps = {"1d": 270, "4h": 1600, "1h": 3000, "15m": 3000, "5m": 2000}
        r["B4"] = {}
        for tf, a in ctx.items():
            if tf == "1m":
                continue
            # replay on the last 2 x steps bars (the analysis is a single causal pass, so a
            # prefix of a sub-series tests the same property as a prefix of the full series)
            sub = a.bars[-2 * steps[tf] :]
            f = CACHE / f"b4_{sym}_{tf}.json"
            if f.exists():
                r["B4"][tf] = json.loads(f.read_text())
            else:
                r["B4"][tf] = replay(sub, tf, p, 200 if quick else steps[tf])
                f.write_text(json.dumps(r["B4"][tf]))
            print(sym, "B4", tf, r["B4"][tf], flush=True)
        r["B5"] = {}
        for name, over in VARIANTS.items():
            r["B5"][name] = truncation(m, params(sym, **over), 3 if quick else 8, seed=MAIN.index(sym) + 1)
            print(sym, "B5", name, {k: v for k, v in r["B5"][name].items() if k != "diffs"}, len(r["B5"][name]["diffs"]), flush=True)
        r["LW"] = {}
        for name, over in VARIANTS.items():
            r["LW"][name] = live_window(m, params(sym, **over))
            print(sym, "LW", name, {k: v for k, v in r["LW"][name].items() if k != "differences"}, flush=True)
        out[sym] = r
        (OUT / "lookahead.json").write_text(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
