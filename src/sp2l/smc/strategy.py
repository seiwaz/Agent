"""Top-down Smart Money entry model, SMC-2.0 (bias -> zone setup -> limit at the order block).

1. BIAS     the bias timeframe's trend (BOS / CHoCH on 4h) must point the trade's way.
2. SETUP    on the zone timeframe (1h), in this order: (with `require_sweep`) a liquidity
            sweep (wick beyond unswept swing lows / equal lows, close back inside), a
            displacement that breaks
            structure by close, the order block = last opposite candle (at or after the sweep)
            with the FVG right after it. One function, `zone_setups`, finds these for
            the engine, the backtest and the chart.
3. ARMING   the first minute after the setup is known that trades into the FVG. The order is
            a limit (maker) at the OB edge touching the FVG (`entry_ref: fvg_mid`: at 50 %
            of the FVG); it rests from the confirmation, so it can fill in the arming
            minute; it is cancelled `pending_expiry_min` after it is placed. Only a fresh
            OB is entered (first touch); one order per setup. `confirm_exec`: first wait
            (`confirm_window_min`) for an execution-TF BOS / CHoCH with the setup, then
            enter at market at its close or (`confirm_entry: limit`) rest the limit from it.
4. STOP     one tick beyond the order block's wick (`sl_ref: ob_height`: beyond the OB by
            its own height).
5. TARGET   one: the edge of the previous HH candle (long: its high) / LL candle (short:
            its low), placed `tp_front_run_atr` x ATR before it; None beyond the entry: no
            trade. `tp_rr` > 0 instead: entry + tp_rr x the stop distance.
6. FILTERS  (reject, never move the stop or the target) entry in the discount (long) /
            premium (short) half of sweep wick -> HH / LL (`require_discount`), net R at the
            TP >= `min_net_rr`, costs <= `max_cost_frac` of the stop (0 = off).
7. SIZE     risk_pct of the wallet over the stop distance + costs, within max_leverage.

Every input is causal: closed bars only, swings once confirmed, zone state as of the order.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal
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
    [sweep ->] displacement (BOS / CHoCH by close) -> order block (last opposite candle, at or
    after the sweep) with the FVG right after it. Anything out of that order is ignored. The
    sweep is required only with `require_sweep`; otherwise it is recorded when there is one."""
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
        if sweep is None and p.require_sweep:
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
    arming minute and within `confirm_window_min` (0: the pending window): (close time, close
    price)."""
    dur = length(ax.tf)
    end = armed_at - MINUTE + timedelta(minutes=p.confirm_window_min or p.pending_expiry_min)
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
    """R after fees if the whole position closed at `px` (a market exit: taker fee and the
    slippage allowance, exactly as the lifecycle books it)."""
    sgn = 1 if side is Side.LONG else -1
    fee_in = costs.taker_fee if market else costs.maker_fee
    fill = px - px * costs.slippage * sgn
    return ((fill - entry) * sgn - entry * fee_in - fill * costs.taker_fee) / ru


def entry_price(s: ZoneSetup, p: SmcParams) -> Decimal:
    """The limit price: the OB edge touching the FVG, or 50 % of the FVG (`entry_ref`),
    rounded to the tick away from the market (long: down, short: up)."""
    if p.entry_ref != "fvg_mid":
        return s.edge
    long = s.direction is Side.LONG
    return _round((s.gap[0] + s.gap[1]) / 2, p.tick, ROUND_FLOOR if long else ROUND_CEILING)


def stop_price(s: ZoneSetup, p: SmcParams) -> Decimal:
    """One tick beyond the OB's wick, or beyond the OB by its own height (`sl_ref`)."""
    long = s.direction is Side.LONG
    if p.sl_ref == "ob_height":
        h = s.ob.top - s.ob.bottom
        return s.ob.bottom - h if long else s.ob.top + h
    return s.ob.bottom - p.tick if long else s.ob.top + p.tick


def _beyond(side: Side, px: Decimal, ref: Decimal) -> bool:
    return px > ref if side is Side.LONG else px < ref


def previous_extreme(
    s: ZoneSetup, ctx: Mapping[str, Analysis], p: SmcParams, t: datetime, entry: Decimal
) -> tuple[Decimal, str] | None:
    """The target: the edge of the previous HH candle (LONG: its high) / LL candle (SHORT: its
    low) known at `t`. `tp_ref: leg`: the extreme price made after the OB candle and before it
    came back to the zone (closed zone bars after the OB + the minutes of the forming one, up
    to `t`); `swing`: the last confirmed zone-TF swing high / low beyond the entry."""
    za, m1 = ctx[p.zone_tf], ctx["1m"]
    long = s.direction is Side.LONG
    k = last_closed(za, t)
    if k < s.event.break_idx:
        return None
    if p.tp_ref == "swing":
        for sw in reversed(za.swings):
            if sw.confirmed_idx > k or (sw.kind == "HIGH") != long:
                continue
            if _beyond(s.direction, sw.price, entry):
                return sw.price, f"{za.tf} {'HH' if long else 'LL'} (swing)"
            return None
        return None
    leg = za.bars[s.ob.idx + 1 : k + 1]
    ext = max(b.high for b in leg) if long else min(b.low for b in leg)
    rng = m1_range(m1, za.bars[k].open_time + length(za.tf), last_closed(m1, t))
    if rng is not None:
        ext = max(ext, rng[0]) if long else min(ext, rng[1])
    if not _beyond(s.direction, ext, entry):
        return None
    return ext, f"{za.tf} {'HH' if long else 'LL'}"


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
    za = ctx[p.zone_tf]
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
    entry = market_price if market_price is not None else entry_price(s, p)
    sl = stop_price(s, p)
    if not _beyond(side, entry, sl):
        return result(["BAD_STOP"], entry=entry, sl=sl)
    reasons: list[str] = []
    ru = risk_unit(side, entry, sl, costs, market=market)
    dist = abs(entry - sl)
    fee_in = costs.taker_fee if market else costs.maker_fee
    cost_frac = (entry * fee_in + sl * costs.taker_fee + sl * costs.slippage) / dist
    if p.max_cost_frac > 0 and cost_frac > p.max_cost_frac:
        reasons.append("COST_HEAVY")

    lv = previous_extreme(s, ctx, p, t, entry)
    tp: Target | None = None
    mid: Decimal | None = None
    if p.tp_rr > 0:  # fixed price R:R from the stop distance
        r = p.tp_rr * dist
        px = (
            _round(entry + r, tick, ROUND_FLOOR) if long else _round(entry - r, tick, ROUND_CEILING)
        )
        level = lv[0] if lv is not None else None
        tp = Target(px, f"{p.tp_rr.normalize()}R", net_r(side, entry, px, ru, costs, market), level)
    if lv is not None:
        level, src = lv
        # the target sits a little before the pool (rounded towards the entry)
        front = p.tp_front_run_atr * (za.atr[k] or Decimal(0))
        px = (
            _round(level - front, tick, ROUND_FLOOR)
            if long
            else _round(level + front, tick, ROUND_CEILING)
        )
        if p.tp_rr <= 0 and _beyond(side, px, entry):
            tp = Target(px, src, net_r(side, entry, px, ru, costs, market), level)
        # dealing range: the sweep wick (else the OB wick) -> the HH / LL
        start = s.sweep.wick if s.sweep is not None else (s.ob.bottom if long else s.ob.top)
        mid = (start + level) / 2
        if p.require_discount and (entry > mid if long else entry < mid):
            reasons.append("NOT_DISCOUNT" if long else "NOT_PREMIUM")
    if tp is None:
        reasons.append("NO_TARGET")  # no previous HH / LL beyond the entry
    elif p.min_net_rr > 0 and tp.net_r < p.min_net_rr:
        reasons.append("LOW_NET_RR")
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
        tp=tp,
        range_mid=mid,
        cost_frac=cost_frac,
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
    if c is None:
        return None
    # limit: rests at the entry price from the confirming close on; market: fills at it
    return (c[0], None) if p.confirm_entry == "limit" else c


def tracked(s: Setup, p: SmcParams, costs: Costs) -> Tracked:
    """The lifecycle object of an accepted setup."""
    assert s.entry is not None and s.sl is not None and s.tp is not None
    return Tracked(
        key=s.key,
        side=s.direction,
        entry=s.entry,
        sl=s.sl,
        tp=s.tp.price,
        created_at=s.created_at,
        risk=risk_unit(s.direction, s.entry, s.sl, costs, market=s.market),
        market=s.market,
        time_stop_min=p.time_stop_min,
    )


def choch_closes(za: Analysis) -> dict[datetime, set[Side]]:
    """Close time of every zone-TF CHoCH bar -> the CHoCH direction(s)."""
    out: dict[datetime, set[Side]] = {}
    dur = length(za.tf)
    for ev in za.events:
        if ev.kind == "CHOCH":
            out.setdefault(ev.break_time + dur, set()).add(ev.direction)
    return out


def invalidates(t: Tracked, bar: Candle, closes: dict[datetime, set[Side]], p: SmcParams) -> bool:
    """`exit_on_choch`: this M1 bar is the last minute of a zone-TF bar that closed a CHoCH
    against the open trade, after the fill."""
    if not p.exit_on_choch or t.state is not State.OPEN or t.filled_at is None:
        return False
    against = Side.SHORT if t.side is Side.LONG else Side.LONG
    return bar.open_time > t.filled_at and against in closes.get(bar.open_time + MINUTE, set())
