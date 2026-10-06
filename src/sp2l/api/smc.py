"""SMC views for the dashboard (read-only; the browser only draws what is returned here).

The API analyses the same merged M1 series with the same parameters as the runner, so the
zones on the chart are exactly the ones the engine reasons about. Signals and their
lifecycle are only ever written by the runner; here they are read.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine

from sp2l.api.queries import _dec, jsonable, last_trade, one, rows
from sp2l.core.types import Side
from sp2l.smc.backtest import run as run_backtest
from sp2l.smc.context import ContextBuilder, needed_tfs
from sp2l.smc.history import coverage, extremes_since, load_bars, series_end
from sp2l.smc.lifecycle import ACTIVE_SQL
from sp2l.smc.model import VERSION, Analysis, Costs, SmcParams, Zone, ZoneStatus
from sp2l.smc.store import setup_json, target_json
from sp2l.smc.strategy import (
    ARMED_BEFORE,
    TREND,
    entry_price,
    evaluate,
    find_arming,
    order_for,
    trend_at,
    zone_setups,
)
from sp2l.smc.structure import leg_gaps, update_zone
from sp2l.smc.timeframes import ORDER, length

TREND_LABEL = {1: "BULLISH", -1: "BEARISH", 0: "UNDEFINED"}
REASON_TEXT = {
    "NO_BIAS": "Bias timeframe has no trend yet",
    "BIAS_MISMATCH": "Against the bias timeframe's trend",
    "ZONE_INVALID": "Order block closed through or expired before the order",
    "NOT_FRESH": "Order block already touched (only the first touch is an entry)",
    "BAD_STOP": "Stop would sit on the wrong side of the entry",
    "NO_TARGET": "No previous HH (long) / LL (short) beyond the entry for the target",
    "LEVERAGE": "Position size would need more than the allowed leverage",
    "NOT_DISCOUNT": "Long entry above 50 % of the dealing range (sweep wick -> HH)",
    "NOT_PREMIUM": "Short entry below 50 % of the dealing range (sweep wick -> LL)",
    "LOW_NET_RR": "Net R at the target below min_net_rr after fees and slippage",
    "COST_HEAVY": "Entry + exit fees + slippage above max_cost_frac of the stop distance",
}
RULE_TEXT = {
    "sweep": "Liquidity sweep (only with require_sweep): a zone-TF wick beyond unswept swing"
    " lows / equal lows (highs) that closes back inside",
    "displacement": "Displacement: a zone-TF bar closes beyond the last swing (BOS / CHoCH)"
    " within sweep_max_bars of the sweep",
    "order_block": "Order block: the last opposite-colour candle before the break, at or after"
    " the sweep, at least ob_min_atr x ATR",
    "fvg": "FVG right after the order block (any size): the setup is known when it closes",
    "fresh": "Only the first touch: the order is armed when price first trades into the FVG",
    "bias": "Bias timeframe trend in the trade's direction when the order is placed",
}
SETUP_STATE = {"PENDING": "pending", "OPEN": "open"}
FRESH_ARMING = timedelta(minutes=3)  # the runner's window for a new arming


def plan_json(s: Any) -> dict[str, Any]:
    """An evaluated setup (strategy.Setup) as the chart shows it."""
    return {
        "accepted": s.accepted,
        "reasons": [{"code": r, "text": REASON_TEXT.get(r, r)} for r in s.reasons],
        "entry": None if s.entry is None else _dec(s.entry),
        "sl": None if s.sl is None else _dec(s.sl),
        "tp": target_json(s.tp),
        "range_mid": None if s.range_mid is None else _dec(s.range_mid),
        "cost_frac": None if s.cost_frac is None else _dec(round(s.cost_frac, 4)),
        "market": s.market,
    }


FILTER_REASONS = {"NOT_DISCOUNT", "NOT_PREMIUM", "LOW_NET_RR", "COST_HEAVY"}


def _t(d: datetime) -> str:
    return d.astimezone(UTC).isoformat()


def zone_out(z: Zone, a: Analysis, k: int) -> dict[str, Any]:
    end_idx = z.mitigated_idx if z.mitigated_idx is not None and z.mitigated_idx <= k else None
    expired = end_idx is None and k > z.expires_idx
    if expired:
        end_idx = z.expires_idx
    return {
        "id": z.id,
        "tf": a.tf,
        "kind": z.kind,
        "direction": z.direction.value,
        "top": _dec(z.top),
        "bottom": _dec(z.bottom),
        "from": _t(z.time),
        "to": None if end_idx is None else _t(a.bars[end_idx].open_time + length(a.tf)),
        "status": "EXPIRED" if expired else z.status.value,
        "tested": z.tested_idx is not None and z.tested_idx <= k,
    }


Range = tuple[Decimal, Decimal] | None  # (high, low) traded since the last closed bar


def live_zone(zd: dict[str, Any], rng: Range, p: SmcParams) -> dict[str, Any] | None:
    """A zone as of NOW: the closed-bar state plus what the forming bar already did. A touch
    and a fair value gap filled end to end by a wick are irreversible, so they apply at once
    (an order block still needs a close to become invalid)."""
    if rng is None:
        return zd
    hi, lo = rng
    top, bottom = Decimal(zd["top"]), Decimal(zd["bottom"])
    long = zd["direction"] == "LONG"
    touched = lo <= top if long else hi >= bottom
    filled = lo <= bottom if long else hi >= top
    if zd["kind"] == "FVG" and p.fvg_fill == "wick" and filled:
        return None
    if touched and not zd["tested"]:
        return {**zd, "tested": True, "status": "TESTED", "live": True}
    return zd


def _with_zone(dicts: list[dict[str, Any]], zones: list[Zone]) -> list[tuple[dict[str, Any], Zone]]:
    by = {z.id: z for z in zones}
    return [(d, by[d["id"]]) for d in dicts if d["id"] in by]


def ob_gaps(z: Zone, a: Analysis, k: int, rng: Range, p: SmcParams) -> list[dict[str, Any]]:
    """Every gap (any size) the move from an order block to its break left, as of NOW. An open
    gap is drawn like any fair value gap (`fresh` until price returns to it); a filled one only
    over its three candles, to show the block did leave a gap."""
    out: list[dict[str, Any]] = []
    fill_wick = p.fvg_fill == "wick"
    for m, bottom, top in leg_gaps(a.bars, z.idx, z.created_idx, z.direction):
        g = Zone(
            id=f"{a.tf}:FVG:{z.direction.value}:{int(a.bars[m].open_time.timestamp())}",
            kind="FVG",
            direction=z.direction,
            top=top,
            bottom=bottom,
            idx=m,
            time=a.bars[m].open_time,
            created_idx=m + 1,
        )
        for t in range(m + 2, k + 1):
            update_zone(g, t, a.bars[t], fill_wick)
            if g.status is ZoneStatus.MITIGATED:
                break
        d = None if g.status is ZoneStatus.MITIGATED else live_zone(zone_out(g, a, k), rng, p)
        if d is None:  # filled (by a closed bar or by the forming one)
            d = {
                **zone_out(g, a, k),
                "from": _t(a.bars[m - 1].open_time),
                "to": _t(a.bars[m + 1].open_time + length(a.tf)),
                "tested": True,
            }
            d["status"] = "FILLED"
        d["ob"] = z.id
        out.append(d)
    return out


def liquidity(
    a: Analysis, price: Decimal | None, per_side: int = 3, rng: Range = None
) -> list[dict[str, Any]]:
    """Unswept swing highs (buy-side liquidity) and lows (sell-side) nearest the price; a level
    the forming bar already traded through is swept."""
    if not a.bars or price is None:
        return []
    k = len(a.bars) - 1
    hi = rng[0] if rng else None
    lo = rng[1] if rng else None
    highs: list[dict[str, Any]] = []
    lows: list[dict[str, Any]] = []
    for s in reversed(a.swings):
        seg = a.bars[s.idx + 1 : k + 1]
        if (
            s.kind == "HIGH"
            and s.price > price
            and (hi is None or hi <= s.price)
            and not any(b.high > s.price for b in seg)
        ):
            highs.append({"kind": "BSL", "price": _dec(s.price), "from": _t(s.time)})
        if (
            s.kind == "LOW"
            and s.price < price
            and (lo is None or lo >= s.price)
            and not any(b.low < s.price for b in seg)
        ):
            lows.append({"kind": "SSL", "price": _dec(s.price), "from": _t(s.time)})
    highs.sort(key=lambda x: Decimal(x["price"]))
    lows.sort(key=lambda x: -Decimal(x["price"]))
    return highs[:per_side] + lows[:per_side]


def dealing_range(a: Analysis) -> dict[str, Any] | None:
    """Premium / discount of the current dealing range (last swing high and low)."""
    hi = next((s for s in reversed(a.swings) if s.kind == "HIGH"), None)
    lo = next((s for s in reversed(a.swings) if s.kind == "LOW"), None)
    if hi is None or lo is None or hi.price <= lo.price:
        return None
    return {
        "high": _dec(hi.price),
        "low": _dec(lo.price),
        "eq": _dec((hi.price + lo.price) / 2),
        "from": _t(min(hi.time, lo.time)),
    }


class SmcView:
    def __init__(self, db: Engine, symbol: str, params: SmcParams, costs: Costs | None) -> None:
        self.db = db
        self.symbol = symbol
        self.params = params
        self.costs = costs or Costs()
        self.ctx = ContextBuilder(db, symbol, params, max_age=60.0)
        self._lock = threading.Lock()
        self._bt_lock = threading.Lock()
        self._bt: tuple[float, dict[str, Any]] | None = None

    # ---- helpers ------------------------------------------------------------------------
    def _context(self, tfs: list[str]) -> tuple[datetime | None, dict[str, Analysis]]:
        upto = series_end(self.db, self.symbol)
        if upto is None:
            return None, {}
        with self._lock:
            return upto, self.ctx.build(upto, tfs)

    def _live_range(self, a: Analysis, upto: datetime, forming: list[dict[str, Any]]) -> Range:
        """What traded after the last closed bar of `a`: final minutes + the live minute."""
        if not a.bars:
            return None
        start = a.bars[-1].open_time + length(a.tf)
        rng = extremes_since(self.db, self.symbol, start, upto)
        his = [Decimal(f["h"]) for f in forming] + ([rng[0]] if rng else [])
        los = [Decimal(f["l"]) for f in forming] + ([rng[1]] if rng else [])
        return (max(his), min(los)) if his else None

    def _forming(self) -> list[dict[str, Any]]:
        from sp2l.api.queries import live_snapshot

        forming: list[dict[str, Any]] = live_snapshot(self.db, self.symbol)["forming"]
        return forming

    def _price(self) -> Decimal | None:
        return last_price(self.db, self.symbol)

    # ---- chart --------------------------------------------------------------------------
    def candles(self, tf: str, limit: int) -> dict[str, Any]:
        upto = series_end(self.db, self.symbol)
        if upto is None:
            return {"tf": tf, "items": []}
        bars = load_bars(self.db, self.symbol, tf, limit, upto, include_forming=True)
        items = []
        for b in bars:
            items.append(
                {
                    "open_time": _t(b.open_time),
                    "open": _dec(b.open),
                    "high": _dec(b.high),
                    "low": _dec(b.low),
                    "close": _dec(b.close),
                    "volume": _dec(b.volume),
                    "forming": b.open_time + length(tf) > upto,
                }
            )
        return {"tf": tf, "upto": _t(upto), "items": items}

    def setups(
        self,
        ctx: dict[str, Analysis],
        upto: datetime,
        price: Decimal | None,
        forming: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """(shown, every setup): the zone setups by state, each with its plan (evaluated now
        while it waits, else at the moment its order existed). Shown by default: fresh, valid,
        with the bias, not rejected by a filter and within chart_near_atr x ATR of price, or
        tied to an active signal; those first, then the nearest, at most chart_top_n."""
        p = self.params
        za, m1 = ctx[p.zone_tf], ctx["1m"].bars
        k = len(za.bars) - 1
        if k < 0:
            return [], []
        bias = trend_at(ctx.get(p.bias_tf), upto)
        rng = self._live_range(za, upto, forming)
        atr = za.atr[k]
        act = {
            r["key"]: r
            for r in rows(
                self.db,
                "SELECT id, key, state FROM smc_signals WHERE symbol = :s AND state IN "
                + ACTIVE_SQL,
                s=self.symbol,
            )
        }
        out = []
        for zs in zone_setups(za, p):
            long = zs.direction is Side.LONG
            sig = act.get(zs.key)
            armed = find_arming(zs, za, k, m1)
            live_in = rng is not None and (rng[1] <= zs.gap[1] if long else rng[0] >= zs.gap[0])
            if sig is not None:
                st = SETUP_STATE.get(sig["state"], "open")
            elif not zs.ob.valid_at(k):
                st = "expired" if k > zs.ob.expires_idx else "invalid"
            elif armed is not None:  # traded into the FVG: armed now, used once that minute passed
                recent = armed != ARMED_BEFORE and m1[armed].open_time >= upto - FRESH_ARMING
                st = "armed" if recent else "used"
            elif live_in:
                st = "armed"
            else:
                st = "waiting"
            edge = entry_price(zs, p)
            dist = None
            if price is not None and atr:
                dist = max(Decimal(0), (price - edge) if long else (edge - price)) / atr
            out.append(
                {
                    **setup_json(zs),
                    "state": st,
                    "aligned": bias == TREND[zs.direction],
                    "age_min": int((upto - zs.confirmed_at).total_seconds() // 60),
                    "distance_atr": None if dist is None else _dec(round(dist, 2)),
                    "signal_id": None if sig is None else sig["id"],
                    "plan": None,
                    "_zs": zs,
                    "_armed": armed,
                }
            )
        for x in out:  # the plan: now while waiting, else as it was when its order existed
            zs, armed = x["_zs"], x["_armed"]
            if x["state"] in ("waiting", "armed") or armed is None:
                x["plan"] = plan_json(evaluate(zs, ctx, p, self.costs, upto))
            elif armed != ARMED_BEFORE and (order := order_for(zs, ctx, p, armed, m1)):
                x["plan"] = plan_json(
                    evaluate(zs, ctx, p, self.costs, order[0], market_price=order[1])
                )

        def filtered(x: dict[str, Any]) -> bool:
            return x["plan"] is not None and bool(
                {r["code"] for r in x["plan"]["reasons"]} & FILTER_REASONS
            )

        tied = [x for x in out if x["signal_id"] is not None]
        near = sorted(
            (
                x
                for x in out
                if x["signal_id"] is None
                and x["state"] in ("waiting", "armed")
                and x["aligned"]
                and not filtered(x)
                and x["distance_atr"] is not None
                and Decimal(x["distance_atr"]) <= p.chart_near_atr
            ),
            key=lambda x: Decimal(x["distance_atr"]),
        )
        shown = tied + near[: max(0, p.chart_top_n - len(tied))]
        for x in out:
            x.pop("_zs")
            x.pop("_armed")
        return shown, out

    def analysis(self, tf: str, bars: int) -> dict[str, Any]:
        p = self.params
        htfs = [
            x for x in ORDER if ORDER.index(x) > ORDER.index(tf) and x in (p.zone_tf, p.bias_tf)
        ]
        upto, ctx = self._context(sorted({tf, *htfs, *needed_tfs(p)}, key=ORDER.index))
        if upto is None:
            return {"tf": tf, "ready": False}
        a = ctx[tf]
        k = len(a.bars) - 1
        first = a.bars[max(0, k - bars + 1)].open_time if a.bars else upto
        price = self._price() or (a.bars[-1].close if a.bars else None)

        # an order block closed through or a fair value gap filled is invalid: never drawn;
        # touches and fills of the forming bar apply at once (live_zone)
        forming = self._forming()
        rng = self._live_range(a, upto, forming)
        zones = [
            zd
            for z in a.zones
            if z.valid_at(k) and (zd := live_zone(zone_out(z, a, k), rng, p)) is not None
        ]
        for zd, z in _with_zone(zones, a.zones):
            if z.kind == "OB":
                zd["gaps"] = ob_gaps(z, a, k, rng, p)
        htf_zones = []
        for h in htfs:
            ha = ctx[h]
            hk = len(ha.bars) - 1
            hr = self._live_range(ha, upto, forming)
            htf_zones += [
                zd
                for z in ha.zones
                if z.valid_at(hk) and (zd := live_zone(zone_out(z, ha, hk), hr, p)) is not None
            ]
            for zd, z in _with_zone(htf_zones, ha.zones):
                if z.kind == "OB":
                    zd["gaps"] = ob_gaps(z, ha, hk, hr, p)
        shown, every = self.setups(ctx, upto, price, forming)
        floor = len(a.bars) - 1 - p.lookback(tf)
        events = [
            {
                "id": e.id,
                "kind": e.kind,
                "direction": e.direction.value,
                "level": _dec(e.level),
                "from": _t(e.level_time),
                "to": _t(e.break_time),
            }
            for e in a.events
            if e.break_time >= first and e.break_idx >= floor
        ]
        return {
            "tf": tf,
            "symbol": self.symbol,
            "ready": True,
            "version": VERSION,
            "params_hash": p.digest(),
            "upto": _t(upto),
            "bars": len(a.bars),
            "trend": TREND_LABEL[a.trend[-1] if a.trend else 0],
            "zones": zones,
            "htf_zones": htf_zones,
            "events": events,
            "liquidity": liquidity(a, price, rng=rng),
            "setups": shown,
            "setups_all": every,
            "zone_tf": p.zone_tf,
            "range": dealing_range(a),
            "swings": [
                {"kind": s.kind, "price": _dec(s.price), "time": _t(s.time)}
                for s in a.swings
                if s.time >= first
            ],
        }

    # ---- top-down radar -----------------------------------------------------------------
    def radar(self) -> dict[str, Any]:
        p = self.params
        upto, ctx = self._context(list(ORDER))
        if upto is None:
            return {"ready": False}
        price = self._price()
        roles = {p.bias_tf: "Bias", p.zone_tf: "Zone (break, OB + FVG)", p.exec_tf: "Execution"}
        roles.setdefault("1m", "Fills and exits")
        tfs = []
        for tf in ORDER:
            a = ctx.get(tf)
            if a is None:
                continue
            last = a.events[-1] if a.events else None
            tfs.append(
                {
                    "tf": tf,
                    "role": roles.get(tf, ""),
                    "trend": TREND_LABEL[trend_at(a, upto)],
                    "bars": len(a.bars),
                    "last_event": None
                    if last is None
                    else {
                        "kind": last.kind,
                        "direction": last.direction.value,
                        "level": _dec(last.level),
                        "time": _t(last.break_time),
                    },
                }
            )
        bias = trend_at(ctx.get(p.bias_tf), upto)
        shown, every = self.setups(ctx, upto, price, self._forming())
        return {
            "ready": True,
            "upto": _t(upto),
            "price": None if price is None else _dec(price),
            "bias": TREND_LABEL[bias],
            "timeframes": tfs,
            "setups": shown,
            "recent": list(reversed(every[-12:])),
            "rule_text": RULE_TEXT,
        }

    # ---- per-market performance ----------------------------------------------------------
    def performance(self) -> dict[str, Any]:
        return performance(self.db, [self.symbol])

    # ---- backtest -----------------------------------------------------------------------
    def backtest(self, days: int = 30) -> dict[str, Any]:
        with self._bt_lock:
            if self._bt is not None and time.time() - self._bt[0] < 900:
                return self._bt[1]
            upto = series_end(self.db, self.symbol)
            if upto is None:
                return {"ready": False}
            m1 = load_bars(self.db, self.symbol, "1m", days * 1440, upto)
            t0 = time.time()
            res = run_backtest(m1, self.params, self.costs)
            trades = res["trades"]
            closed = [(s, t) for s, t in trades if t.result_r is not None]
            out = {
                "ready": True,
                "from": _t(m1[0].open_time) if m1 else None,
                "to": _t(upto),
                "bars": len(m1),
                "seconds": round(time.time() - t0, 1),
                "params_hash": self.params.digest(),
                "stats": res["stats"],
                **summarize(
                    [t.result_r for _, t in closed if t.result_r is not None],
                    [
                        {
                            "closed_at": _t(t.closed_at),
                            "result_r": _dec(t.result_r),
                            "side": t.side.value,
                        }
                        for _, t in closed
                        if t.closed_at and t.result_r is not None
                    ],
                ),
                "trades": [
                    {
                        "created_at": _t(s.created_at),
                        "side": s.direction.value,
                        "entry": _dec(t.entry),
                        "sl": _dec(s.sl) if s.sl is not None else None,
                        "tp": target_json(s.tp),
                        "state": t.state.value,
                        "result_r": None if t.result_r is None else _dec(round(t.result_r, 2)),
                        "filled_at": None if t.filled_at is None else _t(t.filled_at),
                        "closed_at": None if t.closed_at is None else _t(t.closed_at),
                    }
                    for s, t in trades[-200:]
                ],
            }
            self._bt = (time.time(), jsonable(out))
            return self._bt[1]

    # ---- status -------------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        st = one(self.db, "SELECT * FROM smc_runner_state WHERE symbol = :s", s=self.symbol)
        age = None
        if st is not None:
            age = int(
                (datetime.now(UTC) - datetime.fromisoformat(st["heartbeat_at"])).total_seconds()
            )
        first, last = coverage(self.db, self.symbol)
        upto = series_end(self.db, self.symbol)
        lag = None if upto is None else int((datetime.now(UTC) - upto).total_seconds())
        return {
            "runner": None
            if st is None
            else {
                "status": "RUNNING" if age is not None and age < 120 else "STALE",
                "heartbeat_age_s": age,
                "started_at": st["started_at"],
                "last_m1": st["last_m1"],
                "params_hash": (st["status"] or {}).get("params_hash"),
                "history": (st["status"] or {}).get("history"),
            },
            "history": {
                "first": None if first is None else _t(first),
                "last": None if last is None else _t(last),
                "days": None
                if first is None or upto is None
                else round((upto - first) / timedelta(days=1), 1),
                "target_days": self.params.history_days,
            },
            "series_lag_s": lag,
            "params_hash": self.params.digest(),
        }

    def params_view(self) -> dict[str, Any]:
        """Every parameter in force for this market (tick = its own price increment)."""
        p = self.params
        groups = {
            "Structure (every timeframe)": [
                "swing_len",
                "atr_len",
                "ob_lookback",
                "ob_rule",
                "ob_min_atr",
                "ob_require_fvg",
                "fvg_adjacent",
                "fvg_min_atr",
                "fvg_fill",
                "eq_tol_atr",
                "require_sweep",
                "sweep_max_bars",
            ],
            "Top-down model": [
                "bias_tf",
                "zone_tf",
                "exec_tf",
                "confirm_exec",
                "confirm_entry",
                "confirm_window_min",
            ],
            "Entry, stop and target": [
                "entry_ref",
                "sl_ref",
                "tick",
                "tp_rr",
                "tp_ref",
                "tp_front_run_atr",
            ],
            "Filters": ["require_discount", "min_net_rr", "max_cost_frac", "exit_on_choch"],
            "Lifecycle": [
                "pending_expiry_min",
                "time_stop_min",
                "max_hold_min",
                "max_active",
                "max_positions",
            ],
            "Chart": ["chart_near_atr", "chart_top_n"],
            "Data": [f"lookback_{tf}" for tf in ORDER] + ["history_days"],
            "Shared wallet": ["account_usdt", "risk_pct", "max_leverage"],
        }
        d = p.as_dict()
        return {
            "version": VERSION,
            "params_hash": p.digest(),
            "groups": [
                {
                    "name": g,
                    "items": [
                        {
                            "key": k,
                            "value": jsonable(list(d[k]) if isinstance(d[k], tuple) else d[k]),
                        }
                        for k in keys
                    ],
                }
                for g, keys in groups.items()
            ],
            "costs": jsonable(
                {
                    "maker_fee": self.costs.maker_fee,
                    "taker_fee": self.costs.taker_fee,
                    "slippage": self.costs.slippage,
                }
            ),
            "rules": RULE_TEXT,
            "reasons": REASON_TEXT,
        }


def last_price(db: Engine, symbol: str) -> Decimal | None:
    """The newest of: the last live trade, the last close of the merged 1-minute series."""
    t = last_trade(db, symbol)
    upto = series_end(db, symbol)
    if t and (upto is None or datetime.fromisoformat(t["exch_ts"]) >= upto - timedelta(minutes=2)):
        return Decimal(str(t["price"]))
    bars = load_bars(db, symbol, "1m", 1, upto) if upto else []
    if bars:
        return bars[-1].close
    return Decimal(str(t["price"])) if t else None


def last_prices(db: Engine, symbols: list[str]) -> dict[str, Decimal]:
    out = {}
    for sym in symbols:
        p = last_price(db, sym)
        if p is not None:
            out[sym] = p
    return out


FILLED_STATES = ("OPEN",)


def open_pnl(
    r: dict[str, Any], price: Decimal | None, costs: Costs
) -> tuple[str | None, str | None]:
    """(R of the position so far: realized parts + the open rest marked at `price` after its
    exit fee; unrealized PnL in USDT of the open rest)."""
    if r["state"] not in FILLED_STATES or price is None:
        return None, None
    sgn = 1 if r["side"] == "LONG" else -1
    entry = Decimal(r["entry"])
    fee_in = costs.taker_fee if (r.get("detail") or {}).get("market") else costs.maker_fee
    net = (price - entry) * sgn - entry * fee_in - price * costs.taker_fee  # per unit, as R is
    risk = Decimal(r["risk"]) if r.get("risk") else abs(entry - Decimal(r["sl"]))
    parts = r.get("parts") or []
    rest = 1 - sum((Decimal(x["frac"]) for x in parts), Decimal(0))
    done = sum((Decimal(x["r"]) for x in parts), Decimal(0))
    open_r = _dec(round(done + rest * net / risk, 2)) if risk else None
    qty = Decimal(r["qty"]) if r.get("qty") else Decimal(0)
    return open_r, _dec(round(qty * rest * net, 4))


def signals(
    db: Engine,
    symbols: list[str],
    *,
    active: bool | None,
    limit: int,
    since: datetime | None = None,
    costs: Costs | None = None,
    params: dict[str, SmcParams] | None = None,
) -> list[dict[str, Any]]:
    """`params` (per market) adds when an active position ends on its own: a PENDING limit
    expires at `deadline`, an OPEN one is closed at market at `deadline`."""
    where = ["symbol = ANY(:syms)"]
    if active is True:
        where.append("state IN " + ACTIVE_SQL)
    elif active is False:
        where.append("state NOT IN " + ACTIVE_SQL)
    if since is not None:
        where.append("(closed_at IS NULL OR closed_at >= :since)")
    out = rows(
        db,
        "SELECT id, symbol, key, side, state, created_at, entry, sl, tp, risk, rr, net_rr, score,"
        " tp_source, trigger_kind, poi_tf, detail, qty, notional, leverage, margin, filled_at,"
        " closed_at, exit_price, result_r, pnl_usdt, fees_usdt, updated_at, targets, parts,"
        " version FROM smc_signals WHERE "
        + " AND ".join(where)
        + " ORDER BY created_at DESC LIMIT :n",
        syms=symbols,
        n=limit,
        since=since,
    )
    prices = last_prices(db, sorted({r["symbol"] for r in out if r["state"] in FILLED_STATES}))
    for r in out:
        for k, dp in (("result_r", 2), ("net_rr", 2), ("rr", 2), ("pnl_usdt", 4), ("fees_usdt", 4)):
            r[k] = None if r[k] is None else _dec(round(Decimal(r[k]), dp))
        r["open_r"], r["open_pnl"] = open_pnl(r, prices.get(r["symbol"]), costs or Costs())
        sp = (params or {}).get(r["symbol"])
        r["deadline"] = r["time_stop_at"] = None
        ts_min = int((r.get("detail") or {}).get("time_stop_min") or 0)
        if sp is not None and r["state"] == "PENDING":
            r["deadline"] = _t(
                datetime.fromisoformat(r["created_at"]) + timedelta(minutes=sp.pending_expiry_min)
            )
        elif sp is not None and r["state"] in FILLED_STATES and r["filled_at"]:
            filled = datetime.fromisoformat(r["filled_at"])
            r["deadline"] = _t(filled + timedelta(minutes=sp.max_hold_min))
            if ts_min > 0:
                r["time_stop_at"] = _t(filled + timedelta(minutes=ts_min))
    return out


def signal_events(db: Engine, sid: int) -> list[dict[str, Any]]:
    return rows(
        db,
        "SELECT ts, kind, price, detail FROM smc_signal_events WHERE signal_id = :i ORDER BY id",
        i=sid,
    )


def performance(db: Engine, symbols: list[str]) -> dict[str, Any]:
    closed = rows(
        db,
        "SELECT closed_at, result_r, state, side, symbol, pnl_usdt FROM smc_signals"
        " WHERE symbol = ANY(:s) AND result_r IS NOT NULL ORDER BY closed_at",
        s=symbols,
    )
    counts = {
        r["state"]: int(r["n"])
        for r in rows(
            db,
            "SELECT state, COUNT(*) AS n FROM smc_signals WHERE symbol = ANY(:s) GROUP BY 1",
            s=symbols,
        )
    }
    by_symbol = {
        sym: summarize(
            [Decimal(r["result_r"]) for r in closed if r["symbol"] == sym],
            [r for r in closed if r["symbol"] == sym],
        )
        for sym in symbols
    }
    for v in by_symbol.values():
        v.pop("equity")
    pnl = sum((Decimal(r["pnl_usdt"]) for r in closed if r["pnl_usdt"] is not None), Decimal(0))
    return {
        "counts": counts,
        "pnl_usdt": _dec(round(pnl, 4)),
        "by_symbol": by_symbol,
        **summarize([Decimal(r["result_r"]) for r in closed], closed),
    }


def wallet(
    db: Engine,
    symbols: list[str],
    costs: Costs | None = None,
    params: dict[str, SmcParams] | None = None,
) -> dict[str, Any]:
    """The shared simulated wallet: balance, open positions marked to the last price, ledger."""
    w = one(
        db, "SELECT id, initial_usdt, symbols, started_at FROM smc_wallets WHERE ended_at IS NULL"
    )
    if w is None:
        return {"ready": False}
    ledger = rows(
        db,
        "SELECT l.ts, l.symbol, l.kind, l.amount, l.balance_after, l.signal_id, l.part, l.fees,"
        " s.side, s.state"
        " FROM smc_wallet_ledger l LEFT JOIN smc_signals s ON s.id = l.signal_id"
        " WHERE l.wallet_id = :w ORDER BY l.id",
        w=w["id"],
    )
    balance = Decimal(ledger[-1]["balance_after"]) if ledger else Decimal(w["initial_usdt"])
    act = signals(db, symbols, active=True, limit=50, costs=costs, params=params)
    margin = sum((Decimal(r["margin"]) for r in act if r.get("margin")), Decimal(0))
    upnl = sum((Decimal(r["open_pnl"]) for r in act if r.get("open_pnl")), Decimal(0))
    realized = [r for r in ledger if r["kind"] == "REALIZED_PNL"]
    fees = one(
        db,
        "SELECT COALESCE(SUM(fees_usdt), 0) AS f FROM smc_signals WHERE wallet_id = :w",
        w=w["id"],
    ) or {"f": "0"}
    initial = Decimal(w["initial_usdt"])
    return {
        "ready": True,
        "id": w["id"],
        "symbols": w["symbols"],
        "started_at": w["started_at"],
        "initial": _dec(initial),
        "balance": _dec(round(balance, 4)),
        "equity": _dec(round(balance + upnl, 4)),
        "unrealized": _dec(round(upnl, 4)),
        "margin_used": _dec(round(margin, 4)),
        "free": _dec(round(balance - margin, 4)),
        "realized": _dec(round(balance - initial, 4)),
        "return_pct": float(round((balance + upnl - initial) / initial, 5)),
        "fees": _dec(round(Decimal(fees["f"]), 4)),
        "positions": act,
        "curve": [{"t": r["ts"], "balance": r["balance_after"]} for r in ledger],
        "ledger": list(reversed(realized[-100:])),
    }


def summarize(rs: list[Decimal], closed: list[dict[str, Any]]) -> dict[str, Any]:
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    eq = Decimal(0)
    curve = []
    peak = dd = Decimal(0)
    for r, c in zip(rs, closed, strict=False):
        eq += r
        peak, dd = max(peak, eq), max(dd, peak - eq)
        curve.append({"t": c["closed_at"], "r": _dec(round(eq, 3))})
    total = sum(rs, start=Decimal(0))
    loss_sum = -sum(losses, start=Decimal(0))
    return {
        "closed": len(rs),
        "wins": len(wins),
        "win_rate": None if not rs else round(len(wins) / len(rs), 3),
        "total_r": _dec(round(total, 2)),
        "avg_r": None if not rs else _dec(round(total / len(rs), 3)),
        "profit_factor": None
        if not loss_sum
        else _dec(round(sum(wins, start=Decimal(0)) / loss_sum, 2)),
        "max_drawdown_r": _dec(round(dd, 2)),
        "equity": curve,
    }
