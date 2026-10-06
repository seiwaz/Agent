"""F2. Hand-audit charts: one SVG per trade (zone-TF and execution-TF panels) with the sweep,
the break (BOS / CHoCH level from the swing to the breaking bar), the OB and its FVG drawn
only from the moment they became known (faded before), the execution-TF confirmation, entry,
SL, TP / liquidity level, fill and exit, and a ledger row under the panels.

Population: every closed trade of BTC / XRP (all parameter sets) plus other markets until 10
winners and 10 losers are reached (or the population is exhausted).

Usage: uv run python -m audit.charts -> docs/audit/trades/*.svg, docs/audit/trades/index.json
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from html import escape
from typing import Any

from audit.common import EXTRA, MAIN, OUT, m1, params
from audit.execution import VARIANTS, population
from sp2l.core.types import Candle
from sp2l.smc.timeframes import MINUTE, aggregate, length

W, H, PAD = 640, 420, 46
C = {"bg": "#131722", "txt": "#d1d4dc", "up": "#26a69a", "dn": "#ef5350", "ob": "rgba(255,193,7,0.25)",
     "pre": "rgba(255,193,7,0.07)", "fvg": "rgba(84,160,255,0.25)", "sl": "#ef5350", "tp": "#26a69a",
     "entry": "#e0e0e0", "lvl": "#b388ff", "sweep": "#c494ff", "known": "#ffb74d", "bos": "#90caf9"}


def panel(x0: int, title: str, bars: list[Candle], s: Any, t: Any, marks: list[tuple[datetime, str]]) -> list[str]:
    zs = s.zone
    lines = [(s.entry, "entry", C["entry"]), (s.sl, "SL", C["sl"]), (s.tp.price, "TP", C["tp"])]
    if s.tp.level is not None:
        lines.append((s.tp.level, "liq", C["lvl"]))
    ps = [b.high for b in bars] + [b.low for b in bars] + [x[0] for x in lines]
    hi, lo = max(ps), min(ps)
    sp = (hi - lo) or Decimal(1)
    t0, t1 = bars[0].open_time, bars[-1].open_time + length(title.split()[-1])

    def X(x: datetime) -> float:
        x = min(max(x, t0), t1)
        return x0 + PAD + (x - t0).total_seconds() / (t1 - t0).total_seconds() * (W - 2 * PAD)

    def Y(p: Decimal) -> float:
        return float(PAD + (hi - p) / sp * (H - 2 * PAD))

    o = [f'<text x="{x0 + PAD}" y="20" fill="{C["txt"]}" font-size="13">{escape(title)}</text>']
    xr = x0 + W - PAD
    known = zs.confirmed_at
    for (a, b), col in (((zs.ob.bottom, zs.ob.top), "ob"), (zs.gap, "fvg")):
        xa, xk = X(zs.ob.time), X(known)
        o.append(f'<rect x="{xa:.1f}" y="{Y(b):.1f}" width="{max(0.0, xk - xa):.1f}" height="{max(1.0, Y(a) - Y(b)):.1f}" fill="{C["pre"]}"/>')
        o.append(f'<rect x="{xk:.1f}" y="{Y(b):.1f}" width="{max(0.0, xr - xk):.1f}" height="{max(1.0, Y(a) - Y(b)):.1f}" fill="{C[col]}"/>')
    bw = max(1.0, (W - 2 * PAD) / max(1, len(bars)) * 0.65)
    dur = length(title.split()[-1])
    for b in bars:
        x = X(b.open_time + dur / 2)
        col = C["up"] if b.close >= b.open else C["dn"]
        top, bot = max(b.open, b.close), min(b.open, b.close)
        o.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{Y(b.high):.1f}" y2="{Y(b.low):.1f}" stroke="{col}"/>'
                 f'<rect x="{x - bw / 2:.1f}" y="{Y(top):.1f}" width="{bw:.1f}" height="{max(0.8, Y(bot) - Y(top)):.1f}" fill="{col}"/>')
    ev = zs.event
    o.append(f'<line x1="{X(ev.level_time):.1f}" x2="{X(ev.break_time + length(zs.tf)):.1f}" y1="{Y(ev.level):.1f}" y2="{Y(ev.level):.1f}" stroke="{C["bos"]}" stroke-width="1.5"/>'
             f'<text x="{X(ev.break_time):.1f}" y="{Y(ev.level) - 4:.1f}" fill="{C["bos"]}" font-size="10">{ev.kind}</text>')
    if zs.sweep is not None:
        xs = X(zs.sweep.time)
        o.append(f'<line x1="{xs - 25:.1f}" x2="{xs + 8:.1f}" y1="{Y(zs.sweep.level):.1f}" y2="{Y(zs.sweep.level):.1f}" stroke="{C["sweep"]}" stroke-dasharray="4 3"/>'
                 f'<circle cx="{xs:.1f}" cy="{Y(zs.sweep.wick):.1f}" r="3" fill="{C["sweep"]}"/>')
    for p_, name, col in lines:
        o.append(f'<line x1="{x0 + PAD}" x2="{xr}" y1="{Y(p_):.1f}" y2="{Y(p_):.1f}" stroke="{col}" stroke-dasharray="6 3"/>'
                 f'<text x="{xr + 2}" y="{Y(p_) + 4:.1f}" fill="{col}" font-size="10">{name} {p_.normalize()}</text>')
    for when, name in marks:
        x = X(when)
        o.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{PAD - 8}" y2="{H - PAD}" stroke="{C["known"]}" stroke-dasharray="2 3"/>'
                 f'<text x="{x + 2:.1f}" y="{H - PAD + 12 + 11 * (marks.index((when, name)) % 3)}" fill="{C["known"]}" font-size="9">{escape(name)}</text>')
    for when, px in ((t.filled_at, s.entry), (t.closed_at, t.exit_price)):
        if when is not None and px is not None:
            o.append(f'<circle cx="{X(when):.1f}" cy="{Y(px):.1f}" r="4" fill="none" stroke="#fff" stroke-width="2"/>')
    return o


def render(path: str, sym: str, v: str, s: Any, t: Any, bars1: list[Candle]) -> dict[str, Any]:
    p = params(sym, **VARIANTS[v])
    zs = s.zone
    start = (zs.sweep.time if zs.sweep is not None else zs.ob.time) - length(zs.tf) * 12
    end = (t.closed_at or s.created_at) + length(zs.tf) * 8
    seg = [b for b in bars1 if start - timedelta(days=1) <= b.open_time < end]
    up = seg[-1].open_time + MINUTE
    za = [b for b in aggregate(seg, zs.tf, up, p.htf_grid) if b.open_time >= start]
    es = s.confirm.event.break_time - timedelta(hours=3) if s.confirm is not None else s.created_at - timedelta(hours=3)
    ex = [b for b in aggregate(seg, p.exec_tf, up, p.htf_grid) if es <= b.open_time < (t.closed_at or s.created_at) + timedelta(hours=1)]
    marks = [(zs.confirmed_at, "setup known"), (s.created_at, "order")]
    if s.confirm is not None:
        marks.append((s.confirm.t, f"{p.exec_tf} {s.confirm.event.kind}"))
    title = f"{sym} {v} {s.direction.value} {t.state.value} {t.result_r and round(t.result_r, 2)}R"
    body = panel(0, f"{title} · {zs.tf}", za, s, t, marks) + panel(W + 70, f"{p.exec_tf}", ex, s, t, marks)
    row = {
        "symbol": sym, "params": v, "key": s.key, "side": s.direction.value,
        "sweep": None if zs.sweep is None else [zs.sweep.time.isoformat(), str(zs.sweep.level), str(zs.sweep.wick)],
        "break": [zs.event.kind, str(zs.event.level), (zs.event.break_time + length(zs.tf)).isoformat()],
        "ob": [str(zs.ob.bottom), str(zs.ob.top), zs.ob.time.isoformat()], "fvg": [str(x) for x in zs.gap],
        "setup_known": zs.confirmed_at.isoformat(),
        "confirm": None if s.confirm is None else [s.confirm.event.kind, s.confirm.t.isoformat(), str(s.confirm.origin)],
        "order": s.created_at.isoformat(), "entry": str(s.entry), "sl": str(s.sl), "tp": str(s.tp.price),
        "tp_source": s.tp.source, "fill": t.filled_at and t.filled_at.isoformat(),
        "exit": [t.state.value, t.closed_at and t.closed_at.isoformat(), str(t.exit_price)], "r": str(round(t.result_r, 3)),
    }
    row = json.loads(json.dumps(row, default=str), parse_float=str)
    row = {k: _tidy(v) for k, v in row.items()}
    ledger = "  ·  ".join(f"{k}: {v_}" for k, v_ in row.items() if k not in ("symbol", "params", "key"))
    words, lines_, cur = ledger.split("  ·  "), [], ""
    for w in words:
        if len(cur) + len(w) > 190:
            lines_.append(cur)
            cur = ""
        cur += ("  ·  " if cur else "") + w
    lines_.append(cur)
    text = "".join(f'<text x="10" y="{H + 30 + 14 * i}" fill="{C["txt"]}" font-size="10">{escape(x)}</text>' for i, x in enumerate(lines_))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{2 * W + 140}" height="{H + 40 + 14 * len(lines_)}" font-family="sans-serif">'
           f'<rect width="100%" height="100%" fill="{C["bg"]}"/>' + "".join(body) + text + "</svg>")
    with open(path, "w") as f:
        f.write(svg)
    return row


def _tidy(v: Any) -> Any:
    """Decimals as plain numbers (no trailing zeros) in the ledger text."""
    if isinstance(v, list):
        return [_tidy(x) for x in v]
    if isinstance(v, str):
        try:
            return format(Decimal(v).normalize(), "f") if any(ch.isdigit() for ch in v) and "T" not in v else v
        except Exception:
            return v
    return v


def main() -> None:
    d = OUT / "trades"
    d.mkdir(parents=True, exist_ok=True)
    picked: list[tuple[str, str, Any, Any]] = []
    wins = losses = 0
    for sym in MAIN + EXTRA:
        for v in VARIANTS:
            for s, t in population(sym, v)["trades"]:
                if t.result_r is None:
                    continue
                win = t.result_r > 0
                if sym in MAIN or (win and wins < 10) or (not win and losses < 10):
                    picked.append((sym, v, s, t))
                    wins += win
                    losses += not win
    rows = []
    for n, (sym, v, s, t) in enumerate(picked, 1):
        name = f"{n:02d}_{sym}_{v}_{s.created_at:%Y%m%d_%H%M}.svg"
        rows.append({"file": name, **render(str(d / name), sym, v, s, t, m1(sym))})
    (d / "index.json").write_text(json.dumps(rows, indent=1))
    print(len(rows), "charts;", wins, "winners", losses, "losers")


if __name__ == "__main__":
    main()
