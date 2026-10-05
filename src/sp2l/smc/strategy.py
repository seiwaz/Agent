"""Top-down Smart Money entry model, SMC-2.0 (bias -> zone setup -> limit at the order block).

1. BIAS     the bias timeframe's trend (BOS / CHoCH on 4h) must point the trade's way.
2. SETUP    on the zone timeframe (1h), in this order: a liquidity sweep (wick beyond
            unswept swing lows / equal lows, close back inside), a displacement that breaks
            structure by close, the order block = last opposite candle (at or after the sweep)
            with the FVG right after it. One function, `zone_setups`, finds these for
            the engine, the backtest and the chart.
3. ARMING   the first minute after the setup is known that trades into the FVG. The order is
            a limit (maker) at the OB edge touching the FVG; it rests from the confirmation,
            so it can fill in the arming minute; it is cancelled `pending_expiry_min`
            after arming. Only a fresh OB is entered (first touch); one order per setup.
            `confirm_exec`: instead wait for an execution-TF BOS / CHoCH, enter at market.
4. STOP     one tick beyond the order block's wick.
5. TARGETS  from the market alone, never from the stop distance: TP1 = nearest internal
            liquidity / previous swing (execution or zone TF), TP2 = next unfilled zone-TF FVG
            beyond TP1 (none: its share stays in the runner), TP3 = external liquidity
            (previous day / week, equal highs / lows) for the runner with a trailing stop.
            No TP1 level: no trade.
6. SIZE     risk_pct of the wallet over the stop distance + costs, within max_leverage.

Every input is causal: closed bars only, swings once confirmed, zone state as of the order.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

from sp2l.core.types import Candle, Side
from sp2l.smc.lifecycle import State, Tracked, risk_unit
from sp2l.smc.model import Analysis, Costs, Setup, SmcParams, Target, Zone, ZoneSetup
from sp2l.smc.timeframes import MINUTE, length

TREND = {Side.LONG: 1, Side.SHORT: -1}


def last_closed(a: Analysis, t: datetime) -> int:
    """Index of the last bar of `a` whose close time is <= t (-1 if none)."""
    dur = length(a.tf)
    lo, hi = 0, len(a.bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if a.bars[mid].open_time + dur <= t:
            lo = mid + 1
        else:
            hi = mid
    return lo - 1


def trend_at(a: Analysis | None, t: datetime) -> int:
    if a is None:
        return 0
    k = last_closed(a, t)
    return a.trend[k] if k >= 0 else 0


def zones_at(a: Analysis, k: int, p: SmcParams) -> list[Zone]:
    """Zones of `a` valid as of the close of bar k (newest first)."""
    out = []
    floor = k - p.lookback(a.tf)
    for z in reversed(a.zones):  # appended in creation order
        if z.created_idx < floor:
            break
        if z.valid_at(k):
            out.append(z)
    return out


def _round(v: Decimal, tick: Decimal, mode: str) -> Decimal:
    return (v / tick).to_integral_value(rounding=mode) * tick


def m1_range(m1: Analysis, start: datetime, i: int) -> tuple[Decimal, Decimal] | None:
    """(highest high, lowest low) of the M1 bars that opened at/after `start`, up to bar i:
    what happened inside a higher-timeframe bar that has not closed yet."""
    lo, hi = 0, i + 1
    while lo < hi:
        mid = (lo + hi) // 2
        if m1.bars[mid].open_time < start:
            lo = mid + 1
        else:
            hi = mid
    seg = m1.bars[lo : i + 1]
    if not seg:
        return None
    return max(b.high for b in seg), min(b.low for b in seg)


def filled_live(z: Zone, rng: tuple[Decimal, Decimal] | None, p: SmcParams) -> bool:
    """A fair value gap completely filled by a wick inside the still-forming bar (irreversible,
    so it is invalid before that bar closes). Order blocks need a close: never here."""
    if rng is None or z.kind != "FVG" or p.fvg_fill != "wick":
        return False
    return rng[1] <= z.bottom if z.direction is Side.LONG else rng[0] >= z.top


def _swept(a: Analysis, idx: int, k: int, price: Decimal, high: bool) -> bool:
    seg = a.bars[idx + 1 : k + 1]
    return any(b.high > price for b in seg) if high else any(b.low < price for b in seg)


# ---- zone setups ------------------------------------------------------------------------
def zone_setups(a: Analysis, p: SmcParams) -> list[ZoneSetup]:
    """Every complete sequence on the zone timeframe, in the order it became known:
    sweep -> displacement (BOS / CHoCH by close) -> order block (last opposite candle, at or
    after the sweep) with the FVG right after it. Anything out of that order is ignored."""
    out: list[ZoneSetup] = []
    dur = length(a.tf)
    for ev in a.events:
        z = a.event_ob.get(ev.id)
        if z is None or z.gap is None:
            continue
        side = ev.direction
        sweep = None
        for sw in reversed(a.sweeps):  # the latest sweep of that side at or before the OB
            if sw.idx > z.idx or sw.direction is not side:
                continue
            if sw.idx >= ev.break_idx - p.sweep_max_bars:
                sweep = sw
            break
        if sweep is None:
            continue
        out.append(
            ZoneSetup(
                key=z.id,
                tf=a.tf,
                direction=side,
                ob=z,
                gap=z.gap,
                sweep=sweep,
                event=ev,
                confirmed_at=a.bars[z.created_idx].open_time + dur,
            )
        )
    out.sort(key=lambda s: s.confirmed_at)
    return out


def touches(s: ZoneSetup, b: Candle) -> bool:
    """The bar traded into the setup's FVG (arming)."""
    return b.low <= s.gap[1] if s.direction is Side.LONG else b.high >= s.gap[0]


ARMED_BEFORE = -1  # armed inside minutes that are no longer available (never fresh)


def find_arming(s: ZoneSetup, za: Analysis, k: int, m1: Sequence[Candle]) -> int | None:
    """Index in `m1` of the first minute after the setup was confirmed that traded into its
    FVG, looking at zone bars closed up to k and at the minutes after them; None if price has
    not come back yet, ARMED_BEFORE if it did in minutes `m1` no longer holds."""
    dur = length(za.tf)
    last = min(k, s.ob.expires_idx)
    hit = next((j for j in range(s.ob.created_idx + 1, last + 1) if touches(s, za.bars[j])), None)
    if hit is not None:
        lo_t, hi_t = za.bars[hit].open_time, za.bars[hit].open_time + dur
    elif k > s.ob.expires_idx:
        return None  # expired untouched
    else:
        lo_t, hi_t = (za.bars[k].open_time + dur if k >= 0 else s.confirmed_at), None
    if not m1 or m1[0].open_time > lo_t:
        return ARMED_BEFORE if hit is not None else None
    i = _bisect(m1, lo_t)
    while i < len(m1) and (hi_t is None or m1[i].open_time < hi_t):
        if touches(s, m1[i]):
            return i
        i += 1
    return None


def _bisect(m1: Sequence[Candle], t: datetime) -> int:
    lo, hi = 0, len(m1)
    while lo < hi:
        mid = (lo + hi) // 2
        if m1[mid].open_time < t:
            lo = mid + 1
        else:
            hi = mid
    return lo


def confirmation(
    s: ZoneSetup, ax: Analysis, armed_at: datetime, p: SmcParams
) -> tuple[datetime, Decimal] | None:
    """`confirm_exec`: the first execution-TF BOS / CHoCH with the setup that closed after the
    arming minute and within the pending window: (close time, close price)."""
    dur = length(ax.tf)
    end = armed_at - MINUTE + timedelta(minutes=p.pending_expiry_min)
    for ev in ax.events:
        t = ev.break_time + dur
        if ev.direction is not s.direction or t < armed_at:
            continue
        if t > end:
            return None
        return t, ax.bars[ev.break_idx].close
    return None


# ---- evaluation at the moment the order exists --------------------------------------------
def net_r(
    side: Side, entry: Decimal, px: Decimal, ru: Decimal, costs: Costs, market: bool
) -> Decimal:
    """R after fees if the whole position closed at `px` (taker exit)."""
    sgn = 1 if side is Side.LONG else -1
    fee_in = costs.taker_fee if market else costs.maker_fee
    return ((px - entry) * sgn - entry * fee_in - px * costs.taker_fee) / ru


def _beyond(side: Side, px: Decimal, ref: Decimal) -> bool:
    return px > ref if side is Side.LONG else px < ref


def internal_liquidity(
    ctx: Mapping[str, Analysis], p: SmcParams, t: datetime, side: Side, entry: Decimal
) -> tuple[Decimal, str] | None:
    """TP1: the nearest internal liquidity / previous swing beyond the entry: an unswept
    confirmed swing high (LONG) / low of the execution or the zone timeframe."""
    m1 = ctx["1m"]
    long = side is Side.LONG
    best: tuple[Decimal, str] | None = None
    for tf in dict.fromkeys((p.exec_tf, p.zone_tf)):
        a = ctx[tf]
        k = last_closed(a, t)
        if k < 0:
            continue
        rng = m1_range(m1, a.bars[k].open_time + length(tf), last_closed(m1, t))
        floor = k - p.lookback(tf)
        for sw in reversed(a.swings):
            if sw.idx < floor:
                break
            if sw.confirmed_idx > k or (sw.kind == "HIGH") != long:
                continue
            if not _beyond(side, sw.price, entry) or _swept(a, sw.idx, k, sw.price, long):
                continue
            if rng is not None and (rng[0] > sw.price if long else rng[1] < sw.price):
                continue
            if best is None or abs(sw.price - entry) < abs(best[0] - entry):
                best = (sw.price, f"{tf} swing {'high' if long else 'low'}")
    return best


def next_fvg(
    za: Analysis, m1: Analysis, p: SmcParams, t: datetime, side: Side, beyond: Decimal
) -> tuple[Decimal, str] | None:
    """TP2: the near edge of the nearest unfilled zone-TF fair value gap beyond `beyond`."""
    k = last_closed(za, t)
    if k < 0:
        return None
    long = side is Side.LONG
    rng = m1_range(m1, za.bars[k].open_time + length(za.tf), last_closed(m1, t))
    best: tuple[Decimal, str] | None = None
    for z in zones_at(za, k, p):
        if z.kind != "FVG" or filled_live(z, rng, p):
            continue
        edge = z.bottom if long else z.top
        if not _beyond(side, edge, beyond):
            continue
        if best is None or abs(edge - beyond) < abs(best[0] - beyond):
            best = (edge, f"{za.tf} FVG")
    return best


def external_liquidity(
    ctx: Mapping[str, Analysis], p: SmcParams, t: datetime, side: Side
) -> list[tuple[Decimal, str]]:
    """TP3 candidates still untaken: previous UTC day / ISO week high (LONG) or low, and
    zone-TF equal highs / lows (two or more swings within eq_tol_atr x ATR)."""
    ax, za, m1 = ctx[p.exec_tf], ctx[p.zone_tf], ctx["1m"]
    long = side is Side.LONG
    out: list[tuple[Decimal, str]] = []
    k = last_closed(ax, t)
    if k >= 0:
        dur = length(ax.tf)
        rng = m1_range(m1, ax.bars[k].open_time + dur, last_closed(m1, t))
        day0 = t.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        week0 = day0 - timedelta(days=day0.weekday())
        periods = (
            (day0 - timedelta(days=1), day0, "PDH" if long else "PDL"),
            (week0 - timedelta(days=7), week0, "PWH" if long else "PWL"),
        )
        bars = ax.bars[max(0, k + 1 - p.lookback(ax.tf)) : k + 1]
        for start, end, name in periods:
            seg = [b for b in bars if start <= b.open_time < end]
            if not seg or seg[0].open_time != start or seg[-1].open_time + dur != end:
                continue  # the period is not complete in the analysed bars
            level = max(b.high for b in seg) if long else min(b.low for b in seg)
            after = [b for b in bars if b.open_time >= end]
            taken = any((b.high > level) if long else (b.low < level) for b in after) or (
                rng is not None and (rng[0] > level if long else rng[1] < level)
            )
            if not taken:
                out.append((level, name))
    kz = last_closed(za, t)
    atr = za.atr[kz] if kz >= 0 else None
    if kz >= 0 and atr is not None:
        tol = p.eq_tol_atr * atr
        rng = m1_range(m1, za.bars[kz].open_time + length(za.tf), last_closed(m1, t))
        floor = kz - p.lookback(za.tf)
        pts = sorted(
            sw.price
            for sw in za.swings
            if sw.idx >= floor
            and sw.confirmed_idx <= kz
            and (sw.kind == "HIGH") == long
            and not _swept(za, sw.idx, kz, sw.price, long)
            and not (rng is not None and (rng[0] > sw.price if long else rng[1] < sw.price))
        )
        cluster: list[Decimal] = []
        for px in [*pts, None]:
            if px is not None and cluster and px - cluster[-1] <= tol:
                cluster.append(px)
                continue
            if len(cluster) >= 2:
                out.append(
                    (
                        max(cluster) if long else min(cluster),
                        f"{za.tf} equal {'highs' if long else 'lows'}",
                    )
                )
            cluster = [] if px is None else [px]
    return out


def evaluate(
    s: ZoneSetup,
    ctx: Mapping[str, Analysis],
    p: SmcParams,
    costs: Costs,
    t: datetime,
    *,
    market_price: Decimal | None = None,
    equity: Decimal | None = None,
) -> Setup:
    """The setup as an order created at `t` (limit: the open of the arming minute; with
    `confirm_exec` a market order at the close of the confirming bar, `market_price`)."""
    side = s.direction
    long = side is Side.LONG
    za, m1 = ctx[p.zone_tf], ctx["1m"]
    bias = trend_at(ctx.get(p.bias_tf), t)
    market = market_price is not None

    def result(reasons: list[str], **kw: Any) -> Setup:
        return Setup(
            key=s.key,
            direction=side,
            accepted=not reasons,
            reasons=tuple(reasons),
            created_at=t,
            zone=s,
            bias=bias,
            market=market,
            **kw,
        )

    if bias == 0:
        return result(["NO_BIAS"])
    if bias != TREND[side]:
        return result(["BIAS_MISMATCH"])
    k = last_closed(za, t)
    if k < 0 or not s.ob.valid_at(k):
        return result(["ZONE_INVALID"])
    if s.ob.tested_idx is not None and s.ob.tested_idx <= k and not market:
        return result(["NOT_FRESH"])
    tick = p.tick
    entry = market_price if market_price is not None else s.edge
    # the stop sits one tick beyond the order block's wick
    sl = s.ob.bottom - tick if long else s.ob.top + tick
    if not _beyond(side, entry, sl):
        return result(["BAD_STOP"], entry=entry, sl=sl)
    reasons: list[str] = []
    ru = risk_unit(side, entry, sl, costs, market=market)

    def target(lv: tuple[Decimal, str] | None) -> Target | None:
        if lv is None:
            return None
        return Target(lv[0], lv[1], net_r(side, entry, lv[0], ru, costs, market))

    tp1 = target(internal_liquidity(ctx, p, t, side, entry))
    tp2 = tp3 = None
    if tp1 is None:
        reasons.append("NO_TARGET")  # no internal liquidity / previous swing beyond the entry
    else:
        tp2 = target(next_fvg(za, m1, p, t, side, tp1.price))
        last = (tp2 or tp1).price
        ext = [x for x in external_liquidity(ctx, p, t, side) if _beyond(side, x[0], last)]
        if ext:
            tp3 = target(min(ext, key=lambda x: abs(x[0] - last)))
    bal = equity if equity is not None and equity > 0 else p.account_usdt
    qty = (bal * p.risk_pct / ru).quantize(Decimal("0.00001"), rounding=ROUND_DOWN)
    notional = qty * entry
    leverage = notional / bal
    if leverage > p.max_leverage:
        reasons.append("LEVERAGE")
    return result(
        reasons,
        entry=entry,
        sl=sl,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        qty=qty,
        notional=notional,
        leverage=leverage,
    )


def order_for(
    s: ZoneSetup,
    ctx: Mapping[str, Analysis],
    p: SmcParams,
    armed: int,
    m1: Sequence[Candle],
) -> tuple[datetime, Decimal | None] | None:
    """When (and at which market price, None = resting limit) the order of an armed setup
    exists: the arming minute's open, or the close of the confirming execution-TF bar."""
    t = m1[armed].open_time
    if not p.confirm_exec:
        return t, None
    c = confirmation(s, ctx[p.exec_tf], t + MINUTE, p)
    return c


def trail_stop(ax: Analysis, side: Side, t: datetime, p: SmcParams) -> Decimal | None:
    """The trailing stop known at `t`: one tick beyond the last confirmed execution-TF swing
    low (LONG) / high."""
    k = last_closed(ax, t)
    if k < 0:
        return None
    long = side is Side.LONG
    for sw in reversed(ax.swings):
        if sw.confirmed_idx > k or (sw.kind == "LOW") != long:
            continue
        return sw.price - p.tick if long else sw.price + p.tick
    return None


def tracked(s: Setup, p: SmcParams, costs: Costs) -> Tracked:
    """The lifecycle object of an accepted setup (ladder shares as configured; the runner
    replaces them with the shares its quantity steps allow)."""
    assert s.entry is not None and s.sl is not None and s.tp1 is not None
    return Tracked(
        key=s.key,
        side=s.direction,
        entry=s.entry,
        sl=s.sl,
        tp=None if s.tp3 is None else s.tp3.price,
        created_at=s.created_at,
        risk=risk_unit(s.direction, s.entry, s.sl, costs, market=s.market),
        market=s.market,
        tp1=s.tp1.price,
        tp2=None if s.tp2 is None else s.tp2.price,
        frac1=p.tp1_frac,
        frac2=p.tp2_frac if s.tp2 is not None else Decimal(0),
        time_stop_min=p.time_stop_min,
    )


def bar_trail(t: Tracked, ax: Analysis, bar: Candle, p: SmcParams) -> Decimal | None:
    """The trailing stop for one M1 bar of a signal: once the runner is all that is left
    (after TP2, or after TP1 when there is no TP2)."""
    runner = t.state is State.TP2 or (t.state is State.TP1 and t.tp2 is None)
    return trail_stop(ax, t.side, bar.open_time, p) if runner else None
