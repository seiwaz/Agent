"""SMC runner: the process that turns closed M1 bars into signals and tracks them.

One runner per market; all runners of the process share one simulated wallet (sizing at
entry, realized PnL at exit, `max_positions` across markets).

Loop (every couple of seconds):
1. keep the Tabdeal chart history current (full depth on start: no live warmup);
2. when a new final minute exists in the merged series: rebuild the timeframes whose bar
   closed, advance every PENDING/OPEN signal through the new M1 bars, then evaluate the
   fresh M1 triggers (break bar closed within the last `FRESH` window) and store accepted
   ones while capacity (`max_active`) allows;
3. write a heartbeat with the top-down status shown by the dashboard.

Triggers are never created retroactively: after downtime, only fresh ones can fire, while
signals that were already active resume from their last applied minute.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, text

from sp2l.smc import store
from sp2l.smc.context import ContextBuilder
from sp2l.smc.history import Fetch, ensure_history, load_bars, series_end
from sp2l.smc.lifecycle import Tracked, advance, risk_unit
from sp2l.smc.model import Analysis, Costs, SmcParams
from sp2l.smc.strategy import evaluate, trend_at
from sp2l.smc.timeframes import MINUTE
from sp2l.smc.wallet import Wallet

log = logging.getLogger("sp2l.smc")
FRESH = timedelta(minutes=3)
HISTORY_EVERY = timedelta(seconds=60)


def tf_status(ctx: dict[str, Analysis], upto: datetime) -> list[dict[str, Any]]:
    out = []
    for tf, a in ctx.items():
        last = a.events[-1] if a.events else None
        out.append(
            {
                "tf": tf,
                "trend": trend_at(a, upto),
                "bars": len(a.bars),
                "last_event": None
                if last is None
                else {
                    "kind": last.kind,
                    "direction": last.direction.value,
                    "level": str(last.level),
                    "time": last.break_time.isoformat(),
                },
            }
        )
    return out


class SmcRunner:
    def __init__(
        self,
        db: Engine,
        symbol: str,
        params: SmcParams,
        costs: Costs,
        fetch: Fetch | None,
        wallet: Wallet | None = None,
        qty_step: Decimal = Decimal("0.001"),
    ) -> None:
        self.db = db
        self.wallet = wallet
        self.qty_step = qty_step
        self.symbol = symbol
        self.params = params
        self.costs = costs
        self.fetch = fetch
        self.ctx = ContextBuilder(db, symbol, params)
        self.upto: datetime | None = None
        self.started = datetime.now(UTC)
        self.history: dict[str, Any] = {}
        self._history_at: datetime | None = None

    # ---- history -----------------------------------------------------------------------
    def refresh_history(self, now: datetime) -> None:
        if self.fetch is None:
            return
        if self._history_at is not None and now - self._history_at < HISTORY_EVERY:
            return
        if self._history_at is not None and now.second < 6:
            return  # let the minute settle on Tabdeal's side first
        self._history_at = now
        try:
            self.history = ensure_history(
                self.db, self.symbol, self.fetch, self.params.history_days, now
            )
        except Exception as e:  # network: the live canonical series keeps the engine going
            log.warning("history refresh failed: %r", e)
            self.history = {**self.history, "error": repr(e), "error_at": now.isoformat()}

    # ---- one step ----------------------------------------------------------------------
    def step(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        self.refresh_history(now)
        upto = series_end(self.db, self.symbol)
        if upto is None or (self.upto is not None and upto <= self.upto):
            self.heartbeat(now)
            return False
        ctx = self.ctx.build(upto)
        self.advance_active(upto)
        created = self.create_signals(ctx, upto)
        self.upto = upto
        self.heartbeat(now, ctx)
        if created:
            log.info("new signals: %s", created)
        return True

    def advance_active(self, upto: datetime) -> None:
        act = store.active(self.db, self.symbol)
        if not act:
            return
        since = min(t.last_m1 or (t.created_at - MINUTE) for _, t, _ in act)
        minutes = max(1, int((upto - since) / MINUTE))
        bars = load_bars(self.db, self.symbol, "1m", min(minutes, 60 * 24 * 30), upto)
        with self.db.begin() as c:
            for sid, t, qty in act:
                for b in bars:
                    if b.open_time < t.created_at:
                        continue
                    ev = advance(t, b, self.params, self.costs)
                    if ev is not None:
                        price = (
                            t.entry
                            if ev == "FILLED"
                            else t.sl
                            if ev == "BREAKEVEN"
                            else t.exit_price
                        )
                        detail: dict[str, Any] | None = None
                        if t.result_r is not None:
                            detail = {"result_r": str(t.result_r)}
                        if ev in ("TP", "SL", "TIMEOUT") and self.wallet is not None and qty > 0:
                            pnl = self.wallet.book_close(c, sid, self.symbol, t, qty, self.costs)
                            detail = {**(detail or {}), "pnl_usdt": str(pnl)}
                        store.event(c, sid, b.open_time + MINUTE, ev, price, detail)
                    if not t.active:
                        break
                store.save(c, sid, t)

    def create_signals(self, ctx: dict[str, Analysis], upto: datetime) -> list[str]:
        p = self.params
        m1 = ctx[p.trigger_tf]
        fresh = [
            e
            for e in m1.events
            if e.break_time + MINUTE >= upto - FRESH
            and e.break_time + MINUTE >= self.started - FRESH
        ]
        out: list[str] = []
        if not fresh:
            return out
        n_active = len(store.active(self.db, self.symbol))
        mkt = p.entry_mode == "market"
        with self.db.begin() as c:
            for e in fresh:
                bal = self.wallet.balance(c) if self.wallet is not None else None
                s = evaluate(e, ctx, p, self.costs, equity=bal)
                if not s.accepted or n_active >= p.max_active:
                    continue
                if self.wallet is not None and self.wallet.active_count(c) >= p.max_positions:
                    log.info(
                        "%s: %s skipped, all %d positions in use",
                        self.symbol,
                        s.key,
                        p.max_positions,
                    )
                    continue
                assert s.entry is not None and s.sl is not None and s.tp is not None
                t = Tracked(
                    key=s.key,
                    side=s.direction,
                    entry=s.entry,
                    sl=s.sl,
                    tp=s.tp,
                    created_at=s.created_at,
                    risk=risk_unit(s.direction, s.entry, s.sl, self.costs, market=mkt),
                    market=mkt,
                )
                size = None
                if self.wallet is not None:
                    sized = self.wallet.size(c, s.entry, t.risk, self.qty_step)
                    if isinstance(sized, str):
                        log.info("%s: %s not opened: %s", self.symbol, s.key, sized)
                        continue
                    size = sized
                wid = self.wallet.id if self.wallet is not None else None
                if store.insert_signal(c, self.symbol, s, t, p.digest(), size, wid):
                    n_active += 1
                    out.append(s.key)
        return out

    def heartbeat(self, now: datetime, ctx: dict[str, Analysis] | None = None) -> None:
        status: dict[str, Any] = {"history": self.history, "params_hash": self.params.digest()}
        if ctx is not None and self.upto is not None:
            status["timeframes"] = tf_status(ctx, self.upto)
        with self.db.begin() as c:
            if ctx is None:
                c.execute(
                    text(
                        "INSERT INTO smc_runner_state (symbol, started_at, heartbeat_at, status)"
                        " VALUES (:s, :st, :h, CAST(:j AS jsonb)) ON CONFLICT (symbol) DO UPDATE"
                        " SET heartbeat_at = :h, started_at = :st,"
                        " status = smc_runner_state.status || CAST(:j AS jsonb)"
                    ),
                    {"s": self.symbol, "st": self.started, "h": now, "j": json.dumps(status)},
                )
            else:
                c.execute(
                    text(
                        "INSERT INTO smc_runner_state (symbol, started_at, heartbeat_at, last_m1,"
                        " status) VALUES (:s, :st, :h, :m, CAST(:j AS jsonb))"
                        " ON CONFLICT (symbol) DO UPDATE SET heartbeat_at = :h, started_at = :st,"
                        " last_m1 = :m, status = CAST(:j AS jsonb)"
                    ),
                    {
                        "s": self.symbol,
                        "st": self.started,
                        "h": now,
                        "m": None if self.upto is None else self.upto - MINUTE,
                        "j": json.dumps(status),
                    },
                )


async def run_smc(runners: list[SmcRunner], stop: asyncio.Event, poll: float = 2.0) -> None:
    for r in runners:
        log.info(
            "SMC runner for %s (params %s): bias %s, POI %s, trigger %s",
            r.symbol,
            r.params.digest(),
            r.params.bias_tf,
            ",".join(r.params.poi_tfs),
            r.params.trigger_tf,
        )
        r.heartbeat(datetime.now(UTC))  # visible as "loading history" right away
    while not stop.is_set():
        for r in runners:
            if stop.is_set():
                break
            try:
                await asyncio.to_thread(r.step)
            except Exception:
                log.exception("SMC step failed for %s; retrying", r.symbol)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=poll)
