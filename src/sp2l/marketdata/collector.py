"""Always-on market-data collector for one symbol over 1..N independent connections.

(MKT-04; V5.1 B05/B25, V5.2 B31, V5.3 B34, V5.5 B39/B45; B46 redundancy test.)

Coverage is proven, never assumed - independently per connection:
- Each connection pings the server itself. A pong for a ping sent at local time t proves
  every frame the server sent on THAT connection before receiving the ping has arrived (the
  stream is ordered). Its coverage is proven through t - clock_safety, and its receipts are
  proven through t.
- After (re)subscribing, a connection's coverage restarts only at its first pong's receive
  time + clock_safety.
- Merged coverage = union of all connections' proven intervals. A minute is covered only if
  one merged interval contains it entirely; otherwise it is DATA_GAP (fail closed). With a
  single connection this is exactly the original single-feed rule.
- Minutes are finalized only once merged coverage is proven past their end (+2 s grace)
  while any connection is up; with no connection up they finalize on the clock (DATA_GAP).

Trades from all connections are deduplicated by the fingerprint (sequence, exch_ts, price,
amount) plus occurrence number - NOT by `sequence` alone, which is neither contiguous nor
unique (one sequence can carry several distinct fills). Multi-payload sequences and real
conflicts are reported, never merged.
A trade is stored once and journaled only after a connection that actually received it has
proven it (a pong on A never proves a trade only B saw).

Market-event journal for the Shadow runner (B39): a GAP is journaled when merged coverage is
lost (no connection's proof continues it); trades never proven are journaled flagged
`in_gap`. Host sleep (B45) is detected from wall-clock time advancing faster than monotonic
time; it is diagnostic only.

V5.6 gap repair (market-data integrity, not strategy): a transport interruption that merged
coverage survives is no gap at all. A real merged gap no longer than `repair_max_gap` is
GAP_PENDING_REPAIR: its minutes are held, and once coverage is back the missing trades are
recovered from Tabdeal's public recent-trades and accepted only if exact (repair.py). Then
they flow through the same M1/M5 path as live trades (REPAIRED_TABDEAL lineage). Otherwise
the gap is UNRECOVERED and its minutes finalize as DATA_GAP exactly as before.

V5.8 three-tier recovery: tier 1 = the redundant WebSocket connections (a gap only exists if
merged coverage breaks), tier 2 = recent-trades (EXACT_RAW_REPAIR, above), tier 3 = Tabdeal's
chart history for what tier 2 cannot reach (long outages, restarts): the gap's minutes stay
held, the history response is validated against our own canonical minutes (history.py) and
accepted minutes become CANDLE_HISTORY_REPAIR candles (OHLCV, trade count UNKNOWN). A gap is
UNRECOVERED only if every tier fails.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

import websockets

from sp2l.marketdata.coverage import MergedCoverage
from sp2l.marketdata.history import HistoryPolicy
from sp2l.marketdata.history import validate as validate_history
from sp2l.marketdata.m1_builder import (
    DEFAULT_GRACE,
    M1Builder,
    M1Result,
    Quality,
    Trade,
    floor_minute,
)
from sp2l.marketdata.m5_aggregator import M5Aggregator, M5Result
from sp2l.marketdata.repair import KnownTrade, RepairOutcome, repair_gap
from sp2l.marketdata.tabdeal_public import RestTrade
from sp2l.marketdata.tabdeal_ws import WS_URL, StreamError, parse_message, ws_market

log = logging.getLogger("sp2l.collector")


class CollectorSink(Protocol):
    def on_trade(self, trade: Trade, late: bool) -> None: ...
    def on_m1(self, m1: M1Result) -> None: ...
    def on_m5(self, m5: M5Result) -> None: ...
    def on_gap(self, start: datetime, end: datetime, reason: str) -> None: ...
    def on_connection(self, connected: bool, ts: datetime) -> None: ...
    def on_proven_trade(self, trade: Trade, late: bool, in_gap: bool) -> None: ...
    def on_gap_open(self, start: datetime, ts: datetime, reason: str) -> None: ...
    def on_host_sleep(self, start: datetime, end: datetime) -> None: ...
    def on_connection_event(
        self, conn: str, kind: str, ts: datetime, reason: str | None, detail: dict[str, Any]
    ) -> None: ...
    def on_conflict(self, trade_id: str, first: dict[str, Any], other: dict[str, Any]) -> None: ...
    def on_repair(
        self, start: datetime, end: datetime, reason: str, outcome: RepairOutcome
    ) -> None: ...
    def on_late_rest_trade(self, trade: Trade) -> None: ...
    def on_latency(self, minute: datetime, stages: dict[str, float | None]) -> None: ...
    def on_live(self, event: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    symbol: str
    url: str = WS_URL
    grace: timedelta = DEFAULT_GRACE
    ping_interval: float = 1.0
    ping_timeout: float = 3.0
    clock_safety: timedelta = timedelta(seconds=1)
    tick_interval: float = 0.25
    host_sleep_threshold: timedelta = timedelta(seconds=5)
    backoff: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)
    connections: tuple[str, ...] = ("A",)
    stagger: float = 0.0  # seconds between starting consecutive connections
    # V5.6 gap repair (off unless configured; tests and replays keep the V5.5 behavior)
    repair_enabled: bool = False
    repair_max_gap: timedelta = timedelta(seconds=120)
    repair_delta_max: timedelta = timedelta(milliseconds=500)
    repair_delta_min: timedelta = timedelta(milliseconds=-100)
    repair_settle: timedelta = timedelta(milliseconds=1500)
    repair_deadline: timedelta = timedelta(seconds=30)
    # V5.7 continuous REST reconciliation (third source; see ingest_rest)
    rest_enabled: bool = False
    rest_settle: timedelta = timedelta(seconds=1)  # WS delivery p99 1.0 s (server, 4,625 trades)
    rest_cover_delta: timedelta = timedelta(seconds=1)  # record - stream time bound for coverage
    #   proofs (max observed 653 ms); matching itself uses the wider align_tolerance
    rest_phase_margin: float = 0.2  # s after the earliest instant a poll can prove a minute
    rest_phase_align: bool = True
    rest_stale: timedelta = timedelta(seconds=45)  # no successful poll for this long: REST down
    reconcile_deadline: timedelta = timedelta(seconds=60)  # then finalize WS-only (LIVE_WS_ONLY)
    rest_delta_est: timedelta = timedelta(milliseconds=33)  # median record-WS offset (measured)
    rest_poll_min: float = 3.0
    rest_poll_max: float = 15.0
    align_tolerance: timedelta = timedelta(seconds=3)  # REST<->WS alignment time tolerance
    # V5.8 tier 3: validated Tabdeal chart history (off unless configured)
    history_enabled: bool = False
    history_max_gap: timedelta = timedelta(days=7)  # retention measured >= 90 days
    history_deadline: timedelta = timedelta(minutes=5)  # retries after the job is eligible
    history_retry: timedelta = timedelta(seconds=10)
    history_policy: HistoryPolicy = HistoryPolicy()


@dataclass
class RepairJob:
    start: datetime
    end: datetime
    reason: str
    attempts: int = 0
    in_flight: bool = False
    tier: str = "REST"  # REST (recent-trades, exact) -> HISTORY (chart history) -> decided
    rest_failure: str | None = None
    next_try: datetime | None = None
    # tier 3: last failed history request made while Tabdeal was unreachable from here (no
    # stream connection and no healthy REST); the history deadline only runs after it
    offline_until: datetime | None = None


def utcnow() -> datetime:
    return datetime.now(UTC)


def _unrecovered(reason: str) -> RepairOutcome:
    return RepairOutcome("UNRECOVERED", "NONE", reason)


@dataclass
class ConnState:
    name: str
    connected: bool = False
    confirmed: bool = False
    coverage_from: datetime | None = None
    healthy_until: datetime | None = None
    proven_recv: datetime | None = None  # receipts on this connection proven through here
    sessions: int = 0
    disconnects: int = 0
    connected_at: datetime | None = None
    last_close: datetime | None = None
    last_close_reason: str | None = None
    received: int = 0


@dataclass
class _Pending:
    trade: Trade
    late: bool
    receipts: dict[str, datetime] = field(default_factory=dict)  # conn -> recv on that conn
    dead: set[str] = field(default_factory=set)  # receiving conns that died unproven


MINUTE_TD = timedelta(minutes=1)


def _align(rest: list[RestTrade], ws: list[list[Any]], tol: timedelta) -> dict[int, list[Any]]:
    """Align a REST window with the WS trades of the same span (V5.7 trade identity).

    Identity can only link trades with equal price and amount, and the two feeds' clocks
    differ per trade (record - stream time -25 ms .. +653 ms measured), so near-simultaneous
    trades of DIFFERENT price/amount may appear in a different order in each feed. Alignment
    is therefore done per (price, amount) group, where the order is reliable: within a group
    it is an order-preserving alignment (longest common subsequence) with record - stream time
    in [-1 s, tol], and among equally long alignments the smallest total time difference wins,
    so a run of identical fills can never be paired one position off. Returns rest index ->
    WS entry. Deterministic."""
    groups: dict[tuple[Any, Any], tuple[list[int], list[list[Any]]]] = {}
    for i, r in enumerate(rest):
        groups.setdefault((r.price, r.qty), ([], []))[0].append(i)
    for w in ws:
        g = groups.get((w[1], w[2]))
        if g is not None:
            g[1].append(w)
    out: dict[int, list[Any]] = {}
    for idx, cand in groups.values():
        cand.sort(key=lambda w: w[0])
        out.update(_align_group([rest[i] for i in idx], idx, cand, tol))
    return out


def _align_group(
    rest: list[RestTrade], idx: list[int], ws: list[list[Any]], tol: timedelta
) -> dict[int, list[Any]]:
    n, m = len(rest), len(ws)
    early = timedelta(seconds=-1)

    def cost(i: int, j: int) -> float | None:
        d = rest[i].created - ws[j][0]
        return abs(d.total_seconds()) if early <= d <= tol else None

    # dp[i][j] = (matches, -total_cost) for rest[i:], ws[j:]
    dp: list[list[tuple[int, float]]] = [[(0, 0.0)] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            best = max(dp[i + 1][j], dp[i][j + 1])
            c = cost(i, j)
            if c is not None:
                k, neg = dp[i + 1][j + 1]
                best = max(best, (k + 1, neg - c))
            dp[i][j] = best
    out: dict[int, list[Any]] = {}
    i = j = 0
    while i < n and j < m:
        c = cost(i, j)
        if c is not None:
            k, neg = dp[i + 1][j + 1]
            if dp[i][j] == (k + 1, neg - c):
                out[idx[i]] = ws[j]
                i += 1
                j += 1
                continue
        if dp[i][j] == dp[i + 1][j]:
            i += 1
        else:
            j += 1
    return out


class _SourceCover:
    """Coverage oracle over one source's intervals (REST reconciliation proof)."""

    def __init__(self, src: Any) -> None:
        self.src = src

    def covers(self, start: datetime, end: datetime) -> bool:
        return any(a <= start and end <= b for a, b in self.src.spans)

    def component_start(self, t: datetime) -> datetime | None:
        return next((a for a, b in self.src.spans if a <= t <= b), None)


class Collector:
    def __init__(
        self,
        cfg: CollectorConfig,
        sink: CollectorSink,
        *,
        clock: Callable[[], datetime] = utcnow,
        connect: Callable[..., Any] = websockets.connect,
        resume_from: datetime | None = None,
        resume_m1: list[M1Result] | None = None,
        resume_trades: list[Trade] | None = None,
        resume_recent: list[M1Result] | None = None,
    ) -> None:
        """resume_from (V5.7): the end of the last canonical minute in the database. The new
        process continues the candle series from there instead of from its own start time, so
        a restart is a coverage gap like any other (repairable from REST), never a reset.
        resume_m1: the already-final minutes of the current M5 bucket, so M5 continues too."""
        self.cfg = cfg
        self.sink = sink
        self.clock = clock
        self._connect = connect
        self.market = ws_market(cfg.symbol)
        start = resume_from or clock()
        self.coverage = MergedCoverage()
        self.conns = {n: ConnState(n) for n in cfg.connections}
        for n in cfg.connections:
            self.coverage.source(n)
        self.builder = M1Builder(start, cfg.grace, coverage=self.coverage)
        self.agg = M5Aggregator()
        for m in resume_m1 or []:
            self.agg.add(m)
        # V5.8: the latest canonical minutes (tier-3 overlap validation reference)
        self.recent_m1: deque[M1Result] = deque(resume_recent or [], maxlen=30)
        self._gap_open_since: datetime | None = start  # no coverage before the first session
        self._gap_reason = "COLLECTOR_RESTART" if resume_from else "COLLECTOR_START"
        # V5.7 REST reconciliation state
        self.rest_src = self.coverage.source("R") if cfg.rest_enabled else None
        self.provenance: dict[str, set[str]] = {}
        if cfg.rest_enabled:
            self.builder.rest_cover = _SourceCover(self.coverage.source("R"))
            self.builder.provenance = self.provenance
        self._ws_recent: list[list[Any]] = []  # [exch_ts, price, qty, instance_id, rest_seen]
        self._rest_only: list[list[Any]] = []  # [created, price, qty, trade_id, ws_seen]
        self._rest_seen: dict[tuple[str, str, str], datetime] = {}
        self.last_rest_ok: datetime | None = None
        self.rest_spans_s: list[float] = []  # recent window spans (retention measurement)
        self.reconcile_latency_s: list[float] = []
        self._lat: dict[datetime, dict[str, float | None]] = {}  # minute end -> stage ms
        # V5.9 display only: the forming M1 of every not-yet-final minute, from canonical
        # trades as they arrive. Never read by the M1 builder, M5 or any strategy code.
        self.forming: dict[datetime, dict[str, Any]] = {}
        # trades the previous process already stored for the resumed minute: known, so REST
        # alignment recognises them (never stored twice)
        # (trades of already-final minutes are registered as known only: REST windows still
        # list them after a restart and must not re-add them)
        for t in resume_trades or []:
            if t.exch_ts >= self.builder.next_open_minute:
                self.builder.add_trade(t)
                self._live_trade(t, emit=False)  # the live view continues, not restarts
            self.provenance[t.trade_id] = {t.source}
            if t.source == "REST":
                self._rest_only.append(
                    [t.exch_ts + cfg.rest_delta_est, t.price, t.qty, t.trade_id, False]
                )
            else:
                self._ws_recent.append([t.exch_ts, t.price, t.qty, t.trade_id, False])
        self._gap_open_emitted = False
        self._pending: list[_Pending] = []
        FP = tuple[str, str, str, str]
        self._stored: dict[FP, int] = {}  # fingerprint -> stored multiplicity
        self._conn_counts: dict[str, dict[FP, int]] = {n: {} for n in cfg.connections}
        self._seq_variants: dict[str, list[FP]] = {}
        self._seen_order: list[tuple[datetime, FP]] = []
        self._prev_tick: tuple[datetime, float] | None = None
        self.last_trade_exch_ts: datetime | None = None
        self.jobs: list[RepairJob] = []  # V5.6: closed gaps awaiting a repair decision
        # health counters (reporting only; they never influence coverage decisions)
        self.stats: dict[str, int] = {
            "trades_total": 0,  # unique trades
            "late_total": 0,
            "duplicates": 0,
            "conflicts": 0,
            "multi_fill_sequences": 0,
            "orphans": 0,
            "m1_ok": 0,
            "m1_synthetic": 0,
            "m1_data_gap": 0,
            "m1_unanchored": 0,
            "gaps_total": 0,  # merged coverage gaps
            "reconnects": 0,
            "host_sleeps": 0,
            "gaps_repaired": 0,
            "gaps_unrecovered": 0,
            "gaps_history_repaired": 0,
            "m1_history_repaired": 0,
            "rest_polls": 0,
            "rest_errors": 0,
            "rest_trades": 0,  # distinct REST trades seen
            "rest_only": 0,  # canonical trades the WebSocket never delivered (B48)
            "ws_only": 0,  # WS trades REST never showed although REST covered their time
            "rest_revisions": 0,
        }

    # ---- compatibility (single connection) -------------------------------------------------

    @property
    def connected(self) -> bool:
        return any(c.connected for c in self.conns.values())

    @property
    def healthy_until(self) -> datetime | None:
        return self.coverage.end()

    def _c(self, conn: str) -> ConnState:
        return self.conns[conn]

    # ---- synchronous core (deterministic, unit-tested) ---------------------------------------

    def ingest(self, raw: str | bytes, recv_ts: datetime, conn: str = "A") -> Trade | None:
        """Store each distinct trade once across connections.

        Observed 2026-09-26: one exchange `sequence` can carry SEVERAL distinct trades (same
        exch_ts, different price/amount - multi-level fills), delivered identically on every
        connection. Identity is therefore the fingerprint (sequence, exch_ts, price, amount)
        plus its occurrence number, and the stored multiplicity of a fingerprint is the maximum
        any single connection delivered (multiset union: nothing double-counted, nothing lost).
        """
        trade = parse_message(raw, recv_ts, self.market)
        if trade is None:
            return None
        c = self._c(conn)
        c.received += 1
        seq = trade.trade_id
        fp = (seq, trade.exch_ts.isoformat(), str(trade.price), str(trade.qty))
        k = self._conn_counts[conn].get(fp, 0) + 1
        self._conn_counts[conn][fp] = k
        instance_id = f"{seq}:{trade.price}:{trade.qty}#{k}"
        stored = self._stored.get(fp, 0)
        if k <= stored:  # this connection repeats an instance already stored
            self.stats["duplicates"] += 1
            self.provenance.setdefault(instance_id, set()).add(conn)
            for p in self._pending:  # an extra receipt can prove the same instance
                if p.trade.trade_id == instance_id:
                    p.receipts.setdefault(conn, recv_ts)
                    break
            return None
        rest_twin = self._match_rest_only(trade)
        if rest_twin is not None:  # already canonical from REST: same trade, one more source
            rest_twin[4] = True
            self.provenance.setdefault(rest_twin[3], {"REST"}).add(conn)
            self.stats["duplicates"] += 1
            self._stored[fp] = k
            return None
        self._classify_sequence(seq, fp, conn, trade.raw)
        self._stored[fp] = k
        self._seen_order.append((recv_ts, fp))
        trade = replace(trade, trade_id=instance_id)
        late = not self.builder.add_trade(trade)
        self.stats["trades_total"] += 1
        self.stats["late_total"] += int(late)
        self.last_trade_exch_ts = trade.exch_ts
        self.sink.on_trade(trade, late)
        if not late:
            self._live_trade(trade)
        self._pending.append(_Pending(trade, late, {conn: recv_ts}))
        self.provenance[instance_id] = {conn}
        if self.cfg.rest_enabled:
            self._ws_recent.append([trade.exch_ts, trade.price, trade.qty, instance_id, False])
        return trade

    def _classify_sequence(
        self, seq: str, fp: tuple[str, str, str, str], conn: str, raw: str | None = None
    ) -> None:
        """Report sequences carrying more than one payload. Different price/amount at the
        same time = MULTI_FILL_SAME_SEQUENCE (observation). Same price/amount at a different
        time = TIMESTAMP_CONFLICT (a real disagreement)."""
        variants = self._seq_variants.setdefault(seq, [])
        if fp in variants:
            return
        for other in variants:
            same_px_qty = other[2:] == fp[2:]
            kind = (
                "TIMESTAMP_CONFLICT"
                if same_px_qty and other[1] != fp[1]
                else ("MULTI_FILL_SAME_SEQUENCE")
            )
            if kind == "TIMESTAMP_CONFLICT":
                self.stats["conflicts"] += 1
                log.error("CONFLICT for sequence %s: %s vs %s (%s)", seq, other, fp, conn)
            elif len(variants) == 1:
                self.stats["multi_fill_sequences"] += 1
            self.sink.on_conflict(
                seq,
                {"fingerprint": list(other), "classification": kind},
                {"fingerprint": list(fp), "conn": conn, "raw": raw},
            )
            break
        variants.append(fp)

    def session_started(self, ts: datetime, conn: str = "A") -> None:
        c = self._c(conn)
        c.sessions += 1
        c.connected, c.confirmed, c.connected_at = True, False, ts
        self.stats["reconnects"] = sum(max(x.sessions - 1, 0) for x in self.conns.values())
        self._emit_gap_open(ts)  # collector start: no coverage before the first session
        self.sink.on_connection(True, ts)
        self.sink.on_connection_event(conn, "CONNECTED", ts, None, {"session": c.sessions})

    def pong(self, ping_sent: datetime, pong_recv: datetime, conn: str = "A") -> None:
        """Liveness proof for everything the server sent on `conn` before it got the ping."""
        c = self._c(conn)
        safety = self.cfg.clock_safety
        src = self.coverage.source(conn)
        if not c.confirmed:
            c.confirmed = True
            c.coverage_from = pong_recv + safety
            c.healthy_until = None
            src.open(c.coverage_from)
            self.sink.on_connection_event(
                conn, "CONFIRMED", pong_recv, None, {"coverage_from": c.coverage_from.isoformat()}
            )
            if self._gap_open_since is not None:
                restart = c.coverage_from
                if restart > self._gap_open_since:
                    self.stats["gaps_total"] += 1
                    self.sink.on_gap(self._gap_open_since, restart, self._gap_reason)
                    self._gap_closed(self._gap_open_since, restart, self._gap_reason)
                self._gap_open_since = None
                self._gap_open_emitted = False
                self._update_hold(pong_recv)
        else:
            until = ping_sent - safety
            assert c.coverage_from is not None
            if until > c.coverage_from and (c.healthy_until is None or until > c.healthy_until):
                c.healthy_until = until
                src.extend(until)
        c.proven_recv = ping_sent if c.proven_recv is None else max(c.proven_recv, ping_sent)
        self._release()

    def session_ended(self, ts: datetime, reason: str, conn: str = "A") -> None:
        c = self._c(conn)
        was = c.connected
        c.connected, c.confirmed = False, False
        c.proven_recv = None
        if was:
            c.disconnects += 1
            c.last_close, c.last_close_reason = ts, reason
            self.sink.on_connection(False, ts)
            self.sink.on_connection_event(
                conn,
                "CLOSED",
                ts,
                reason,
                {
                    "session": c.sessions,
                    "lifetime_s": (ts - c.connected_at).total_seconds() if c.connected_at else None,
                    "healthy_until": c.healthy_until.isoformat() if c.healthy_until else None,
                },
            )
        for p in self._pending:  # its unproven receipts can no longer be proven by it
            if conn in p.receipts:
                p.dead.add(conn)
        if self._gap_open_since is None and not self._continued():
            end = self.coverage.end()
            self._gap_open_since = min(end, ts) if end is not None else ts
            self._gap_reason = f"{conn}:{reason}" if len(self.conns) > 1 else reason
        self._emit_gap_open(ts)
        self._release(force=self._gap_open_since is not None)
        self._update_hold(ts)

    def _continued(self) -> bool:
        """Does any live, proven connection continue merged coverage without a hole?"""
        end = self.coverage.end()
        return any(
            x.connected
            and x.confirmed
            and x.coverage_from is not None
            and end is not None
            and x.coverage_from <= end
            for x in self.conns.values()
        )

    # ---- V5.7 continuous REST reconciliation -------------------------------------------------

    def _rest_healthy(self, now: datetime) -> bool:
        if self.last_rest_ok is None:
            return now - self.builder.next_open_minute < self.cfg.rest_stale + MINUTE_TD
        return now - self.last_rest_ok <= self.cfg.rest_stale

    def _in_window(self, created: datetime, exch_ts: datetime) -> bool:
        d = created - exch_ts
        return self.cfg.repair_delta_min <= d <= self.cfg.repair_delta_max

    def _match_rest_only(self, t: Trade) -> list[Any] | None:
        tol = self.cfg.align_tolerance
        cands = [
            r
            for r in self._rest_only
            if not r[4] and r[1] == t.price and r[2] == t.qty and abs(r[0] - t.exch_ts) <= tol
        ]
        return min(cands, key=lambda r: abs(r[0] - t.exch_ts), default=None)

    def next_poll_in(self, now: datetime) -> float:
        """Phase-aligned scheduling: besides the overlap cadence, poll at the earliest instant
        a window can prove the minute that just closed (boundary + settle + coverage bound),
        so a minute is final seconds after it closes instead of after a random poll cycle."""
        regular = self.next_rest_delay()
        if not self.cfg.rest_phase_align:
            return regular
        lead = (self.cfg.rest_settle + self.cfg.rest_cover_delta).total_seconds()
        lead += self.cfg.rest_phase_margin
        minute = now.replace(second=0, microsecond=0)
        target = minute + timedelta(seconds=lead)
        if target <= now:
            target += MINUTE_TD
        return max(0.05, min(regular, (target - now).total_seconds()))

    def _mark_ws_proof(self, now: datetime) -> None:
        """Latency marks: when the WebSocket coverage proved each closed minute."""
        t = self.builder.next_open_minute + MINUTE_TD
        while t <= now:
            self._lat.setdefault(t, {})
            t += MINUTE_TD
        ws_end = max(
            (sp[-1][1] for n in self.conns if (sp := self.coverage.source(n).spans)),
            default=None,
        )
        for t_end, marks in self._lat.items():
            if "ws_proof" not in marks and ws_end is not None and ws_end >= t_end:
                marks["ws_proof"] = (now - t_end).total_seconds() * 1000
        if len(self._lat) > 120:
            for k in sorted(self._lat)[:-120]:
                del self._lat[k]

    def next_rest_delay(self) -> float:
        """Poll so consecutive 50-trade windows always overlap: a third of the last window."""
        span = self.rest_spans_s[-1] if self.rest_spans_s else 30.0
        return max(self.cfg.rest_poll_min, min(self.cfg.rest_poll_max, span / 3))

    def ingest_rest(
        self,
        rest: list[RestTrade] | None,
        fetched_at: datetime,
        requested_at: datetime | None = None,
    ) -> None:
        """Reconcile one recent-trades window (newest 50 trades) against the canonical store.

        Identity (measured, not assumed): REST has no sequence. REST and WS list the same
        trades in the same order, so the window is ALIGNED against the WS trades as two
        sequences (longest common subsequence on equal price and amount, record time within
        `align_tolerance` of the stream time). Order, not a tight time bound, decides identity:
        a slow record time (up to 653 ms observed on the server) can no longer duplicate a
        trade, and identical fills stay distinct. Unaligned REST trades are canonical REST-only
        trades (WS omission, B48). Trades newer than `rest_settle` are left for the next
        (overlapping) window, since WS may still deliver them.
        The window proves completeness for stream time [oldest - delta_min, cutoff - delta_max].
        """
        if rest is None:
            self.stats["rest_errors"] += 1
            return
        t_start = time.perf_counter()
        self.stats["rest_polls"] += 1
        self.last_rest_ok = fetched_at
        if not rest:
            return
        rest = sorted(rest, key=lambda r: r.created)
        # the window is Tabdeal's state at request time (not at response time): proofs use the
        # request instant, which is never later than the snapshot
        cutoff = (requested_at or fetched_at) - self.cfg.rest_settle
        self.rest_spans_s.append((fetched_at - rest[0].created).total_seconds())
        del self.rest_spans_s[:-200]
        window = [r for r in rest if r.created <= cutoff]
        tol = self.cfg.align_tolerance
        lo = window[0].created - tol if window else cutoff
        pool = sorted(
            (w for w in self._ws_recent if lo <= w[0] <= cutoff + tol), key=lambda w: w[0]
        )
        aligned = _align(window, pool, tol)
        for i, r in enumerate(window):
            key = (r.created.isoformat(), str(r.price), str(r.qty))
            if key in self._rest_seen:
                continue
            self._rest_seen[key] = r.created
            self.stats["rest_trades"] += 1
            twin = aligned.get(i)
            if twin is not None:
                twin[4] = True
                self.provenance.setdefault(twin[3], set()).add("REST")
                continue
            t = Trade(
                trade_id=f"R:{key[0]}:{key[1]}:{key[2]}",
                exch_ts=r.created - self.cfg.rest_delta_est,
                recv_ts=fetched_at,
                price=r.price,
                qty=r.qty,
                taker_side=r.side,
                raw=json.dumps(r.raw, sort_keys=True),
                source="REST",
            )
            self.provenance[t.trade_id] = {"REST"}
            self._rest_only.append([r.created, r.price, r.qty, t.trade_id, False])
            self.stats["rest_only"] += 1
            self.stats["trades_total"] += 1
            late = not self.builder.add_trade(t)
            self.sink.on_trade(t, late)
            if not late:
                self._live_trade(t)
            if late:  # its minute was already final (REST was down): auditable revision
                self.stats["rest_revisions"] += 1
                self.sink.on_late_rest_trade(t)
            else:
                self.sink.on_proven_trade(t, False, False)
        span = (rest[0].created - self.cfg.repair_delta_min, cutoff - self.cfg.rest_cover_delta)
        if span[1] > span[0] and self.rest_src is not None:
            if self.rest_src.spans and span[0] <= self.rest_src.spans[-1][1]:
                self.rest_src.extend(span[1])
            else:
                self.rest_src.open(span[0])
                self.rest_src.extend(span[1])
        self._prune_rest(fetched_at)
        self._update_hold(fetched_at)
        if self.rest_src is not None and self.rest_src.spans:
            r_end = self.rest_src.spans[-1][1]
            match_ms = (time.perf_counter() - t_start) * 1000
            req_ms = (fetched_at - requested_at).total_seconds() * 1000 if requested_at else None
            for t_end, marks in self._lat.items():
                if "rest_cover" not in marks and t_end <= r_end:
                    marks["rest_cover"] = (fetched_at - t_end).total_seconds() * 1000
                    marks["rest_request"] = req_ms
                    marks["match"] = match_ms

    # ---- V5.9 live forming candle (UI / observability only; zero trading authority) ----------

    def _live_trade(self, t: Trade, emit: bool = True) -> None:
        m = floor_minute(t.exch_ts)
        f = self.forming.get(m)
        if f is None:
            f = self.forming[m] = {
                "o": t.price,
                "h": t.price,
                "l": t.price,
                "c": t.price,
                "v": t.qty,
                "n": 1,
                "first": t.exch_ts,
                "last": t.exch_ts,
            }
        else:
            f["h"], f["l"] = max(f["h"], t.price), min(f["l"], t.price)
            if t.exch_ts < f["first"]:  # open = first trade by exchange time
                f["o"], f["first"] = t.price, t.exch_ts
            if t.exch_ts >= f["last"]:  # close = latest trade by exchange time
                f["c"], f["last"] = t.price, t.exch_ts
            f["v"] += t.qty
            f["n"] += 1
        if not emit:
            return
        self.sink.on_live(
            {
                "type": "trade",
                "t": m.isoformat(),
                "o": str(f["o"]),
                "h": str(f["h"]),
                "l": str(f["l"]),
                "c": str(f["c"]),
                "v": str(f["v"]),
                "n": f["n"],
                "price": str(t.price),
                "exch_ts": t.exch_ts.isoformat(),
                "recv_ts": t.recv_ts.isoformat(),
                "src": t.source,
            }
        )

    def _live_final(self, m1: M1Result) -> None:
        self.forming.pop(m1.open_time, None)
        c = m1.candle
        self.sink.on_live(
            {
                "type": "final",
                "t": m1.open_time.isoformat(),
                "status": m1.status.value,
                "quality": m1.quality.value if m1.quality else "DATA_GAP",
                **(
                    {
                        "o": str(c.open),
                        "h": str(c.high),
                        "l": str(c.low),
                        "c": str(c.close),
                        "v": str(c.volume),
                        "n": c.trade_count,
                    }
                    if c is not None
                    else {}
                ),
            }
        )

    def _prune_rest(self, now: datetime) -> None:
        horizon = now - timedelta(minutes=10)
        covered = self.coverage.source("R")
        for w in self._ws_recent:
            if w[0] < horizon and not w[4] and any(a <= w[0] <= b for a, b in covered.spans):
                self.stats["ws_only"] += 1
        self._ws_recent = [w for w in self._ws_recent if w[0] >= horizon]
        self._rest_only = [r for r in self._rest_only if r[0] >= horizon]
        self._rest_seen = {k: v for k, v in self._rest_seen.items() if v >= horizon}
        if len(self.provenance) > 20000:
            for k in list(self.provenance)[:5000]:
                self.provenance.pop(k, None)

    def resolve_rest_jobs(self, now: datetime) -> None:
        """A closed gap is REPAIRED once REST coverage spans it (all its trades are canonical
        now); UNRECOVERED if REST could not reach back far enough before the deadline."""
        rc = self.coverage.source("R")
        for job in list(self.jobs):
            if job.tier != "REST":
                continue
            if any(a <= job.start and job.end <= b for a, b in rc.spans):
                n = sum(
                    1
                    for r in self._rest_only
                    if job.start <= r[0] - self.cfg.rest_delta_est < job.end
                )
                self.builder.mark_repaired(job.start, job.end)
                self._finish(
                    job,
                    RepairOutcome(
                        "REPAIRED",
                        "REST_CONTINUOUS",
                        None,
                        [],
                        {
                            "recovered": n,
                            "rest_span_s": self.rest_spans_s[-1] if self.rest_spans_s else None,
                        },
                    ),
                )
            elif now - job.end > self.cfg.rest_stale + self.cfg.rest_settle:
                self._escalate(job, "REST_WINDOW_DID_NOT_REACH_GAP_START")

    def rest_health(self, now: datetime) -> dict[str, Any]:
        spans = sorted(self.rest_spans_s)
        lat = sorted(self.reconcile_latency_s)
        lo = spans[0] if spans else None
        return {
            "enabled": self.rest_src is not None,
            "healthy": self._rest_healthy(now) if self.rest_src is not None else False,
            "last_ok": self.last_rest_ok.isoformat() if self.last_rest_ok else None,
            "window_trades": 50,
            "window_span_s_min": round(lo, 1) if lo else None,
            "window_span_s_p50": round(spans[len(spans) // 2], 1) if spans else None,
            "poll_interval_s": round(self.next_rest_delay(), 1),
            # a host outage is exactly repairable while the first post-outage window still
            # reaches before it: bounded by the shortest recent window minus the margins
            "max_recoverable_gap_s": (
                round(
                    lo
                    - (self.cfg.rest_settle + self.cfg.rest_cover_delta).total_seconds()
                    - self.next_rest_delay(),
                    1,
                )
                if lo
                else None
            ),
            "reconcile_latency_s_p50": round(lat[len(lat) // 2], 1) if lat else None,
            "reconcile_latency_s_max": round(lat[-1], 1) if lat else None,
        }

    # ---- V5.6 gap repair -------------------------------------------------------------------

    def _repairable(self, start: datetime, end: datetime) -> bool:
        return self.cfg.repair_enabled and end - start <= self.cfg.repair_max_gap

    def _gap_closed(self, start: datetime, end: datetime, reason: str) -> None:
        # the start-up gap cannot be completed: the first minute's trades before the process
        # started were never observable (collector downtime is a hole, handled downstream)
        if not self.cfg.repair_enabled or reason == "COLLECTOR_START":
            return
        # V5.7: with continuous REST a gap is resolved by REST coverage (resolve_rest_jobs),
        # a restart gap included (the series resumes from the last canonical minute)
        if self._repairable(start, end):
            self.jobs.append(RepairJob(start, end, reason))
        else:
            self._escalate(RepairJob(start, end, reason), "GAP_TOO_LONG_FOR_REPAIR")

    # ---- V5.8 tier 3: validated Tabdeal chart history ----------------------------------------

    def _escalate(self, job: RepairJob, rest_failure: str) -> None:
        """Tier 2 could not prove the gap: hand it to tier 3, or fail it (UNRECOVERED)."""
        if not self.cfg.history_enabled or job.end - job.start > self.cfg.history_max_gap:
            if self.cfg.history_enabled:
                rest_failure = "GAP_TOO_LONG_FOR_HISTORY"
            self._finish(job, _unrecovered(rest_failure))
            return
        job.tier, job.rest_failure, job.attempts, job.in_flight = "HISTORY", rest_failure, 0, False
        if job not in self.jobs:
            self.jobs.append(job)
        log.info(
            "gap %s -> %s: recent-trades could not prove it (%s); trying Tabdeal history",
            job.start.isoformat(),
            job.end.isoformat(),
            rest_failure,
        )
        self._update_hold(self.clock())

    def history_eligible_at(self, job: RepairJob) -> datetime:
        return floor_minute(job.end) + MINUTE_TD + self.cfg.history_policy.settle

    def due_history_jobs(self, now: datetime) -> list[RepairJob]:
        return [
            j
            for j in self.jobs
            if j.tier == "HISTORY"
            and not j.in_flight
            and now >= self.history_eligible_at(j)
            and (j.next_try is None or now >= j.next_try)
        ]

    def history_in_time(self, job: RepairJob, now: datetime) -> bool:
        """The history deadline counts only while Tabdeal is reachable: time spent in a full
        outage (stream and REST down) never fails a gap that history can still repair."""
        base = self.history_eligible_at(job)
        if job.offline_until is not None and job.offline_until > base:
            base = job.offline_until
        return now - base < self.cfg.history_deadline

    def history_window(self, job: RepairJob) -> tuple[datetime, datetime]:
        """Request range: the gap's minutes plus the canonical overlap before it."""
        return (
            floor_minute(job.start) - self.cfg.history_policy.overlap - MINUTE_TD,
            floor_minute(job.end) + MINUTE_TD,
        )

    def resolve_history(
        self, job: RepairJob, raw: list[dict[str, Any]] | None, requested_at: datetime
    ) -> None:
        """Decide a tier-3 job from one chart-history response (None = request failed).
        Deterministic; the asyncio shell only performs the GET."""
        job.in_flight = False
        job.attempts += 1
        now = self.clock()
        if now - job.end > self.cfg.history_max_gap:  # beyond what history is trusted for
            self._finish(job, _unrecovered("HISTORY_UNAVAILABLE"), job.rest_failure)
            return
        if raw is None and not self.connected and not self._rest_healthy(now):
            job.offline_until = now  # the whole feed is down too: not a history failure
        in_time = self.history_in_time(job, now)
        if raw is None:
            if in_time:
                job.next_try = now + self.cfg.history_retry
                return
            self._finish(job, _unrecovered("HISTORY_UNAVAILABLE"), job.rest_failure)
            return
        first, last = floor_minute(job.start), floor_minute(job.end)
        needed = []
        m = max(first, self.builder.next_open_minute)
        while m <= last:
            if not self.coverage.covers(m, m + MINUTE_TD):
                needed.append(m)
            m += MINUTE_TD
        lo = first - self.cfg.history_policy.overlap
        canonical = {
            r.open_time: r.candle
            for r in self.recent_m1
            if r.candle is not None
            and r.quality in (Quality.LIVE_RECONCILED, Quality.LIVE_PROVEN_RAW)
            and lo <= r.open_time < first
        }
        out = validate_history(raw, needed, canonical, requested_at, self.cfg.history_policy)
        if not out.ok:
            if in_time and out.reason in ("HISTORY_NOT_FINAL", "HISTORY_INCOMPLETE"):
                job.next_try = now + self.cfg.history_retry
                return
            fail = RepairOutcome("UNRECOVERED", "TABDEAL_HISTORY", out.reason, [], out.detail)
            self._finish(job, fail, job.rest_failure)
            return
        self.builder.history.update(out.candles)
        self.builder.mark_repaired(job.start, job.end)  # minutes REST covered meanwhile
        self._finish(
            job,
            RepairOutcome(
                "REPAIRED",
                "TABDEAL_HISTORY",
                None,
                [],
                {**out.detail, "repair_type": "CANDLE_HISTORY_REPAIR", "unknown": ["trade_count"]},
            ),
            job.rest_failure,
        )

    def _update_hold(self, now: datetime) -> None:
        """Hold minutes that a pending or still-possible repair could complete."""
        if not self.cfg.repair_enabled:
            return
        starts = [j.start for j in self.jobs]
        g = self._gap_open_since
        limit = self.cfg.history_max_gap if self.cfg.history_enabled else self.cfg.repair_max_gap
        if g is not None and self._gap_reason != "COLLECTOR_START" and now - g <= limit:
            starts.append(g)
        if self.rest_src is not None and self._rest_healthy(now):
            # V5.7: a minute is final only once REST reconciliation has covered it, or after
            # the reconcile deadline (then it is finalized WS-only and flagged LIVE_WS_ONLY)
            end = self.rest_src.spans[-1][1] if self.rest_src.spans else None
            floor = now - self.cfg.reconcile_deadline - MINUTE_TD
            starts.append(max(end, floor) if end is not None else floor)
        self.builder.hold_from = min(starts) if starts else None

    def due_jobs(self, now: datetime) -> list[RepairJob]:
        return [
            j
            for j in self.jobs
            if not j.in_flight
            and now >= j.end + max(self.cfg.repair_settle, self.cfg.repair_delta_max)
        ]

    def resolve(self, job: RepairJob, rest: list[RestTrade] | None, fetched_at: datetime) -> None:
        """Decide one repair job with the REST window fetched at `fetched_at` (None = fetch
        failed). Deterministic; the asyncio shell only supplies the fetch."""
        job.in_flight = False
        job.attempts += 1
        if rest is None:
            if fetched_at - job.end < self.cfg.repair_deadline and job.attempts < 3:
                return  # retry on a later tick
            self._finish(job, _unrecovered("FETCH_FAILED"))
            return
        lo, hi = floor_minute(job.start), floor_minute(job.end) + timedelta(minutes=1)
        known = [
            KnownTrade(datetime.fromisoformat(fp[1]), Decimal(fp[2]), Decimal(fp[3]))
            for fp, n in self._stored.items()
            for _ in range(n)
            if lo - timedelta(minutes=1)
            <= datetime.fromisoformat(fp[1])
            < hi + timedelta(minutes=1)
        ]
        out = repair_gap(
            job.start,
            job.end,
            rest,
            known,
            delta_max=self.cfg.repair_delta_max,
            fetched_at=fetched_at,
            delta_min=self.cfg.repair_delta_min,
        )
        if out.repaired and any(t.exch_ts < self.builder.next_open_minute for t in out.trades):
            out = _unrecovered("MINUTE_ALREADY_FINALIZED")
        if out.repaired:
            for t in out.trades:
                self.builder.add_trade(t)
                self.stats["trades_total"] += 1
                self.sink.on_trade(t, False)
                self.sink.on_proven_trade(t, False, False)
            src = self.coverage.source("REPAIR")
            src.open(job.start)
            src.extend(job.end)
            self.builder.mark_repaired(job.start, job.end)
        self._finish(job, out)

    def _finish(self, job: RepairJob, out: RepairOutcome, rest_failure: str | None = None) -> None:
        if job in self.jobs:
            self.jobs.remove(job)
        if rest_failure:
            out.detail.setdefault("recent_trades_failure", rest_failure)
        self.stats["gaps_repaired" if out.repaired else "gaps_unrecovered"] += 1
        if out.repaired and out.method == "TABDEAL_HISTORY":
            self.stats["gaps_history_repaired"] += 1
        log.log(
            logging.INFO if out.repaired else logging.WARNING,
            "gap %s -> %s %s (%s) %s",
            job.start.isoformat(),
            job.end.isoformat(),
            out.status,
            out.reason or out.method,
            out.detail,
        )
        self.sink.on_repair(job.start, job.end, job.reason, out)
        self._update_hold(self.clock())

    def _emit_gap_open(self, ts: datetime) -> None:
        if self._gap_open_since is not None and not self._gap_open_emitted:
            self._gap_open_emitted = True
            self.sink.on_gap_open(self._gap_open_since, ts, self._gap_reason)

    def _release(self, force: bool = False) -> None:
        """Journal pending trades in first-arrival order once proven by a connection that
        received them. `force` (merged coverage lost): release the rest flagged in_gap."""
        while self._pending:
            p = self._pending[0]
            provers = [
                n
                for n, r in p.receipts.items()
                if (pr := self._c(n).proven_recv) is not None and r <= pr and n not in p.dead
            ]
            proven = bool(provers)
            orphan = not proven and set(p.receipts) <= p.dead
            if not (proven or orphan or force):
                break
            if orphan and not force:
                self.stats["orphans"] += 1
            self._pending.pop(0)
            # in_gap unless a proving connection received it after its coverage started
            in_gap = not any(
                (cf := self._c(n).coverage_from) is not None and p.receipts[n] >= cf
                for n in provers
            )
            self.sink.on_proven_trade(p.trade, p.late, in_gap)

    def advance(self, now: datetime) -> list[M1Result]:
        """Finalize minutes: bounded by merged proven coverage while any connection is up."""
        bound = now
        if self.connected:
            end = self.coverage.end()
            if end is None:
                return []
            bound = min(now, end + self.cfg.grace)
        self._mark_ws_proof(now)
        results = self.builder.advance(bound)
        for m1 in results:
            self.reconcile_latency_s.append((now - m1.open_time - MINUTE_TD).total_seconds())
            del self.reconcile_latency_s[:-500]
            marks = self._lat.pop(m1.open_time + MINUTE_TD, {})
            marks["finalize"] = (now - m1.open_time - MINUTE_TD).total_seconds() * 1000
            t_persist = time.perf_counter()
            key = {
                "OK": "m1_ok",
                "SYNTHETIC_NO_TRADE": "m1_synthetic",
                "DATA_GAP": "m1_data_gap",
                "UNANCHORED": "m1_unanchored",
            }[m1.status.value]
            self.stats[key] += 1
            if m1.quality is Quality.TABDEAL_HISTORY_REPAIRED:
                self.stats["m1_history_repaired"] += 1
            self.recent_m1.append(m1)
            self.sink.on_m1(m1)
            self._live_final(m1)
            m5 = self.agg.add(m1)
            if m5 is not None:
                self.sink.on_m5(m5)
            marks["persist"] = (time.perf_counter() - t_persist) * 1000
            self.sink.on_latency(m1.open_time, marks)
        if results:
            horizon = results[-1].open_time - timedelta(minutes=10)
            self.coverage.prune(horizon)
            while self._seen_order and self._seen_order[0][0] < horizon:
                _, fp = self._seen_order.pop(0)
                self._stored.pop(fp, None)
                self._seq_variants.pop(fp[0], None)
                for counts in self._conn_counts.values():
                    counts.pop(fp, None)
        return results

    def tick(self, wall: datetime, mono: float) -> None:
        """One ticker step: host-sleep detection (diagnostic, B45) then minute finalization."""
        prev = self._prev_tick
        self._prev_tick = (wall, mono)
        if prev is not None:
            wall_dt = wall - prev[0]
            mono_dt = timedelta(seconds=mono - prev[1])
            if wall_dt - mono_dt > self.cfg.host_sleep_threshold:
                self.stats["host_sleeps"] += 1
                log.warning(
                    "HOST SLEEP suspected %s -> %s (%.0fs); coverage lost, DATA_GAP",
                    prev[0].isoformat(),
                    wall.isoformat(),
                    wall_dt.total_seconds(),
                )
                self.sink.on_host_sleep(prev[0], wall)
        self._update_hold(wall)
        if self.rest_src is not None:
            self.resolve_rest_jobs(wall)
        self.advance(wall)

    def health(self, now: datetime) -> dict[str, Any]:
        end = self.coverage.end()
        lag = None if end is None else int((now - end).total_seconds() * 1000)
        conns = {
            n: {
                "connected": c.connected,
                "confirmed": c.confirmed,
                "coverage_lag_ms": (
                    None
                    if c.healthy_until is None or not c.connected
                    else int((now - c.healthy_until).total_seconds() * 1000)
                ),
                "sessions": c.sessions,
                "disconnects": c.disconnects,
                "received": c.received,
                "last_close": c.last_close.isoformat() if c.last_close else None,
                "last_close_reason": c.last_close_reason,
            }
            for n, c in self.conns.items()
        }
        return {
            "connected": self.connected,
            "healthy_until": end,
            "coverage_lag_ms": lag,
            "merged_status": (
                "COVERED" if self._gap_open_since is None and self.connected else "GAP"
            ),
            "rest": self.rest_health(now),
            "repair": {
                "enabled": self.cfg.repair_enabled,
                "pending": [
                    {"start": j.start.isoformat(), "end": j.end.isoformat(), "tier": j.tier}
                    for j in self.jobs
                ],
                "history_enabled": self.cfg.history_enabled,
                "synchronizing_history": any(j.tier == "HISTORY" for j in self.jobs),
                "gap_open_since": (
                    self._gap_open_since.isoformat() if self._gap_open_since else None
                ),
                "holding_from": (
                    self.builder.hold_from.isoformat() if self.builder.hold_from else None
                ),
                # a real gap is open or awaiting its repair decision (the routine
                # reconciliation hold of every new minute is not a repair)
                "gap_pending": bool(self.jobs)
                or (self._gap_open_since is not None and self._gap_reason != "COLLECTOR_START"),
            },
            "last_trade_exch_ts": self.last_trade_exch_ts,
            **self.stats,
            "connections": conns,
        }

    # ---- asyncio shell -------------------------------------------------------------------

    async def run(
        self,
        stop: asyncio.Event,
        fetch_recent: Callable[[], list[RestTrade]] | None = None,
        fetch_history: Callable[[datetime, datetime], list[dict[str, Any]]] | None = None,
    ) -> None:
        ticker = asyncio.create_task(self._ticker(stop))
        historian = (
            asyncio.create_task(self._history_worker(stop, fetch_history))
            if self.cfg.history_enabled and fetch_history is not None
            else None
        )
        repairer = (
            asyncio.create_task(
                self._rest_poller(stop, fetch_recent)
                if self.rest_src is not None
                else self._repairer(stop, fetch_recent)
            )
            if (self.cfg.repair_enabled or self.rest_src is not None) and fetch_recent is not None
            else None
        )
        loops = [
            asyncio.create_task(self._connection_loop(n, i * self.cfg.stagger, stop))
            for i, n in enumerate(self.cfg.connections)
        ]
        try:
            await asyncio.gather(*loops)
        finally:
            ticker.cancel()
            if repairer is not None:
                repairer.cancel()
            if historian is not None:
                historian.cancel()
            self.advance(self.clock())

    async def _rest_poller(self, stop: asyncio.Event, fetch: Callable[[], list[RestTrade]]) -> None:
        """V5.7: continuous, overlapping recent-trades windows (rate from measured retention)."""
        while not stop.is_set():
            requested = self.clock()
            try:
                rest: list[RestTrade] | None = await asyncio.wait_for(
                    asyncio.to_thread(fetch), timeout=10
                )
            except Exception as e:  # network / parse: counted; REST staleness handles it
                log.warning("REST reconciliation poll failed: %r", e)
                rest = None
            self.ingest_rest(rest, self.clock(), requested)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.next_poll_in(self.clock()))

    async def _history_worker(
        self, stop: asyncio.Event, fetch: Callable[[datetime, datetime], list[dict[str, Any]]]
    ) -> None:
        """V5.8 tier 3: one read-only chart-history GET per due job."""
        while not stop.is_set():
            for job in self.due_history_jobs(self.clock()):
                job.in_flight = True
                a, b = self.history_window(job)
                requested = self.clock()
                try:
                    raw: list[dict[str, Any]] | None = await asyncio.wait_for(
                        asyncio.to_thread(fetch, a, b), timeout=30
                    )
                except Exception as e:  # network / parse: retried until the deadline
                    log.warning("history fetch failed: %r", e)
                    raw = None
                self.resolve_history(job, raw, requested)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=1.0)

    async def _repairer(self, stop: asyncio.Event, fetch: Callable[[], list[RestTrade]]) -> None:
        while not stop.is_set():
            for job in self.due_jobs(self.clock()):
                job.in_flight = True
                try:
                    rest: list[RestTrade] | None = await asyncio.wait_for(
                        asyncio.to_thread(fetch), timeout=15
                    )
                except Exception as e:  # network / parse: retried, then UNRECOVERED
                    log.warning("repair fetch failed: %r", e)
                    rest = None
                self.resolve(job, rest, self.clock())
            await asyncio.sleep(self.cfg.tick_interval)

    async def _ticker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self.tick(self.clock(), time.monotonic())
            await asyncio.sleep(self.cfg.tick_interval)

    async def _connection_loop(self, conn: str, delay: float, stop: asyncio.Event) -> None:
        if delay > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)  # staggered start
        attempt = 0
        while not stop.is_set():
            reason = "DISCONNECTED"
            try:
                async with self._connect(
                    self.cfg.url, ping_interval=None, open_timeout=10, close_timeout=2
                ) as ws:
                    await ws.send(self.market)
                    self.session_started(self.clock(), conn)
                    attempt = 0
                    await self._session(ws, conn, stop)
                    reason = "STOPPED" if stop.is_set() else "DISCONNECTED"
            except StreamError as e:
                reason = f"STREAM_ERROR:{e}"
                log.error("[%s] stream error: %s", conn, e)
            except websockets.exceptions.ConnectionClosed as e:
                rcvd = e.rcvd
                reason = (
                    f"CLOSE_{rcvd.code}:{rcvd.reason}" if rcvd is not None else "CLOSED_NO_FRAME"
                )
                log.warning("[%s] connection lost: %r", conn, e)
            except (OSError, TimeoutError, websockets.exceptions.WebSocketException) as e:
                reason = f"{type(e).__name__}:{e}"[:200]
                log.warning("[%s] connection lost: %r", conn, e)
            self.session_ended(self.clock(), reason, conn)
            if stop.is_set():
                break
            delay_s = self.cfg.backoff[min(attempt, len(self.cfg.backoff) - 1)]
            attempt += 1
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay_s)

    async def _session(self, ws: Any, conn: str, stop: asyncio.Event) -> None:
        pinger = asyncio.create_task(self._pinger(ws, conn))
        stopper = asyncio.create_task(stop.wait())
        try:
            while True:
                recv = asyncio.create_task(ws.recv())
                done, _ = await asyncio.wait(
                    {recv, stopper, pinger}, return_when=asyncio.FIRST_COMPLETED
                )
                if recv in done:
                    self.ingest(recv.result(), self.clock(), conn)
                    continue
                recv.cancel()
                if pinger in done:
                    pinger.result()  # re-raise a ping failure
                return
        finally:
            pinger.cancel()
            stopper.cancel()

    async def _pinger(self, ws: Any, conn: str) -> None:
        while True:
            sent = self.clock()
            waiter = await ws.ping()
            try:
                await asyncio.wait_for(waiter, timeout=self.cfg.ping_timeout)
            except TimeoutError:
                await ws.close()
                raise websockets.exceptions.ConnectionClosedError(None, None) from None
            self.pong(sent, self.clock(), conn)
            await asyncio.sleep(self.cfg.ping_interval)
