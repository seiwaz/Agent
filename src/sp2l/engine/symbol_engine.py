"""Per-symbol canonical engine, identical for Backtest / Replay / Shadow (SHD-02, SHD-03).

Inputs arrive in exact causal order (the collector's market-event journal in Shadow):
  - `on_trade`: a trade whose feed coverage is proven. PullbackStart detection, then fills and
    exits on orders that were already active (the broker never fills an order from a trade
    processed before the order existed).
  - `on_m1`: a finalized minute (OK / SYNTHETIC_NO_TRADE / DATA_GAP / UNANCHORED): aggregate
    and finalize M5, run the active setup's M1-close procedure, then promote new P-Gaps.
  - `on_gap`: the collector lost coverage (disconnect, host sleep, restart).

V5.5 B39: any DATA_GAP / UNANCHORED minute, missing-minute hole or reported coverage gap that
overlaps live exposure (active E1/E2 order or an open position) finalizes that setup as
AMBIGUOUS_DATA_GAP: simulated orders and position are discarded with no fabricated close or
PnL. Counterfactual runs with exposure become AMBIGUOUS. Unexposed setups follow the normal
run-end rules. The symbol then returns to normal warmup/scanning.

V5.6 repaired history (market-data integrity): a minute tagged REPAIRED_TABDEAL was rebuilt
after the fact from recovered Tabdeal trades. It restores indicator/context continuity (no
re-anchor, no 150-bar warmup), but it is history, never a live decision point:
  - no P-Gap is promoted on a repaired minute (no setup is ever created retroactively);
  - any setup or counterfactual still running when repaired history arrives is finalized /
    marked AMBIGUOUS_DATA_GAP (it cannot continue across minutes it never saw live; exposed
    ones were already finalized at the GAP by B39). No fill, exit or PnL is derived.
An UNRECOVERED gap arrives as DATA_GAP minutes and follows the V5.5 rules unchanged.

V5.8 execution certainty vs indicator continuity: a reported coverage GAP no longer decides
anything by itself. From the GAP on, trades are buffered (incl. recovered REST trades) and
released in exchange-time order at each minute close; the decision is made per minute when
the gap's minutes arrive:
  - EXACT_RAW_REPAIR (recent-trades; every trade and its order recovered): causal replay -
    the buffered trades drive the setup exactly as live ones would have (no ambiguity);
  - CANDLE_HISTORY_REPAIR (Tabdeal chart history, OHLCV only): indicator continuity is
    restored, but no fill, PullbackStart, SL/TP order or partial fill can be derived: a live
    setup is finalized AMBIGUOUS_DATA_GAP, counterfactuals with exposure become AMBIGUOUS;
  - DATA_GAP (every tier failed): V5.5 rules (B39).
No P-Gap is ever promoted on a repaired minute (no retroactive setups).

Capacity (CAP-01): one setup per market (E1 + reserved E2); P-Gaps seen while busy, or while
the M5 state is not warm after a DATA_GAP (B25), are logged and spend their run.
Rejected candidates spawn isolated counterfactual runs (B20).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sp2l.counterfactual.room_probe import ROOM_REASON, RoomProbe
from sp2l.counterfactual.simulator import CounterfactualRun
from sp2l.engine.gates import M5Gates
from sp2l.engine.model import SetupState
from sp2l.engine.recording import NullRecorder, Recorder
from sp2l.engine.setup_machine import MachineConfig, SetupMachine
from sp2l.execution.shadow_broker import ShadowBroker
from sp2l.indicators.m5_state import M5State
from sp2l.marketdata.m1_builder import (
    CANDLE_HISTORY_REPAIR,
    MINUTE,
    M1Result,
    M1Status,
    floor_minute,
)
from sp2l.marketdata.m5_aggregator import M5Aggregator, M5Result, M5Status
from sp2l.strategy.risk.engine import (
    STRATEGY_LEVERAGE,
    CostModel,
    ExchangeFilters,
    Mode,
    unverifiable_liquidation,
)
from sp2l.strategy.spike import RunTracker, Spike

log = logging.getLogger("sp2l.engine")
SHADOW_INITIAL_WALLET = Decimal(100)
CF_STAGES = {
    SetupState.REJECTED_CONTEXT: "CONTEXT",
    SetupState.REJECTED_EXHAUSTION: "EXHAUSTION",
}
GAP_STATUSES = (M1Status.DATA_GAP, M1Status.UNANCHORED)


@dataclass(frozen=True, slots=True)
class PGapLog:
    ts: datetime
    side: str
    left_open_time: datetime
    middle_open_time: datetime
    right_open_time: datetime
    promoted: bool
    reason: str | None
    quality: dict[str, Any] | None = None  # V5.9: every measured P-Gap value


class ShadowSymbolEngine:
    def __init__(
        self,
        symbol: str,
        *,
        tick: Decimal,
        costs: CostModel,
        filters: ExchangeFilters,
        leverage: int = STRATEGY_LEVERAGE,
        warmup_bars: int = 150,
        initial_wallet: Decimal = SHADOW_INITIAL_WALLET,
        recorder: Recorder | None = None,
        session_id: str = "shadow",
        probes: bool = True,
    ) -> None:
        if leverage != STRATEGY_LEVERAGE:  # V5.8: frozen; never taken from the exchange
            raise ValueError(f"strategy leverage is exactly {STRATEGY_LEVERAGE}x, got {leverage}")
        self.leverage = leverage
        self.symbol = symbol
        self.tick = tick
        self.costs = costs
        self.m5 = M5State(warmup_bars=warmup_bars)
        self.m5agg = M5Aggregator()
        self.runs = RunTracker(tick)
        self.broker = ShadowBroker(costs)
        self.initial_wallet = initial_wallet
        self.gates = M5Gates(
            m5=self.m5,
            mode=Mode.SHADOW,
            leverage=leverage,
            costs=costs,
            filters=filters,
            wallet_balance=self.wallet_balance,
            available_margin=self.wallet_balance,
            liquidation=unverifiable_liquidation,
        )
        self.active: SetupMachine | None = None
        self.finished: list[SetupMachine] = []
        self.counterfactuals: list[CounterfactualRun] = []
        # analysis-only RoomToTP probes: isolated like counterfactuals, never checkpointed
        self.probes_enabled = probes
        self.probes: list[RoomProbe] = []
        self.pgaps: list[PGapLog] = []
        self.last_trade: Decimal | None = None
        self.last_m1_open: datetime | None = None
        self._next_id = 1
        self.recorder: Recorder = recorder or NullRecorder()
        self.session_id = session_id
        self._cf_recorded: set[int] = set()
        # V5.8: trades buffered from a reported GAP until its minutes are resolved
        self.gap_buffer: list[tuple[datetime, Decimal]] | None = None
        self.gap_start: datetime | None = None
        self.gap_reason: str | None = None

    def wallet_balance(self) -> Decimal:
        """B16 Shadow: initial + realized PnL - fees."""
        return self.initial_wallet + self.broker.realized_pnl - self.broker.fees

    # ---- inputs ---------------------------------------------------------------------------

    def on_trade(self, price: Decimal, ts: datetime) -> None:
        self.last_trade = price
        if self.active is not None:
            self.active.on_trade(price, ts)
        fills, exits = self.broker.on_trade(price, ts)
        if self.active is not None:
            for f in fills:
                self.active.on_fill(f)
            for e in exits:
                self.active.on_exit(e)
            self._archive_if_done(ts)
        for cf in self.counterfactuals:
            cf.on_trade(price, ts)
        self._probe_each(lambda p: p.on_trade(price, ts))

    def feed_trade(self, price: Decimal, ts: datetime, source: str) -> None:
        """Journal trade input. Outside a gap only live WS trades drive the engine (a REST-only
        trade arrives after later WS trades; V5.7). Inside a gap every canonical trade is
        buffered for causal release at the minute close (V5.8 EXACT_RAW_REPAIR replay)."""
        if self.gap_buffer is not None:
            self.gap_buffer.append((ts, price))
        elif source == "WS":
            self.on_trade(price, ts)

    def on_gap(self, start: datetime, end: datetime | None, ts: datetime, reason: str) -> None:
        """The collector reported lost coverage starting at `start` (B39/B45). V5.8: the
        decision waits for the gap's minutes (repaired exactly, from history, or DATA_GAP)."""
        if self.gap_buffer is None:
            self.gap_buffer = []
            self.gap_start, self.gap_reason = start, reason

    def _gap_detail(self) -> dict[str, Any]:
        """The reported coverage gap a DATA_GAP / history minute belongs to (audit)."""
        if self.gap_start is None:
            return {}
        return {"gap_start": self.gap_start.isoformat(), "reason": self.gap_reason}

    def _release_buffer(self, before: datetime | None) -> None:
        """Replay buffered trades with ts < `before` (all if None) in exchange-time order.
        A trade for a minute already closed is dropped (it can never be causal)."""
        assert self.gap_buffer is not None
        closed = self.last_m1_open + MINUTE if self.last_m1_open is not None else None
        keep: list[tuple[datetime, Decimal]] = []
        due: list[tuple[datetime, Decimal]] = []
        for t in self.gap_buffer:
            (due if before is None or t[0] < before else keep).append(t)
        self.gap_buffer = keep
        for ts, px in sorted(due, key=lambda t: t[0]):
            if closed is None or ts >= closed:
                self.on_trade(px, ts)

    def on_m1(self, m1: M1Result) -> None:
        eval_time = m1.open_time + MINUTE
        # cf-2: only counterfactuals that existed BEFORE this minute receive its close. One
        # created while processing it (a rejection now) starts at the next finalized minute;
        # feeding it this close again froze its Spike (duplicate minute) - cf-1 bug.
        n_cf = len(self.counterfactuals)
        n_pr = len(self.probes)
        hole = self.last_m1_open is not None and m1.open_time > self.last_m1_open + MINUTE
        decisive = hole or m1.status in GAP_STATUSES or m1.repair_type == CANDLE_HISTORY_REPAIR
        if self.gap_buffer is not None:
            # causal order: trades before the first unproven minute, then the gap decision,
            # then (only if nothing unproven) this minute's trades
            first_bad = (self.last_m1_open + MINUTE) if hole and self.last_m1_open else m1.open_time
            self._release_buffer(first_bad if decisive else eval_time)
        if hole and self.last_m1_open is not None:
            self._hole(self.last_m1_open + MINUTE, m1.open_time, eval_time)
        if m1.status in GAP_STATUSES:
            self._data_gap(
                eval_time,
                {
                    "minute": m1.open_time.isoformat(),
                    "status": m1.status.value,
                    **self._gap_detail(),
                },
            )
        if m1.repair_type == CANDLE_HISTORY_REPAIR:
            self._repaired_history(eval_time, m1.open_time)
        if self.gap_buffer is not None and decisive:
            self._release_buffer(eval_time)  # after the decision: nothing exposed remains
        self.last_m1_open = m1.open_time
        self.recorder.on_m1(m1)
        m5 = self.m5agg.add(m1)
        if m5 is not None:
            self.recorder.on_m5(m5)
            if self.m5.add(m5) is not None:
                self._publish_indicators()
        if self.active is not None:
            self.active.on_m1_close(m1, eval_time, self.last_trade)
            self._archive_if_done(eval_time)
        for sig in self.runs.on_m1(m1):
            reason: str | None = None
            if not sig.first_in_run:  # V5.12: quality is measured only, never a rejection
                reason = "NOT_FIRST_IN_RUN"
            elif self.active is not None:
                reason = "CAPACITY_BUSY"
            elif not self.m5.warm:
                reason = "DATA_WARMUP"
            elif m1.repaired:
                reason = "REPAIRED_HISTORY"  # V5.6: never a retroactive setup
            log = PGapLog(
                eval_time,
                str(sig.side),
                sig.left.open_time,
                sig.middle.open_time,
                sig.right.open_time,
                reason is None,
                reason,
                sig.quality.as_dict(),
            )
            self.pgaps.append(log)
            pgap_ref = self.recorder.on_pgap(log)
            if reason is None:
                self._create(Spike(sig.side, list(sig.run_candles)), eval_time, pgap_ref)
        for cf in self.counterfactuals[:n_cf]:
            cf.on_m1_close(m1, eval_time, self.last_trade)
        self._record_finished_counterfactuals()
        slot_free = self.active is None
        self._probe_each(
            lambda p: p.on_m1(m1, eval_time, self.last_trade, slot_free), self.probes[:n_pr]
        )
        self._flush_probes()
        if (
            self.gap_buffer is not None
            and self.gap_start is not None
            and m1.open_time >= floor_minute(self.gap_start)
            and m1.status not in GAP_STATUSES
            and not m1.repaired
        ):  # the first live minute at/after the gap: the gap is resolved
            self._release_buffer(None)
            self.gap_buffer, self.gap_start, self.gap_reason = None, None, None
        self.recorder.checkpoint(self)

    def _publish_indicators(self) -> None:
        """Display only: the exact values the gates read at this finalized M5 bar."""
        from sp2l.indicators.snapshot import indicator_snapshot

        snap = indicator_snapshot(self.m5)
        if snap is not None:
            self.recorder.on_indicators(snap)

    # ---- data gaps (B39) --------------------------------------------------------------------

    def _hole(self, first_missing: datetime, next_present: datetime, ts: datetime) -> None:
        """Minutes that never arrived (e.g. collector downtime): no proven coverage."""
        self._data_gap(
            ts,
            {
                "missing_from": first_missing.isoformat(),
                "missing_until": next_present.isoformat(),
                "status": "HOLE",
                **self._gap_detail(),
            },
        )
        self.runs.on_m1(M1Result(first_missing, M1Status.DATA_GAP, None))
        self.m5agg = M5Aggregator()
        self.m5.add(M5Result(first_missing, M5Status.DATA_GAP, None))

    def _data_gap(self, ts: datetime, detail: dict[str, Any]) -> None:
        self._probe_each(lambda p: p.abort("AMBIGUOUS_DATA_GAP"))
        m = self.active
        if m is not None:
            orders, pos = m.exposure()
            if orders or pos != 0 or m.e1_filled > 0:
                self.broker.discard_unknowable(orders)
                m.finalize_ambiguous(
                    ts, {**detail, "discarded_orders": orders, "discarded_position": str(pos)}
                )
                self._archive_if_done(ts)
        for cf in self.counterfactuals:
            if cf.done:
                continue
            orders, pos = cf.machine.exposure()
            if orders or pos != 0:
                cf.broker.discard_unknowable(orders)
                cf.ambiguous = True
        self._record_finished_counterfactuals()

    def _repaired_history(self, ts: datetime, minute: datetime) -> None:
        """CANDLE_HISTORY_REPAIR: OHLCV only - nothing about fills can be derived."""
        self._probe_each(lambda p: p.abort("AMBIGUOUS_DATA_GAP"))
        detail = {
            **self._gap_detail(),
            "repaired_minute": minute.isoformat(),
            "repair": "REPAIRED_HISTORY",
            "repair_type": CANDLE_HISTORY_REPAIR,
        }
        m = self.active
        if m is not None and not m.terminal:
            orders, pos = m.exposure()
            self.broker.discard_unknowable(orders)
            m.finalize_ambiguous(
                ts, {**detail, "discarded_orders": orders, "discarded_position": str(pos)}
            )
            self._archive_if_done(ts)
        for cf in self.counterfactuals:
            if not cf.done and not cf.ambiguous:
                orders, _ = cf.machine.exposure()
                cf.broker.discard_unknowable(orders)
                cf.ambiguous = True
        self._record_finished_counterfactuals()

    # ---- internals ------------------------------------------------------------------------

    def _create(self, spike: Spike, eval_time: datetime, pgap_ref: Any) -> None:
        key = f"{self.symbol}-{self.session_id[:8]}-{self._next_id}"  # unique across sessions
        self._next_id += 1
        cfg = MachineConfig(setup_id=key, tick=self.tick)
        machine = SetupMachine(cfg, self.broker, self.gates, spike, None)  # B27 on 1st eval
        machine.candidate_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"sp2l:{self.session_id}:{key}"))
        machine.created_at = eval_time
        self.recorder.on_candidate_created(machine, pgap_ref)  # row exists before any event
        machine.observer = self._on_setup_event
        self.active = machine
        machine.start(eval_time, self.last_trade)
        self._archive_if_done(eval_time)

    def _on_setup_event(self, machine: SetupMachine, kind: str, event: dict[str, Any]) -> None:
        """Continuous persistence: every material event is recorded (atomically per input)."""
        self.recorder.on_setup_event(machine, kind, event)
        self.recorder.checkpoint(self)

    def _archive_if_done(self, ts: datetime) -> None:
        m = self.active
        if m is None or not m.terminal:
            return
        self.recorder.on_archived(m, self.broker)
        self.finished.append(m)
        self.active = None
        stage = CF_STAGES.get(m.state)
        if stage is not None:
            if m.ever_armed:
                stage += "_AFTER_ARM"
            self.counterfactuals.append(
                CounterfactualRun(m, stage, self.costs, ts, self.last_trade)
            )
        self._maybe_probe(m, ts)

    # ---- analysis-only RoomToTP probes (never influence decisions) -----------------------

    def _maybe_probe(self, m: SetupMachine, ts: datetime) -> None:
        if not self.probes_enabled or m.state is not SetupState.REJECTED_CONTEXT:
            return
        if m.ever_armed or not m.evaluations:
            return
        ctx = m.evaluations[0][2].context
        if ctx is None or ROOM_REASON not in ctx.reasons or ctx.nearest_obstacle is None:
            return
        try:
            self.probes.append(RoomProbe(m, self.gates, self.costs, ts))
        except Exception:  # analysis must never disturb the engine
            log.exception("room probe creation failed for %s", m.cfg.setup_id)

    def _probe_each(self, fn: Any, probes: list[RoomProbe] | None = None) -> None:
        for p in list(self.probes if probes is None else probes):
            if p.done:
                continue
            try:
                fn(p)
            except Exception:  # analysis must never disturb the engine
                log.exception("room probe %s failed; dropped", p.key)
                p.abort("PROBE_ERROR")

    def _flush_probes(self) -> None:
        on_probe = getattr(self.recorder, "on_probe", None)
        keep = []
        for p in self.probes:
            if not p.done:
                keep.append(p)
                continue
            if on_probe is not None:
                try:
                    on_probe(p.record())
                except Exception:
                    log.exception("room probe %s could not be recorded", p.key)
        self.probes = keep

    def _record_finished_counterfactuals(self) -> None:
        for i, cf in enumerate(self.counterfactuals):
            if i not in self._cf_recorded and cf.done:
                self.recorder.on_counterfactual(cf)
                self._cf_recorded.add(i)

    def close(self) -> None:
        """Shutdown. Nothing is finalized: state is checkpointed and resumes on restart by
        journal replay (B39). Gaps during downtime are handled when they are replayed."""
        self.recorder.checkpoint(self)

    # ---- checkpoint / restore -------------------------------------------------------------

    def checkpoint(self) -> dict[str, Any]:
        from sp2l.engine import checkpoint as ck

        seg = self.m5.segment
        return {
            "session_id": self.session_id,
            "next_id": self._next_id,
            "last_trade": None if self.last_trade is None else str(self.last_trade),
            "last_m1_open": None if self.last_m1_open is None else self.last_m1_open.isoformat(),
            "m5_anchor": seg.anchor_open_time.isoformat() if seg else None,
            "m5_last_open": seg.bars[-1].open_time.isoformat() if seg and seg.bars else None,
            "aggregator": ck.dump_aggregator(self.m5agg),
            "runs": ck.dump_runs(self.runs),
            "active": ck.dump_machine(self.active) if self.active else None,
            "counterfactuals": [
                {
                    "candidate_key": cf.candidate_id,
                    "stage": cf.rejection_stage,
                    "machine": ck.dump_machine(cf.machine),
                    "broker": ck.dump_broker(cf.broker),
                    "recorded": i in self._cf_recorded,
                    "ambiguous": cf.ambiguous,
                    "sim": cf.simulator_version,
                }
                for i, cf in enumerate(self.counterfactuals)
                if not (cf.done and i in self._cf_recorded)
            ],
            "pgaps_seen": len(self.pgaps),
            "leverage": self.leverage,
            "gap_start": self.gap_start.isoformat() if self.gap_start else None,
            "gap_reason": self.gap_reason,
            "gap_buffer": (
                None
                if self.gap_buffer is None
                else [[t.isoformat(), str(p)] for t, p in self.gap_buffer]
            ),
        }

    def restore(
        self, state: dict[str, Any], broker_state: dict[str, Any], m5_segment: list[M5Result]
    ) -> None:
        """Exact restore of a checkpoint. `m5_segment` = the stored M5 bars from the checkpoint's
        anchor through its last bar (incremental indicators are deterministic from the anchor).
        The caller then replays the market-event journal after the checkpoint cursor (B39)."""
        from sp2l.engine import checkpoint as ck
        from sp2l.engine.gates import BaseSp2lGates

        self.broker = ck.load_broker(broker_state, self.costs)
        self._next_id = int(state["next_id"])
        self.last_trade = Decimal(state["last_trade"]) if state["last_trade"] else None
        self.last_m1_open = (
            datetime.fromisoformat(state["last_m1_open"]) if state.get("last_m1_open") else None
        )
        self.m5agg = ck.load_aggregator(state["aggregator"])
        self.runs = ck.load_runs(state["runs"], self.tick)
        buf = state.get("gap_buffer")
        self.gap_buffer = (
            None if buf is None else [(datetime.fromisoformat(t), Decimal(p)) for t, p in buf]
        )
        self.gap_start = (
            datetime.fromisoformat(state["gap_start"]) if state.get("gap_start") else None
        )
        self.gap_reason = state.get("gap_reason")
        for m5 in m5_segment:
            self.m5.add(m5)
        expected = state.get("m5_last_open")
        got = self.m5.last.open_time.isoformat() if self.m5.last else None
        if expected != got:
            raise RuntimeError(f"M5 rebuild mismatch: checkpoint {expected}, rebuilt {got}")
        self._publish_indicators()
        self.pgaps = []
        self.counterfactuals = []
        self._cf_recorded = set()
        for i, c in enumerate(state["counterfactuals"]):
            cf_broker = ck.load_broker(c["broker"], self.costs)
            cf = CounterfactualRun.restored(
                c["candidate_key"],
                c["stage"],
                ck.load_machine(c["machine"], cf_broker, BaseSp2lGates()),
                cf_broker,
                c.get("sim", "cf-1"),  # checkpoints written before cf-2 ran under cf-1
            )
            cf.ambiguous = bool(c["ambiguous"])
            self.counterfactuals.append(cf)
            if c["recorded"]:
                self._cf_recorded.add(i)
        self.active = (
            ck.load_machine(state["active"], self.broker, self.gates) if state["active"] else None
        )
        if self.active is not None:
            self.active.observer = self._on_setup_event

    def prime(self, m5_history: list[M5Result], last_m1_open: datetime | None) -> None:
        """New session: warm the M5 state from stored contiguous M5 bars (indicators only; no
        setups are simulated on history). Journal replay then starts at the next M5 bucket."""
        for m5 in m5_history:
            self.m5.add(m5)
        self.last_m1_open = last_m1_open
        self._publish_indicators()

    def reheal(self, m5_history: list[M5Result]) -> bool:
        """A break in the M5 series was later healed from validated Tabdeal history: rebuild
        the M5 state from the stored contiguous bars ending at the last bar this engine has
        processed, instead of waiting for a new 150-bar warmup. Only while the engine is
        still warming up and no setup is in progress (no decision is revisited); the 150-bar
        requirement itself is unchanged."""
        last = self.m5.last
        if self.m5.warm or self.active is not None or last is None or self.m5.segment is None:
            return False
        if not m5_history or m5_history[-1].open_time != last.open_time:
            return False
        if len(m5_history) < self.m5.warmup_bars or len(m5_history) <= len(self.m5.segment.bars):
            return False
        self.m5.rebuild(m5_history)
        if not self.m5.warm:  # pragma: no cover - guarded by the length check above
            return False
        self._publish_indicators()
        return True

    def summary(self) -> dict[str, Any]:
        by_state: dict[str, int] = {}
        for m in self.finished:
            by_state[str(m.state)] = by_state.get(str(m.state), 0) + 1
        return {
            "wallet": str(self.wallet_balance()),
            "finished": by_state,
            "active": str(self.active.state) if self.active else None,
            "counterfactuals": len(self.counterfactuals),
            "pgaps": len(self.pgaps),
        }
