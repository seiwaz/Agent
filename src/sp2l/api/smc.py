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
from sp2l.smc.history import coverage, load_bars, series_end
from sp2l.smc.model import VERSION, Analysis, Costs, SmcParams, Zone
from sp2l.smc.strategy import evaluate, trend_at
from sp2l.smc.timeframes import ORDER, length

TREND_LABEL = {1: "BULLISH", -1: "BEARISH", 0: "UNDEFINED"}
REASON_TEXT = {
    "NO_BIAS": "Higher-timeframe bias not yet defined",
    "BIAS_MISMATCH": "Against the higher-timeframe bias",
    "NO_ORDER_BLOCK": "No M1 order block behind the break",
    "WARMUP": "Not enough M1 history",
    "NO_POI": "Not inside an unmitigated higher-timeframe zone",
    "BAD_STOP": "Stop would sit on the wrong side of the entry",
    "SL_TOO_WIDE": "Stop farther than the allowed risk",
    "NO_TARGET": "No liquidity target pays the minimum net R:R after fees",
    "LOW_SCORE": "Confluence score below the minimum",
    "LEVERAGE": "Position size would need more than the allowed leverage",
}
FACTOR_TEXT = {
    "fresh": "Fresh POI (first touch)",
    "choch": "M1 CHoCH (reversal)",
    "sweep": "Liquidity sweep before the break",
    "m1_fvg": "Displacement FVG on M1",
    "poi_confluence": "OB and FVG overlap at the POI",
    "bias_confirm": "Confirmation timeframe agrees",
}


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


def liquidity(a: Analysis, price: Decimal | None, per_side: int = 3) -> list[dict[str, Any]]:
    """Unswept swing highs (buy-side liquidity) and lows (sell-side) nearest the price."""
    if not a.bars or price is None:
        return []
    k = len(a.bars) - 1
    highs: list[dict[str, Any]] = []
    lows: list[dict[str, Any]] = []
    for s in reversed(a.swings):
        seg = a.bars[s.idx + 1 : k + 1]
        if s.kind == "HIGH" and s.price > price and not any(b.high > s.price for b in seg):
            highs.append({"kind": "BSL", "price": _dec(s.price), "from": _t(s.time)})
        if s.kind == "LOW" and s.price < price and not any(b.low < s.price for b in seg):
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
        self.ctx = ContextBuilder(db, symbol, params)
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

    def _price(self) -> Decimal | None:
        """Latest live trade, else the last close of the merged series."""
        t = last_trade(self.db, self.symbol)
        if t:
            return Decimal(str(t["price"]))
        upto = series_end(self.db, self.symbol)
        if upto is None:
            return None
        last = load_bars(self.db, self.symbol, "1m", 1, upto)
        return last[-1].close if last else None

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

    def analysis(self, tf: str, bars: int) -> dict[str, Any]:
        p = self.params
        htfs = [x for x in ORDER if ORDER.index(x) > ORDER.index(tf) and x in p.poi_tfs]
        upto, ctx = self._context(sorted({tf, *htfs}, key=ORDER.index))
        if upto is None:
            return {"tf": tf, "ready": False}
        a = ctx[tf]
        k = len(a.bars) - 1
        first = a.bars[max(0, k - bars + 1)].open_time if a.bars else upto
        price = self._price() or (a.bars[-1].close if a.bars else None)

        def visible(z: Zone) -> bool:
            alive = z.valid_at(k)
            return alive or (
                z.mitigated_idx is not None and a.bars[z.mitigated_idx].open_time >= first
            )

        zones = [
            zone_out(z, a, k) for z in a.zones if z.time >= first - length(tf) * 50 and visible(z)
        ]
        htf_zones = []
        for h in htfs:
            ha = ctx[h]
            hk = len(ha.bars) - 1
            htf_zones += [zone_out(z, ha, hk) for z in ha.zones if z.valid_at(hk)]
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
            if e.break_time >= first
        ]
        return {
            "tf": tf,
            "ready": True,
            "version": VERSION,
            "params_hash": p.digest(),
            "upto": _t(upto),
            "bars": len(a.bars),
            "trend": TREND_LABEL[a.trend[-1] if a.trend else 0],
            "zones": zones,
            "htf_zones": htf_zones,
            "events": events,
            "liquidity": liquidity(a, price),
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
        upto, ctx = self._context(needed_tfs(p) if p.trigger_tf in ORDER else list(ORDER))
        if upto is None:
            return {"ready": False}
        price = self._price()
        roles = {p.bias_tf: "Bias", p.confirm_bias_tf: "Confirmation", p.trigger_tf: "Trigger"}
        for tf in p.poi_tfs:
            roles[tf] = "POI" if tf not in roles else roles[tf] + " + POI"
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
        side = Side.LONG if bias == 1 else Side.SHORT if bias == -1 else None
        pois = []
        for tf in p.poi_tfs:
            a = ctx.get(tf)
            if a is None or side is None:
                continue
            k = len(a.bars) - 1
            for z in a.zones:
                if z.direction is side and z.valid_at(k):
                    dist = None
                    if price is not None:
                        edge = z.top if side is Side.LONG else z.bottom
                        dist = (
                            (price - edge) / price if side is Side.LONG else (edge - price) / price
                        )
                    pois.append(
                        {
                            **zone_out(z, a, k),
                            "distance": None if dist is None else _dec(round(dist, 6)),
                        }
                    )
        pois.sort(
            key=lambda z: abs(Decimal(z["distance"])) if z["distance"] is not None else Decimal(9)
        )
        trig = ctx.get(p.trigger_tf)
        recent = []
        if trig is not None:
            for e in trig.events[-12:]:
                s = evaluate(e, ctx, p, self.costs)
                recent.append(
                    {
                        "time": _t(s.created_at),
                        "kind": s.trigger_kind,
                        "direction": s.direction.value,
                        "accepted": s.accepted,
                        "reasons": [
                            {"code": r, "text": REASON_TEXT.get(r, r.replace("_", " ").title())}
                            for r in s.reasons
                        ],
                        "score": s.score,
                        "factors": s.factors,
                        "poi_tf": s.poi_tf,
                        "net_rr": None if s.net_rr is None else _dec(round(s.net_rr, 2)),
                    }
                )
        recent.reverse()
        return {
            "ready": True,
            "upto": _t(upto),
            "price": None if price is None else _dec(price),
            "bias": TREND_LABEL[bias],
            "timeframes": tfs,
            "pois": pois[:8],
            "triggers": recent,
            "factor_text": FACTOR_TEXT,
        }

    # ---- signals ------------------------------------------------------------------------
    def signals(
        self, *, active: bool | None, limit: int, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        where = ["symbol = :s"]
        if active is True:
            where.append("state IN ('PENDING', 'OPEN')")
        elif active is False:
            where.append("state NOT IN ('PENDING', 'OPEN')")
        if since is not None:
            where.append("(closed_at IS NULL OR closed_at >= :since)")
        out = rows(
            self.db,
            "SELECT id, key, side, state, created_at, entry, sl, tp, rr, net_rr, score, tp_source,"
            " trigger_kind, poi_tf, detail, qty, notional, leverage, filled_at, closed_at,"
            " exit_price, result_r, updated_at FROM smc_signals WHERE "
            + " AND ".join(where)
            + " ORDER BY created_at DESC LIMIT :n",
            s=self.symbol,
            n=limit,
            since=since,
        )
        price = self._price()
        for r in out:
            r["result_r"] = (
                None if r["result_r"] is None else _dec(round(Decimal(r["result_r"]), 2))
            )
            r["net_rr"] = None if r["net_rr"] is None else _dec(round(Decimal(r["net_rr"]), 2))
            r["rr"] = None if r["rr"] is None else _dec(round(Decimal(r["rr"]), 2))
            if r["state"] == "OPEN" and price is not None:
                sgn = 1 if r["side"] == "LONG" else -1
                risk = abs(Decimal(r["entry"]) - Decimal(r["sl"]))
                r["open_r"] = (
                    _dec(round((price - Decimal(r["entry"])) * sgn / risk, 2)) if risk else None
                )
        return out

    def signal_events(self, sid: int) -> list[dict[str, Any]]:
        return rows(
            self.db,
            "SELECT ts, kind, price, detail FROM smc_signal_events WHERE signal_id = :i"
            " ORDER BY id",
            i=sid,
        )

    def performance(self) -> dict[str, Any]:
        closed = rows(
            self.db,
            "SELECT closed_at, result_r, state, side FROM smc_signals WHERE symbol = :s"
            " AND result_r IS NOT NULL ORDER BY closed_at",
            s=self.symbol,
        )
        counts = {
            r["state"]: int(r["n"])
            for r in rows(
                self.db,
                "SELECT state, COUNT(*) AS n FROM smc_signals WHERE symbol = :s GROUP BY 1",
                s=self.symbol,
            )
        }
        return {"counts": counts, **summarize([Decimal(r["result_r"]) for r in closed], closed)}

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
                        "sl": _dec(t.sl),
                        "tp": _dec(t.tp),
                        "state": t.state.value,
                        "result_r": None if t.result_r is None else _dec(round(t.result_r, 2)),
                        "filled_at": None if t.filled_at is None else _t(t.filled_at),
                        "closed_at": None if t.closed_at is None else _t(t.closed_at),
                        "score": s.score,
                        "poi_tf": s.poi_tf,
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
                "status": "RUNNING" if age is not None and age < 30 else "STALE",
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
        p = self.params
        groups = {
            "Structure (every timeframe)": ["swing_len", "atr_len", "ob_lookback", "fvg_min_atr"],
            "Top-down model": ["bias_tf", "confirm_bias_tf", "poi_tfs", "trigger_tf"],
            "Entry / stop / target": [
                "entry_mode",
                "entry_on",
                "sl_mode",
                "sl_buffer_atr",
                "min_sl_atr",
                "max_risk_pct",
                "tp_mode",
                "min_net_rr",
                "tick",
            ],
            "Quality": ["min_score", "require"],
            "Lifecycle": ["pending_expiry_min", "max_hold_min", "max_active"],
            "Data": [f"lookback_{tf}" for tf in ORDER] + ["history_days"],
            "Advisory sizing": ["account_usdt", "risk_pct", "max_leverage"],
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
            "factors": FACTOR_TEXT,
            "reasons": REASON_TEXT,
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
