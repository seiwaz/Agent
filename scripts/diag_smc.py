# ruff: noqa: E501  (report tables and texts are long by nature)
"""SMC backtest diagnosis (read-only): integrity audit, result decomposition, owner-vs-bot
material. Writes docs/diagnostics/ (report.md, CSVs, loser charts). Changes nothing.

    uv run python scripts/diag_smc.py [--days 272] [--no-fetch]
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

from sp2l.config import RuntimeConfig
from sp2l.core.types import Side
from sp2l.smc.backtest import build_context
from sp2l.smc.diagnostics.core import Walked, bias_ignored, simulate, utc_grid, with_stop_buffer
from sp2l.smc.diagnostics.svg import render
from sp2l.smc.history import load_bars, series_end
from sp2l.smc.model import Costs, Setup, SmcParams
from sp2l.smc.strategy import evaluate, last_closed, zone_setups
from sp2l.smc.timeframes import length

OUT = Path("docs/diagnostics")
SYMS = ("BTCUSDT", "XRPUSDT")
D0 = Decimal(0)


def fnum(v: Any, d: int = 2) -> str:
    if v is None:
        return "—"
    return f"{float(v):.{d}f}"


def summary(ws: list[Walked]) -> dict[str, Any]:
    rs = [w.t.result_r for w in ws if w.t.result_r is not None]
    wins = [r for r in rs if r > 0]
    loss = -sum((r for r in rs if r <= 0), D0)
    return {
        "trades": len(ws),
        "closed": len(rs),
        "wins": len(wins),
        "win_rate": None if not rs else len(wins) / len(rs),
        "total": sum(rs, D0),
        "pf": None if not loss else sum(wins, D0) / loss,
    }


def row(label: str, by: dict[str, dict[str, Any]]) -> str:
    cells = []
    for sym in SYMS:
        v = by.get(sym)
        if v is None:
            cells.append("—")
            continue
        wr = "—" if v["win_rate"] is None else f"{v['win_rate'] * 100:.0f}%"
        pf = "—" if v["pf"] is None else f"{float(v['pf']):.2f}"
        cells.append(f"{v['closed']} / {wr} / {float(v['total']):+.2f}R / PF {pf}")
    tot = sum((by[s]["total"] for s in SYMS if s in by), D0)
    n = sum(by[s]["closed"] for s in SYMS if s in by)
    return f"| {label} | " + " | ".join(cells) + f" | {n} / {float(tot):+.2f}R |"


def table_head() -> str:
    return (
        "| variant | BTC closed / win / total / PF | XRP closed / win / total / PF | both |\n"
        "|---|---|---|---|"
    )


def in_stops(w: Walked, costs: Costs) -> tuple[Decimal, Decimal]:
    """(gross, net) result in stop distances: gross = the move from entry to the order's own
    exit level (stop or target price, or the market exit), no fee, no slippage; net = after
    entry fee, exit fee and the stop slippage. Same unit, so gross - net = costs."""
    t = w.t
    assert t.exit_price is not None
    d = abs(t.entry - (t.sl0 or t.sl))
    sgn = 1 if t.side is Side.LONG else -1
    level = (t.sl0 or t.sl) if t.state.value == "SL" else t.exit_price
    return (level - t.entry) * sgn / d, t.net_per_unit(t.exit_price, costs) / d


def stop_stats(ws: list[Walked], costs: Costs) -> dict[str, Any]:
    pct, mult = [], []
    for w in ws:
        t = w.t
        d = abs(t.entry - (t.sl0 or t.sl))
        cost = t.entry * t.fee_in(costs) + t.sl * costs.taker_fee + t.sl * costs.slippage
        pct.append(float(d / t.entry * 100))
        mult.append(float(d / cost))
    if not pct:
        return {}
    return {
        "stop_pct_median": statistics.median(pct),
        "stop_pct_min": min(pct),
        "stop_pct_max": max(pct),
        "fee_multiple_median": statistics.median(mult),
        "fee_share_of_1R_median": statistics.median(1 / (1 + m) for m in mult),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=272)
    ap.add_argument("--no-fetch", action="store_true")
    args = ap.parse_args()
    cfg = RuntimeConfig.load(Path("config/runtime.yaml"))
    db = create_engine(cfg.database_url)
    costs = cfg.costs()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "losers").mkdir(exist_ok=True)
    rep: list[str] = []
    add = rep.append

    m1: dict[str, Any] = {}
    params: dict[str, SmcParams] = {}
    ctx: dict[str, Any] = {}
    for sym in SYMS:
        p = cfg.symbol_params(sym)
        params[sym] = p
        upto = series_end(db, sym)
        assert upto is not None
        m1[sym] = load_bars(db, sym, "1m", args.days * 1440, upto)
        ctx[sym] = build_context(m1[sym], p)
    first = min(m1[s][0].open_time for s in SYMS)
    last = max(m1[s][-1].open_time for s in SYMS)
    add("# SMC backtest diagnosis\n")
    add(
        f"Data: local merged 1-minute series (Tabdeal chart history + collector), "
        f"{first:%Y-%m-%d} .. {last:%Y-%m-%d} UTC, BTC/USDT and XRP/USDT. Parameters: "
        f"config/runtime.yaml (hash BTC {params['BTCUSDT'].digest()}, XRP "
        f"{params['XRPUSDT'].digest()}). Costs: maker {costs.maker_fee}, taker {costs.taker_fee},"
        f" slippage allowance {costs.slippage}. Generated by scripts/diag_smc.py.\n"
    )
    add(
        "Two populations are reported: **current** = the parameters in force; **wide** = the "
        "same rules with `require_sweep: false` (more trades, so the per-trade statistics mean "
        "something; diagnostic only).\n"
    )

    pops: dict[str, dict[str, SmcParams]] = {
        "current": params,
        "wide": {s: replace(params[s], require_sweep=False) for s in SYMS},
    }
    base: dict[str, dict[str, tuple[list[Setup], list[Walked]]]] = {}
    for name, ps in pops.items():
        base[name] = {s: simulate(ctx[s], ps[s], costs) for s in SYMS}

    # ---------------------------------------------------------------------------- A1
    add("## A. Backtest integrity\n")
    add("### A1. Limit fill rule\n")
    add(
        "Code: `lifecycle.advance` (PENDING branch): a long limit fills when `bar.low <= entry`"
        " (touch), a short when `bar.high >= entry`; the fill minute's stop counts, its target"
        " does not. Test: `tests/smc/test_diagnostics.py::test_fill_on_touch_versus_through`.\n"
    )
    add(
        "| population | market | filled (touch) | filled only by a bare touch | their R | filled (through) | total R touch → through |"
    )
    add("|---|---|---|---|---|---|---|")
    for name, ps in pops.items():
        for sym in SYMS:
            found, touch = base[name][sym]
            _, through = simulate(ctx[sym], ps[sym], costs, setups=found, fill="through")
            ft = [w for w in touch if w.t.filled_at is not None]
            only = [w for w in ft if w.touch_only]
            fth = [w for w in through if w.t.filled_at is not None]
            add(
                f"| {name} | {sym} | {len(ft)} | {len(only)} | "
                f"{', '.join(fnum(w.t.result_r) for w in only) or '—'} | {len(fth)} | "
                f"{fnum(summary(touch)['total'])} → {fnum(summary(through)['total'])} |"
            )
    add(
        "\nA resting maker limit on Tabdeal fills only when a trade prints at the order's price "
        "and the queue in front of it is consumed; a bare touch (low == entry) does not "
        "guarantee a fill. Touch is the optimistic reading, through the conservative one.\n"
    )

    # ---------------------------------------------------------------------------- A2
    add("### A2. Intrabar rules\n")
    add(
        "Code: `lifecycle.advance` checks the stop before the target in every minute, and in"
        " the fill minute only the stop. Tests: `test_both_minute_and_target_in_the_fill_minute"
        "_readings`.\n"
    )
    add(
        "| population | market | minutes with stop and target → read as stop | their R if read as target | fill minutes that also reached the target | their R if credited |"
    )
    add("|---|---|---|---|---|---|")
    for name in pops:
        for sym in SYMS:
            _, ws = base[name][sym]
            both = [w for w in ws if w.both_minute]
            fill = [w for w in ws if w.tp_in_fill_minute]
            add(
                f"| {name} | {sym} | {len(both)} | {', '.join(fnum(w.alt_both_r) for w in both) or '—'}"
                f" | {len(fill)} | {', '.join(fnum(w.alt_fill_r) for w in fill) or '—'} |"
            )
    add("")

    # ---------------------------------------------------------------------------- A3
    add("### A3. Fees and R (worked examples)\n")
    add(
        "Code: `lifecycle.risk_unit` (R unit = stop distance + entry fee + slippage + taker fee"
        " on the slipped stop fill), `Tracked.net_per_unit` (move − entry fee − exit fee),"
        " `wallet.part_pnl` (fees = qty × price × rate, i.e. on notional, once each; maker on"
        " a limit entry, taker on every exit). Leverage only sets the margin, never the fees."
        " Test: `test_fees_are_charged_once_on_notional_maker_in_taker_out`.\n"
    )
    for sym in SYMS:
        ws = [w for w in base["current"][sym][1] + base["wide"][sym][1] if w.t.result_r is not None]
        if not ws:
            continue
        w = ws[0]
        t = w.t
        step = cfg.instrument(sym)[1]
        bal = params[sym].account_usdt
        qty = (bal * params[sym].risk_pct / t.risk / step).to_integral_value(
            rounding="ROUND_FLOOR"
        ) * step
        cap = (bal * params[sym].max_leverage / t.entry / step).to_integral_value(
            rounding="ROUND_FLOOR"
        ) * step
        qty = min(qty, cap)
        assert t.exit_price is not None
        fee_in = qty * t.entry * t.fee_in(costs)
        fee_out = qty * t.exit_price * costs.taker_fee
        sgn = 1 if t.side is Side.LONG else -1
        gross = qty * (t.exit_price - t.entry) * sgn
        slip = qty * t.sl * costs.slippage if t.state.value == "SL" else D0
        add(
            f"- **{sym}** ({t.side.value}, {t.state.value}, {t.created_at:%Y-%m-%d}): entry "
            f"{t.entry.normalize()}, stop {(t.sl0 or t.sl).normalize()}, target {t.tp.normalize()},"
            f" exit {t.exit_price.normalize()}; qty {qty.normalize()} (1 % of {bal} USDT over the"
            f" R unit {fnum(t.risk, 4)}, capped by 10× margin); notional {fnum(qty * t.entry)} USDT;"
            f" entry fee (maker) {fnum(fee_in, 4)}, exit fee (taker) {fnum(fee_out, 4)}, stop "
            f"slippage inside the exit price {fnum(slip, 4)}; gross {fnum(gross, 4)}, net "
            f"{fnum(gross - fee_in - fee_out, 4)} USDT; R = {fnum(t.result_r, 3)}"
            f" (= net / (qty × R unit) = {fnum((gross - fee_in - fee_out) / (qty * t.risk), 3)})."
        )
    add(
        "\nFindings: fees are charged once, on notional; maker in, taker out; the stop pays "
        "the slippage allowance, the target pays none. Owner evidence (screenshot, Tabdeal, "
        "2026-10-04 22:02 UTC): a take-profit is a trigger order executed at market — trigger "
        "86,132, average fill 86,111.9 (−20.1 USDT, −0.023 %) — so on Tabdeal a TP pays taker "
        "plus slippage; the backtest credits the exact TP price.\n"
    )

    # ---------------------------------------------------------------------------- A4
    add("### A4. Timeframe grid (Tehran hh:30 vs UTC hh:00 for 1h / 4h)\n")
    grid: dict[str, dict[str, Any]] = {}
    with utc_grid():
        for sym in SYMS:
            cu = build_context(m1[sym], params[sym])
            found, ws = simulate(cu, params[sym], costs)
            grid[sym] = {
                "setups": len(zone_setups(cu[params[sym].zone_tf], params[sym])),
                "armed": len(found),
                **summary(ws),
            }
    add("| grid | market | complete setups | armed | closed | total R |")
    add("|---|---|---|---|---|---|")
    for sym in SYMS:
        p = params[sym]
        add(
            f"| Tehran (engine) | {sym} | {len(zone_setups(ctx[sym][p.zone_tf], p))} | "
            f"{len(base['current'][sym][0])} | {summary(base['current'][sym][1])['closed']} | "
            f"{fnum(summary(base['current'][sym][1])['total'])} |"
        )
        g = grid[sym]
        add(
            f"| UTC (diagnostic) | {sym} | {g['setups']} | {g['armed']} | {g['closed']} | {fnum(g['total'])} |"
        )
    add("")

    # ---------------------------------------------------------------------------- A5
    add("### A5. Timing: look-ahead and late signals\n")
    add(
        "Look-ahead: `tests/smc/test_lookahead.py` (truncating the future changes no past zone,"
        " setup or order) and `tests/db/test_smc.py` (the minute-by-minute runner equals the"
        " backtest) pass. Below: how late a setup becomes tradeable.\n"
    )
    add(
        "| market | complete setups | median h sweep → known | median h OB candle → known | median h FVG done → known | OB edge revisited after the FVG closed, before the setup was known | median h known → armed |"
    )
    add("|---|---|---|---|---|---|---|")
    for sym in SYMS:
        p = params[sym]
        za = ctx[sym][p.zone_tf]
        ln = length(p.zone_tf)
        sw, obd, fvd, armd, before = [], [], [], [], 0
        found = {s.key: s for s in base["current"][sym][0]}
        zss = zone_setups(za, p)
        for zs in zss:
            if zs.sweep is not None:
                sw.append((zs.confirmed_at - zs.sweep.time).total_seconds() / 3600)
            obd.append((zs.confirmed_at - zs.ob.time).total_seconds() / 3600)
            fvd.append((zs.confirmed_at - (zs.ob.time + 3 * ln)).total_seconds() / 3600)
            long = zs.direction is Side.LONG
            seg = za.bars[(zs.ob.gap_idx or zs.ob.idx + 1) + 2 : zs.ob.created_idx + 1]
            if any((b.low <= zs.edge) if long else (b.high >= zs.edge) for b in seg):
                before += 1
            if zs.key in found:
                armd.append((found[zs.key].created_at - zs.confirmed_at).total_seconds() / 3600)
        med = lambda xs: fnum(statistics.median(xs), 1) if xs else "—"  # noqa: E731
        add(
            f"| {sym} | {len(zss)} | {med(sw)} | {med(obd)} | {med(fvd)} | "
            f"{before} / {len(zss)} | {med(armd)} |"
        )
    add("")

    # ---------------------------------------------------------------------------- A6
    add("### A6. Data\n")
    data_check: dict[str, Any] = {}
    with db.connect() as c:
        for sym in SYMS:
            times = [b.open_time for b in m1[sym]]
            gaps = [
                (a, b)
                for a, b in zip(times, times[1:], strict=False)
                if b - a > timedelta(minutes=1)
            ]
            missing = sum(int((b - a).total_seconds() // 60) - 1 for a, b in gaps)
            bad = sum(
                1
                for b in m1[sym]
                if not (b.low <= min(b.open, b.close) and b.high >= max(b.open, b.close))
            )
            dups = len(times) - len(set(times))
            top = sorted(gaps, key=lambda g: g[1] - g[0], reverse=True)[:3]
            data_check[sym] = {
                "minutes": len(times),
                "gaps": len(gaps),
                "missing_minutes": missing,
                "longest_gap_min": max(
                    (int((b - a).total_seconds() // 60) - 1 for a, b in gaps), default=0
                ),
                "duplicates": dups,
                "ohlc_inconsistent": bad,
                "largest_gaps": [
                    f"{a:%Y-%m-%d %H:%M} .. {b:%Y-%m-%d %H:%M} UTC ({int((b - a).total_seconds() // 60) - 1} min)"
                    for a, b in top
                ],
            }
            if not args.no_fetch:
                from sp2l.marketdata.tabdeal_public import chart_history
                from sp2l.marketdata.tabdeal_ws import ws_market

                rnd = random.Random(7)
                checked = mism = absent = 0
                examples = []
                for _ in range(8):
                    day = first + timedelta(
                        days=rnd.randrange(2, args.days - 3), hours=rnd.randrange(0, 21)
                    )
                    a = day.replace(minute=0, second=0, microsecond=0)
                    b = a + timedelta(hours=2)
                    # the API reports another open for the first bar of a request: start earlier
                    ref = chart_history(
                        ws_market(sym), "1", a - timedelta(minutes=5), b, timeout=30
                    )
                    rows = {
                        r[0]: r[1:]
                        for r in c.execute(
                            text(
                                "SELECT open_time, open, high, low, close FROM exchange_m1"
                                " WHERE symbol = :s AND open_time >= :a AND open_time < :b"
                            ),
                            {"s": sym, "a": a, "b": b},
                        )
                    }
                    for x in ref:
                        t = datetime.fromtimestamp(x["time"], UTC)
                        if t >= b or t < a:
                            continue
                        checked += 1
                        mine = rows.get(t)
                        if mine is None:
                            absent += 1
                            continue
                        theirs = tuple(Decimal(str(x[k])) for k in ("open", "high", "low", "close"))
                        if tuple(Decimal(v) for v in mine) != theirs:
                            mism += 1
                            if len(examples) < 3:
                                examples.append(
                                    {
                                        "t": t.isoformat(),
                                        "db": [str(v) for v in mine],
                                        "tabdeal": [str(v) for v in theirs],
                                    }
                                )
                data_check[sym]["refetch"] = {
                    "minutes_checked": checked,
                    "missing_in_db": absent,
                    "ohlc_differs": mism,
                    "examples": examples,
                }
    (OUT / "data_check.json").write_text(json.dumps(data_check, indent=1, default=str))
    add(
        "| market | minutes | gaps | missing minutes | longest gap (min) | duplicates | OHLC inconsistent | re-fetched minutes | missing in DB | OHLC differs |"
    )
    add("|---|---|---|---|---|---|---|---|---|---|")
    for sym, d in data_check.items():
        rf = d.get("refetch", {})
        add(
            f"| {sym} | {d['minutes']} | {d['gaps']} | {d['missing_minutes']} | {d['longest_gap_min']} |"
            f" {d['duplicates']} | {d['ohlc_inconsistent']} | {rf.get('minutes_checked', '—')} |"
            f" {rf.get('missing_in_db', '—')} | {rf.get('ohlc_differs', '—')} |"
        )
    for sym, d in data_check.items():
        add(f"- {sym} largest gaps: {'; '.join(d['largest_gaps'])}")
    add(
        "\nGaps are minutes for which Tabdeal's chart returns no bar. Re-fetch: 8 random 2-hour "
        "windows per market, fetched with a 5-minute margin (Tabdeal's chart API reports a "
        "different open / high for the first bar of a request: 2026-06-22 14:00 BTC open 65,530.2"
        " from a window starting 14:00 vs 65,525.0 from one starting 13:58; the stored value is"
        " the latter).\n"
    )

    # ---------------------------------------------------------------------------- B
    add("## B. Result decomposition\n")
    trade_rows: list[dict[str, Any]] = []
    for name in pops:
        add(f"### Population: {name}\n")
        add(
            "| market | closed | gross (stop distances, no costs) | net (stop distances) | costs (stop distances) | net R (engine R unit) | stop % of price (median, min–max) | stop / round-trip cost (median) | costs as share of 1R (median) |"
        )
        add("|---|---|---|---|---|---|---|---|---|")
        for sym in SYMS:
            ws = [w for w in base[name][sym][1] if w.t.result_r is not None]
            gn = [in_stops(w, costs) for w in ws]
            g = sum((x[0] for x in gn), D0)
            n = sum((x[1] for x in gn), D0)
            r = sum((w.t.result_r or D0 for w in ws), D0)
            st = stop_stats(ws, costs)
            add(
                f"| {sym} | {len(ws)} | {fnum(g)} | {fnum(n)} | {fnum(g - n)} | {fnum(r)} | "
                f"{fnum(st.get('stop_pct_median'))} ({fnum(st.get('stop_pct_min'))}–{fnum(st.get('stop_pct_max'))}) | "
                f"{fnum(st.get('fee_multiple_median'), 1)} | {fnum(st.get('fee_share_of_1R_median') and st['fee_share_of_1R_median'] * 100, 0)}% |"
            )
            for w in base[name][sym][1]:
                t = w.t
                trade_rows.append(
                    {
                        "population": name,
                        "market": sym,
                        "side": t.side.value,
                        "created_utc": t.created_at.isoformat(),
                        "filled_utc": t.filled_at.isoformat() if t.filled_at else "",
                        "state": t.state.value,
                        "entry": t.entry,
                        "sl": t.sl0 or t.sl,
                        "tp": t.tp,
                        "tp_level": w.setup.tp.level if w.setup.tp else "",
                        "net_r": t.result_r,
                        "gross_r": w.gross_r,
                        "mfe_r": w.mfe_r,
                        "mae_r": w.mae_r,
                        "touch_only": w.touch_only,
                        "both_minute": w.both_minute,
                        "tp_in_fill_minute": w.tp_in_fill_minute,
                        "stop_pct": abs(t.entry - (t.sl0 or t.sl)) / t.entry * 100,
                    }
                )
        add("")
        add("MFE / MAE (in stop distances, from the fill):\n")
        add(
            "| market | losers | losers that first reached +1R / +1.5R / +2R | winners | winners whose MAE ≥ 0.8R | median MFE losers | median MAE winners |"
        )
        add("|---|---|---|---|---|---|---|")
        for sym in SYMS:
            ws = [w for w in base[name][sym][1] if w.t.result_r is not None]
            losers = [w for w in ws if w.t.result_r <= 0]
            winners = [w for w in ws if w.t.result_r > 0]
            reach = {
                k: sum(1 for w in losers if w.first_reach and w.first_reach[k])
                for k in ("1R", "1.5R", "2R")
            }
            close = sum(1 for w in winners if w.mae_r is not None and w.mae_r >= Decimal("0.8"))
            mf = [float(w.mfe_r) for w in losers if w.mfe_r is not None]
            ma = [float(w.mae_r) for w in winners if w.mae_r is not None]
            add(
                f"| {sym} | {len(losers)} | {reach['1R']} / {reach['1.5R']} / {reach['2R']} | {len(winners)}"
                f" | {close} | {fnum(statistics.median(mf)) if mf else '—'} | {fnum(statistics.median(ma)) if ma else '—'} |"
            )
        add("")
        add("Exits and rejections:\n")
        for sym in SYMS:
            found, ws = base[name][sym]
            add(
                f"- {sym}: exits {dict(Counter(w.t.state.value for w in ws))}; reasons of all "
                f"armed setups {dict(Counter(r for s in found for r in s.reasons))}"
            )
        add("")
        add("Breakdown (closed trades, net R):\n")
        add("| group | closed | wins | total R |")
        add("|---|---|---|---|")
        groups: dict[str, list[Decimal]] = defaultdict(list)
        for sym in SYMS:
            for w in base[name][sym][1]:
                if w.t.result_r is None:
                    continue
                r = w.t.result_r
                groups[f"market {sym}"].append(r)
                groups[f"side {w.t.side.value}"].append(r)
                assert w.t.filled_at is not None
                h = w.t.filled_at.hour
                groups[f"fill hour UTC {h // 6 * 6:02d}–{h // 6 * 6 + 5:02d}"].append(r)
        for k in sorted(groups):
            v = groups[k]
            add(f"| {k} | {len(v)} | {sum(1 for r in v if r > 0)} | {fnum(sum(v, D0))} |")
        # with / against the 4h bias: the bias check switched off, the real bias recorded
        rec: dict[str, int] = {}
        with bias_ignored(rec):
            nb = {s: simulate(ctx[s], pops[name][s], costs) for s in SYMS}
        trend = {Side.LONG: 1, Side.SHORT: -1}
        for label, keep in (
            ("with the 4h bias", True),
            ("against the 4h bias (diagnostic)", False),
        ):
            v = [
                w.t.result_r
                for s in SYMS
                for w in nb[s][1]
                if w.t.result_r is not None
                and (rec.get(w.setup.key) == trend[w.setup.direction]) == keep
            ]
            add(f"| {label} | {len(v)} | {sum(1 for r in v if r > 0)} | {fnum(sum(v, D0))} |")
        add("")

    with (OUT / "trades.csv").open("w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(trade_rows[0]))
        wr.writeheader()
        wr.writerows(trade_rows)
    with (OUT / "setups.csv").open("w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(
            [
                "population",
                "market",
                "key",
                "direction",
                "order_utc",
                "accepted",
                "reasons",
                "entry",
                "sl",
                "tp",
                "tp_level",
                "net_r",
                "range_mid",
            ]
        )
        for name in pops:
            for sym in SYMS:
                for s in base[name][sym][0]:
                    wr.writerow(
                        [
                            name,
                            sym,
                            s.key,
                            s.direction.value,
                            s.created_at.isoformat(),
                            s.accepted,
                            " ".join(s.reasons),
                            s.entry,
                            s.sl,
                            s.tp and s.tp.price,
                            s.tp and s.tp.level,
                            s.tp and s.tp.net_r,
                            s.range_mid,
                        ]
                    )

    # ---------------------------------------------------------------------------- sensitivity
    add("### Sensitivity (diagnostic only, no parameter change)\n")
    for name in pops:
        add(f"Population **{name}**:\n")
        add(table_head())
        add(
            row(
                "engine readings (touch, stop first, Tehran grid, stop at the wick)",
                {s: summary(base[name][s][1]) for s in SYMS},
            )
        )
        for sl in (2, 3):
            by = {}
            for s in SYMS:
                q = replace(pops[name][s], swing_len=sl)
                by[s] = summary(simulate(build_context(m1[s], q), q, costs)[1])
            add(row(f"swing_len {sl}", by))
        add(
            row(
                "fill only through the price",
                {
                    s: summary(
                        simulate(
                            ctx[s], pops[name][s], costs, setups=base[name][s][0], fill="through"
                        )[1]
                    )
                    for s in SYMS
                },
            )
        )
        add(
            row(
                "target first when both in one minute",
                {
                    s: summary(
                        simulate(
                            ctx[s], pops[name][s], costs, setups=base[name][s][0], stop_first=False
                        )[1]
                    )
                    for s in SYMS
                },
            )
        )
        with utc_grid():
            add(
                row(
                    "UTC grid for 1h / 4h",
                    {
                        s: summary(
                            simulate(build_context(m1[s], pops[name][s]), pops[name][s], costs)[1]
                        )
                        for s in SYMS
                    },
                )
            )
        add(
            row(
                "stop 0.1 ATR(1h) beyond the wick",
                {
                    s: summary(
                        simulate(
                            ctx[s],
                            pops[name][s],
                            costs,
                            setups=[
                                with_stop_buffer(x, ctx[s], pops[name][s], costs, Decimal("0.1"))
                                for x in base[name][s][0]
                            ],
                        )[1]
                    )
                    for s in SYMS
                },
            )
        )
        add(
            row(
                "no costs at all",
                {
                    s: summary(
                        simulate(
                            ctx[s],
                            pops[name][s],
                            Costs(),
                            setups=[
                                evaluate(
                                    x.zone,
                                    ctx[s],
                                    pops[name][s],
                                    Costs(),
                                    x.created_at,
                                    market_price=x.entry if x.market else None,
                                )
                                for x in base[name][s][0]
                            ],
                        )[1]
                    )
                    for s in SYMS
                },
            )
        )
        add("")

    # ---------------------------------------------------------------------------- C
    add("## C. Owner's trades vs the bot\n")
    manual = OUT / "manual_trades.csv"
    if not manual.exists():
        (OUT / "manual_trades_template.csv").write_text(
            "date_time_utc,symbol,direction,bias_tf,zone_tf,entry_tf,entry,sl,tp,exit,result_r,"
            "fee_paid,entry_type,why_taken,screenshot\n"
        )
        add(
            "`docs/diagnostics/manual_trades.csv` is not there yet: comparison pending. A template "
            "with the requested columns is in `manual_trades_template.csv`. One owner trade is "
            "known from a screenshot: BTC/USDT long, 0.00052 BTC, take-profit trigger 86,132, "
            "filled 2026-10-04 22:02:28 UTC at 86,111.9 (market). Entry, stop and setup are "
            "needed to compare it.\n"
        )
    add("### Bot losers for the owner to mark\n")
    losers: list[tuple[str, str, Walked]] = []
    for name in pops:
        for sym in SYMS:
            for w in base[name][sym][1]:
                if w.t.result_r is not None and w.t.result_r <= 0:
                    losers.append((name, sym, w))
    rnd = random.Random(11)
    cur = [x for x in losers if x[0] == "current"]
    wide = [
        x for x in losers if x[0] == "wide" and all(x[2].setup.key != c[2].setup.key for c in cur)
    ]
    pick = cur + rnd.sample(wide, min(len(wide), max(0, 20 - len(cur))))
    add(
        "| # | population | market | side | order (UTC) | entry | SL | TP | result | chart | would take? why |"
    )
    add("|---|---|---|---|---|---|---|---|---|---|---|")
    for i, (name, sym, w) in enumerate(sorted(pick, key=lambda x: x[2].t.created_at), 1):
        t = w.t
        p = pops[name][sym]
        za = ctx[sym][p.zone_tf]
        k = last_closed(za, t.closed_at or t.created_at)
        h1 = za.bars[max(0, w.setup.zone.ob.idx - 30) : k + 4]
        ax = ctx[sym][p.exec_tf]
        a0 = (t.filled_at or t.created_at) - timedelta(hours=10)
        a1 = (t.closed_at or t.created_at) + timedelta(hours=3)
        m15 = [b for b in ax.bars if a0 <= b.open_time <= a1]
        fn = f"losers/{i:02d}_{sym}_{t.created_at:%Y%m%d_%H%M}.svg"
        render(
            str(OUT / fn),
            f"#{i} {sym} {t.side.value} {t.created_at:%Y-%m-%d %H:%M} UTC ({name})",
            h1,
            m15,
            w,
        )
        add(
            f"| {i} | {name} | {sym} | {t.side.value} | {t.created_at:%Y-%m-%d %H:%M} | "
            f"{t.entry.normalize()} | {(t.sl0 or t.sl).normalize()} | {t.tp.normalize()} | "
            f"{t.state.value} {fnum(t.result_r)}R | [svg]({fn}) | |"
        )
    add("")
    findings = OUT / "findings.md"  # the hand-written conclusions, kept across runs
    if findings.exists():
        rep[3:3] = [findings.read_text()]
    (OUT / "report.md").write_text("\n".join(rep) + "\n")
    print("\n".join(rep))


if __name__ == "__main__":
    main()
