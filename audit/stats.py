"""G. Statistics and robustness of the engine's trades (population of audit.execution).

G1 sample size; G2 bootstrap 95 % CI of the expectancy (R per closed trade), net and gross;
G3 random-entry baseline: the same markets, sides, stop and target distances (in % of price),
   costs and holding limit, entered at random minutes (500 seeds), walked on M1 with the same
   stop-first rule; G4 breakdowns; G6 parameter neighbourhood (each value one full backtest).

Usage: uv run python -m audit.stats [--no-neighbourhood] -> docs/audit/stats.json
"""

from __future__ import annotations

import json
import random
import sys
from collections import defaultdict
from datetime import datetime
from statistics import mean
from typing import Any

import numpy as np

from audit.common import MAIN, NOFILTER, OUT, SMC22, costs, m1, params
from sp2l.smc.backtest import run

VARIANTS = {"current": {}, "nofilter": NOFILTER, "smc22": SMC22}


def boot(xs: list[float], n: int = 10000, seed: int = 1) -> dict[str, Any]:
    if not xs:
        return {"n": 0}
    rng = np.random.default_rng(seed)
    a = np.array(xs)
    means = a[rng.integers(0, len(a), (n, len(a)))].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return {
        "n": len(xs),
        "mean": round(float(a.mean()), 3),
        "ci95": [round(float(lo), 3), round(float(hi), 3)],
        "total": round(float(a.sum()), 2),
        "win_rate": round(float((a > 0).mean()), 3),
        "label": "inconclusive (n < 100)" if len(xs) < 100 else ("edge" if lo > 0 else "no proven edge"),
    }


class Arrays:
    def __init__(self, sym: str) -> None:
        bars = m1(sym)
        self.t = np.array([b.open_time.timestamp() for b in bars])
        self.o = np.array([float(b.open) for b in bars])
        self.h = np.array([float(b.high) for b in bars])
        self.lo = np.array([float(b.low) for b in bars])
        self.c = np.array([float(b.close) for b in bars])


def random_trade(a: Arrays, i: int, long: bool, stop_f: float, tp_f: float, hold: int, cst: dict[str, float]) -> float:
    """R of one random trade entered at the open of minute i (maker in, taker out, the same
    risk unit as the engine), stop-first in a shared minute, no TP in the entry minute."""
    e = a.o[i]
    sl = e * (1 - stop_f) if long else e * (1 + stop_f)
    tp = e * (1 + tp_f) if long else e * (1 - tp_f)
    j = min(len(a.o), i + hold + 1)
    h, lo = a.h[i:j], a.lo[i:j]
    stop = lo <= sl if long else h >= sl
    tgt = h >= tp if long else lo <= tp
    tgt[0] = False
    ks = np.argmax(stop) if stop.any() else None
    kt = np.argmax(tgt) if tgt.any() else None
    sg = 1 if long else -1
    slip, mk, tk = cst["slip"], cst["maker"], cst["taker"]
    sfill = sl * (1 - sg * slip)
    risk = abs(e - sl) + e * mk + sl * slip + sfill * tk
    if ks is not None and (kt is None or ks <= kt):
        px = sfill
    elif kt is not None:
        px = tp * (1 - sg * slip)
    else:
        px = a.c[j - 1]
    return ((px - e) * sg - e * mk - px * tk) / risk


def baseline(trades: list[dict[str, Any]], seeds: int, hold: int) -> dict[str, Any]:
    c = costs()
    cst = {"slip": float(c.slippage), "maker": float(c.maker_fee), "taker": float(c.taker_fee)}
    arrays = {s: Arrays(s) for s in {t["sym"] for t in trades}}
    means = []
    for seed in range(seeds):
        rng = random.Random(seed)
        rs = []
        for t in trades:
            a = arrays[t["sym"]]
            i = rng.randrange(len(a.o) // 3, len(a.o) - hold - 1)  # after the engine's warmup
            e, sl, tp = float(t["entry"]), float(t["sl"]), float(t["tp"])
            rs.append(random_trade(a, i, t["side"] == "LONG", abs(e - sl) / e, abs(tp - e) / e, hold, cst))
        means.append(mean(rs))
    m = np.array(means)
    return {
        "seeds": seeds,
        "trades_per_seed": len(trades),
        "mean_r_per_trade": round(float(m.mean()), 3),
        "p2.5_p97.5_of_seed_means": [round(float(x), 3) for x in np.percentile(m, [2.5, 97.5])],
    }


def breakdown(trades: list[dict[str, Any]], key: Any) -> dict[str, Any]:
    g: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        g[str(key(t))].append(t["r"])
    return {k: {"n": len(v), "total_r": round(sum(v), 2), "mean": round(mean(v), 3)} for k, v in sorted(g.items())}


NEIGHBOURS = {
    "swing_len": [3, 4, 6, 7],
    "sl_buffer_atr": ["0.1", "0.3"],
    "ob_min_atr": ["0.3", "0.7"],
    "fvg_min_atr": ["0.3", "0.7"],
    "confirm_window_min": [120, 480],
    "sweep_max_bars": [6, 24],
}


def neighbourhood() -> dict[str, Any]:
    out: dict[str, Any] = {}
    c = costs()
    for v in ("current", "nofilter"):
        for k, vals in NEIGHBOURS.items():
            for val in vals:
                row = {}
                for sym in MAIN:
                    st = run(m1(sym), params(sym, **{**VARIANTS[v], k: val}), c)["stats"]
                    row[sym] = {x: st[x] for x in ("accepted", "closed", "total_r", "win_rate")}
                out[f"{v}:{k}={val}"] = row
                print(v, k, val, row, flush=True)
    return out


def main() -> None:
    rows = json.loads((OUT / "trades.json").read_text())
    closed = [t for t in rows if t["r"] is not None]
    res: dict[str, Any] = {}
    for v in VARIANTS:
        xs = [t for t in closed if t["variant"] == v]
        main_ = [t for t in xs if t["sym"] in MAIN]
        res[v] = {
            "G1_G2_all_markets_net": boot([t["r"] for t in xs]),
            "G2_all_markets_gross": boot([t["gross_r"] for t in xs]),
            "G2_btc_xrp_net": boot([t["r"] for t in main_]),
            "G3_random_baseline": baseline(xs, 500, params("BTCUSDT", **VARIANTS[v]).max_hold_min) if xs else None,
            "G4_by_market": breakdown(xs, lambda t: t["sym"]),
            "G4_by_side": breakdown(xs, lambda t: t["side"]),
            "G4_by_quarter": breakdown(xs, lambda t: f"{t['created'][:4]}-Q{(int(t['created'][5:7]) - 1) // 3 + 1}"),
            "G4_by_hour_utc_block": breakdown(xs, lambda t: f"{int(t['created'][11:13]) // 6 * 6:02d}-{int(t['created'][11:13]) // 6 * 6 + 5:02d}h"),
            "G4_by_stop_size": breakdown(xs, lambda t: "stop<0.5%" if t["stop_pct"] < 0.5 else "stop 0.5-1%" if t["stop_pct"] < 1 else "stop>=1%"),
            "top_trade_share": None if not xs else round(max(t["r"] for t in xs) / sum(t["r"] for t in xs if t["r"] > 0), 3) if any(t["r"] > 0 for t in xs) else None,
        }
        print(v, json.dumps(res[v]["G1_G2_all_markets_net"]), json.dumps(res[v]["G3_random_baseline"]), flush=True)
    if "--no-neighbourhood" not in sys.argv:
        res["G6_neighbourhood"] = neighbourhood()
    res["generated"] = datetime.now().isoformat()
    (OUT / "stats.json").write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
