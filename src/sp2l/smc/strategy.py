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
4. STOP     one tick beyond the order block's wick (`sl_mode: ob_height`: beyond the OB by
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
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal
from typing import Any

from sp2l.core.types import Candle, Side
from sp2l.smc.lifecycle import State, Tracked, risk_unit
from sp2l.smc.model import (
    Analysis,
    Confirm,
    Costs,
    LtfZone,
    Order,
    Setup,
    SmcParams,
    Target,
    Zone,
    ZoneSetup,
)
from sp2l.smc.timeframes import MINUTE, bucket_start, length

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


def _ltf_zones(
    ax: Analysis, side: Side, origin: int, k: int
) -> tuple[LtfZone | None, LtfZone | None]:
    """The confirming move's newest execution-TF FVG (its third bar between origin and the
    break bar k) and its last opposite candle (the exec-TF order block), both known at k."""
    long = side is Side.LONG
    bars = ax.bars
    fvg = None
    for j in range(k, origin + 1, -1):
        a, c = bars[j - 2], bars[j]
        if long and c.low > a.high:
            fvg = LtfZone("FVG", a.high, c.low, bars[j - 1].open_time)
            break
        if not long and c.high < a.low:
            fvg = LtfZone("FVG", c.high, a.low, bars[j - 1].open_time)
            break
    ob = None
    for j in range(k, origin - 1, -1):
        b = bars[j]
        if (b.close < b.open) if long else (b.close > b.open):
            ob = LtfZone("OB", b.low, b.high, b.open_time)
            break
    return fvg, ob


def confirmation(
    s: ZoneSetup, ax: Analysis, armed_at: datetime, p: SmcParams
) -> Confirm | tuple[str, datetime] | None:
    """`confirm_exec`: the first execution-TF BOS / CHoCH with the setup that closed after the
    arming minute and within `confirm_window_min` (0: the pending window). With
    `confirm_in_zone` the exec-TF swing it reacts from (long: the lowest low between the broken
    swing and the break) must lie inside the zone (OB far edge .. FVG far edge) and be made
    after arming, and an exec-TF close beyond the OB's far edge first invalidates the setup.
    Returns the confirmation, a rejection (NO_CONFIRM / ZONE_INVALID, with the time it was
    decided), or None while the window is still open."""
    dur = length(ax.tf)
    end = armed_at - MINUTE + timedelta(minutes=p.confirm_window_min or p.pending_expiry_min)
    long = s.direction is Side.LONG
    far = s.ob.bottom if long else s.ob.top
    lo, hi = (s.ob.bottom, s.gap[1]) if long else (s.gap[0], s.ob.top)
    first = bucket_start(armed_at - MINUTE, ax.tf)  # the exec bar holding the arming minute
    breaks: dict[int, list[Any]] = {}
    for ev in ax.events:
        if ev.direction is s.direction:
            breaks.setdefault(ev.break_idx, []).append(ev)
    for i in range(_bisect(ax.bars, first), len(ax.bars)):
        b = ax.bars[i]
        t = b.open_time + dur
        if t > end:
            return "NO_CONFIRM", end
        if p.confirm_in_zone and (b.close < far if long else b.close > far):
            return "ZONE_INVALID", t
        for ev in breaks.get(i, []):
            seg = range(ev.level_idx, i + 1)
            o = (
                min(seg, key=lambda j: ax.bars[j].low)
                if long
                else max(seg, key=lambda j: ax.bars[j].high)
            )
            origin = ax.bars[o].low if long else ax.bars[o].high
            if p.confirm_in_zone and (not lo <= origin <= hi or ax.bars[o].open_time < first):
                continue  # the reaction did not start in the zone
            fvg, ob = _ltf_zones(ax, s.direction, o, i)
            return Confirm(t, b.close, ev, origin, ax.bars[o].open_time, fvg, ob)
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


def _ce(z: tuple[Decimal, Decimal], tick: Decimal, long: bool) -> Decimal:
    """50 % of a gap (consequent encroachment), rounded away from the market."""
    return _round((z[0] + z[1]) / 2, tick, ROUND_FLOOR if long else ROUND_CEILING)


def entry_price(s: ZoneSetup, p: SmcParams, confirm: Confirm | None = None) -> Decimal:
    """The limit price (`entry_ref`): the OB edge touching the FVG; 50 % of the zone-TF FVG
    (htf_fvg_ce / fvg_mid); 50 % of the confirming move's exec-TF FVG (ltf_fvg_ce), else the
    proximal edge of its exec-TF order block (ltf_ob_edge), else 50 % of the zone-TF FVG."""
    long = s.direction is Side.LONG
    ref = p.entry_ref
    if ref == "ob_edge":
        return s.edge
    if confirm is not None and ref in ("ltf_fvg_ce", "ltf_ob_edge"):
        if ref == "ltf_fvg_ce" and confirm.fvg is not None:
            return _ce((confirm.fvg.bottom, confirm.fvg.top), p.tick, long)
        if confirm.ob is not None:
            return confirm.ob.top if long else confirm.ob.bottom
    return _ce(s.gap, p.tick, long)


def stop_price(s: ZoneSetup, p: SmcParams, atr: Decimal | None = None) -> Decimal:
    """`sl_mode`: one tick beyond the OB's wick (wick); beyond the OB by its own height
    (ob_height); beyond the farther of the OB wick and the sweep wick plus `sl_buffer_atr` x
    ATR(zone TF), rounded to the tick away from the entry (structure)."""
    long = s.direction is Side.LONG
    if p.sl_mode == "ob_height":
        h = s.ob.top - s.ob.bottom
        return s.ob.bottom - h if long else s.ob.top + h
    if p.sl_mode == "structure":
        wicks = [s.ob.bottom if long else s.ob.top]
        if s.sweep is not None:
            wicks.append(s.sweep.wick)
        buf = p.sl_buffer_atr * (atr or Decimal(0))
        if long:
            return _round(min(wicks) - buf, p.tick, ROUND_FLOOR)
        return _round(max(wicks) + buf, p.tick, ROUND_CEILING)
    return s.ob.bottom - p.tick if long else s.ob.top + p.tick


def tp_mode(p: SmcParams) -> str:
    """The effective target mode (SMC-2.2 configs set only `tp_rr`)."""
    if p.tp_mode == "hh_ll" and p.tp_rr > 0:
        return "fixed"
    return p.tp_mode


def displacement_mid(s: ZoneSetup, za: Analysis) -> Decimal:
    """50 % of the dealing range sweep wick (else the OB's far edge) -> the displacement's
    extreme up to the bar on whose close the setup was known."""
    long = s.direction is Side.LONG
    start_idx = s.sweep.idx if s.sweep is not None else s.ob.idx
    leg = za.bars[start_idx : s.ob.created_idx + 1]
    start = s.sweep.wick if s.sweep is not None else (s.ob.bottom if long else s.ob.top)
    ext = max(b.high for b in leg) if long else min(b.low for b in leg)
    return (start + ext) / 2


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


def _unswept(
    a: Analysis, idx: int, k: int, price: Decimal, high: bool, extra: tuple[Decimal, Decimal] | None
) -> bool:
    """No closed bar after `idx` up to k (nor the forming minutes, `extra` = (high, low))
    traded beyond `price`."""
    if _swept(a, idx, k, price, high):
        return False
    if extra is None:
        return True
    return extra[0] <= price if high else extra[1] >= price


def liquidity_target(
    s: ZoneSetup, ctx: Mapping[str, Analysis], p: SmcParams, t: datetime, entry: Decimal
) -> tuple[Decimal, str, Decimal] | None:
    """`tp_mode: liquidity`: the nearest unswept external liquidity beyond the entry, known at
    t: the displacement-leg extreme (HH / LL), unswept zone-TF and 1h swing highs (long) /
    lows (short; swings within `eq_tol_atr` x ATR are reported as equal highs / lows), the
    previous UTC day's high / low. Returns (level, source, TP price): the TP sits
    `tp_front_run_atr` x ATR(zone TF) before the level, rounded towards the entry; levels
    whose TP would not lie beyond the entry are skipped. Never a farther level."""
    long = s.direction is Side.LONG
    za, m1 = ctx[p.zone_tf], ctx["1m"]
    k = last_closed(za, t)
    if k < s.event.break_idx:
        return None
    im = last_closed(m1, t)
    atr = za.atr[k] or Decimal(0)
    front = p.tp_front_run_atr * atr
    cands: list[tuple[Decimal, str]] = []
    lv = previous_extreme(s, ctx, replace(p, tp_ref="leg"), t, entry)
    if lv is not None:
        cands.append((lv[0], f"{za.tf} {'HH' if long else 'LL'}"))
    swings: list[tuple[Decimal, str]] = []
    for tf in dict.fromkeys((p.zone_tf, "1h")):
        a = ctx.get(tf)
        if a is None:
            continue
        ka = last_closed(a, t)
        extra = m1_range(m1, a.bars[ka].open_time + length(tf), im) if ka >= 0 else None
        for sw in a.swings:
            if sw.confirmed_idx > ka or (sw.kind == "HIGH") != long:
                continue
            if _unswept(a, sw.idx, ka, sw.price, long, extra):
                swings.append((sw.price, tf))
    cands += [(px, f"{tf} swing {'high' if long else 'low'}") for px, tf in swings]
    d = ctx.get("1d")
    if d is not None:
        kd = last_closed(d, t)
        if kd >= 0:
            day = d.bars[kd]
            today = m1_range(m1, day.open_time + length("1d"), im)
            lvl = day.high if long else day.low
            if today is None or (today[0] <= lvl if long else today[1] >= lvl):
                cands.append((lvl, "PDH" if long else "PDL"))
    best: tuple[Decimal, str, Decimal] | None = None
    for lvl, src in cands:
        px = (
            _round(lvl - front, p.tick, ROUND_FLOOR)
            if long
            else _round(lvl + front, p.tick, ROUND_CEILING)
        )
        if not _beyond(s.direction, px, entry):
            continue
        if best is None or (lvl < best[0] if long else lvl > best[0]):
            best = (lvl, src, px)
    if best is None:
        return None
    tol = p.eq_tol_atr * atr
    if sum(1 for px, _ in swings if abs(px - best[0]) <= tol) >= 2:
        best = (best[0], f"{best[1].split()[0]} equal {'highs' if long else 'lows'}", best[2])
    return best


def evaluate(
    s: ZoneSetup,
    ctx: Mapping[str, Analysis],
    p: SmcParams,
    costs: Costs,
    t: datetime,
    *,
    market_price: Decimal | None = None,
    equity: Decimal | None = None,
    confirm: Confirm | None = None,
) -> Setup:
    """The setup as an order created at `t` (limit: the open of the arming minute; with
    `confirm_exec` a market order at the close of the confirming bar, `market_price`, or a
    limit from that close, `confirm`)."""
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
            confirm=confirm,
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
    entry = market_price if market_price is not None else entry_price(s, p, confirm)
    sl = stop_price(s, p, za.atr[k])
    if not _beyond(side, entry, sl):
        return result(["BAD_STOP"], entry=entry, sl=sl)
    reasons: list[str] = []
    ru = risk_unit(side, entry, sl, costs, market=market)
    dist = abs(entry - sl)
    fee_in = costs.taker_fee if market else costs.maker_fee
    cost_frac = (entry * fee_in + sl * costs.taker_fee + sl * costs.slippage) / dist
    if p.max_cost_frac > 0 and cost_frac > p.max_cost_frac:
        reasons.append("COST_HEAVY")

    mode = tp_mode(p)
    lv = previous_extreme(s, ctx, p, t, entry)
    tp: Target | None = None
    mid: Decimal | None = None
    if mode == "fixed":  # a fixed price R:R from the stop distance
        r = p.tp_rr * dist
        px = (
            _round(entry + r, tick, ROUND_FLOOR) if long else _round(entry - r, tick, ROUND_CEILING)
        )
        level = lv[0] if lv is not None else None
        tp = Target(px, f"{p.tp_rr.normalize()}R", net_r(side, entry, px, ru, costs, market), level)
    elif mode == "liquidity":
        liq = liquidity_target(s, ctx, p, t, entry)
        if liq is not None:
            tp = Target(liq[2], liq[1], net_r(side, entry, liq[2], ru, costs, market), liq[0])
    if p.discount_ref == "displacement":
        # dealing range: sweep wick -> displacement extreme; the OB's proximal edge in the
        # discount (long) / premium (short) half
        mid = displacement_mid(s, za)
        if p.require_discount and (s.edge > mid if long else s.edge < mid):
            reasons.append("NOT_DISCOUNT" if long else "NOT_PREMIUM")
    if lv is not None:
        level, src = lv
        # the target sits a little before the pool (rounded towards the entry)
        front = p.tp_front_run_atr * (za.atr[k] or Decimal(0))
        px = (
            _round(level - front, tick, ROUND_FLOOR)
            if long
            else _round(level + front, tick, ROUND_CEILING)
        )
        if mode == "hh_ll" and _beyond(side, px, entry):
            tp = Target(px, src, net_r(side, entry, px, ru, costs, market), level)
        if p.discount_ref != "displacement":
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
) -> Order | None:
    """When the order of an armed setup exists: the arming minute's open (a resting limit);
    with `confirm_exec` the close of the confirming execution-TF bar (market at its close, or a
    limit from it, `confirm_entry`), or the rejection when no confirmation came (NO_CONFIRM) /
    the zone broke first (ZONE_INVALID). None while the confirmation window is still open."""
    t = m1[armed].open_time
    if not p.confirm_exec:
        return Order(t)
    c = confirmation(s, ctx[p.exec_tf], t + MINUTE, p)
    if c is None:
        return None
    if isinstance(c, tuple):
        return Order(c[1], reject=c[0])
    if p.confirm_entry == "limit":
        return Order(c.t, None, c)
    return Order(c.t, c.price, c)


def evaluate_order(
    s: ZoneSetup,
    ctx: Mapping[str, Analysis],
    p: SmcParams,
    costs: Costs,
    order: Order,
    *,
    equity: Decimal | None = None,
) -> Setup:
    """`evaluate` at the order's moment, or the rejection the order carries."""
    if order.reject is not None:
        return Setup(
            key=s.key,
            direction=s.direction,
            accepted=False,
            reasons=(order.reject,),
            created_at=order.t,
            zone=s,
            bias=trend_at(ctx.get(p.bias_tf), order.t),
        )
    return evaluate(
        s,
        ctx,
        p,
        costs,
        order.t,
        market_price=order.market_price,
        equity=equity,
        confirm=order.confirm,
    )


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
