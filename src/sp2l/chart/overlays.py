"""Chart overlays computed from closed bars of one timeframe (display only, no trading).

Every drawing is bounded to its own time range, never extended into the future:

- Structure (`sp2l.smc.structure.analyze`): swings labelled HH / LH (highs) and HL / LL
  (lows) against the previous swing of the same kind; BOS / CHoCH from the broken swing to
  the closing break; order blocks and fair value gaps from their candle to the bar that
  mitigated / filled them, or to the last bar while still valid.
- Support / resistance: confirmed swing pivots within `sr_tol_atr` x ATR of each other form
  one zone (at least `sr_touches` pivots). It is drawn from its first pivot to the first close
  through it after its last touch (broken), or to the last bar while unbroken. Zones form in
  ranges and in trends alike (the higher lows of an uptrend are supports).
- Trendlines: two consecutive rising swing lows (support) or falling swing highs
  (resistance), with no close beyond the line between them, drawn from the first pivot to the
  first close beyond the line, or to the last bar while unbroken.
- Trend: the structure trend after the last closed bar (+1 bullish, -1 bearish, 0 none).

Pivots are known only `swing_len` bars after the pivot bar, so a zone or line never uses a bar
that had not closed when it was formed.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from sp2l.core.types import Candle
from sp2l.smc.model import Analysis, SmcParams, Swing, Zone, ZoneStatus
from sp2l.smc.structure import analyze

TREND_LABEL = {1: "BULLISH", -1: "BEARISH", 0: "UNDEFINED"}


@dataclass(frozen=True, slots=True)
class ChartParams:
    swing_len: int = 5  # pivot = N bars on each side
    sr_tol_atr: float = 0.25  # pivots this close (x ATR) are one zone
    sr_touches: int = 2  # pivots needed for a zone
    sr_max: int = 8  # zones returned (the most recently touched)
    tl_max: int = 3  # trendlines per side (the most recent)


def _t(d: datetime) -> str:
    return d.isoformat()


def _f(x: Decimal | float | None) -> float | None:
    return None if x is None else float(x)


def structure(bars: Sequence[Candle], tf: str, base: SmcParams, cp: ChartParams) -> Analysis:
    return analyze(bars, tf, replace(base, swing_len=cp.swing_len))


def labelled_swings(a: Analysis) -> list[dict[str, Any]]:
    out = []
    prev: dict[str, Swing | None] = {"HIGH": None, "LOW": None}
    for s in a.swings:
        p = prev[s.kind]
        if s.kind == "HIGH":
            label = None if p is None else ("HH" if s.price > p.price else "LH")
        else:
            label = None if p is None else ("HL" if s.price > p.price else "LL")
        prev[s.kind] = s
        if label is not None:
            out.append({"kind": s.kind, "label": label, "price": _f(s.price), "time": _t(s.time)})
    return out


def events(a: Analysis) -> list[dict[str, Any]]:
    return [
        {
            "kind": e.kind,
            "direction": e.direction.value,
            "level": _f(e.level),
            "from": _t(e.level_time),
            "to": _t(e.break_time),
        }
        for e in a.events
    ]


def _zone_end(z: Zone, a: Analysis) -> tuple[int, bool]:
    """(index of the last bar the zone is drawn on, still valid)."""
    k = len(a.bars) - 1
    if z.mitigated_idx is not None and z.mitigated_idx <= k:
        return z.mitigated_idx, False
    if z.expires_idx < k:
        return z.expires_idx, False
    return k, True


def zones(a: Analysis) -> list[dict[str, Any]]:
    out = []
    for z in a.zones:
        end, valid = _zone_end(z, a)
        out.append(
            {
                "kind": z.kind,
                "direction": z.direction.value,
                "top": _f(z.top),
                "bottom": _f(z.bottom),
                "from": _t(a.bars[z.idx].open_time),
                "to": _t(a.bars[end].open_time),
                "valid": valid,
                "tested": z.status is not ZoneStatus.ACTIVE,
            }
        )
    return out


def support_resistance(a: Analysis, cp: ChartParams) -> list[dict[str, Any]]:
    bars = a.bars
    k = len(bars) - 1
    levels: list[dict[str, Any]] = []
    for s in sorted(a.swings, key=lambda s: s.confirmed_idx):
        atr = a.atr[s.idx] or a.atr[min(k, s.confirmed_idx)]
        if atr is None:
            continue
        tol = float(atr) * cp.sr_tol_atr
        px = float(s.price)
        hit = None
        for lv in levels:
            # a zone takes pivots only while unbroken as of this pivot's confirmation
            if lv["broken_idx"] is not None and lv["broken_idx"] <= s.confirmed_idx:
                continue
            if lv["bottom"] - tol <= px <= lv["top"] + tol:
                hit = lv
                break
        if hit is None:
            levels.append({"top": px, "bottom": px, "pivots": [s], "broken_idx": None, "tol": tol})
        else:
            hit["top"], hit["bottom"] = max(hit["top"], px), min(hit["bottom"], px)
            hit["pivots"].append(s)
            hit["broken_idx"] = None
        lv = hit or levels[-1]
        # the first close through the zone after its last confirmed pivot ends it
        last = lv["pivots"][-1].confirmed_idx
        side = 1 if float(bars[last].close) >= (lv["top"] + lv["bottom"]) / 2 else -1
        lv["side"] = side  # price above: a support; below: a resistance
        for i in range(last + 1, k + 1):
            c = float(bars[i].close)
            if (side > 0 and c < lv["bottom"] - lv["tol"]) or (
                side < 0 and c > lv["top"] + lv["tol"]
            ):
                lv["broken_idx"] = i
                break
    out = []
    for lv in levels:
        if len(lv["pivots"]) < cp.sr_touches:
            continue
        end = lv["broken_idx"] if lv["broken_idx"] is not None else k
        pad = lv["tol"] / 2
        out.append(
            {
                "top": lv["top"] + pad,
                "bottom": lv["bottom"] - pad,
                "role": "SUPPORT" if lv["side"] > 0 else "RESISTANCE",
                "touches": len(lv["pivots"]),
                "from": _t(lv["pivots"][0].time),
                "to": _t(bars[end].open_time),
                "last_touch": _t(lv["pivots"][-1].time),
                "broken": lv["broken_idx"] is not None,
            }
        )
    out.sort(key=lambda z: z["last_touch"], reverse=True)
    return out[: cp.sr_max]


def trendlines(a: Analysis, cp: ChartParams) -> list[dict[str, Any]]:
    bars = a.bars
    k = len(bars) - 1
    out: list[dict[str, Any]] = []
    for kind, rising in (("LOW", True), ("HIGH", False)):
        piv = [s for s in a.swings if s.kind == kind]
        lines = []
        for s1, s2 in zip(piv, piv[1:], strict=False):
            p1, p2 = float(s1.price), float(s2.price)
            if (p2 <= p1) if rising else (p2 >= p1):
                continue
            slope = (p2 - p1) / (s2.idx - s1.idx)

            def at(i: int, p1: float = p1, i1: int = s1.idx, m: float = slope) -> float:
                return p1 + m * (i - i1)

            def beyond(i: int, f: Callable[[int], float] = at, up: bool = rising) -> bool:
                c = float(bars[i].close)
                return c < f(i) if up else c > f(i)

            if any(beyond(i) for i in range(s1.idx + 1, s2.idx)):
                continue  # not a valid line: price closed through it between the pivots
            end, broken = k, False
            for i in range(s2.confirmed_idx, k + 1):
                if beyond(i):
                    end, broken = i, True
                    break
            lines.append(
                {
                    "role": "SUPPORT" if rising else "RESISTANCE",
                    "from": _t(s1.time),
                    "from_price": p1,
                    "to": _t(bars[end].open_time),
                    "to_price": at(end),
                    "broken": broken,
                    "_k": s2.confirmed_idx,
                }
            )
        lines.sort(key=lambda x: x["_k"], reverse=True)
        out += lines[: cp.tl_max]
    for x in out:
        x.pop("_k")
    return out


def trend(a: Analysis) -> str:
    return TREND_LABEL[a.trend[-1] if a.trend else 0]


def overlays(bars: Sequence[Candle], tf: str, base: SmcParams, cp: ChartParams) -> dict[str, Any]:
    if len(bars) < 2 * cp.swing_len + 2:
        return {"tf": tf, "ready": False, "reason": f"{len(bars)} closed {tf} bars"}
    a = structure(bars, tf, base, cp)
    return {
        "tf": tf,
        "ready": True,
        "bars": len(bars),
        "last": _t(bars[-1].open_time),
        "trend": trend(a),
        "swings": labelled_swings(a),
        "events": events(a),
        "zones": zones(a),
        "sr": support_resistance(a, cp),
        "trendlines": trendlines(a, cp),
    }
