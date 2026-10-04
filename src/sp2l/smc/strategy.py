"""Top-down Smart Money entry model (bias -> point of interest -> M1 trigger).

For every structure event on the trigger timeframe (M1) the model asks, as of the close of the
bar that confirmed it:

1. BIAS     the bias timeframe's trend (BOS/CHoCH on 4h by default) must point the same way.
2. POI      an unmitigated order block / fair value gap of that direction on a POI timeframe
            (1h, then 15m by default) must overlap the M1 order block that the trigger
            created: price reacted inside a higher-timeframe zone.
3. ENTRY    on M1: at the trigger close (`entry_mode: market`) or a limit on the M1 order
            block / the POI; stop beyond the POI and the M1 block plus an ATR buffer
            (`sl_mode: poi`) or beyond the M1 block only; target = the nearest liquidity
            (unswept swing high/low on any analysed timeframe or the M1 leg extreme) that
            pays at least `min_net_rr` AFTER fees and slippage (`tp_mode: liquidity`).
4. QUALITY  confluence score (fresh POI, CHoCH reversal, liquidity sweep, M1 displacement
            FVG, POI confluence, confirmation-timeframe agreement) >= `min_score`, `require`d
            factors present; stop <= `max_risk_pct`; advisory size within `max_leverage`.

Every input is causal (higher-timeframe bars only once closed, zone status as of the trigger).
Rejected triggers are returned with reason codes so the dashboard can say why nothing fired.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from sp2l.core.types import Side
from sp2l.smc.lifecycle import risk_unit
from sp2l.smc.model import Analysis, Costs, Setup, SmcParams, StructureEvent, Zone
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


def _swept(a: Analysis, idx: int, k: int, price: Decimal, high: bool) -> bool:
    seg = a.bars[idx + 1 : k + 1]
    return any(b.high > price for b in seg) if high else any(b.low < price for b in seg)


def targets(
    ctx: Mapping[str, Analysis],
    p: SmcParams,
    m1: Analysis,
    ob: Zone,
    i: int,
    t: datetime,
    side: Side,
    entry: Decimal,
) -> list[tuple[Decimal, str]]:
    """Liquidity above (LONG) / below (SHORT) the entry, nearest first."""
    out: list[tuple[Decimal, str]] = []
    long = side is Side.LONG
    for tf, a in ctx.items():
        k = last_closed(a, t)
        if k < 0:
            continue
        floor = k - p.lookback(tf)
        for s in reversed(a.swings):
            if s.idx < floor:
                break
            if s.confirmed_idx > k or (s.kind == "HIGH") != long:
                continue
            if ((s.price > entry) if long else (s.price < entry)) and not _swept(
                a, s.idx, k, s.price, long
            ):
                out.append((s.price, f"{tf} swing {'high' if long else 'low'}"))
    leg = m1.bars[ob.idx : i + 1]
    if long:
        ext = max(b.high for b in leg)
        if ext > entry:
            out.append((ext, "1m leg high"))
    else:
        ext = min(b.low for b in leg)
        if ext < entry:
            out.append((ext, "1m leg low"))
    seen: set[Decimal] = set()
    uniq = []
    for pr, src in sorted(out, key=lambda x: abs(x[0] - entry)):
        if pr not in seen:
            seen.add(pr)
            uniq.append((pr, src))
    return uniq


def find_poi(
    ctx: Mapping[str, Analysis], p: SmcParams, ob: Zone, t: datetime, side: Side
) -> tuple[Zone, str] | None:
    for tf in p.poi_tfs:
        a = ctx.get(tf)
        if a is None:
            continue
        k = last_closed(a, t)
        found = [z for z in zones_at(a, k, p) if z.direction is side and z.overlaps(ob)]
        if found:
            found.sort(key=lambda z: (z.kind != "OB", -z.idx))
            return found[0], tf
    return None


def evaluate(
    ev: StructureEvent,
    ctx: Mapping[str, Analysis],
    p: SmcParams,
    costs: Costs,
) -> Setup:
    m1 = ctx[p.trigger_tf]
    i = ev.break_idx
    side = ev.direction
    t = m1.bars[i].open_time + MINUTE
    bias = trend_at(ctx.get(p.bias_tf), t)

    def reject(*reasons: str, **kw: object) -> Setup:
        return Setup(
            key=f"{kw.pop('key', ev.id)}",
            direction=side,
            accepted=False,
            reasons=reasons,
            created_at=t,
            trigger_kind=ev.kind,
            trigger_event_id=ev.id,
            bias=bias,
            **kw,  # type: ignore[arg-type]
        )

    if bias == 0:
        return reject("NO_BIAS")
    if bias != TREND[side]:
        return reject("BIAS_MISMATCH")
    ob = m1.event_ob.get(ev.id)
    atr1 = m1.atr[i]
    if ob is None:
        return reject("NO_ORDER_BLOCK")
    if atr1 is None:
        return reject("WARMUP")
    poi = find_poi(ctx, p, ob, t, side)
    if poi is None:
        return reject("NO_POI", entry_zone=ob)
    zone, poi_tf = poi
    key = f"{zone.id}|{ev.id}"
    long = side is Side.LONG
    edges = {
        "proximal": ob.top if long else ob.bottom,
        "mid": ob.mid,
        "distal": ob.bottom if long else ob.top,
    }
    tick = p.tick
    ez = zone if p.entry_on == "poi" else ob
    edges = {
        "proximal": ez.top if long else ez.bottom,
        "mid": ez.mid,
        "distal": ez.bottom if long else ez.top,
    }
    market = p.entry_mode == "market"
    entry = (
        m1.bars[i].close if market else _round(edges.get(p.entry_mode, ez.mid), tick, ROUND_HALF_UP)
    )
    if not market and (
        (long and entry >= m1.bars[i].close) or (not long and entry <= m1.bars[i].close)
    ):
        entry = _round(ob.mid, tick, ROUND_HALF_UP)  # zone already left: refine on M1
    poi_a = ctx[poi_tf]
    atr_poi = poi_a.atr[last_closed(poi_a, t)] or atr1
    if p.sl_mode == "poi":
        # the idea is wrong only once price leaves the higher-timeframe zone
        buf = max(p.sl_buffer_atr * atr_poi, 2 * tick)
        far = min(ob.bottom, zone.bottom) - buf if long else max(ob.top, zone.top) + buf
    else:
        buf = max(p.sl_buffer_atr * atr1, 2 * tick)
        far = ob.bottom - buf if long else ob.top + buf
    min_d = p.min_sl_atr * atr_poi
    if min_d > 0:
        far = min(far, entry - min_d) if long else max(far, entry + min_d)
    sl = _round(far, tick, ROUND_FLOOR if long else ROUND_CEILING)
    reasons: list[str] = []
    common = {"poi": zone, "poi_tf": poi_tf, "entry_zone": ob, "key": key}
    if (long and not sl < entry) or (not long and not sl > entry):
        return reject("BAD_STOP", entry=entry, sl=sl, **common)
    if abs(entry - sl) / entry > p.max_risk_pct:
        reasons.append("SL_TOO_WIDE")

    tp: Decimal | None = None
    tp_src: str | None = None
    best_net: Decimal | None = None
    gross: Decimal | None = None
    ru = risk_unit(side, entry, sl, costs, market=market)
    cands = targets(ctx, p, m1, ob, i, t, side, entry)
    if p.tp_mode == "rr" and cands:
        # exact net target, provided some liquidity lies at or beyond it
        fee_in = entry * (costs.taker_fee if market else costs.maker_fee)
        need = p.min_net_rr * ru + fee_in
        # net = (|px - entry| - entry*maker - px*taker) / ru  solved for px
        px = (
            (entry + need) / (1 - costs.taker_fee)
            if long
            else (entry - need) / (1 + costs.taker_fee)
        )
        far_liq = max(c[0] for c in cands) if long else min(c[0] for c in cands)
        px = _round(px, tick, ROUND_CEILING if long else ROUND_FLOOR)  # never below the R
        if (far_liq >= px) if long else (far_liq <= px):
            cands = [(px, f"{p.min_net_rr}R (liquidity beyond)")]
        else:
            cands = []
    for price, src in cands:
        px = (
            price
            if p.tp_mode == "rr"
            else _round(price, tick, ROUND_FLOOR if long else ROUND_CEILING)
        )
        # keep the target on the entry side of the liquidity it sits at
        reward = (px - entry) if long else (entry - px)
        if reward <= 0:
            continue
        net = (
            reward - entry * (costs.taker_fee if market else costs.maker_fee) - px * costs.taker_fee
        ) / ru
        best_net, gross = net, reward / abs(entry - sl)
        if net >= p.min_net_rr:
            tp, tp_src = px, src
            break
    if tp is None:
        reasons.append("NO_TARGET")

    sw = _sweep(m1, ob, long)
    m1_fvg = any(
        z.kind == "FVG" and z.direction is side and ob.idx <= z.idx for z in zones_at(m1, i, p)
    )
    factors = {
        "fresh": _fresh(poi_a, zone, ob.time),
        "choch": ev.kind == "CHOCH",
        "sweep": sw,
        "m1_fvg": m1_fvg,
        "poi_confluence": _poi_confluence(ctx, p, zone, t, side),
        "bias_confirm": trend_at(ctx.get(p.confirm_bias_tf), t) == TREND[side],
    }
    score = sum(factors.values())
    if score < p.min_score:
        reasons.append("LOW_SCORE")
    missing = [f for f in p.require if not factors.get(f, False)]
    if missing:
        reasons.append("MISSING_" + "_".join(m.upper() for m in missing))

    risk_cash = p.account_usdt * p.risk_pct
    qty = (risk_cash / ru).quantize(Decimal("0.001"), rounding=ROUND_DOWN)
    notional = qty * entry
    leverage = notional / p.account_usdt
    if leverage > p.max_leverage:
        reasons.append("LEVERAGE")
    return Setup(
        key=key,
        direction=side,
        accepted=not reasons,
        reasons=tuple(reasons),
        created_at=t,
        trigger_kind=ev.kind,
        trigger_event_id=ev.id,
        bias=bias,
        poi=zone,
        poi_tf=poi_tf,
        entry_zone=ob,
        entry=entry,
        sl=sl,
        tp=tp,
        tp_source=tp_src,
        rr=gross,
        net_rr=best_net,
        score=score,
        factors=factors,
        qty=qty,
        notional=notional,
        leverage=leverage,
    )


def _fresh(a: Analysis, zone: Zone, tap: datetime) -> bool:
    """First touch: the zone had not been traded into before the bar of the M1 reaction."""
    if zone.tested_idx is None:
        return True
    return a.bars[zone.tested_idx].open_time + length(a.tf) > tap


def _sweep(m1: Analysis, ob: Zone, long: bool) -> bool:
    """The M1 order block took out the last M1 swing on its side (liquidity grab)."""
    for s in reversed(m1.swings):
        if s.idx >= ob.idx or s.confirmed_idx > ob.idx:
            continue
        if (s.kind == "LOW") == long:
            return ob.bottom < s.price if long else False
        continue
    return False


def _poi_confluence(
    ctx: Mapping[str, Analysis], p: SmcParams, zone: Zone, t: datetime, side: Side
) -> bool:
    other = "OB" if zone.kind == "FVG" else "FVG"
    for tf in p.poi_tfs:
        a = ctx.get(tf)
        if a is None:
            continue
        k = last_closed(a, t)
        if any(
            z.kind == other and z.direction is side and z.overlaps(zone) for z in zones_at(a, k, p)
        ):
            return True
    return False


def find_setups(
    ctx: Mapping[str, Analysis], p: SmcParams, costs: Costs, *, since_idx: int = 0
) -> list[Setup]:
    m1 = ctx[p.trigger_tf]
    return [evaluate(e, ctx, p, costs) for e in m1.events if e.break_idx >= since_idx]
