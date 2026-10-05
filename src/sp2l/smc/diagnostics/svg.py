"""A self-contained SVG chart of one trade: 1h and 15m panels with the setup's zones, the
sweep, entry / stop / target, the 50 % line and the fill / exit marks (no dependencies)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from html import escape

from sp2l.core.types import Candle
from sp2l.smc.diagnostics.core import Walked

W, H, PAD = 620, 460, 40
COL = {
    "up": "#26a69a",
    "down": "#ef5350",
    "ob": "rgba(38,166,154,0.22)",
    "ob_s": "rgba(239,83,80,0.22)",
    "fvg": "rgba(84,160,255,0.22)",
    "entry": "#e0e0e0",
    "sl": "#ef5350",
    "tp": "#26a69a",
    "level": "#b388ff",
    "mid": "#9e9e9e",
    "sweep": "#c494ff",
    "bg": "#131722",
    "txt": "#d1d4dc",
}


def _panel(
    x0: int, title: str, bars: Sequence[Candle], w: Walked, lines: list[tuple[Decimal, str, str]]
) -> list[str]:
    s = w.setup
    zs = s.zone
    prices = [b.high for b in bars] + [b.low for b in bars] + [x[0] for x in lines]
    hi, lo = max(prices), min(prices)
    span = (hi - lo) or Decimal(1)
    t0, t1 = bars[0].open_time, bars[-1].open_time
    tw = (t1 - t0).total_seconds() or 1.0

    def X(t: datetime) -> float:
        return x0 + PAD + (t - t0).total_seconds() / tw * (W - 2 * PAD)

    def Y(p: Decimal) -> float:
        return float(PAD + (hi - p) / span * (H - 2 * PAD))

    out = [f'<text x="{x0 + PAD}" y="22" fill="{COL["txt"]}" font-size="13">{escape(title)}</text>']
    bw = max(1.0, (W - 2 * PAD) / max(1, len(bars)) * 0.7)
    ob_col = COL["ob"] if s.direction.value == "LONG" else COL["ob_s"]
    xr = x0 + W - PAD
    xo = max(X(zs.ob.time), x0 + PAD)
    out.append(
        f'<rect x="{xo:.1f}" y="{Y(zs.ob.top):.1f}" width="{xr - xo:.1f}"'
        f' height="{max(1.0, Y(zs.ob.bottom) - Y(zs.ob.top)):.1f}" fill="{ob_col}"/>'
    )
    out.append(
        f'<rect x="{xo:.1f}" y="{Y(zs.gap[1]):.1f}" width="{xr - xo:.1f}"'
        f' height="{max(1.0, Y(zs.gap[0]) - Y(zs.gap[1])):.1f}" fill="{COL["fvg"]}"/>'
    )
    for b in bars:
        x = X(b.open_time)
        c = COL["up"] if b.close >= b.open else COL["down"]
        out.append(
            f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{Y(b.high):.1f}" y2="{Y(b.low):.1f}"'
            f' stroke="{c}" stroke-width="1"/>'
        )
        top, bot = max(b.open, b.close), min(b.open, b.close)
        out.append(
            f'<rect x="{x - bw / 2:.1f}" y="{Y(top):.1f}" width="{bw:.1f}"'
            f' height="{max(0.8, Y(bot) - Y(top)):.1f}" fill="{c}"/>'
        )
    if zs.sweep is not None and t0 <= zs.sweep.time <= t1:
        xs = X(zs.sweep.time)
        out.append(
            f'<line x1="{xs - 30:.1f}" x2="{xs + 10:.1f}" y1="{Y(zs.sweep.level):.1f}"'
            f' y2="{Y(zs.sweep.level):.1f}" stroke="{COL["sweep"]}" stroke-dasharray="4 3"/>'
            f'<circle cx="{xs:.1f}" cy="{Y(zs.sweep.wick):.1f}" r="3" fill="{COL["sweep"]}"/>'
        )
    for price, name, color in lines:
        y = Y(price)
        out.append(
            f'<line x1="{x0 + PAD}" x2="{xr}" y1="{y:.1f}" y2="{y:.1f}" stroke="{color}"'
            f' stroke-dasharray="{"2 3" if name.startswith("50") else "6 3"}" stroke-width="1"/>'
            f'<text x="{xr + 2}" y="{y + 4:.1f}" fill="{color}" font-size="10">'
            f"{escape(name)} {price.normalize()}</text>"
        )
    t = w.t
    marks: list[tuple[datetime | None, Decimal | None, str]] = [
        (t.filled_at, t.entry, "fill"),
        (t.closed_at, t.exit_price, "exit"),
    ]
    for when, at, mark in marks:
        if when is not None and at is not None and t0 <= when <= t1:
            out.append(
                f'<circle cx="{X(when):.1f}" cy="{Y(at):.1f}" r="4" fill="none"'
                f' stroke="{COL["entry"]}" stroke-width="2"><title>{mark}</title></circle>'
            )
    return out


def render(path: str, title: str, h1: Sequence[Candle], m15: Sequence[Candle], w: Walked) -> None:
    s = w.setup
    assert s.entry is not None and s.sl is not None and s.tp is not None
    lines: list[tuple[Decimal, str, str]] = [
        (s.entry, "entry", COL["entry"]),
        (s.sl, "SL", COL["sl"]),
        (s.tp.price, "TP", COL["tp"]),
    ]
    if s.tp.level is not None:
        lines.append((s.tp.level, "HH/LL", COL["level"]))
    if s.range_mid is not None:
        lines.append((s.range_mid, "50%", COL["mid"]))
    body = _panel(0, f"{title} · 1h", h1, w, lines) + _panel(W + 60, "15m", m15, w, lines)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{2 * W + 120}" height="{H}"'
        f' font-family="sans-serif"><rect width="100%" height="100%" fill="{COL["bg"]}"/>'
        + "".join(body)
        + "</svg>"
    )
    with open(path, "w") as f:
        f.write(svg)
