"""Market structure: swings, BOS / CHoCH, order blocks and fair value gaps.

One causal pass over closed bars. Nothing at bar i uses a later bar:

- Swing: a bar whose high (low) is strictly above (below) the `swing_len` bars on its left
  and not below (above) the `swing_len` bars on its right. It is known only `swing_len` bars
  later (`confirmed_idx`).
- Break: a bar CLOSING beyond the last unbroken swing. It is a BOS when it continues the
  current trend and a CHoCH when it breaks against it (the first break only starts a trend and
  is labelled BOS).
- Order block: on a bullish break, the candle with the lowest low between the broken swing
  and the break (the origin of the impulse); mirrored for a bearish break. The zone is that
  candle's full range.
- Order blocks smaller than `ob_min_atr` x ATR are not kept on the higher timeframes
  (measured 2026-10-04: blocks under half an ATR react no better than random ranges).
- Fair value gap: three-bar imbalance - bullish when low[i] > high[i-2], bearish when
  high[i] < low[i-2] - larger than `fvg_min_atr` x ATR.
- Zone status: TESTED once a later bar trades into it. An order block is MITIGATED (invalid)
  once a later bar CLOSES through its far side; a fair value gap once it is completely filled
  (a wick reaches its far side; `fvg_fill: close` keeps the close rule). A zone older than
  the timeframe's lookback expires (the live engine only ever sees that many bars, so a
  full-history backtest must forget it too).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from sp2l.core.types import Candle, Side
from sp2l.smc.model import Analysis, SmcParams, StructureEvent, Swing, Zone, ZoneStatus


def atr_series(bars: Sequence[Candle], n: int) -> list[Decimal | None]:
    out: list[Decimal | None] = [None] * len(bars)
    trs: list[Decimal] = []
    atr: Decimal | None = None
    prev: Decimal | None = None
    for i, b in enumerate(bars):
        tr = (
            b.high - b.low
            if prev is None
            else max(b.high - b.low, abs(b.high - prev), abs(b.low - prev))
        )
        prev = b.close
        if atr is None:
            trs.append(tr)
            if len(trs) == n:
                atr = sum(trs, start=Decimal(0)) / n
                out[i] = atr
        else:
            atr = (atr * (n - 1) + tr) / n
            out[i] = atr
    return out


def _epoch(t: datetime) -> int:
    return int(t.timestamp())


def _pivot_high(bars: Sequence[Candle], p: int, n: int) -> bool:
    h = bars[p].high
    return all(h > bars[p - k].high for k in range(1, n + 1)) and all(
        h >= bars[p + k].high for k in range(1, n + 1)
    )


def _pivot_low(bars: Sequence[Candle], p: int, n: int) -> bool:
    lo = bars[p].low
    return all(lo < bars[p - k].low for k in range(1, n + 1)) and all(
        lo <= bars[p + k].low for k in range(1, n + 1)
    )


def _update_zone(z: Zone, i: int, b: Candle, fvg_wick: bool) -> None:
    if z.status is ZoneStatus.MITIGATED or i <= z.created_idx:
        return
    wick = fvg_wick and z.kind == "FVG"  # a gap traded through end to end is filled
    if z.direction is Side.LONG:
        touched = b.low <= z.top
        broken = b.low <= z.bottom if wick else b.close < z.bottom
    else:
        touched = b.high >= z.bottom
        broken = b.high >= z.top if wick else b.close > z.top
    if (touched or broken) and z.tested_idx is None:
        z.tested_idx = i
        z.status = ZoneStatus.TESTED
    if broken:
        z.mitigated_idx = i
        z.status = ZoneStatus.MITIGATED


def analyze(bars: Sequence[Candle], tf: str, p: SmcParams) -> Analysis:
    n = len(bars)
    a = Analysis(tf=tf, bars=list(bars), atr=atr_series(bars, p.atr_len), trend=[0] * n)
    swing_high: list[Swing | None] = [None]  # last unbroken swing high / low
    swing_low: list[Swing | None] = [None]
    cur = 0
    sl_ = p.swing_len
    live: list[Zone] = []
    seen = 0
    for i, b in enumerate(bars):
        for z in live:
            _update_zone(z, i, b, p.fvg_fill == "wick")
        live = [z for z in live if z.status is not ZoneStatus.MITIGATED and i < z.expires_idx]
        piv = i - sl_
        if piv >= sl_:
            if _pivot_high(bars, piv, sl_):
                s = Swing("HIGH", piv, bars[piv].open_time, bars[piv].high, i)
                a.swings.append(s)
                swing_high[0] = s
            if _pivot_low(bars, piv, sl_):
                s = Swing("LOW", piv, bars[piv].open_time, bars[piv].low, i)
                a.swings.append(s)
                swing_low[0] = s
        if i >= 2:
            _detect_fvg(a, i, p)
        sh, sl = swing_high[0], swing_low[0]
        if sh is not None and b.close > sh.price:
            kind = "CHOCH" if cur == -1 else "BOS"
            _break(a, i, sh, Side.LONG, kind, p)
            swing_high[0] = None
            cur = 1
        elif sl is not None and b.close < sl.price:
            kind = "CHOCH" if cur == 1 else "BOS"
            _break(a, i, sl, Side.SHORT, kind, p)
            swing_low[0] = None
            cur = -1
        a.trend[i] = cur
        for z in a.zones[seen:]:
            z.expires_idx = z.created_idx + p.lookback(tf)
            live.append(z)
        seen = len(a.zones)
    return a


def _detect_fvg(a: Analysis, i: int, p: SmcParams) -> None:
    b0, b1, b2 = a.bars[i - 2], a.bars[i - 1], a.bars[i]
    atr = a.atr[i]
    if b2.low > b0.high:
        side, bottom, top = Side.LONG, b0.high, b2.low
    elif b2.high < b0.low:
        side, bottom, top = Side.SHORT, b2.high, b0.low
    else:
        return
    if atr is not None and top - bottom < p.fvg_min_atr * atr:
        return
    a.zones.append(
        Zone(
            id=f"{a.tf}:FVG:{side.value}:{_epoch(b1.open_time)}",
            kind="FVG",
            direction=side,
            top=top,
            bottom=bottom,
            idx=i - 1,
            time=b1.open_time,
            created_idx=i,
        )
    )


def _imbalance(bars: Sequence[Candle], j: int, i: int, side: Side) -> bool:
    """The move from the block candle j to the break i left a gap (any size): a three-bar
    imbalance whose middle bar lies in (j, i)."""
    for m in range(j + 1, i):
        before, after = bars[m - 1], bars[m + 1]
        if (after.low > before.high) if side is Side.LONG else (after.high < before.low):
            return True
    return False


def _break(a: Analysis, i: int, level: Swing, side: Side, kind: str, p: SmcParams) -> None:
    b = a.bars[i]
    ev = StructureEvent(
        id=f"{a.tf}:{kind}:{side.value}:{_epoch(b.open_time)}",
        kind=kind,
        direction=side,
        level=level.price,
        level_idx=level.idx,
        level_time=level.time,
        break_idx=i,
        break_time=b.open_time,
    )
    a.events.append(ev)
    lo = max(level.idx + 1, i - p.ob_lookback)
    if lo >= i:
        return
    rng = range(lo, i)
    if side is Side.LONG:
        j = min(rng, key=lambda k: (a.bars[k].low, -k))  # lowest low, latest on ties
    else:
        j = max(rng, key=lambda k: (a.bars[k].high, k))  # highest high, latest on ties
    c = a.bars[j]
    atr = a.atr[i]
    if (
        p.ob_min_atr > 0
        and a.tf != p.trigger_tf  # the M1 block is the entry refinement, never filtered
        and atr is not None
        and c.high - c.low < p.ob_min_atr * atr
    ):
        return  # the break stands, the block is too small to matter
    if (
        p.ob_require_fvg
        and a.tf != p.trigger_tf
        and not _imbalance(a.bars, j, i, side)
    ):
        return  # no displacement (imbalance) left the block: not an order block
    z = Zone(
        id=f"{a.tf}:OB:{side.value}:{_epoch(c.open_time)}",
        kind="OB",
        direction=side,
        top=c.high,
        bottom=c.low,
        idx=j,
        time=c.open_time,
        created_idx=i,
        event_id=ev.id,
    )
    a.zones.append(z)
    a.event_ob[ev.id] = z
