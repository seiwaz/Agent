# ruff: noqa: E501  (HTML / CSS / JS templates are long by nature)
"""Backtest one market with the current strategy and write one HTML page with every 1h zone
setup the model found and a chart of each, from the real 1-minute data (read-only).

    uv run python scripts/backtest_cases.py [--symbol BTCUSDT] [--days 270] [--out FILE]

Every setup of `zone_setups` (sweep -> BOS/CHoCH -> OB + adjacent FVG) is one case:
- TRADE: accepted and walked with the live lifecycle (TP / SL / TIMEOUT / EXPIRED / MISSED);
- SKIPPED: accepted, but the market already had `max_active` positions;
- REJECTED: evaluated when its order would exist, with the reason codes of `evaluate`;
- NOT_ARMED: price never came back into the FVG before the zone expired (or the data ended);
- ARMED_BEFORE: armed before the first minute of the data (never fresh).
Rejected setups that stopped at an early check (bias, zone, freshness) have no levels from
`evaluate`; the chart then shows the levels the order would have had (entry at the OB edge, SL
one tick beyond the wick, TP from `previous_extreme`), marked "planned".

Each chart: 1h candles from before the sweep to after the outcome, OB and FVG boxes, the sweep
(dashed level, dot at the wick), the broken level, the confirmation and arming times, entry /
SL / TP / HH-LL / 50 % lines and the fill / exit marks; a 15m panel around the order.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine

from sp2l.config import RuntimeConfig
from sp2l.core.types import Candle, Side
from sp2l.smc.backtest import build_context, exit_kind, run
from sp2l.smc.history import load_bars, series_end
from sp2l.smc.model import Analysis, Setup, SmcParams, ZoneSetup
from sp2l.smc.strategy import (
    ARMED_BEFORE,
    _round,
    find_arming,
    last_closed,
    order_for,
    previous_extreme,
    zone_setups,
)
from sp2l.smc.timeframes import length

TEHRAN = timedelta(hours=3, minutes=30)  # Iran has no daylight saving time since 2022


def f(x: Decimal | None) -> float | None:
    return None if x is None else float(x)


def ts(t: datetime | None) -> int | None:
    return None if t is None else int(t.timestamp())


def bars_json(bars: Sequence[Candle], i0: int, i1: int) -> list[list[float]]:
    return [
        [int(b.open_time.timestamp()), float(b.open), float(b.high), float(b.low), float(b.close)]
        for b in bars[max(0, i0) : max(0, i1)]
    ]


def index_at(bars: Sequence[Candle], t: datetime) -> int:
    """First bar opening at or after t (binary search)."""
    lo, hi = 0, len(bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars[mid].open_time < t:
            lo = mid + 1
        else:
            hi = mid
    return lo


def planned(
    zs: ZoneSetup, ctx: dict[str, Analysis], p: SmcParams, t: datetime
) -> dict[str, float | None]:
    """The levels an order created at t would have had (for setups `evaluate` stopped early)."""
    long = zs.direction is Side.LONG
    entry = zs.edge
    sl = zs.ob.bottom - p.tick if long else zs.ob.top + p.tick
    out: dict[str, float | None] = {
        "entry": f(entry),
        "sl": f(sl),
        "tp": None,
        "level": None,
        "mid": None,
    }
    lv = previous_extreme(zs, ctx, p, t, entry)
    if lv is not None:
        za = ctx[p.zone_tf]
        k = last_closed(za, t)
        front = p.tp_front_run_atr * (za.atr[k] or Decimal(0)) if k >= 0 else Decimal(0)
        level = lv[0]
        px = (
            _round(level - front, p.tick, ROUND_FLOOR)
            if long
            else _round(level + front, p.tick, ROUND_CEILING)
        )
        start = zs.sweep.wick if zs.sweep is not None else (zs.ob.bottom if long else zs.ob.top)
        out.update(tp=f(px), level=f(level), mid=f((start + level) / 2))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--days", type=int, default=270)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    sym = args.symbol
    cfg = RuntimeConfig.load(Path("config/runtime.yaml"))
    db = create_engine(cfg.database_url)
    costs = cfg.costs()
    p = cfg.symbol_params(sym)
    upto = series_end(db, sym)
    assert upto is not None
    m1 = load_bars(db, sym, "1m", args.days * 1440, upto)
    ctx = build_context(m1, p)
    res = run(m1, p, costs, ctx)
    za, m15 = ctx[p.zone_tf], ctx["15m"].bars
    h1 = za.bars
    zdur = length(p.zone_tf)

    evaluated: dict[str, Setup] = {s.key: s for s in res["setups"]}
    walked = {s.key: t for s, t in res["trades"]}
    cases: list[dict[str, Any]] = []
    kz = len(h1) - 1
    for zs in zone_setups(za, p):
        armed = find_arming(zs, za, kz, ctx["1m"].bars)
        s = evaluated.get(zs.key)
        t = walked.get(zs.key)
        lv: dict[str, Any] = {}
        trade: dict[str, Any] | None = None
        order_t: datetime | None = None
        if armed is None:
            cat, reasons = (
                "NOT_ARMED",
                ["price never came back into the FVG before the zone expired or the data ended"],
            )
        elif armed == ARMED_BEFORE:
            cat, reasons = "ARMED_BEFORE", ["armed before the first minute of the data"]
        elif s is None:
            o = order_for(zs, ctx, p, armed, ctx["1m"].bars)
            cat, reasons = "NO_ORDER", ["no order yet (the confirmation window is still open)"]
            order_t = o.t if o else None
        else:
            order_t = s.created_at
            if s.accepted:
                cat = "TRADE" if t is not None else "SKIPPED"
                reasons = (
                    []
                    if t is not None
                    else ["accepted, but a position was already active (max_active 1)"]
                )
            else:
                cat, reasons = "REJECTED", list(s.reasons)
            if s.entry is not None:
                lv = {
                    "entry": f(s.entry),
                    "sl": f(s.sl),
                    "tp": f(s.tp.price) if s.tp else None,
                    "level": f(s.tp.level) if s.tp else None,
                    "mid": f(s.range_mid),
                    "net_r": f(s.tp.net_r) if s.tp else None,
                    "cost_frac": f(s.cost_frac),
                    "leverage": f(s.leverage),
                    "planned": False,
                }
                if lv["tp"] is None and s.entry is not None:
                    extra = planned(zs, ctx, p, s.created_at)
                    lv.update({k: extra[k] for k in ("level", "mid")})
            else:
                lv = {**planned(zs, ctx, p, s.created_at), "planned": True}
            if t is not None:
                trade = {
                    "state": t.state.value,
                    "exit": exit_kind(t) if t.parts else t.state.value,
                    "filled_at": ts(t.filled_at),
                    "closed_at": ts(t.closed_at),
                    "exit_price": f(t.exit_price),
                    "r": f(t.result_r),
                }
        if not lv and cat in ("NOT_ARMED", "ARMED_BEFORE", "NO_ORDER"):
            lv = {**planned(zs, ctx, p, order_t or zs.confirmed_at), "planned": True}
        # chart windows
        start_idx = min(zs.ob.idx, zs.sweep.idx if zs.sweep else zs.ob.idx, zs.event.level_idx) - 12
        end_t = (
            trade and trade["closed_at"] and datetime.fromtimestamp(trade["closed_at"], UTC)
        ) or (order_t + timedelta(hours=p.pending_expiry_min / 60 + 6) if order_t else None)
        end_idx = index_at(h1, end_t) + 12 if end_t else min(zs.ob.expires_idx, kz) + 3
        end_idx = min(end_idx, zs.ob.created_idx + 160, len(h1))
        start_idx = max(0, min(start_idx, end_idx - 40))
        focus = order_t or zs.confirmed_at
        i15a = index_at(m15, focus - timedelta(hours=8))
        stop15 = (end_t or focus + timedelta(hours=12)) + timedelta(hours=3)
        i15b = min(index_at(m15, stop15), i15a + 200)
        cases.append(
            {
                "key": zs.key,
                "dir": zs.direction.value,
                "cat": cat,
                "reasons": reasons,
                "ob": [f(zs.ob.bottom), f(zs.ob.top), ts(zs.ob.time)],
                "fvg": [f(zs.gap[0]), f(zs.gap[1])],
                "sweep": None
                if zs.sweep is None
                else {"level": f(zs.sweep.level), "wick": f(zs.sweep.wick), "t": ts(zs.sweep.time)},
                "event": {
                    "kind": zs.event.kind,
                    "level": f(zs.event.level),
                    "level_t": ts(zs.event.level_time),
                    "t": ts(zs.event.break_time),
                },
                "confirmed": ts(zs.confirmed_at),
                "order": ts(order_t),
                "expiry": ts(order_t + timedelta(minutes=p.pending_expiry_min))
                if order_t
                else None,
                "bias": s.bias if s else None,
                "lv": lv,
                "trade": trade,
                "h1": bars_json(h1, start_idx, end_idx),
                "m15": bars_json(m15, i15a, i15b),
            }
        )
    cases.sort(key=lambda c: c["confirmed"])
    for i, c in enumerate(cases, 1):
        c["n"] = i

    st = res["stats"]
    cats = Counter(c["cat"] for c in cases)
    rej = Counter(r for c in cases if c["cat"] == "REJECTED" for r in c["reasons"])
    meta = {
        "symbol": sym,
        "from": ts(m1[0].open_time),
        "to": ts(m1[-1].open_time + timedelta(minutes=1)),
        "minutes": len(m1),
        "hash": p.digest(),
        "version": p.__class__.__module__,
        "stats": st,
        "cats": dict(cats),
        "rej": dict(rej.most_common()),
        "costs": {
            "maker": f(costs.maker_fee),
            "taker": f(costs.taker_fee),
            "slippage": f(costs.slippage),
        },
        "params": {k: str(v) for k, v in p.as_dict().items()},
        "zone_dur": int(zdur.total_seconds()),
        "generated": ts(datetime.now(UTC)),
    }
    out = Path(args.out or f"backtest_{sym}_{args.days}d.html")
    html = TEMPLATE.replace(
        "/*DATA*/", json.dumps({"meta": meta, "cases": cases}, separators=(",", ":"))
    )
    out.write_text(html)
    print(
        f"{out}: {len(cases)} cases {dict(cats)}; stats {st['closed']} closed, total {st['total_r']} R"
    )


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BTC SMC Backtest</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--text:#1d2129;--muted:#667085;--line:#e3e6ea;--up:#16a34a;--down:#dc2626;--ob-l:rgba(22,163,74,.16);--ob-s:rgba(220,38,38,.14);--fvg:rgba(37,99,235,.16);--entry:#111827;--sl:#dc2626;--tp:#16a34a;--lvl:#7c3aed;--mid:#6b7280;--sweep:#9333ea;--evt:#d97706;--accent:#2563eb;--chartbg:#fff;--grid:#eef0f3}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f1115;--panel:#171a21;--text:#e6e8ec;--muted:#9aa3b2;--line:#2a2f3a;--up:#26a69a;--down:#ef5350;--ob-l:rgba(38,166,154,.2);--ob-s:rgba(239,83,80,.2);--fvg:rgba(84,160,255,.2);--entry:#e5e7eb;--sl:#ef5350;--tp:#26a69a;--lvl:#b388ff;--mid:#9e9e9e;--sweep:#c494ff;--evt:#fbbf24;--accent:#60a5fa;--chartbg:#131722;--grid:#1e2230}}
:root[data-theme="dark"]{--bg:#0f1115;--panel:#171a21;--text:#e6e8ec;--muted:#9aa3b2;--line:#2a2f3a;--up:#26a69a;--down:#ef5350;--ob-l:rgba(38,166,154,.2);--ob-s:rgba(239,83,80,.2);--fvg:rgba(84,160,255,.2);--entry:#e5e7eb;--sl:#ef5350;--tp:#26a69a;--lvl:#b388ff;--mid:#9e9e9e;--sweep:#c494ff;--evt:#fbbf24;--accent:#60a5fa;--chartbg:#131722;--grid:#1e2230}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
.wrap{max-width:1320px;margin:0 auto;padding:20px 16px 60px}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 10px}
.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:14px 0}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 12px}.kpi .k{font-size:12px;color:var(--muted)}.kpi .v{font-size:20px;font-weight:600;font-variant-numeric:tabular-nums}
table{border-collapse:collapse;width:100%;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-variant-numeric:tabular-nums}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;font-size:13px;white-space:nowrap}th{color:var(--muted);font-weight:500;background:var(--bg)}
tr:hover td{background:color-mix(in srgb,var(--accent) 6%,transparent)}a{color:var(--accent);text-decoration:none}
.tw{overflow-x:auto;border-radius:10px}
.bar{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0;position:sticky;top:0;background:var(--bg);padding:8px 0;z-index:5}
.bar button{border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:999px;padding:4px 12px;cursor:pointer;font:inherit;font-size:13px}
.bar button[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#fff}
.case{background:var(--panel);border:1px solid var(--line);border-radius:12px;margin:14px 0;padding:14px}
.head{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:6px}.head h3{margin:0;font-size:16px}
.tag{font-size:12px;padding:1px 8px;border-radius:999px;border:1px solid var(--line)}
.t-TRADE{background:color-mix(in srgb,var(--up) 18%,transparent)}.t-REJECTED{background:color-mix(in srgb,var(--down) 15%,transparent)}.t-SKIPPED{background:color-mix(in srgb,var(--evt) 20%,transparent)}
.long{color:var(--up)}.short{color:var(--down)}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:2px 16px;font-size:13px;margin:6px 0 10px}.facts b{font-weight:500;color:var(--muted)}
.charts{display:grid;grid-template-columns:3fr 2fr;gap:10px}@media (max-width:900px){.charts{grid-template-columns:1fr}}
.chart{background:var(--chartbg);border-radius:8px;min-height:340px;position:relative}.chart svg{display:block;width:100%;height:auto}
.legend{font-size:12px;color:var(--muted);margin:8px 0}.legend span{margin-right:12px;white-space:nowrap}
.sw{display:inline-block;width:12px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
.reason{font-weight:600}.pos{color:var(--up)}.neg{color:var(--down)}
.theme{position:absolute;right:16px;top:16px}.theme button{border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:8px;padding:4px 10px;cursor:pointer}
</style>
</head>
<body>
<div class="wrap" style="position:relative">
<div class="theme"><button type="button" id="theme">Light / dark</button></div>
<h1 id="title"></h1>
<div class="muted" id="sub"></div>
<div class="grid" id="kpis"></div>
<h2>What happened to every setup</h2>
<div class="grid" id="cats"></div>
<div class="tw"><table id="rej"></table></div>
<h2>All cases</h2>
<div class="tw"><table id="list"></table></div>
<h2>Charts, one per case</h2>
<div class="legend">
<span><i class="sw" style="background:var(--ob-l)"></i>OB (long)</span><span><i class="sw" style="background:var(--ob-s)"></i>OB (short)</span><span><i class="sw" style="background:var(--fvg)"></i>FVG</span>
<span style="color:var(--sweep)">- - sweep level · ● wick</span><span style="color:var(--evt)">— broken level (BOS / CHoCH)</span>
<span style="color:var(--entry)">— entry</span><span style="color:var(--sl)">— SL</span><span style="color:var(--tp)">— TP</span><span style="color:var(--lvl)">- - HH / LL</span><span style="color:var(--mid)">··· 50 %</span>
<span>│ vertical: C = setup confirmed, A = armed (order), E = order expiry · ○ fill · ✕ exit</span>
</div>
<div class="bar" id="filters"></div>
<div id="cases"></div>
</div>
<script>
const D = /*DATA*/;
const $ = (id) => document.getElementById(id);
const OFF = 12600; // Tehran +03:30
function tt(s, withDay = true) { if (s == null) return "—"; const d = new Date((s + OFF) * 1000); const p = (n) => String(n).padStart(2, "0");
  return (withDay ? `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ` : "") + `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`; }
function px(v) { return v == null ? "—" : Number(v).toLocaleString("en-US", { maximumFractionDigits: 1 }); }
function rr(v) { return v == null ? "—" : `${v >= 0 ? "+" : ""}${Number(v).toFixed(2)}R`; }
const CATTXT = { TRADE: "Accepted, order placed", SKIPPED: "Accepted, skipped (capacity)", REJECTED: "Rejected", NOT_ARMED: "Not armed (price never came back)", ARMED_BEFORE: "Armed before data", NO_ORDER: "No order" };
const REASON = { NO_BIAS: "4h bias undefined", BIAS_MISMATCH: "against the 4h bias", ZONE_INVALID: "OB closed through / expired", NOT_FRESH: "OB already touched", BAD_STOP: "stop on the wrong side",
  NO_TARGET: "no previous HH / LL beyond the entry", NOT_DISCOUNT: "long entry above 50 % of the range", NOT_PREMIUM: "short entry below 50 % of the range", LOW_NET_RR: "net R at TP below 2", COST_HEAVY: "costs too high", LEVERAGE: "would need more than 10x" };
const m = D.meta, st = m.stats;
$("title").textContent = `${m.symbol.replace("USDT", "/USDT")} · SMC backtest · ${D.cases.length} setups`;
$("sub").textContent = `Data ${tt(m.from)} .. ${tt(m.to)} Tehran time (${(m.minutes).toLocaleString()} one-minute bars, local merged series). Strategy SMC-2.1, parameter hash ${m.hash} (same as the server). Costs: maker ${(m.costs.maker * 100).toFixed(3)} %, taker ${(m.costs.taker * 100).toFixed(3)} %, slippage ${(m.costs.slippage * 100).toFixed(4)} %. All times Tehran (UTC+03:30).`;
const k = (a, b, cls = "") => `<div class="kpi"><div class="k">${a}</div><div class="v ${cls}">${b}</div></div>`;
$("kpis").innerHTML = [k("Setups found (1h)", D.cases.length), k("Evaluated (armed)", st.setups), k("Accepted", st.accepted), k("Orders placed", st.signals), k("Closed", st.closed),
  k("Wins", `${st.wins} / ${st.closed}`), k("Total", rr(Number(st.total_r)), Number(st.total_r) >= 0 ? "pos" : "neg"), k("Avg per trade", st.avg_r == null ? "—" : rr(Number(st.avg_r))), k("Max drawdown", `${st.max_drawdown_r}R`)].join("");
$("cats").innerHTML = Object.entries(m.cats).map(([c, n]) => k(CATTXT[c] || c, n)).join("");
$("rej").innerHTML = "<tr><th>Rejection reason (a setup may have several)</th><th>count</th></tr>" + Object.entries(m.rej).map(([r, n]) => `<tr><td><b>${r}</b> · ${REASON[r] || ""}</td><td>${n}</td></tr>`).join("");
$("list").innerHTML = "<tr><th>#</th><th>side</th><th>confirmed</th><th>armed / order</th><th>outcome</th><th>reason</th><th>entry</th><th>SL</th><th>TP</th><th>net R at TP</th><th>result</th></tr>" +
  D.cases.map((c) => `<tr><td><a href="#c${c.n}">${c.n}</a></td><td class="${c.dir === "LONG" ? "long" : "short"}">${c.dir}</td><td>${tt(c.confirmed)}</td><td>${tt(c.order)}</td><td>${c.trade ? c.trade.exit : (CATTXT[c.cat] || c.cat)}</td>
  <td>${c.cat === "REJECTED" ? c.reasons.join(", ") : ""}</td><td>${px(c.lv.entry)}</td><td>${px(c.lv.sl)}</td><td>${px(c.lv.tp)}</td><td>${c.lv.net_r == null ? "—" : c.lv.net_r.toFixed(2)}</td><td class="${c.trade && c.trade.r != null ? (c.trade.r >= 0 ? "pos" : "neg") : ""}">${c.trade ? rr(c.trade.r) : ""}</td></tr>`).join("");

const cats = ["ALL", ...Object.keys(m.cats)];
let filter = "ALL";
$("filters").innerHTML = cats.map((c) => `<button type="button" data-c="${c}" aria-pressed="${c === filter}">${c === "ALL" ? "All" : (CATTXT[c] || c)} (${c === "ALL" ? D.cases.length : m.cats[c]})</button>`).join("");
$("filters").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; filter = b.dataset.c;
  for (const x of $("filters").children) x.setAttribute("aria-pressed", String(x.dataset.c === filter));
  for (const el of document.querySelectorAll(".case")) el.style.display = filter === "ALL" || el.dataset.cat === filter ? "" : "none"; });

function css(v) { return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); }
function chart(c, bars, title, tf) {
  if (!bars.length) return `<div class="muted" style="padding:20px">no ${tf} bars</div>`;
  const W = 900, H = 420, L = 8, R = 92, T = 26, B = 26;
  const lv = c.lv, lines = [];
  if (lv.entry != null) lines.push([lv.entry, `entry${lv.planned ? " (planned)" : ""}`, "--entry", ""]);
  if (lv.sl != null) lines.push([lv.sl, "SL", "--sl", ""]);
  if (lv.tp != null) lines.push([lv.tp, "TP", "--tp", ""]);
  if (lv.level != null) lines.push([lv.level, "HH/LL", "--lvl", "6 3"]);
  if (lv.mid != null) lines.push([lv.mid, "50%", "--mid", "2 3"]);
  const vals = bars.flatMap((b) => [b[2], b[3]]).concat(lines.map((l) => l[0]), [c.ob[0], c.ob[1]]);
  if (c.sweep) vals.push(c.sweep.wick);
  let hi = Math.max(...vals), lo = Math.min(...vals); const pad = (hi - lo) * 0.04 || 1; hi += pad; lo -= pad;
  const t0 = bars[0][0], dt = bars.length > 1 ? bars[1][0] - bars[0][0] : 3600, t1 = bars[bars.length - 1][0] + dt;
  const X = (t) => L + (t - t0) / (t1 - t0) * (W - L - R), Y = (p) => T + (hi - p) / (hi - lo) * (H - T - B);
  const inX = (t) => t != null && t >= t0 && t <= t1;
  const o = [`<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" font-family="system-ui,sans-serif" font-size="11">`];
  o.push(`<text x="${L + 4}" y="16" fill="${css("--text")}" font-size="12" font-weight="600">${title}</text>`);
  for (let i = 0; i <= 5; i++) { const p = lo + (hi - lo) * i / 5, y = Y(p); o.push(`<line x1="${L}" x2="${W - R}" y1="${y}" y2="${y}" stroke="${css("--grid")}"/><text x="${W - R + 4}" y="${y + 4}" fill="${css("--muted")}">${px(p)}</text>`); }
  const xo = Math.max(X(c.ob[2]), L), xr = W - R;
  if (c.ob[2] <= t1) {
    o.push(`<rect x="${xo}" y="${Y(c.ob[1])}" width="${xr - xo}" height="${Math.max(1, Y(c.ob[0]) - Y(c.ob[1]))}" fill="${css(c.dir === "LONG" ? "--ob-l" : "--ob-s")}"/>`);
    o.push(`<rect x="${xo}" y="${Y(c.fvg[1])}" width="${xr - xo}" height="${Math.max(1, Y(c.fvg[0]) - Y(c.fvg[1]))}" fill="${css("--fvg")}"/>`);
    o.push(`<text x="${xo + 3}" y="${Y(c.ob[c.dir === "LONG" ? 0 : 1]) + (c.dir === "LONG" ? 12 : -4)}" fill="${css("--muted")}">OB</text><text x="${xo + 3}" y="${Y(c.fvg[c.dir === "LONG" ? 1 : 0]) + (c.dir === "LONG" ? -4 : 12)}" fill="${css("--accent")}">FVG</text>`);
  }
  const bw = Math.max(1, (W - L - R) / bars.length * 0.65);
  for (const b of bars) { const x = X(b[0] + dt / 2), up = b[4] >= b[1], col = css(up ? "--up" : "--down");
    o.push(`<line x1="${x}" x2="${x}" y1="${Y(b[2])}" y2="${Y(b[3])}" stroke="${col}"/><rect x="${x - bw / 2}" y="${Y(Math.max(b[1], b[4]))}" width="${bw}" height="${Math.max(0.8, Math.abs(Y(b[1]) - Y(b[4])))}" fill="${col}"/>`); }
  if (c.event && inX(c.event.t)) { const x1 = Math.max(X(c.event.level_t), L), x2 = X(c.event.t + dt), y = Y(c.event.level);
    o.push(`<line x1="${x1}" x2="${x2}" y1="${y}" y2="${y}" stroke="${css("--evt")}" stroke-width="1.5"/><text x="${x2 + 3}" y="${y - 3}" fill="${css("--evt")}" font-weight="600">${c.event.kind}</text>`); }
  if (c.sweep && inX(c.sweep.t)) { const x = X(c.sweep.t + dt / 2);
    o.push(`<line x1="${Math.max(L, x - 60)}" x2="${x + 14}" y1="${Y(c.sweep.level)}" y2="${Y(c.sweep.level)}" stroke="${css("--sweep")}" stroke-dasharray="4 3" stroke-width="1.5"/><circle cx="${x}" cy="${Y(c.sweep.wick)}" r="4" fill="${css("--sweep")}"/><text x="${x + 6}" y="${Y(c.sweep.wick) + (c.dir === "LONG" ? 14 : -6)}" fill="${css("--sweep")}">sweep</text>`); }
  // price labels on the right: pushed apart so none overlaps (a short leader shows the true level)
  const labs = lines.map(([p, name, v]) => ({ y: Y(p), ly: Y(p), name, v, p })).sort((a, b) => a.y - b.y);
  for (let i = 1; i < labs.length; i++) labs[i].ly = Math.max(labs[i].ly, labs[i - 1].ly + 16);
  for (let i = labs.length - 2; i >= 0; i--) if (labs[i + 1].ly > H - B) labs[i + 1].ly = Math.min(labs[i + 1].ly, H - B), labs[i].ly = Math.min(labs[i].ly, labs[i + 1].ly - 16);
  for (const [p, name, v, dash] of lines) { const y = Y(p);
    o.push(`<line x1="${L}" x2="${W - R}" y1="${y}" y2="${y}" stroke="${css(v)}" stroke-width="1.3" ${dash ? `stroke-dasharray="${dash}"` : ""}/>`); }
  for (const l of labs) o.push(`<line x1="${W - R - 6}" x2="${W - R + 1}" y1="${l.y}" y2="${l.ly}" stroke="${css(l.v)}"/><rect x="${W - R + 1}" y="${l.ly - 8}" width="${R - 2}" height="15" rx="3" fill="${css(l.v)}" opacity=".92"/><text x="${W - R + 4}" y="${l.ly + 3}" fill="${css("--chartbg")}" font-weight="600">${l.name.split(" ")[0]} ${px(l.p)}</text>`);
  for (const [t, lab] of [[c.confirmed, "C"], [c.order, "A"], [c.trade ? null : c.expiry, "E"]]) if (inX(t)) { const x = X(t);
    o.push(`<line x1="${x}" x2="${x}" y1="${T}" y2="${H - B}" stroke="${css("--muted")}" stroke-dasharray="3 4"/><text x="${x + 3}" y="${T + 10}" fill="${css("--muted")}" font-weight="600">${lab}</text>`); }
  if (c.trade) { const tr = c.trade;
    if (inX(tr.filled_at) && lv.entry != null) o.push(`<circle cx="${X(tr.filled_at)}" cy="${Y(lv.entry)}" r="5" fill="none" stroke="${css("--entry")}" stroke-width="2"/>`);
    if (inX(tr.closed_at) && tr.exit_price != null) { const x = X(tr.closed_at), y = Y(tr.exit_price);
      o.push(`<path d="M${x - 5} ${y - 5}L${x + 5} ${y + 5}M${x + 5} ${y - 5}L${x - 5} ${y + 5}" stroke="${css(tr.r >= 0 ? "--tp" : "--sl")}" stroke-width="2.5"/>`); } }
  const nt = 5; for (let i = 0; i <= nt; i++) { const t = t0 + (t1 - t0) * i / nt; o.push(`<text x="${X(t)}" y="${H - 8}" fill="${css("--muted")}" text-anchor="${i === 0 ? "start" : i === nt ? "end" : "middle"}">${tf === "1h" ? tt(Math.round(t)).slice(5) : tt(Math.round(t), false)}</text>`); }
  o.push("</svg>"); return o.join("");
}
function card(c) {
  const lv = c.lv, tr = c.trade;
  const out = tr ? `${tr.exit} · <span class="${tr.r >= 0 ? "pos" : "neg"}">${rr(tr.r)}</span>` : (CATTXT[c.cat] || c.cat);
  const why = c.cat === "REJECTED" ? c.reasons.map((r) => `<span class="reason">${r}</span> (${REASON[r] || ""})`).join(", ") : c.reasons.join("; ");
  const f = (a, b) => `<div><b>${a}:</b> ${b}</div>`;
  return `<div class="head"><h3>#${c.n} <span class="${c.dir === "LONG" ? "long" : "short"}">${c.dir}</span></h3><span class="tag t-${c.cat}">${out}</span>${why ? `<span class="muted">${why}</span>` : ""}</div>
  <div class="facts">${f("Sweep", c.sweep ? `${tt(c.sweep.t)} · level ${px(c.sweep.level)} · wick ${px(c.sweep.wick)}` : "—")}${f(c.event.kind, `${tt(c.event.t)} · broke ${px(c.event.level)}`)}
  ${f("OB", `${tt(c.ob[2])} · ${px(c.ob[0])} – ${px(c.ob[1])}`)}${f("FVG", `${px(c.fvg[0])} – ${px(c.fvg[1])}`)}${f("Confirmed", tt(c.confirmed))}${f("Armed / order", tt(c.order))}
  ${f("4h bias", c.bias == null ? "—" : c.bias > 0 ? "bullish" : c.bias < 0 ? "bearish" : "undefined")}${f(lv.planned ? "Entry / SL (planned)" : "Entry / SL", `${px(lv.entry)} / ${px(lv.sl)}`)}${f("TP / HH-LL", `${px(lv.tp)} / ${px(lv.level)}`)}
  ${f("50 % of range", px(lv.mid))}${f("Net R at TP", lv.net_r == null ? "—" : lv.net_r.toFixed(2))}${f("Costs / stop", lv.cost_frac == null ? "—" : `${(lv.cost_frac * 100).toFixed(0)} %`)}
  ${tr ? f("Fill / exit", `${tt(tr.filled_at)} / ${tt(tr.closed_at)} @ ${px(tr.exit_price)}`) : ""}</div>
  <div class="charts"><div class="chart" data-tf="1h"></div><div class="chart" data-tf="15m"></div></div>`;
}
const box = $("cases");
const io = new IntersectionObserver((es) => { for (const e of es) if (e.isIntersecting) { const el = e.target, c = D.cases[+el.dataset.i];
  const [a, b] = el.querySelectorAll(".chart"); a.innerHTML = chart(c, c.h1, `#${c.n} · 1h`, "1h"); b.innerHTML = chart(c, c.m15, "15m around the order", "15m"); io.unobserve(el); } }, { rootMargin: "600px" });
D.cases.forEach((c, i) => { const el = document.createElement("section"); el.className = "case"; el.id = `c${c.n}`; el.dataset.cat = c.cat; el.dataset.i = i; el.innerHTML = card(c); box.appendChild(el); io.observe(el); });
function redraw() { for (const el of document.querySelectorAll(".case")) { const c = D.cases[+el.dataset.i], [a, b] = el.querySelectorAll(".chart");
  if (a.innerHTML) { a.innerHTML = chart(c, c.h1, `#${c.n} · 1h`, "1h"); b.innerHTML = chart(c, c.m15, "15m around the order", "15m"); } } }
$("theme").addEventListener("click", () => { const r = document.documentElement, dark = r.dataset.theme ? r.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  r.dataset.theme = dark ? "light" : "dark"; redraw(); });
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", redraw);  // charts read the theme colours when drawn
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
