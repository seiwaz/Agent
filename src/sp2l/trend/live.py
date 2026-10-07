"""System B live (paper): the backtest engine replayed over the stored history.

No orders are ever sent. The paper wallet is defined as the backtest of the configured
parameters from `paper_start` on, with `account_usdt`: bars before that day only warm the
indicators up. Each time a UTC day closes, the service replays the engine over the daily bars
(aggregated from the merged 1-minute series, UTC grid) and records what it decided on that
close in `trend_journal`. Live and backtest therefore share one code path
(`sp2l.trend.backtest.run`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text

from sp2l.core.types import Candle
from sp2l.trend import backtest as tb
from sp2l.trend.model import VERSION, TrendParams

log = logging.getLogger("sp2l.trend")
DAY = timedelta(days=1)
RETENTION_DAYS = 290  # Tabdeal keeps ~296 days of 1-minute chart history (2026-10-05)
ACTIONS = {"entry": "BUY", "exit": "SELL", "add": "ADD"}


@dataclass(frozen=True, slots=True)
class LiveConfig:
    symbol: str
    paper_start: date  # first day a paper entry may fill (at its open)
    account_usdt: float
    fee: float  # per fill
    slippage: float  # per fill

    @classmethod
    def from_mapping(cls, m: dict[str, Any], fee: float, slippage: float) -> LiveConfig:
        known = {"symbol", "paper_start", "account_usdt"}
        unknown = sorted(set(m) - known)
        if unknown:
            raise ValueError(f"trend_live: unknown key(s) {', '.join(unknown)}")
        start = m.get("paper_start")
        if start is None:
            raise ValueError("trend_live.paper_start is required (YYYY-MM-DD)")
        return cls(
            str(m.get("symbol", "BTCUSDT")),
            start if isinstance(start, date) else date.fromisoformat(str(start)),
            float(m.get("account_usdt", 100)),
            fee,
            slippage,
        )


def from_runtime(rc: Any) -> tuple[TrendParams, LiveConfig]:
    """The `trend` (strategy) and `trend_live` (paper wallet) sections of a RuntimeConfig.
    Every fill pays `costs.taker_fee` and `costs.slippage_allowance` (market orders)."""
    costs = rc.costs()
    p = TrendParams.from_mapping(dict(rc.section("trend")))
    live = LiveConfig.from_mapping(
        dict(rc.section("trend_live")), float(costs.taker_fee), float(costs.slippage)
    )
    return p, live


def _start(cfg: LiveConfig) -> datetime:
    return datetime(cfg.paper_start.year, cfg.paper_start.month, cfg.paper_start.day, tzinfo=UTC)


def bars_needed(cfg: LiveConfig, p: TrendParams, upto: datetime) -> int:
    """Daily bars from the warmup before `paper_start` to `upto`."""
    return max(0, (upto - _start(cfg)).days) + p.warmup + 5


def load_daily(db: Engine, cfg: LiveConfig, p: TrendParams, upto: datetime) -> list[Candle]:
    from sp2l.smc.history import load_bars

    return load_bars(db, cfg.symbol, "1d", bars_needed(cfg, p, upto), upto, grid="utc")


def state(bars: list[Candle], p: TrendParams, cfg: LiveConfig) -> dict[str, Any]:
    """The paper wallet and the engine's decision after the last closed day."""
    p = replace(p, initial_equity=cfg.account_usdt)
    start = _start(cfg)
    if len(bars) <= p.warmup + 1:
        return {"ready": False, "reason": f"{len(bars)} daily bars; {p.warmup + 2} needed"}
    r = tb.run(bars, p, tb.Fees(cfg.fee, cfg.slippage), trade_from=start)
    h = [float(b.high) for b in bars]
    lo = [float(b.low) for b in bars]
    c = [float(b.close) for b in bars]
    atr = tb.wilder_atr(h, lo, c, p.atr_len)[-1]
    last = bars[-1]
    entry_level = max(h[-p.entry_len :])  # tomorrow's close must exceed it to buy
    exit_level = min(lo[-p.exit_len :])  # tomorrow's close below it sells
    eq = r.equity[-1]
    i0 = next((i for i, b in enumerate(bars) if b.open_time >= start), len(bars))
    action = ACTIONS.get(r.pending or "", "HOLD" if r.qty > 0 else "WAIT")
    nxt: dict[str, Any] | None = None
    if r.pending == "entry":
        est_qty = eq * p.risk_pct / (p.stop_atr * r.pending_atr)
        est_qty = min(est_qty, p.max_exposure * eq / (c[-1] * (1 + cfg.fee)))
        nxt = {
            "action": "BUY",
            "at": (last.open_time + DAY).isoformat(),  # the open right after that close
            "est_qty": est_qty,
            "est_notional": est_qty * c[-1],
            "est_stop": c[-1] - p.stop_atr * r.pending_atr,
            "risk_usdt": eq * p.risk_pct,
        }
    elif r.pending == "exit":
        nxt = {"action": "SELL", "at": (last.open_time + DAY).isoformat(), "qty": r.qty}
    open_trade = next((t for t in r.trades if t.reason == "OPEN"), None)
    position = None
    if open_trade is not None:
        position = {
            "entry_day": open_trade.entry_time.date().isoformat(),
            "entry_price": open_trade.entry_price,
            "qty": r.qty,
            "units": r.units,
            "stop": r.stop,
            "notional": r.qty * c[-1],
            "unrealized_usdt": open_trade.pnl,
            "r": open_trade.r,
            "days": open_trade.bars,
        }
    closed = [t for t in r.trades if t.reason != "OPEN" and t.entry_time >= start]
    curve = list(zip(r.times[i0:], r.equity[i0:], strict=True))
    return {
        "ready": True,
        "version": VERSION,
        "symbol": cfg.symbol,
        "params": p.as_dict(),
        "params_hash": p.digest(),
        "paper_start": cfg.paper_start.isoformat(),
        "fees": {"fee": cfg.fee, "slippage": cfg.slippage},
        "as_of": last.open_time.date().isoformat(),  # the last closed UTC day
        "close": c[-1],
        "atr": None if math.isnan(atr) else atr,
        "entry_level": entry_level,
        "exit_level": exit_level,
        "action": action,
        "next": nxt,
        "position": position,
        "equity": eq,
        "account_usdt": cfg.account_usdt,
        "return_pct": 100 * (eq / cfg.account_usdt - 1),
        "trades": [t.as_dict() for t in closed],
        "trade_stats": tb.trade_stats(closed),
        "curve": [{"day": t.date().isoformat(), "equity": round(e, 6)} for t, e in curve],
        "stats": tb.curve_stats([t for t, _ in curve], [e for _, e in curve])
        if len(curve) > 1
        else {},
    }


def journal_row(s: dict[str, Any]) -> dict[str, Any]:
    pos = s.get("position") or {}
    return {
        "symbol": s["symbol"],
        "day": date.fromisoformat(s["as_of"]),
        "params_hash": s["params_hash"],
        "action": s["action"],
        "close": s["close"],
        "entry_level": s["entry_level"],
        "exit_level": s["exit_level"],
        "atr": s["atr"],
        "stop": pos.get("stop"),
        "position_qty": pos.get("qty", 0.0),
        "equity": s["equity"],
        "details": json.dumps({"next": s["next"], "position": s["position"]}),
    }


def record(db: Engine, s: dict[str, Any]) -> bool:
    """Journal the decision of the last closed day; False if that day is already recorded."""
    with db.begin() as c:
        n = c.execute(
            text(
                "INSERT INTO trend_journal (symbol, day, params_hash, action, close, entry_level,"
                " exit_level, atr, stop, position_qty, equity, details) VALUES (:symbol, :day,"
                " :params_hash, :action, :close, :entry_level, :exit_level, :atr, :stop,"
                " :position_qty, :equity, CAST(:details AS jsonb))"
                " ON CONFLICT (symbol, day) DO NOTHING"
            ),
            journal_row(s),
        ).rowcount
    return bool(n)


def journal(db: Engine, symbol: str, limit: int = 60) -> list[dict[str, Any]]:
    with db.connect() as c:
        rows = c.execute(
            text(
                "SELECT day, action, close, entry_level, exit_level, atr, stop, position_qty,"
                " equity, params_hash, details, recorded_at FROM trend_journal"
                " WHERE symbol = :s ORDER BY day DESC LIMIT :n"
            ),
            {"s": symbol, "n": limit},
        ).mappings()
        out = []
        for r in rows:
            d = dict(r)
            for k in (
                "close",
                "entry_level",
                "exit_level",
                "atr",
                "stop",
                "position_qty",
                "equity",
            ):
                d[k] = None if d[k] is None else float(d[k])
            d["day"] = d["day"].isoformat()
            d["recorded_at"] = d["recorded_at"].isoformat()
            out.append(d)
    return out


def current(db: Engine, p: TrendParams, cfg: LiveConfig) -> dict[str, Any]:
    """State from the stored history (no exchange request): the API's view."""
    from sp2l.smc.history import series_end

    upto = series_end(db, cfg.symbol)
    if upto is None:
        return {"ready": False, "reason": f"no stored history for {cfg.symbol}"}
    return state(load_daily(db, cfg, p, upto), p, cfg)


async def run_trend(
    db: Engine,
    p: TrendParams,
    cfg: LiveConfig,
    fetch: Any,
    stop: asyncio.Event,
    every_s: float = 60.0,
) -> None:
    """Keep the history current; journal each newly closed UTC day once."""
    from sp2l.smc.history import ensure_history

    log.info(
        "trend %s (%s %s) paper from %s, %s USDT",
        cfg.symbol,
        VERSION,
        replace(p, initial_equity=cfg.account_usdt).digest(),
        cfg.paper_start,
        cfg.account_usdt,
    )
    while not stop.is_set():
        try:
            now = datetime.now(UTC)
            days = min(RETENTION_DAYS, bars_needed(cfg, p, now) + 1)
            try:
                await asyncio.to_thread(ensure_history, db, cfg.symbol, fetch, days)
            except Exception:  # exchange unreachable: decide on what is stored
                log.warning("trend: history top-up failed; using the stored history", exc_info=True)
            s = await asyncio.to_thread(current, db, p, cfg)
            if s.get("ready") and await asyncio.to_thread(record, db, s):
                log.info(
                    "trend %s %s: %s close %.2f equity %.2f next %s",
                    cfg.symbol,
                    s["as_of"],
                    s["action"],
                    s["close"],
                    s["equity"],
                    s["next"],
                )
        except Exception:  # keep the service alive; the next tick retries
            log.exception("trend tick failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every_s)
