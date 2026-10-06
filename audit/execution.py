"""C. Execution realism, D. costs, E. R and accounting, F1 (lifecycle part).

Population: every accepted order of the engine (`sp2l.smc.backtest.run`, unchanged) on 9
markets x 3 parameter sets (current = config/runtime.yaml; nofilter = current without the
reject-only filters; smc22 = the SMC-2.2 settings) - the current set alone makes no trade.

`ref_walk` is an independent, deliberately plain re-implementation of the documented
lifecycle (docs/SMC_STRATEGY.md "Lifecycle"); with default switches it must reproduce the
engine trade for trade (F1). Each switch then changes one execution assumption and the change
in trades and R is reported (C1-C6).

Usage: uv run python -m audit.execution -> docs/audit/execution.json
"""

from __future__ import annotations

import json
import pickle
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Any

from audit.common import CACHE, EXTRA, MAIN, NOFILTER, OUT, SMC22, costs, m1, params
from sp2l.core.types import Candle, Side
from sp2l.smc.backtest import run
from sp2l.smc.lifecycle import risk_unit
from sp2l.smc.model import Costs, SmcParams

MIN = timedelta(minutes=1)
VARIANTS = {"current": {}, "nofilter": NOFILTER, "smc22": SMC22}
D0 = Decimal(0)


def population(sym: str, variant: str) -> dict[str, Any]:
    """Setups and walked trades of one market / parameter set (cached)."""
    f = CACHE / f"pop_{sym}_{variant}.pkl"
    if f.exists():
        return pickle.loads(f.read_bytes())
    res = run(m1(sym), params(sym, **VARIANTS[variant]), costs())
    out = {"setups": res["setups"], "trades": res["trades"], "stats": res["stats"]}
    f.write_bytes(pickle.dumps(out))
    return out


# ---- the reference lifecycle --------------------------------------------------------------
@dataclass
class Rules:
    fill: str = "touch"  # touch / through (>= 1 tick beyond the limit)
    gap_stop: bool = False  # a minute opening beyond the stop fills at its open
    slip_market_exits: bool = False  # TIMEOUT / TIME_STOP exits pay the slippage allowance
    slip_market_entry: bool = False  # a market entry pays the slippage allowance
    marketable_limit: bool = False  # a limit on the wrong side of the market fills at the
    # first minute's open as a taker (it would cross the book)
    same_minute: str = "sl_first"  # sl_first / tp_first (when SL and TP share a minute)
    tp_in_fill_minute: bool = False
    costs_on: bool = True


@dataclass
class Result:
    state: str
    filled_at: datetime | None = None
    closed_at: datetime | None = None
    exit_px: Decimal | None = None
    r: Decimal | None = None
    gross_r: Decimal | None = None  # price move / stop distance, no fee and no slippage
    both_minute: bool = False
    fill_minute_tp: bool = False
    fill_minute_sl: bool = False
    gap_stop: bool = False
    marketable: bool = False
    touch_only: bool = False
    fundings: int = 0


def ref_walk(
    side: Side,
    entry: Decimal,
    sl: Decimal,
    tp: Decimal,
    created: datetime,
    market: bool,
    bars: list[Candle],
    i: int,
    p: SmcParams,
    c: Costs,
    tick: Decimal,
    rules: Rules,
) -> Result:
    long = side is Side.LONG
    sg = 1 if long else -1
    if not rules.costs_on:
        c = Costs()
    slip = c.slippage
    fee_in = c.taker_fee if market else c.maker_fee
    if market and rules.slip_market_entry:
        entry_fill = entry * (1 + sg * slip)
    else:
        entry_fill = entry
    # R's denominator exactly as documented: stop distance + entry fee + slippage + taker fee
    # on the slipped stop fill (planned on the planned entry)
    stop_fill = sl * (1 - sg * slip)
    risk = abs(entry - sl) + entry * fee_in + sl * slip + stop_fill * c.taker_fee
    dist = abs(entry - sl)
    res = Result("PENDING")

    def close(state: str, px: Decimal, at: datetime, raw: Decimal) -> Result:
        res.state, res.exit_px, res.closed_at = state, px, at
        pnl = (px - entry_fill) * sg - entry_fill * fee_in - px * c.taker_fee
        res.r = pnl / risk
        res.gross_r = (raw - entry) * sg / dist
        return res

    filled = market
    if market:
        res.filled_at = created
    else:
        first = bars[i] if i < len(bars) else None
        if rules.marketable_limit and first is not None:
            if (long and first.open <= entry) or (not long and first.open >= entry):
                # the limit is through the market when placed: a taker fill at the open
                res.marketable = True
                filled, res.filled_at = True, first.open_time
                entry_fill, fee_in = first.open, c.taker_fee
    while i < len(bars):
        b = bars[i]
        i += 1
        end = b.open_time + MIN
        hit_sl = b.low <= sl if long else b.high >= sl
        hit_tp = b.high >= tp if long else b.low <= tp
        if not filled:
            if rules.fill == "through":
                fills = b.low <= entry - tick if long else b.high >= entry + tick
            else:
                fills = b.low <= entry if long else b.high >= entry
            if fills:
                filled, res.filled_at = True, b.open_time
                res.touch_only = (b.low == entry) if long else (b.high == entry)
                res.fill_minute_tp, res.fill_minute_sl = hit_tp, hit_sl
                if hit_sl:
                    return close("SL", stop_fill, end, sl)
                if hit_tp and rules.tp_in_fill_minute:
                    return close("TP", tp * (1 - sg * slip), end, tp)
                continue
            if hit_tp:
                res.state, res.closed_at = "MISSED", end
                return res
            if b.open_time >= created + timedelta(minutes=p.pending_expiry_min):
                res.state, res.closed_at = "EXPIRED", end
                return res
            continue
        assert res.filled_at is not None
        if res.filled_at < b.open_time and c.funding_interval_h > 0:
            if int(b.open_time.timestamp()) % (c.funding_interval_h * 3600) == 0:
                res.fundings += 1
        if hit_sl and hit_tp:
            res.both_minute = True
        if hit_sl and not (hit_tp and rules.same_minute == "tp_first"):
            gap = (b.open < sl) if long else (b.open > sl)
            if rules.gap_stop and gap:
                res.gap_stop = True
                return close("SL", b.open * (1 - sg * slip), end, b.open)
            return close("SL", stop_fill, end, sl)
        if hit_tp:
            return close("TP", tp * (1 - sg * slip), end, tp)
        held = b.open_time - res.filled_at
        if held >= timedelta(minutes=p.max_hold_min):
            px = b.close * (1 - sg * slip) if rules.slip_market_exits else b.close
            return close("TIMEOUT", px, end, b.close)
    return res


def walk_trade(s: Any, bars: list[Candle], index: dict[datetime, int], p: SmcParams, rules: Rules) -> Result:
    return ref_walk(
        s.direction, s.entry, s.sl, s.tp.price, s.created_at, s.market, bars,
        index[s.created_at], p, costs(), p.tick, rules,
    )


def summary(rs: list[Result]) -> dict[str, Any]:
    closed = [x for x in rs if x.r is not None]
    tot = sum((x.r for x in closed), start=D0)
    gross = sum((x.gross_r for x in closed if x.gross_r is not None), start=D0)
    return {
        "orders": len(rs),
        "closed": len(closed),
        "states": dict(Counter(x.state for x in rs)),
        "net_r": float(round(tot, 3)),
        "gross_r": float(round(gross, 3)),
    }


def liquidation_ok(s: Any, p: SmcParams) -> tuple[bool, Decimal]:
    """Estimated cross-margin liquidation of the backtest's size (wallet = account_usdt, no
    other position): equity - qty x move = maint x notional."""
    bal = p.account_usdt
    q = s.qty
    move = (bal - p.maint_margin_rate * q * s.entry) / q
    liq = s.entry - move if s.direction is Side.LONG else s.entry + move
    beyond = liq < s.sl if s.direction is Side.LONG else liq > s.sl
    return beyond, liq


def main() -> None:
    c = costs()
    out: dict[str, Any] = {"costs": {k: str(getattr(c, k)) for k in ("maker_fee", "taker_fee", "slippage", "funding_rate")}}
    rows: list[dict[str, Any]] = []
    variants_res: dict[str, dict[str, list[Result]]] = {}
    engine_vs_ref = {"compared": 0, "identical": 0, "diffs": []}
    rules = {
        "baseline": Rules(),
        "C1_trade_through": Rules(fill="through"),
        "C3_gap_stops": Rules(gap_stop=True),
        "C2_market_exit_slip": Rules(slip_market_exits=True),
        "C2_market_entry_slip": Rules(slip_market_entry=True),
        "C1_marketable_limits": Rules(marketable_limit=True),
        "C5_tp_first": Rules(same_minute="tp_first"),
        "C6_tp_in_fill_minute": Rules(tp_in_fill_minute=True),
        "all_realistic": Rules(fill="through", gap_stop=True, slip_market_exits=True, slip_market_entry=True, marketable_limit=True),
        "gross": Rules(costs_on=False),
    }
    qty_step_bad = Counter()
    liq_bad: list[Any] = []
    sl_exact = {"sl_trades": 0, "exactly_minus_1": 0}
    for sym in MAIN + EXTRA:
        bars = m1(sym)
        index = {b.open_time: i for i, b in enumerate(bars)}
        from audit.common import cfg

        step = cfg().instrument(sym)[1] if sym in MAIN else None
        for v in VARIANTS:
            pop = population(sym, v)
            p = params(sym, **VARIANTS[v])
            for s, t in pop["trades"]:
                base = walk_trade(s, bars, index, p, rules["baseline"])
                engine_vs_ref["compared"] += 1
                same = (base.state, base.filled_at, base.closed_at) == (
                    t.state.value, t.filled_at, t.closed_at
                ) and (base.r is None) == (t.result_r is None) and (
                    base.r is None or abs(base.r - t.result_r) < Decimal("1e-12")
                )
                engine_vs_ref["identical"] += same
                if not same and len(engine_vs_ref["diffs"]) < 10:
                    engine_vs_ref["diffs"].append([sym, v, s.key, t.state.value, str(t.result_r), base.state, str(base.r)])
                if t.state.value == "SL":
                    sl_exact["sl_trades"] += 1
                    sl_exact["exactly_minus_1"] += t.result_r == -1
                for name, rl in rules.items():
                    variants_res.setdefault(name, {}).setdefault(v, []).append(
                        base if name == "baseline" else walk_trade(s, bars, index, p, rl)
                    )
                if step is not None and s.qty is not None and (s.qty / step) % 1 != 0:
                    qty_step_bad[sym] += 1
                ok, liq = liquidation_ok(s, p)
                if not ok:
                    liq_bad.append([sym, v, s.key, str(s.entry), str(s.sl), str(round(liq, 6)), str(s.leverage)])
                dist = abs(s.entry - s.sl)
                ru = risk_unit(s.direction, s.entry, s.sl, c, market=s.market)
                rows.append(
                    {
                        "sym": sym, "variant": v, "key": s.key, "side": s.direction.value,
                        "created": s.created_at.isoformat(), "market": s.market,
                        "entry": str(s.entry), "sl": str(s.sl), "tp": str(s.tp.price), "tp_src": s.tp.source,
                        "stop_pct": float(dist / s.entry * 100),
                        "cost_share_of_1R": float((ru - dist) / ru),
                        "state": t.state.value, "r": None if t.result_r is None else float(t.result_r),
                        "gross_r": None if base.gross_r is None else float(base.gross_r),
                        "both_minute": base.both_minute, "fill_minute_tp": base.fill_minute_tp,
                        "fill_minute_sl": base.fill_minute_sl, "touch_only": base.touch_only,
                        "fundings": base.fundings, "leverage": float(s.leverage), "qty": str(s.qty),
                    }
                )
    out["F1_engine_vs_reference_lifecycle"] = engine_vs_ref
    out["impact"] = {name: {v: summary(rs) for v, rs in d.items()} for name, d in variants_res.items()}
    base = variants_res["baseline"]
    out["counts"] = {
        v: {
            "trades": len(rs),
            "filled": sum(1 for x in rs if x.filled_at is not None),
            "sl_and_tp_same_minute": sum(x.both_minute for x in rs),
            "tp_touched_in_fill_minute": sum(x.fill_minute_tp for x in rs),
            "sl_touched_in_fill_minute": sum(x.fill_minute_sl for x in rs),
            "touch_only_fills": sum(x.touch_only for x in rs),
            "gap_through_stop": sum(x.gap_stop for x in variants_res["C3_gap_stops"][v]),
            "marketable_limits": sum(x.marketable for x in variants_res["C1_marketable_limits"][v]),
            "timeouts": sum(1 for x in rs if x.state == "TIMEOUT"),
            "funding_times_spanned": sum(x.fundings for x in rs),
        }
        for v, rs in base.items()
    }
    filled = [r for r in rows if r["r"] is not None]
    out["stops"] = {
        "trades": len(filled),
        "stop_pct_median": median([r["stop_pct"] for r in filled]) if filled else None,
        "stop_pct_min": min([r["stop_pct"] for r in filled], default=None),
        "cost_share_of_1R_median": median([r["cost_share_of_1R"] for r in filled]) if filled else None,
        "cost_share_of_1R_max": max([r["cost_share_of_1R"] for r in filled], default=None),
        "round_trip_cost_pct_maker_in": float((c.maker_fee + c.taker_fee + c.slippage) * 100),
    }
    out["E1_stop_is_minus_1R"] = sl_exact
    out["E2_qty_not_multiple_of_step"] = dict(qty_step_bad)
    out["C8_liquidation_inside_stop"] = {"count": len(liq_bad), "examples": liq_bad[:10]}
    # funding sensitivity: 0.01 % per 8 h (a common perp rate), longs pay
    hyp = Decimal("0.0001")
    fund_r = sum(
        (Decimal(r["fundings"]) * hyp * Decimal(r["entry"]) / (abs(Decimal(r["entry"]) - Decimal(r["sl"])) * Decimal("1.2")) * (1 if r["side"] == "LONG" else -1))
        for r in filled
    )
    out["D2_funding_sensitivity_r_at_0.01pct_8h"] = float(round(fund_r, 3))
    (OUT / "trades.json").write_text(json.dumps(rows, indent=1))
    (OUT / "execution.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str)[:6000])


if __name__ == "__main__":
    main()
