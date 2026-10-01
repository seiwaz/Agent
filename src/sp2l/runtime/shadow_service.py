"""Shadow runner: the canonical engine driven by the collector's market-event journal.

Runs as its own process, separate from the always-on collector (V5.5 B39):
- input = market_events in exact journal order (proven trades, M1 results incl.
  DATA_GAP/UNANCHORED, coverage GAPs), read after the persisted cursor;
- every event is applied atomically: all strategy rows it causes, the engine/setup
  checkpoints and the new cursor commit in ONE transaction, so a crash at any point replays
  the unapplied event exactly once;
- restart = restore the last checkpoint + replay the journal after its cursor. With complete
  coverage the result is identical to an uninterrupted run; no position is ever auto-closed.
  Coverage gaps overlapping live exposure are finalized AMBIGUOUS_DATA_GAP by the engine.
- a new session primes the M5 state from stored contiguous M5 bars and starts at the next
  M5 bucket boundary in the journal (setups are never simulated on history).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, text

from sp2l.config import RuntimeConfig
from sp2l.core.types import Candle
from sp2l.engine.checkpoint import load_m1
from sp2l.engine.symbol_engine import ShadowSymbolEngine
from sp2l.indicators.m5_state import DEFAULT_WARMUP
from sp2l.marketdata.m5_aggregator import M5_STEP, M5Result, M5Status
from sp2l.marketdata.tabdeal_rest import provisional_filters
from sp2l.persistence.market_store import MarketStore
from sp2l.persistence.shadow_recorder import PostgresShadowRecorder
from sp2l.spec.loader import Rules
from sp2l.strategy.risk.engine import CostModel, ExchangeFilters

log = logging.getLogger("sp2l.shadow")
PRIME_BARS = 600


def open_session(db: Engine, symbol: str, spec_sha: str) -> tuple[uuid.UUID, dict[str, Any] | None]:
    with db.connect() as c:
        row = c.execute(
            text(
                "SELECT s.id, e.engine, e.broker, e.input_seq FROM shadow_sessions s LEFT JOIN"
                " engine_checkpoints e ON e.session_id = s.id WHERE s.symbol = :sym AND"
                " s.ended_at IS NULL AND s.spec_sha256 = :h"
                " ORDER BY COALESCE(e.updated_at, s.started_at) DESC LIMIT 1"
            ),
            {"sym": symbol, "h": spec_sha},
        ).first()
    if row is None or row[1] is None:
        return uuid.uuid4(), None
    return row[0], {"engine": row[1], "broker": row[2], "input_seq": int(row[3])}


def load_m5(db: Engine, symbol: str, start: datetime, end: datetime) -> list[M5Result]:
    """Stored M5 bars in [start, end] as engine inputs (with B33 synthetic counts)."""
    with db.connect() as c:
        rows = c.execute(
            text(
                "SELECT open_time, open, high, low, close, volume, trade_count,"
                " synthetic_m1_count, repaired_m1_count, history_m1_count FROM candles_5m"
                " WHERE symbol = :s AND open_time >= :a AND open_time <= :b ORDER BY open_time"
            ),
            {"s": symbol, "a": start, "b": end},
        ).all()
    out = []
    for r in rows:
        t = r[0].astimezone(UTC)
        n = None if r[6] is None else int(r[6])  # V5.8: NULL = UNKNOWN (chart history)
        c5 = Candle(t, r[1], r[2], r[3], r[4], r[5], n, synthetic=int(r[7]) == 5)
        out.append(M5Result(t, M5Status.OK, c5, int(r[7]), int(r[8]), int(r[9])))
    return out


def _contiguous_tail(bars: list[M5Result], end_open: datetime) -> list[M5Result]:
    """The maximal run of consecutive bars ending exactly at `end_open`."""
    by_t = {b.open_time: b for b in bars}
    run: list[M5Result] = []
    t = end_open
    while t in by_t:
        run.append(by_t[t])
        t -= M5_STEP
    return list(reversed(run))


class ShadowRunner:
    def __init__(
        self,
        db: Engine,
        engine: ShadowSymbolEngine,
        recorder: PostgresShadowRecorder,
        symbol: str,
        cursor: int,
    ) -> None:
        self.db = db
        self.engine = engine
        self.recorder = recorder
        self.symbol = symbol
        self.cursor = cursor
        self._decisions: list[str] = []  # setup event kinds emitted by the current input
        record = recorder.on_setup_event

        def observe(machine: Any, kind: str, event: dict[str, Any]) -> Any:
            self._decisions.append(kind)
            return record(machine, kind, event)

        recorder.on_setup_event = observe  # type: ignore[method-assign,assignment]

    @classmethod
    def open(
        cls,
        db: Engine,
        store: MarketStore,
        cfg: RuntimeConfig,
        rules: Rules,
        *,
        costs: CostModel,
        filters: ExchangeFilters,
        start_cursor: int | None = None,
    ) -> ShadowRunner:
        """start_cursor (new sessions only): replay the journal from this seq without priming
        (verification / backtests over the journal); always a new session. Default: resume the
        open session, or prime a new one and start at now."""
        if start_cursor is not None:
            session, ckpt = uuid.uuid4(), None
        else:
            session, ckpt = open_session(db, cfg.symbol, rules.sha256)
        recorder = PostgresShadowRecorder(
            db,
            store,
            symbol=cfg.symbol,
            spec_version=rules.version,
            spec_sha256=rules.sha256,
            session_id=session,
            started_at=datetime.now(UTC),
            resume=ckpt is not None,
            write_market=False,
        )
        engine = ShadowSymbolEngine(
            cfg.symbol,
            leverage=rules.leverage,  # V5.8: the strategy's 10x, never the account's
            tick=filters.tick,
            costs=costs,
            filters=filters,
            warmup_bars=int(cfg.section("shadow").get("warmup_m5_bars", DEFAULT_WARMUP)),
            recorder=recorder,
            session_id=str(session),
        )
        if ckpt is not None:
            st = ckpt["engine"]
            seg: list[M5Result] = []
            if st.get("m5_anchor"):
                seg = load_m5(
                    db,
                    cfg.symbol,
                    datetime.fromisoformat(st["m5_anchor"]),
                    datetime.fromisoformat(st["m5_last_open"]),
                )
            engine.restore(st, ckpt["broker"], seg)
            cursor = ckpt["input_seq"]
            log.warning(
                "shadow session %s restored at journal seq %s; replaying from there "
                "(no automatic close, B39)",
                session,
                cursor,
            )
        elif start_cursor is not None:
            cursor = start_cursor
        else:
            cursor = cls._prime_new(db, engine, cfg.symbol)
            log.info("shadow session %s started at journal seq %s", session, cursor)
        return cls(db, engine, recorder, cfg.symbol, cursor)

    @staticmethod
    def _prime_new(db: Engine, engine: ShadowSymbolEngine, symbol: str) -> int:
        with db.connect() as c:
            rows = c.execute(
                text(
                    "SELECT seq, payload->>'t' FROM market_events WHERE symbol = :s AND"
                    " kind = 'M1' ORDER BY seq DESC LIMIT 400"
                ),
                {"s": symbol},
            ).all()
            top: int = c.execute(
                text("SELECT COALESCE(MAX(seq), 0) FROM market_events WHERE symbol = :s"),
                {"s": symbol},
            ).scalar_one()
        for seq, t in rows:  # newest bucket start present in the journal
            ot = datetime.fromisoformat(t)
            if ot.minute % 5 == 0:
                start_seq = int(seq) - 1
                prev_end = ot - M5_STEP
                hist = load_m5(db, symbol, prev_end - M5_STEP * PRIME_BARS, prev_end)
                engine.prime(_contiguous_tail(hist, prev_end), ot - timedelta(minutes=1))
                with db.connect() as c:  # start right after the event preceding that M1
                    prior: int = c.execute(
                        text(
                            "SELECT COALESCE(MAX(seq), 0) FROM market_events WHERE"
                            " symbol = :s AND seq < :q AND kind = 'M1'"
                        ),
                        {"s": symbol, "q": seq},
                    ).scalar_one()
                return int(prior) if prior else start_seq
        return int(top)

    def apply(self, kind: str, ts: datetime, p: dict[str, Any]) -> None:
        if kind == "TRADE":
            # V5.8: outside a gap only WS trades drive the engine; inside one every canonical
            # trade is buffered for causal replay if the gap is repaired exactly
            self.engine.feed_trade(
                Decimal(p["price"]), datetime.fromisoformat(p["exch_ts"]), p.get("source", "WS")
            )
        elif kind == "M1":
            self._decisions = []
            m1 = load_m1(p)
            self.engine.on_m1(m1)
            self._record_latency(m1.open_time, ts)
        elif kind == "GAP":
            self.engine.on_gap(datetime.fromisoformat(p["start"]), None, ts, p["reason"])

    def _record_latency(self, minute: datetime, finalized: datetime) -> None:
        """finalized_to_strategy_eval_ms (and _to_e1_decision_ms when this minute's close
        submitted an E1): measured from the M1 journal event, i.e. the canonical finalization."""
        now = datetime.now(UTC)
        if now - finalized > timedelta(minutes=5):
            return  # journal catch-up after a restart is not live latency
        ms = (now - finalized).total_seconds() * 1000
        rows = [{"s": self.symbol, "m": minute, "st": "strategy_eval", "ms": ms}]
        if "E1_SUBMITTED" in self._decisions:
            rows.append({"s": self.symbol, "m": minute, "st": "e1_decision", "ms": ms})
        with self.db.begin() as c:
            c.execute(
                text(
                    "INSERT INTO pipeline_latency (symbol, minute, stage, ms)"
                    " VALUES (:s, :m, :st, :ms)"
                ),
                rows,
            )

    def try_reheal(self) -> bool:
        """While warming up, pick up a break healed later from validated history (collector
        tier 3 / history backfill) from the stored canonical M5 series."""
        eng = self.engine
        last = eng.m5.last
        if eng.m5.warm or eng.active is not None or last is None:
            return False
        hist = load_m5(self.db, self.symbol, last.open_time - M5_STEP * PRIME_BARS, last.open_time)
        if not eng.reheal(_contiguous_tail(hist, last.open_time)):
            return False
        seg = eng.m5.segment
        log.warning(
            "M5 continuity healed from stored history: %s bars since %s; warmup complete",
            len(seg.bars) if seg else 0,
            seg.anchor_open_time.isoformat() if seg else None,
        )
        return True

    def step(self, limit: int = 2000) -> int:
        with self.db.connect() as c:
            rows = c.execute(
                text(
                    "SELECT seq, kind, ts, payload FROM market_events WHERE symbol = :s AND"
                    " seq > :q ORDER BY seq LIMIT :n"
                ),
                {"s": self.symbol, "q": self.cursor, "n": limit},
            ).all()
        for seq, kind, ts, payload in rows:
            self.recorder.begin_input()
            try:
                self.apply(kind, ts.astimezone(UTC), payload)
                self.recorder.commit_input(self.engine, int(seq))
            except BaseException:
                self.recorder.abort_input()
                raise
            self.cursor = int(seq)
        return len(rows)

    async def run(self, stop: asyncio.Event, poll: float = 0.25) -> None:
        last_log = 0.0
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            n = self.step()
            if loop.time() - last_log > 60:
                last_log = loop.time()
                if n == 0:  # caught up with the journal
                    try:
                        self.try_reheal()
                    except Exception:  # never stops the runner; retried next minute
                        log.exception("M5 continuity heal check failed")
                log.info("shadow cursor=%s %s", self.cursor, self.engine.summary())
            if n == 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=poll)


async def run_shadow(db: Engine, cfg: RuntimeConfig, rules: Rules, stop: asyncio.Event) -> None:
    costs = cfg.shadow_costs()
    filters = provisional_filters(cfg.symbol)
    log.warning(
        "exchange filters are PROVISIONAL (B26): tick=%s step=%s", filters.tick, filters.step
    )
    store = MarketStore(db, cfg.symbol)
    runner = ShadowRunner.open(db, store, cfg, rules, costs=costs, filters=filters)
    try:
        await runner.run(stop)
    finally:
        log.info("shadow stopped at cursor %s: %s", runner.cursor, runner.engine.summary())
