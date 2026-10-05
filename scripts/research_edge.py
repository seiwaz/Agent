# ruff: noqa: E501  (report tables are long by nature)
"""Edge research (read-only): does any part of the SMC model have an edge before costs, and how
much of the loss is costs? Writes docs/research/ (edge_report.md, events.csv, variants.csv).

    uv run python scripts/research_edge.py [--markets BTCUSDT,...] [--part events,variants,costs,holdout]

Discovery = the first 2/3 of each market's history; holdout = the last 1/3, used once at the
end for at most 3 candidates chosen on discovery data.
"""

from __future__ import annotations

import argparse
import csv
import json
import pickle
from collections import defaultdict
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import create_engine

from sp2l.config import RuntimeConfig
from sp2l.smc.model import Costs, SmcParams
from sp2l.smc.research.data import Market, load
from sp2l.smc.research.events import Event, Trend, bootstrap_diff, study
from sp2l.smc.research.trades import (
    Path as TPath,
)
from sp2l.smc.research.trades import (
    contexts,
    random_baseline,
    result,
    setups,
    stats,
    trades,
)
from sp2l.smc.structure import analyze
from sp2l.smc.timeframes import MINUTE, aggregate

OUT = Path("docs/research")
CACHE = OUT / "cache"
COVERAGE = {
    "BTCUSDT": 296,
    "XRPUSDT": 296,
    "ETHUSDT": 296,
    "SOLUSDT": 253,
    "DOGEUSDT": 253,
    "ADAUSDT": 253,
    "BNBUSDT": 202,
    "LTCUSDT": 197,
    "AVAXUSDT": 167,
}
KINDS = ("OB_last", "OB_extreme", "FVG", "OB_adjFVG", "OB_adjFVG_sweep", "candle_adjFVG")
TFS = ("5m", "15m", "1h", "4h")


def f2(x: Any, d: int = 2) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    return f"{x:+.{d}f}" if isinstance(x, float) else str(x)


# ------------------------------------------------------------------------------- part 1
def part_events(mkts: dict[str, Market], p: SmcParams, rep: list[str]) -> None:
    allev: list[Event] = []
    for sym, m in mkts.items():
        cache = CACHE / f"events_{sym}.pkl"
        if cache.exists():
            evs = pickle.loads(cache.read_bytes())
        else:
            end = m.bars[-1].open_time + MINUTE
            b4 = Trend(analyze(aggregate(m.bars, "4h", end), "4h", p))
            b1 = Trend(analyze(aggregate(m.bars, "1h", end), "1h", p))
            evs = []
            for tf in TFS:
                evs += study(m, tf, p, b4, b1, seed=hash((sym, tf)) % 10_000)
            cache.write_bytes(pickle.dumps(evs))
        print(sym, "events", len(evs), flush=True)
        allev += evs
    disc = [e for e in allev if e.split == "discovery"]
    with (OUT / "events.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "market",
                "tf",
                "kind",
                "direction",
                "touch",
                "split",
                "r",
                "hit1",
                "hit2",
                "hit3",
                "mfe",
                "mae",
                "hold_min",
                "bias4h",
                "bias1h",
                "discount",
                "c_hit1",
                "c_hit2",
                "c_hit3",
                "second_hit1",
                "second_hit2",
            ]
        )
        for e in allev:
            c, s = e.control, e.second
            w.writerow(
                [
                    e.market,
                    e.tf,
                    e.kind,
                    e.direction,
                    e.touch,
                    e.split,
                    e.r,
                    e.hit[1],
                    e.hit[2],
                    e.hit[3],
                    round(e.mfe, 3),
                    round(e.mae, 3),
                    e.hold_min,
                    e.bias4h,
                    e.bias1h,
                    e.discount,
                    c and c["hit"][1],
                    c and c["hit"][2],
                    c and c["hit"][3],
                    s and s["hit"][1],
                    s and s["hit"][2],
                ]
            )

    def groups(ev: Event) -> list[str]:
        g = [ev.kind]
        g.append(f"{ev.kind} · discount" if ev.discount else f"{ev.kind} · premium side")
        g.append(
            f"{ev.kind} · 4h with"
            if ev.bias4h > 0
            else f"{ev.kind} · 4h against"
            if ev.bias4h < 0
            else f"{ev.kind} · 4h none"
        )
        g.append(
            f"{ev.kind} · 1h with"
            if ev.bias1h > 0
            else f"{ev.kind} · 1h against"
            if ev.bias1h < 0
            else f"{ev.kind} · 1h none"
        )
        return g

    rep.append("## 1. Zone edge vs random levels (event study, before costs, discovery data)\n")
    rep.append(
        "Each event is the first touch of a zone; entry at the near edge, stop one tick beyond the far edge "
        "(1R). Columns: events with a measured control; P(+kR before −1R) zone vs random level; the difference "
        "with a 95 % bootstrap interval (paired). **bold** = interval excludes zero. Break-even before costs: "
        "P(+1R) 50 %, P(+2R) 33 %, P(+3R) 25 %.\n"
    )
    summary_rows = []
    for tf in TFS:
        rep.append(f"### {tf} (all markets pooled)\n")
        rep.append(
            "| zone type | n | P(+1R) zone / random | diff +1R [95 % CI] | P(+2R) zone / random | diff +2R [95 % CI] | P(+3R) zone | MFE median | MAE median | hold median (min) |"
        )
        rep.append("|---|---|---|---|---|---|---|---|---|---|")
        by: dict[str, list[Event]] = defaultdict(list)
        for e in disc:
            if e.tf == tf:
                for g in groups(e):
                    by[g].append(e)
        for k in KINDS:
            for g in [
                k,
                f"{k} · discount",
                f"{k} · premium side",
                f"{k} · 4h with",
                f"{k} · 4h against",
                f"{k} · 1h with",
                f"{k} · 1h against",
            ]:
                evs = [e for e in by.get(g, []) if e.control is not None]
                if len(evs) < 30:
                    continue
                z1 = np.array([e.hit[1] for e in evs])
                c1 = np.array([e.control["hit"][1] for e in evs])  # type: ignore[index]
                z2 = np.array([e.hit[2] for e in evs])
                c2 = np.array([e.control["hit"][2] for e in evs])  # type: ignore[index]
                d1 = bootstrap_diff(z1, c1)
                d2 = bootstrap_diff(z2, c2)
                sig1 = d1[1] > 0 or d1[2] < 0
                sig2 = d2[1] > 0 or d2[2] < 0
                cell1 = f"{d1[0] * 100:+.1f} [{d1[1] * 100:+.1f}, {d1[2] * 100:+.1f}]"
                cell2 = f"{d2[0] * 100:+.1f} [{d2[1] * 100:+.1f}, {d2[2] * 100:+.1f}]"
                rep.append(
                    f"| {g} | {len(evs)} | {z1.mean() * 100:.1f} / {c1.mean() * 100:.1f} | {'**' + cell1 + '**' if sig1 else cell1} | "
                    f"{z2.mean() * 100:.1f} / {c2.mean() * 100:.1f} | {'**' + cell2 + '**' if sig2 else cell2} | "
                    f"{np.mean([e.hit[3] for e in evs]) * 100:.1f} | {np.median([e.mfe for e in evs]):.2f} | {np.median([e.mae for e in evs]):.2f} | {np.median([e.hold_min for e in evs]):.0f} |"
                )
                summary_rows.append(
                    {
                        "tf": tf,
                        "group": g,
                        "n": len(evs),
                        "z1": float(z1.mean()),
                        "c1": float(c1.mean()),
                        "d1": d1,
                        "z2": float(z2.mean()),
                        "c2": float(c2.mean()),
                        "d2": d2,
                    }
                )
        # fresh vs already touched
        rep.append("")
        rep.append(
            "Fresh (first touch) vs already touched (second touch of the same zone), P(+1R) / P(+2R):\n"
        )
        rep.append(
            "| zone type | n second touches | first touch +1R / +2R | second touch +1R / +2R |"
        )
        rep.append("|---|---|---|---|")
        for k in KINDS:
            evs = [e for e in disc if e.tf == tf and e.kind == k and e.second is not None]
            if len(evs) < 30:
                continue
            rep.append(
                f"| {k} | {len(evs)} | {np.mean([e.hit[1] for e in evs]) * 100:.1f} / {np.mean([e.hit[2] for e in evs]) * 100:.1f} | "
                f"{np.mean([e.second['hit'][1] for e in evs]) * 100:.1f} / {np.mean([e.second['hit'][2] for e in evs]) * 100:.1f} |"  # type: ignore[index]
            )
        rep.append("")
    # per market, the main zone types, +1R difference
    rep.append("### Per market: diff P(+1R) zone − random, percentage points [95 % CI]\n")
    mk = sorted({e.market for e in disc})
    rep.append("| tf · zone type | " + " | ".join(mk) + " |")
    rep.append("|---|" + "---|" * len(mk))
    for tf in TFS:
        for k in ("OB_last", "OB_adjFVG", "OB_adjFVG_sweep", "FVG", "candle_adjFVG"):
            cells = []
            for s in mk:
                evs = [
                    e
                    for e in disc
                    if e.tf == tf and e.kind == k and e.market == s and e.control is not None
                ]
                if len(evs) < 20:
                    cells.append("—")
                    continue
                d = bootstrap_diff(
                    np.array([e.hit[1] for e in evs]), np.array([e.control["hit"][1] for e in evs])
                )  # type: ignore[index]
                c = f"{d[0] * 100:+.1f} [{d[1] * 100:+.0f},{d[2] * 100:+.0f}] n{len(evs)}"
                cells.append(f"**{c}**" if (d[1] > 0 or d[2] < 0) else c)
            rep.append(f"| {tf} · {k} | " + " | ".join(cells) + " |")
    rep.append("")
    (OUT / "events_summary.json").write_text(json.dumps(summary_rows, indent=1))


# ------------------------------------------------------------------------------- part 2
@dataclass
class Variant:
    name: str
    over: dict[str, Any]
    ctx: str = "s5"  # structure context: s5 / s2 / s3 / utc
    bias_off: bool = False
    note: str = ""
    paths: dict[str, list[TPath]] = field(default_factory=dict)


def variants(p: SmcParams) -> list[Variant]:
    base = {"require_sweep": False, "require_discount": False, "min_net_rr": Decimal(0)}
    full: dict[str, Any] = {}  # SMC-2.1 as configured
    v = [
        Variant(
            "base: OB+adjFVG, limit at OB edge, SL wick, TP leg HH/LL (1h, no bias)",
            base,
            bias_off=True,
        ),
        Variant("base + sweep", {**base, "require_sweep": True}, bias_off=True),
        Variant("base + discount/premium", {**base, "require_discount": True}, bias_off=True),
        Variant("base + 4h bias", base),
        Variant("base + 1h bias", {**base, "bias_tf": "1h"}),
        Variant("base + min net R 2", {**base, "min_net_rr": Decimal(2)}, bias_off=True),
        Variant("SMC-2.1 full (config)", full),
        Variant("full − sweep", {**full, "require_sweep": False}),
        Variant("full − discount", {**full, "require_discount": False}),
        Variant("full − 4h bias", full, bias_off=True),
        Variant("full − min net R", {**full, "min_net_rr": Decimal(0)}),
        Variant("full, 1h bias instead of 4h", {**full, "bias_tf": "1h"}),
    ]
    for tf in ("5m", "15m", "4h"):
        bias = "4h" if tf != "4h" else "1d"
        v.append(Variant(f"base, zone {tf}", {**base, "zone_tf": tf}, bias_off=True))
        v.append(
            Variant(
                f"full, zone {tf}",
                {**full, "zone_tf": tf, **({"bias_tf": bias} if tf == "4h" else {})},
            )
        )
    for sl, key in ((2, "s2"), (3, "s3")):
        v.append(
            Variant(f"base, swing_len {sl}", {**base, "swing_len": sl}, ctx=key, bias_off=True)
        )
        v.append(Variant(f"full, swing_len {sl}", {**full, "swing_len": sl}, ctx=key))
    v.append(Variant("base, UTC grid", base, ctx="utc", bias_off=True))
    v.append(Variant("full, UTC grid", full, ctx="utc"))
    v.append(Variant("base, zone 15m, 1h bias", {**base, "zone_tf": "15m", "bias_tf": "1h"}))
    v.append(Variant("base, zone 5m, 15m bias", {**base, "zone_tf": "5m", "bias_tf": "15m"}))
    v.append(
        Variant(
            "base + sweep, zone 15m",
            {**base, "require_sweep": True, "zone_tf": "15m"},
            bias_off=True,
        )
    )
    v.append(
        Variant(
            "base + sweep, zone 5m", {**base, "require_sweep": True, "zone_tf": "5m"}, bias_off=True
        )
    )
    return v


def run_variants(mkts: dict[str, Market], cfg: RuntimeConfig, costs: Costs) -> list[Variant]:
    vs = variants(cfg.smc_params())
    for sym, m in mkts.items():
        p0 = replace(cfg.smc_params(), tick=m.tick)
        cache = CACHE / f"variants_{sym}.pkl"
        if cache.exists():
            done = pickle.loads(cache.read_bytes())
            for v in vs:
                v.paths[sym] = done[v.name]
            print(sym, "variants (cached)", flush=True)
            continue
        ctxs: dict[str, Any] = {}
        need = {v.ctx for v in vs}
        for key in need:
            sl = {"s2": 2, "s3": 3}.get(key, p0.swing_len)
            ctxs[key] = contexts(
                m, replace(p0, swing_len=sl), grid="utc" if key == "utc" else "tehran"
            )
        done = {}
        for v in vs:
            p = replace(p0, **v.over)
            ss = setups(ctxs[v.ctx], p, costs, bias_off=v.bias_off)
            v.paths[sym] = trades(ss, m, p)
            done[v.name] = v.paths[sym]
            print(sym, v.name, len(v.paths[sym]), flush=True)
        cache.write_bytes(pickle.dumps(done))
    return vs


def split_rows(
    pt: list[TPath], m: Market, costs: Costs, holdout: bool, **kw: Any
) -> list[tuple[float, float]]:
    out = []
    for x in pt:
        if (x.setup.created_at >= m.split) != holdout:
            continue
        r = result(x, costs, **kw)
        if r is not None:
            out.append(r)
    return out


def part_variants(
    mkts: dict[str, Market], vs: list[Variant], costs: Costs, p: SmcParams, rep: list[str]
) -> list[dict[str, Any]]:
    rep.append("## 2. Component ablation (trade level, discovery data)\n")
    rep.append(
        f"{len(vs)} variants were run on {len(mkts)} markets. The engine's own setup finder and order "
        "evaluation; trades walked on M1 with the engine's rules (maker limit entry, market take-profit and "
        "stop with taker fee and slippage). Gross = result in stop distances before any cost; net = engine R. "
        "Random = the same number of market entries at random minutes, same side mix, same stop and target "
        "distances. *Not expressible in the engine without a code change:* 'fresh only' (the engine trades the "
        "first touch only) and 'no displacement / BOS' — see the event study (second touch; candle_adjFVG).\n"
    )
    rep.append(
        "| variant | trades | win | gross avg [95 % CI] | gross total | net total | PF | net avg [95 % CI] | random gross avg | random net avg | markets with gross > 0 |"
    )
    rep.append("|---|---|---|---|---|---|---|---|---|---|---|")
    table = []
    for v in vs:
        rows: list[tuple[float, float]] = []
        rnd: list[tuple[float, float]] = []
        pos = 0
        n_mk = 0
        for sym, m in mkts.items():
            r = split_rows(v.paths[sym], m, costs, holdout=False)
            rows += r
            if r:
                n_mk += 1
                pos += sum(x[0] for x in r) > 0
            disc = [x for x in v.paths[sym] if x.setup.created_at < m.split]
            base_rnd = random_baseline(disc, m, p, m.bars[0].open_time, m.split, seed=len(v.name))
            rnd += [y for y in (result(x, costs) for x in base_rnd) if y is not None]
        st = stats(rows)
        sr = stats(rnd)
        table.append({"name": v.name, **st, "rand": sr, "pos": pos, "n_mk": n_mk})
        if st["n"] == 0:
            rep.append(f"| {v.name} | 0 | | | | | | | | | |")
            continue
        rep.append(
            f"| {v.name} | {st['n']} | {st['win'] * 100:.0f}% | {st['gross_avg']:+.3f} [{st['gross_ci'][0]:+.3f}, {st['gross_ci'][1]:+.3f}] | "
            f"{st['gross']:+.1f} | {st['net']:+.1f} | {st['pf']:.2f} | {st['avg']:+.3f} [{st['ci'][0]:+.3f}, {st['ci'][1]:+.3f}] | "
            f"{sr.get('gross_avg', float('nan')):+.3f} | {sr.get('avg', float('nan')):+.3f} | {pos}/{n_mk} |"
        )
    rep.append("")
    with (OUT / "variants.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "variant",
                "trades",
                "win",
                "gross_avg",
                "gross_lo",
                "gross_hi",
                "gross_total",
                "net_total",
                "pf",
                "net_avg",
                "net_lo",
                "net_hi",
                "random_gross_avg",
                "random_net_avg",
                "markets_gross_pos",
                "markets",
            ]
        )
        for t in table:
            if t["n"]:
                w.writerow(
                    [
                        t["name"],
                        t["n"],
                        t["win"],
                        t["gross_avg"],
                        *t["gross_ci"],
                        t["gross"],
                        t["net"],
                        t["pf"],
                        t["avg"],
                        *t["ci"],
                        t["rand"].get("gross_avg"),
                        t["rand"].get("avg"),
                        t["pos"],
                        t["n_mk"],
                    ]
                )
    return table


# ------------------------------------------------------------------------------- part 3
def scaled(c: Costs, f: float) -> Costs:
    k = Decimal(str(f))
    return Costs(c.maker_fee * k, c.taker_fee * k, c.slippage * k)


def part_costs(
    mkts: dict[str, Market],
    vs: list[Variant],
    table: list[dict[str, Any]],
    costs: Costs,
    p: SmcParams,
    rep: list[str],
) -> None:
    rep.append("## 3. Cost levers (discovery data)\n")
    levels = json.loads(Path("docs/research/commission_levels.json").read_text())[
        "special_margin_commission_levels"
    ]
    ranked = sorted((t for t in table if t["n"] >= 30), key=lambda t: t["gross_avg"], reverse=True)
    pick = [t["name"] for t in ranked[:3]]
    if "SMC-2.1 full (config)" not in pick:
        pick.append("SMC-2.1 full (config)")
    byname = {v.name: v for v in vs}
    rt0 = float(costs.maker_fee + costs.taker_fee + costs.slippage)
    rep.append(
        f"Variants: the 3 with the best gross average (≥ 30 trades) and SMC-2.1. Round trip now = maker {costs.maker_fee} + "
        f"taker {costs.taker_fee} + slippage {costs.slippage} = {rt0 * 100:.4f} % of price.\n"
    )
    # stop size by zone timeframe
    rep.append(
        "Stop distance in % of price and costs as a share of 1R (median), by zone timeframe (base variants):\n"
    )
    rep.append("| zone tf | trades | stop % median | p10 | p90 | costs / 1R median |")
    rep.append("|---|---|---|---|---|---|")
    for name, tf in (
        ("base, zone 5m", "5m"),
        ("base, zone 15m", "15m"),
        ("base: OB+adjFVG, limit at OB edge, SL wick, TP leg HH/LL (1h, no bias)", "1h"),
        ("base, zone 4h", "4h"),
    ):
        v = byname[name]
        pct = []
        for sym, m in mkts.items():
            for x in v.paths[sym]:
                if x.setup.created_at < m.split and x.filled >= 0:
                    e, sl = float(x.setup.entry), float(x.setup.sl)  # type: ignore[arg-type]
                    pct.append(abs(e - sl) / e)
        if pct:
            a = np.array(pct)
            share = rt0 / (a + rt0)
            rep.append(
                f"| {tf} | {a.size} | {np.median(a) * 100:.2f} | {np.percentile(a, 10) * 100:.2f} | {np.percentile(a, 90) * 100:.2f} | {np.median(share) * 100:.0f} % |"
            )
    rep.append("")
    rep.append("| variant | model | trades | net total | net avg [95 % CI] |")
    rep.append("|---|---|---|---|---|")
    for name in pick:
        v = byname[name]

        def line(
            label: str,
            c: Costs,
            tp_through: bool = False,
            k_min: float = 0.0,
            v: Variant = v,
            name: str = name,
            **kw: Any,
        ) -> float:
            rows = []
            for sym, m in mkts.items():
                pts = v.paths[sym]
                if tp_through:
                    pts = trades(
                        [x.setup for x in pts], m, replace(p, tick=m.tick), tp_through=True
                    )
                for x in pts:
                    if x.setup.created_at >= m.split:
                        continue
                    if k_min > 0:
                        e, sl = float(x.setup.entry), float(x.setup.sl)  # type: ignore[arg-type]
                        if abs(e - sl) / e < k_min * rt0:
                            continue
                    r = result(x, c, **kw)
                    if r is not None:
                        rows.append(r)
            st = stats(rows)
            if st["n"]:
                rep.append(
                    f"| {name} | {label} | {st['n']} | {st['net']:+.1f} | {st['avg']:+.3f} [{st['ci'][0]:+.3f}, {st['ci'][1]:+.3f}] |"
                )
            return float(st.get("net", 0.0))

        line("engine (maker entry, market TP)", costs)
        line(
            "TP as resting maker limit, fills one tick through (hypothetical)",
            costs,
            tp_through=True,
            tp="maker_through",
        )
        line("market entry (taker + slippage)", costs, entry="market")
        for lv in levels[:9]:
            c = Costs(
                Decimal(lv["maker_commission_percent"]),
                Decimal(lv["taker_commission_percent"]),
                costs.slippage,
            )
            line(
                f"fees level {lv['level']} (maker {lv['maker_commission_percent']}, taker {lv['taker_commission_percent']})",
                c,
            )
        line("zero cost", Costs())
        for k in (3, 5, 8):
            line(f"skip stops < {k} × round trip", costs, k_min=float(k))
        # break-even round trip: scale every cost until the net total is 0
        lo, hi = 0.0, 1.0
        if _net(v, mkts, scaled(costs, 0.0)) <= 0:
            rep.append(f"| {name} | break-even round trip | — | loses before costs | |")
        else:
            while _net(v, mkts, scaled(costs, hi)) > 0 and hi < 64:
                hi *= 2
            for _ in range(30):
                mid = (lo + hi) / 2
                if _net(v, mkts, scaled(costs, mid)) > 0:
                    lo = mid
                else:
                    hi = mid
            rep.append(
                f"| {name} | break-even round trip | | {lo * rt0 * 100:.4f} % of price ({lo:.2f} × now) | |"
            )
    rep.append("")
    rep.append(
        "Tabdeal (docs/tabdeal_endpoint_map.md, official futures API docs): orders are LIMIT or MARKET only; "
        "`reduceOnly` is 'not supported for now'; take-profit / stop-loss are position-level (`positionSlTp`, one "
        "TP per position) and execute at market when triggered (owner's screenshot). A resting reduce-only maker "
        "TP is therefore not available; a plain opposite LIMIT order in one-way mode is undocumented (would need a "
        "probe, not done).\n"
    )


def _net(v: Variant, mkts: dict[str, Market], c: Costs) -> float:
    tot = 0.0
    for sym, m in mkts.items():
        for x in v.paths[sym]:
            if x.setup.created_at < m.split:
                r = result(x, c)
                if r is not None:
                    tot += r[1]
    return tot


# ------------------------------------------------------------------------------- part 4
def part_holdout(
    mkts: dict[str, Market],
    vs: list[Variant],
    table: list[dict[str, Any]],
    costs: Costs,
    rep: list[str],
) -> None:
    rep.append("## 4. Holdout\n")
    cands = [
        t
        for t in table
        if t["n"] >= 100 and t["gross_ci"][0] > 0 and t["pos"] >= max(1, round(t["n_mk"] * 2 / 3))
    ]
    cands = sorted(cands, key=lambda t: t["gross_avg"], reverse=True)[:3]
    if not cands:
        rep.append(
            "No variant met the rules on discovery data (≥ 100 trades, gross average with a 95 % interval "
            "above zero, gross > 0 on at least 2/3 of the markets). Nothing was run on the holdout.\n"
        )
        return
    byname = {v.name: v for v in vs}
    rep.append(
        "| candidate | trades | win | gross avg [95 % CI] | net total | net avg [95 % CI] | PF |"
    )
    rep.append("|---|---|---|---|---|---|---|")
    for t in cands:
        v = byname[t["name"]]
        rows = []
        for sym, m in mkts.items():
            rows += split_rows(v.paths[sym], m, costs, holdout=True)
        st = stats(rows)
        if st["n"]:
            rep.append(
                f"| {t['name']} | {st['n']} | {st['win'] * 100:.0f}% | {st['gross_avg']:+.3f} [{st['gross_ci'][0]:+.3f}, {st['gross_ci'][1]:+.3f}] | {st['net']:+.1f} | {st['avg']:+.3f} [{st['ci'][0]:+.3f}, {st['ci'][1]:+.3f}] | {st['pf']:.2f} |"
            )
        else:
            rep.append(f"| {t['name']} | 0 | | | | | |")
    rep.append("")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", default=",".join(COVERAGE))
    ap.add_argument("--part", default="events,variants,costs,holdout")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(exist_ok=True)
    cfg = RuntimeConfig.load(Path("config/runtime.yaml"))
    db = create_engine(cfg.database_url)
    costs = cfg.costs()
    p = cfg.smc_params()
    mkts: dict[str, Market] = {}
    for sym in args.markets.split(","):
        tick = cfg.instrument(sym)[0] if sym in cfg.symbols else None
        mkts[sym] = load(db, sym, COVERAGE[sym] + 5, tick)
        m = mkts[sym]
        print(
            sym,
            len(m.bars),
            m.bars[0].open_time,
            m.bars[-1].open_time,
            "split",
            m.split,
            "tick",
            m.tick,
            flush=True,
        )
    rep = ["# SMC edge research\n"]
    rep.append(
        "Markets, 1-minute coverage and the discovery / holdout split (first 2/3 / last 1/3 of each market's history):\n"
    )
    rep.append("| market | from | to | minutes | split at | tick |")
    rep.append("|---|---|---|---|---|---|")
    for sym, m in mkts.items():
        rep.append(
            f"| {sym} | {m.bars[0].open_time:%Y-%m-%d} | {m.bars[-1].open_time:%Y-%m-%d} | {len(m.bars)} | {m.split:%Y-%m-%d} | {m.tick} |"
        )
    rep.append(
        f"\nCosts (Tabdeal level 1): maker {costs.maker_fee}, taker {costs.taker_fee}, slippage allowance {costs.slippage} "
        "(stop and market take-profit). Parameters: config/runtime.yaml (SMC-2.1) unless a variant says otherwise.\n"
    )
    parts = args.part.split(",")
    if "events" in parts:
        part_events(mkts, p, rep)
    table: list[dict[str, Any]] = []
    vs: list[Variant] = []
    if {"variants", "costs", "holdout"} & set(parts):
        vs = run_variants(mkts, cfg, costs)
        table = part_variants(mkts, vs, costs, p, rep)
    if "costs" in parts:
        part_costs(mkts, vs, table, costs, p, rep)
    if "holdout" in parts:
        part_holdout(mkts, vs, table, costs, rep)
    findings = OUT / "findings.md"
    if findings.exists():
        rep[1:1] = [findings.read_text()]
    (OUT / "edge_report.md").write_text("\n".join(rep) + "\n")
    print("written", OUT / "edge_report.md")


if __name__ == "__main__":
    main()
