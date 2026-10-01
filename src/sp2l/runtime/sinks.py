"""Collector sinks: persist market data and the market-event journal (Shadow input)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sp2l.marketdata.collector import CollectorSink
from sp2l.marketdata.m1_builder import M1Result, Trade
from sp2l.marketdata.m5_aggregator import M5Result
from sp2l.persistence.market_store import MarketStore

log = logging.getLogger("sp2l.sinks")


class MarketStoreSink:
    def __init__(self, store: MarketStore) -> None:
        self.store = store

    def on_trade(self, trade: Trade, late: bool) -> None:
        self.store.insert_trade(trade, late)

    def on_m1(self, m1: M1Result) -> None:
        self.store.upsert_m1(m1, lineage=False)  # candle first, then its journal event
        self.store.journal_m1(m1)  # the strategy can act from here on
        self.store.write_lineage(m1)  # per-trade provenance: after the strategy's input
        if m1.candle is None:
            log.warning("M1 %s %s", m1.open_time.isoformat(), m1.status.value)

    def on_m5(self, m5: M5Result) -> None:
        self.store.upsert_m5(m5)

    def on_gap(self, start: datetime, end: datetime, reason: str) -> None:
        self.store.insert_gap(start, end, reason)
        log.warning("coverage gap %s -> %s (%s)", start.isoformat(), end.isoformat(), reason)

    def on_connection(self, connected: bool, ts: datetime) -> None:
        log.info("stream %s at %s", "connected" if connected else "disconnected", ts.isoformat())

    def on_proven_trade(self, trade: Trade, late: bool, in_gap: bool) -> None:
        self.store.journal_trade(trade, late, in_gap)

    def on_gap_open(self, start: datetime, ts: datetime, reason: str) -> None:
        self.store.journal_gap(start, ts, reason)

    def on_host_sleep(self, start: datetime, end: datetime) -> None:
        self.store.insert_host_sleep(start, end)

    def on_connection_event(
        self, conn: str, kind: str, ts: datetime, reason: str | None, detail: dict[str, Any]
    ) -> None:
        self.store.connection_event(conn, kind, ts, reason, detail)
        if kind == "CLOSED":
            log.warning("[%s] closed at %s: %s", conn, ts.isoformat(), reason)

    def on_conflict(self, trade_id: str, first: dict[str, Any], other: dict[str, Any]) -> None:
        self.store.conflict(trade_id, first, other)

    def on_latency(self, minute: datetime, stages: dict[str, float | None]) -> None:
        self.store.record_latency(minute, stages)

    def on_late_rest_trade(self, trade: Trade) -> None:
        """V5.7 rolling reconciliation: a REST-only trade for an already-final minute."""
        out = self.store.revise_m1_from_trades(
            trade.exch_ts, reason="REST_RECONCILIATION_AFTER_FINALIZATION", source="REST"
        )
        if out == "REVISED":  # the chart replaces that minute's candle with the revision
            self.store.notify_candle(trade.exch_ts)

    def on_live(self, event: dict[str, Any]) -> None:
        self.store.notify_live(event)

    def on_repair(self, start: datetime, end: datetime, reason: str, outcome: Any) -> None:
        self.store.record_repair(
            start,
            end,
            reason,
            outcome.status,
            outcome.method,
            outcome.reason,
            len(outcome.trades),
            outcome.detail,
        )


class FanoutSink:
    def __init__(self, *sinks: CollectorSink) -> None:
        self.sinks = sinks

    def on_trade(self, trade: Trade, late: bool) -> None:
        for s in self.sinks:
            s.on_trade(trade, late)

    def on_m1(self, m1: M1Result) -> None:
        for s in self.sinks:
            s.on_m1(m1)

    def on_m5(self, m5: M5Result) -> None:
        for s in self.sinks:
            s.on_m5(m5)

    def on_gap(self, start: datetime, end: datetime, reason: str) -> None:
        for s in self.sinks:
            s.on_gap(start, end, reason)

    def on_connection(self, connected: bool, ts: datetime) -> None:
        for s in self.sinks:
            s.on_connection(connected, ts)

    def on_proven_trade(self, trade: Trade, late: bool, in_gap: bool) -> None:
        for s in self.sinks:
            s.on_proven_trade(trade, late, in_gap)

    def on_gap_open(self, start: datetime, ts: datetime, reason: str) -> None:
        for s in self.sinks:
            s.on_gap_open(start, ts, reason)

    def on_host_sleep(self, start: datetime, end: datetime) -> None:
        for s in self.sinks:
            s.on_host_sleep(start, end)

    def on_connection_event(
        self, conn: str, kind: str, ts: datetime, reason: str | None, detail: dict[str, Any]
    ) -> None:
        for s in self.sinks:
            s.on_connection_event(conn, kind, ts, reason, detail)

    def on_conflict(self, trade_id: str, first: dict[str, Any], other: dict[str, Any]) -> None:
        for s in self.sinks:
            s.on_conflict(trade_id, first, other)

    def on_repair(self, start: datetime, end: datetime, reason: str, outcome: Any) -> None:
        for s in self.sinks:
            s.on_repair(start, end, reason, outcome)

    def on_late_rest_trade(self, trade: Trade) -> None:
        for s in self.sinks:
            s.on_late_rest_trade(trade)

    def on_latency(self, minute: datetime, stages: dict[str, float | None]) -> None:
        for s in self.sinks:
            s.on_latency(minute, stages)

    def on_live(self, event: dict[str, Any]) -> None:
        for s in self.sinks:
            s.on_live(event)
