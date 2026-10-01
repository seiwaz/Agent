"""Postgres recorder for the Shadow engine: continuous persistence (DATA-02/03, SHD-06).

Every material setup event is written as it happens, in one transaction together with the
setup and engine checkpoints, so a crash never loses in-progress state:
- STATE -> candidate_transitions (the terminal one carries the reasons);
- EVALUATION -> one Context and one Exhaustion snapshot (raw values, booleans, reasons,
  thresholds, exact strings, candle ids);
- E1_SUBMITTED -> e1_revisions (with risk figures) + orders; E2_SUBMITTED -> orders;
- ORDER_UPDATE -> orders (current status) + order_events (history);
- FILL -> fills + ledger fee; EXIT -> ledger PnL/fee; POSITION -> position_snapshots;
- every event (incl. PULLBACK_START, FILL_WINDOW, PROTECTION_*, E2_*, EMERGENCY_CLOSE,
  RESTART_RECONCILE, BREAKOUT_LEVEL) -> strategy_events.
Checkpoints (setup_checkpoints, engine_checkpoints) hold the exact serialized state used by
restart recovery. Counterfactual outcomes are written to schema cf once terminal/AMBIGUOUS.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from enum import Enum
from fractions import Fraction
from typing import Any

from sqlalchemy import Connection, Engine, text

from sp2l.core.types import Side
from sp2l.counterfactual.simulator import (
    SIMULATOR_VERSION,  # noqa: E402,F401  (re-export)
    CounterfactualRun,
)
from sp2l.engine.setup_machine import SetupMachine
from sp2l.engine.symbol_engine import PGapLog, ShadowSymbolEngine
from sp2l.execution.shadow_broker import ShadowBroker
from sp2l.marketdata.m1_builder import MINUTE, M1Result
from sp2l.marketdata.m5_aggregator import M5Result
from sp2l.persistence.market_store import MarketStore
from sp2l.strategy.context import engine as ctx_engine
from sp2l.strategy.context.engine import ContextSnapshot
from sp2l.strategy.exhaustion import engine as exh_engine
from sp2l.strategy.exhaustion.engine import ExhaustionSnapshot
from sp2l.strategy.risk.engine import STRATEGY_LEVERAGE

CONTEXT_THRESHOLDS = {
    # V6.0 gate: NetTP AND (LevelBreak OR ChannelEdge OR HTFAligned)
    "rule": "net_tp > 0 AND (level_break OR channel_edge OR htf_aligned)",
    "net_tp": "|TP - E1| - E1*entry_fee_rate - TP*exit_fee_rate > 0",
    "range_bars": ctx_engine.RANGE_BARS,
    "channel_edge": ["<= 1/3 (Long, OriginLow)", ">= 2/3 (Short, OriginHigh)"],
    # informational only since V6.0 (never a reason)
    "info_room_to_tp_min_r": str(ctx_engine.ROOM_MIN_R),
    "info_liquidity_lookback": ctx_engine.LIQ_LOOKBACK,
    "info_liquidity_ratio_below": str(ctx_engine.LIQ_RATIO),
    "info_regime_range": "CHOP14 >= 61.8 AND ADX14 < 20",
    "info_regime_trend": "CHOP14 <= 38.2 OR ADX14 >= 25",
}
EXHAUSTION_THRESHOLDS = {
    "trend_age_bars_gte": exh_engine.TREND_AGE_MIN,
    "microchannel_len_gte": exh_engine.MICROCHANNEL_MIN,
    "stretch_atr_gte": str(exh_engine.STRETCH_MIN),
    "spike_atr_gte": str(exh_engine.SPIKE_ATR_MIN),
    "rp20_long_gte": str(exh_engine.RP20_LONG_MIN),
    "rp20_short_lte": str(exh_engine.RP20_SHORT_MAX),
    "opposing_swing_atr_lte": str(exh_engine.OPPOSING_MAX),
    "fresh_breakout_max_age_m5": exh_engine.FRESH_MAX_AGE,
}


def _num(v: Any) -> Any:
    """Column value: Fractions become 40-digit Decimals, enums their value."""
    if isinstance(v, Fraction):
        with localcontext() as ctx:
            ctx.prec = 40
            return Decimal(v.numerator) / Decimal(v.denominator)
    if isinstance(v, Enum):
        return v.value
    return v


def _exact(v: Any) -> Any:
    if isinstance(v, Fraction | Decimal):
        return str(v)
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, list):
        return [_exact(x) for x in v]
    return v


class PostgresShadowRecorder:
    def __init__(
        self,
        engine: Engine,
        market: MarketStore,
        *,
        symbol: str,
        spec_version: str,
        spec_sha256: str,
        session_id: uuid.UUID,
        started_at: datetime,
        initial_wallet: Decimal = Decimal(100),
        resume: bool = False,
        write_market: bool = True,
    ) -> None:
        self.engine = engine
        self.write_market = write_market  # False when the collector owns candle writes
        self._batch: Connection | None = None
        self._batch_tx: Any = None
        self._ckpt_dirty = False
        self.input_seq = 0
        self.market = market
        self.symbol = symbol
        self.spec_version = spec_version
        self.spec_sha = spec_sha256
        self.session_id = session_id
        self.balance = initial_wallet
        self._pgap_by_right: dict[datetime, int] = {}
        self._snap_ids: dict[tuple[str, int], tuple[int, int]] = {}
        self._ckpt_seq = 0
        if resume:
            with engine.connect() as c:
                bal: Decimal = c.execute(
                    text(
                        "SELECT balance_after FROM shadow_wallet_ledger WHERE session_id = :s"
                        " ORDER BY id DESC LIMIT 1"
                    ),
                    {"s": session_id},
                ).scalar_one()
                seq = c.execute(
                    text("SELECT seq FROM engine_checkpoints WHERE session_id = :s"),
                    {"s": session_id},
                ).scalar_one_or_none()
            self.balance = Decimal(bal)
            self._ckpt_seq = int(seq or 0)
            with engine.connect() as c:
                cur = c.execute(
                    text("SELECT input_seq FROM engine_checkpoints WHERE session_id = :s"),
                    {"s": session_id},
                ).scalar_one_or_none()
            self.input_seq = int(cur or 0)
            return
        with engine.begin() as c:
            # exactly one open Shadow session per symbol: a new one supersedes older ones
            c.execute(
                text(
                    "UPDATE shadow_sessions SET ended_at = :t WHERE symbol = :s"
                    " AND ended_at IS NULL"
                ),
                {"t": started_at, "s": symbol},
            )
            c.execute(
                text(
                    "INSERT INTO spec_versions (sha256, version) VALUES (:h, :v)"
                    " ON CONFLICT DO NOTHING"
                ),
                {"h": spec_sha256, "v": spec_version},
            )
            c.execute(
                text(
                    "INSERT INTO shadow_sessions (id, symbol, spec_sha256, initial_wallet_usdt,"
                    " started_at) VALUES (:id, :s, :h, :w, :t)"
                ),
                {
                    "id": session_id,
                    "s": symbol,
                    "h": spec_sha256,
                    "w": initial_wallet,
                    "t": started_at,
                },
            )
            self._ledger(c, None, started_at, "INITIAL", initial_wallet, set_balance=True)

    # ---- market data --------------------------------------------------------------------

    def on_m1(self, m1: M1Result) -> None:
        if self.write_market:
            self.market.upsert_m1(m1)

    def on_m5(self, m5: M5Result) -> None:
        if self.write_market:
            self.market.upsert_m5(m5)

    def on_indicators(self, snapshot: dict[str, Any]) -> None:
        """Display snapshot per finalized M5 bar (idempotent under journal replay)."""
        with self._tx() as c:
            c.execute(
                text(
                    "INSERT INTO indicator_snapshots (session_id, symbol, open_time, snapshot)"
                    " VALUES (:s, :sym, :t, CAST(:j AS jsonb))"
                    " ON CONFLICT (session_id, open_time) DO NOTHING"
                ),
                {
                    "s": self.session_id,
                    "sym": self.symbol,
                    "t": snapshot["open_time"],
                    "j": json.dumps(snapshot),
                },
            )

    # ---- per-input atomicity (B39) -----------------------------------------------------------

    @contextmanager
    def _tx(self) -> Iterator[Connection]:
        if self._batch is not None:
            self._ckpt_dirty = True  # this input writes: commit it with a checkpoint
            yield self._batch
        else:
            with self.engine.begin() as c:
                yield c

    def begin_input(self) -> None:
        """Everything one market event causes is written in one transaction."""
        conn = self.engine.connect()
        self._batch_tx = conn.begin()
        self._batch = conn
        self._ckpt_dirty = False

    def commit_input(self, eng: ShadowSymbolEngine, input_seq: int) -> None:
        """Commit this input's rows + checkpoint + cursor atomically. An input that wrote
        nothing only changed in-memory state, which replay from the previous checkpoint
        reproduces deterministically, so no checkpoint is needed for it."""
        assert self._batch is not None
        if self._ckpt_dirty:
            self.input_seq = input_seq
            self._write_checkpoint(self._batch, eng)
            self._batch_tx.commit()
        else:
            self._batch_tx.rollback()
        self._batch.close()
        self._batch = None

    def abort_input(self) -> None:
        if self._batch is not None:
            self._batch_tx.rollback()
            self._batch.close()
            self._batch = None

    # ---- strategy -----------------------------------------------------------------------

    def on_pgap(self, log: PGapLog) -> Any:
        ids = [
            self.market.candle_id("1m", t)
            for t in (log.left_open_time, log.middle_open_time, log.right_open_time)
        ]
        if any(i is None for i in ids):
            return None  # candles not persisted (should not happen when on_m1 ran first)
        q: dict[str, Any] = log.quality or {}
        with self._tx() as c:
            row = c.execute(
                text(
                    "INSERT INTO pgaps (symbol, side, left_candle_id, middle_candle_id,"
                    " right_candle_id, confirmed_at, promoted, not_promoted_reason,"
                    " spec_version, impulse_body, impulse_range, impulse_upper_shadow,"
                    " impulse_lower_shadow, impulse_body_ratio, impulse_direction, gap_size,"
                    " gap_body_ratio, gap_size_ticks, strong_impulse_pass, strong_gap_pass,"
                    " final_pgap_pass, failure_reasons, quality)"
                    " VALUES (:s, :side, :l, :m, :r, :t, :p, :why, :sv, :ib, :ir, :iu, :il,"
                    " :ibr, :idir, :gs, :gbr, :gt, :sip, :sgp, :fp, :fr, CAST(:qj AS jsonb))"
                    " RETURNING id"
                ),
                {
                    "s": self.symbol,
                    "side": log.side,
                    "l": ids[0],
                    "m": ids[1],
                    "r": ids[2],
                    "t": log.ts,
                    "p": log.promoted,
                    "why": log.reason,
                    "sv": self.spec_version,
                    "ib": q.get("impulse_body"),
                    "ir": q.get("impulse_range"),
                    "iu": q.get("impulse_upper_shadow"),
                    "il": q.get("impulse_lower_shadow"),
                    "ibr": q.get("impulse_body_ratio"),
                    "idir": q.get("impulse_direction"),
                    "gs": q.get("gap_size"),
                    "gbr": q.get("gap_body_ratio"),
                    "gt": q.get("gap_size_ticks"),
                    "sip": q.get("strong_impulse_pass"),
                    "sgp": q.get("strong_gap_pass"),
                    "fp": q.get("final_pgap_pass"),
                    "fr": q.get("failure_reasons"),
                    "qj": json.dumps(q) if q else None,
                },
            ).one()
        return int(row[0])

    def on_candidate_created(self, machine: SetupMachine, pgap_id: Any) -> None:
        if pgap_id is None or machine.candidate_id is None:
            return
        origin = machine.spike.origin
        origin_id = self.market.candle_id("1m", origin.open_time)
        assert machine.created_at is not None and origin_id is not None
        with self._tx() as c:
            spike_id: int = c.execute(
                text(
                    "INSERT INTO spikes (symbol, side, pgap_id, origin_candle_id, origin_low,"
                    " origin_high, confirmed_at) VALUES (:s, :side, :p, :o, :lo, :hi, :t)"
                    " RETURNING id"
                ),
                {
                    "s": self.symbol,
                    "side": machine.side.value,
                    "p": pgap_id,
                    "o": origin_id,
                    "lo": origin.low,
                    "hi": origin.high,
                    "t": machine.created_at,
                },
            ).scalar_one()
            c.execute(
                text(
                    "INSERT INTO candidates (id, symbol, side, mode, spike_id, spec_version,"
                    " spec_sha256, created_at, setup_key) VALUES (:id, :s, :side, 'SHADOW',"
                    " :sp, :v, :h, :t, :k)"
                ),
                {
                    "id": machine.candidate_id,
                    "s": self.symbol,
                    "side": machine.side.value,
                    "sp": spike_id,
                    "v": self.spec_version,
                    "h": self.spec_sha,
                    "t": machine.created_at,
                    "k": machine.cfg.setup_id,
                },
            )

    def on_archived(self, m: SetupMachine, broker: ShadowBroker) -> None:
        """Everything was already persisted per event; nothing is left to write."""

    def on_setup_event(self, m: SetupMachine, kind: str, event: dict[str, Any]) -> None:
        cid = m.candidate_id
        if cid is None:
            return
        with self._tx() as c:
            ts = datetime.fromisoformat(event["ts"])
            if kind == "STATE":
                seq = len(m.transitions) - 1
                t_ts, frm, to = m.transitions[-1]
                terminal = m.terminal
                c.execute(
                    text(
                        "INSERT INTO candidate_transitions (candidate_id, seq, state_from,"
                        " state_to, primary_reason, reasons, ts) VALUES (:c, :q, :f, :t, :pr,"
                        " :rs, :ts)"
                    ),
                    {
                        "c": cid,
                        "q": seq,
                        "f": frm.value if frm else None,
                        "t": to.value,
                        "pr": m.primary_reason if terminal else None,
                        "rs": m.reasons if terminal else [],
                        "ts": t_ts,
                    },
                )
            elif kind == "EVALUATION" and m.evaluations:
                seq, eval_time, res = m.evaluations[-1]
                if res.context is not None and res.exhaustion is not None:
                    m1_id = self.market.candle_id("1m", eval_time - MINUTE)
                    ids = (
                        self._insert_snapshot(
                            c,
                            "context_snapshots",
                            res.context,
                            m,
                            seq,
                            eval_time,
                            m1_id,
                            CONTEXT_THRESHOLDS,
                        ),
                        self._insert_snapshot(
                            c,
                            "exhaustion_snapshots",
                            res.exhaustion,
                            m,
                            seq,
                            eval_time,
                            m1_id,
                            EXHAUSTION_THRESHOLDS,
                        ),
                    )
                    self._snap_ids[(cid, seq)] = ids
            elif kind == "E1_SUBMITTED":
                self._revision(c, m)
                self._order_created(c, m)
            elif kind == "E2_SUBMITTED":
                self._order_created(c, m)
            elif kind == "ORDER_UPDATE":
                c.execute(
                    text(
                        "UPDATE orders SET status = :st, executed_qty = :ex, updated_at = :t"
                        " WHERE client_order_id = :cid"
                    ),
                    {
                        "st": event["status"],
                        "ex": Decimal(event["executed"]),
                        "t": ts,
                        "cid": self._order_key(m, event["cid"]),
                    },
                )
                c.execute(
                    text(
                        "INSERT INTO order_events (order_id, ts, source, status, executed_qty)"
                        " SELECT id, :t, 'SIMULATOR', :st, :ex FROM orders"
                        " WHERE client_order_id = :cid"
                    ),
                    {
                        "t": ts,
                        "st": event["status"],
                        "ex": Decimal(event["executed"]),
                        "cid": self._order_key(m, event["cid"]),
                    },
                )
            elif kind == "FILL" and m.fills:
                f = m.fills[-1]
                c.execute(
                    text(
                        "INSERT INTO fills (order_id, exchange_fill_id, ts, price, qty, fee,"
                        " fee_asset, is_maker) SELECT id, :fid, :t, :p, :q, :fee, 'USDT', true"
                        " FROM orders WHERE client_order_id = :cid"
                    ),
                    {
                        "fid": f"sim-{len(m.fills) - 1}",
                        "t": f.ts,
                        "p": f.price,
                        "q": f.qty,
                        "fee": f.fee,
                        "cid": self._order_key(m, f.client_order_id),
                    },
                )
                self._ledger(c, cid, f.ts, "FEE", -f.fee)
            elif kind in ("EXIT", "EMERGENCY_CLOSE") and m.exits:
                ex = m.exits[-1]
                if kind == "EXIT" or event.get("price") is not None:
                    self._ledger(c, cid, ex.ts, "REALIZED_PNL", ex.pnl)
                    self._ledger(c, cid, ex.ts, "FEE", -ex.fee)
            elif kind == "POSITION":
                c.execute(
                    text(
                        "INSERT INTO position_snapshots (symbol, candidate_id, mode, ts, source,"
                        " qty, margin_mode, leverage) VALUES (:s, :c, 'SHADOW', :t, 'SIMULATOR',"
                        " :q, 'CROSS', :lev)"
                    ),
                    {
                        "s": self.symbol,
                        "c": cid,
                        "t": ts,
                        "q": Decimal(event["qty"]),
                        "lev": STRATEGY_LEVERAGE,
                    },
                )
            c.execute(
                text(
                    "INSERT INTO strategy_events (symbol, candidate_id, ts, event_type, payload)"
                    " VALUES (:s, :c, :t, :k, CAST(:p AS jsonb))"
                ),
                {
                    "s": self.symbol,
                    "c": cid,
                    "t": ts,
                    "k": kind,
                    "p": json.dumps(event, default=str),
                },
            )

    def checkpoint(self, eng: ShadowSymbolEngine) -> None:
        if self._batch is not None:
            self._ckpt_dirty = True  # written once, atomically, at commit_input
            return
        with self._tx() as c:
            self._write_checkpoint(c, eng)

    def _write_checkpoint(self, c: Connection, eng: ShadowSymbolEngine) -> None:
        from sp2l.engine import checkpoint as ck

        state = eng.checkpoint()
        broker = ck.dump_broker(eng.broker)
        self._ckpt_seq += 1
        now = datetime.now(UTC)
        c.execute(
            text(
                "INSERT INTO engine_checkpoints (session_id, seq, input_seq, engine, broker,"
                " updated_at) VALUES (:s, :q, :i, CAST(:e AS jsonb), CAST(:b AS jsonb), :t)"
                " ON CONFLICT (session_id) DO UPDATE SET seq = EXCLUDED.seq,"
                " input_seq = EXCLUDED.input_seq, engine = EXCLUDED.engine,"
                " broker = EXCLUDED.broker, updated_at = EXCLUDED.updated_at"
            ),
            {
                "s": self.session_id,
                "q": self._ckpt_seq,
                "i": self.input_seq,
                "e": json.dumps(state),
                "b": json.dumps(broker),
                "t": now,
            },
        )
        for m in [eng.active, *eng.finished[-1:]]:
            if m is None or m.candidate_id is None:
                continue
            c.execute(
                text(
                    "INSERT INTO setup_checkpoints (candidate_id, session_id, setup_key,"
                    " state, terminal, seq, checkpoint, updated_at) VALUES (:c, :s, :k,"
                    " :st, :term, :q, CAST(:j AS jsonb), :t) ON CONFLICT (candidate_id)"
                    " DO UPDATE SET state = EXCLUDED.state, terminal = EXCLUDED.terminal,"
                    " seq = EXCLUDED.seq, checkpoint = EXCLUDED.checkpoint,"
                    " updated_at = EXCLUDED.updated_at"
                ),
                {
                    "c": m.candidate_id,
                    "s": self.session_id,
                    "k": m.cfg.setup_id,
                    "st": m.state.value,
                    "term": m.terminal,
                    "q": self._ckpt_seq,
                    "j": json.dumps(ck.dump_machine(m)),
                    "t": now,
                },
            )

    def on_probe(self, record: dict[str, Any]) -> None:
        """Analysis-only RoomToTP probe result (append-only; never read by the engine)."""
        with self._tx() as c:
            c.execute(
                text(
                    "INSERT INTO analysis_probes (session_id, symbol, kind, version,"
                    " candidate_key, created_at, result, record) VALUES (:s, :sym,"
                    " 'ROOM_TO_TP_BREAKOUT', :v, :k, :t, :r, CAST(:j AS jsonb))"
                ),
                {
                    "s": self.session_id,
                    "sym": self.symbol,
                    "v": record.get("probe_version"),
                    "k": record.get("candidate_key"),
                    "t": record.get("created_at"),
                    "r": record.get("result"),
                    "j": json.dumps(record, default=str),
                },
            )

    def on_counterfactual(self, run: CounterfactualRun) -> None:
        cid = self._candidate_uuid(run.candidate_id)
        if cid is None:
            return
        o = run.outcome()
        outcome = (
            o.outcome
            if o.outcome in ("TP", "SL", "EXPIRED_NO_FILL", "EXPIRED_UNARMED", "OPEN", "AMBIGUOUS")
            else "ERROR"
        )
        m = run.machine
        detail = {
            "e1": str(m.levels.e1),
            "sl": str(m.levels.sl),
            "tp": str(m.levels.tp),
            "e2": str(m.levels.e2),
            "r": str(m.levels.r),
            "state": m.state.value,
            "result_r_net": _exact(o.result_r_net),
        }
        filled_at = next((f.ts for f in m.fills if "-E1-" in f.client_order_id), None)
        exit_at = m.exits[-1].ts if m.exits else None
        with self._tx() as c:
            c.execute(
                text(
                    "INSERT INTO cf.counterfactual_outcomes (candidate_id, rejection_stage,"
                    " simulator_version, spec_sha256, outcome, e1_filled_at, e2_filled,"
                    " exit_at, result_r, detail) VALUES (:c, :st, :v, :h, :o, :f, :e2, :x,"
                    " :r, CAST(:d AS jsonb))"
                ),
                {
                    "c": cid,
                    "st": o.rejection_stage,
                    "v": run.simulator_version,
                    "h": self.spec_sha,
                    "o": outcome,
                    "f": filled_at,
                    "e2": o.e2_filled,
                    "x": exit_at,
                    "r": o.result_r_gross,
                    "d": json.dumps(detail),
                },
            )

    # ---- helpers ------------------------------------------------------------------------

    def _candidate_uuid(self, setup_key: str) -> str | None:
        with self._tx() as c:
            row = c.execute(
                text("SELECT id FROM candidates WHERE setup_key = :k"), {"k": setup_key}
            ).first()
        return str(row[0]) if row else None

    def _insert_snapshot(
        self,
        c: Connection,
        table: str,
        snap: ContextSnapshot | ExhaustionSnapshot,
        m: SetupMachine,
        seq: int,
        eval_time: datetime,
        m1_id: int | None,
        thresholds: dict[str, Any],
    ) -> int:
        cols = {
            r[0]
            for r in c.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
                {"t": table},
            )
        }
        values = {f.name: getattr(snap, f.name) for f in dataclasses.fields(snap)}
        row: dict[str, Any] = {k: _num(v) for k, v in values.items() if k in cols}
        if isinstance(snap, ContextSnapshot):
            row["primary_reason"] = snap.primary_reason
        ctx_open = values.get("ctx_open_time")
        row.update(
            {
                "candidate_id": m.candidate_id,
                "evaluation_seq": seq,
                "eval_ts": eval_time,
                "m1_candle_id": m1_id,
                "spec_sha256": self.spec_sha,
                "m5_candle_id": self.market.candle_id("5m", ctx_open) if ctx_open else None,
                "thresholds": json.dumps(thresholds),
                "exact": json.dumps({k: _exact(v) for k, v in values.items()}),
            }
        )
        names = ", ".join(row)
        params = ", ".join(
            f"CAST(:{k} AS jsonb)" if k in ("thresholds", "exact") else f":{k}" for k in row
        )
        return int(
            c.execute(
                text(f"INSERT INTO {table} ({names}) VALUES ({params}) RETURNING id"), row
            ).scalar_one()
        )

    def _order_key(self, m: SetupMachine, cid: str) -> str:
        """Client order ids are unique per session (keys restart numbering per engine)."""
        return f"{self.session_id.hex[:8]}:{cid}"

    def _snapshot_ids(self, c: Connection, cid: str, seq: int) -> tuple[int, int] | None:
        ids = self._snap_ids.get((cid, seq))
        if ids is not None:
            return ids
        row = c.execute(
            text(
                "SELECT (SELECT id FROM context_snapshots WHERE candidate_id = :c AND"
                " evaluation_seq = :q), (SELECT id FROM exhaustion_snapshots WHERE"
                " candidate_id = :c AND evaluation_seq = :q)"
            ),
            {"c": cid, "q": seq},
        ).one()
        return (int(row[0]), int(row[1])) if row[0] is not None and row[1] is not None else None

    def _revision(self, c: Connection, m: SetupMachine) -> None:
        rev, lv, qty, ts, eval_seq, last_open = m.revisions[-1]
        if eval_seq is None or m.candidate_id is None:
            return
        ids = self._snapshot_ids(c, m.candidate_id, eval_seq)
        risk = next((r.risk for s_, _, r in m.evaluations if s_ == eval_seq), None)
        if ids is None or risk is None or risk.budget is None:
            return
        c.execute(
            text(
                "INSERT INTO e1_revisions (candidate_id, revision, last_spike_candle_id, e1,"
                " sl, r, tp, e2_reference, qty, wallet_basis, modeled_worst_loss,"
                " modeled_costs, context_snapshot_id, exhaustion_snapshot_id, created_at)"
                " VALUES (:c, :rev, :ls, :e1, :sl, :r, :tp, :e2, :q, :w, :loss, :cost,"
                " :cs, :es, :t)"
            ),
            {
                "c": m.candidate_id,
                "rev": rev,
                "ls": self.market.candle_id("1m", last_open),
                "e1": lv.e1,
                "sl": lv.sl,
                "r": lv.r,
                "tp": lv.tp,
                "e2": lv.e2,
                "q": qty,
                "w": risk.budget * 100,
                "loss": risk.modeled_worst_loss,
                "cost": risk.modeled_costs,
                "cs": ids[0],
                "es": ids[1],
                "t": ts,
            },
        )

    def _order_created(self, c: Connection, m: SetupMachine) -> None:
        cid, leg, rev, price, qty, ts = m.submissions[-1]
        c.execute(
            text(
                "INSERT INTO orders (candidate_id, mode, leg, leg_id, revision_id,"
                " client_order_id, side, order_type, price, qty, reduce_only, status,"
                " executed_qty, created_at, updated_at) VALUES (:c, 'SHADOW', :leg, :leg,"
                " :rev, :cid, :side, 'LIMIT', :p, :q, false, 'NEW', 0, :t, :t)"
            ),
            {
                "c": m.candidate_id,
                "leg": leg.value,
                "rev": rev,
                "cid": self._order_key(m, cid),
                "side": "BUY" if m.side is Side.LONG else "SELL",
                "p": price,
                "q": qty,
                "t": ts,
            },
        )

    def _ledger(
        self,
        c: Connection,
        candidate_id: str | None,
        ts: datetime,
        kind: str,
        amount: Decimal,
        *,
        set_balance: bool = False,
    ) -> None:
        self.balance = amount if set_balance else self.balance + amount
        c.execute(
            text(
                "INSERT INTO shadow_wallet_ledger (session_id, candidate_id, ts, kind, amount,"
                " balance_after) VALUES (:s, :c, :t, :k, :a, :b)"
            ),
            {
                "s": self.session_id,
                "c": candidate_id,
                "t": ts,
                "k": kind,
                "a": amount,
                "b": self.balance,
            },
        )
