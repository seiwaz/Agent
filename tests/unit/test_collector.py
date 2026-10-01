"""Collector: proven coverage (B31/B34), gap reporting, parsing, async loop."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta
from decimal import Decimal as D

import pytest

from sp2l.marketdata.collector import Collector, CollectorConfig
from sp2l.marketdata.m1_builder import M1Status
from sp2l.marketdata.tabdeal_ws import StreamError, parse_message, ws_market
from tests.conftest import T0


def frame(seq: int, ts, price="84026.2", amount="0.001", market="BTC_USDT") -> str:
    return json.dumps(
        {
            "trade": {
                "amount": amount,
                "price": price,
                "updated": ts.isoformat(sep=" "),
                "sequence": seq,
                "side_name": "Buy",
                "symbol": market,
            }
        }
    )


class Sink:
    def __init__(self):
        self.trades, self.m1, self.m5, self.gaps, self.conn = [], [], [], [], []
        self.journal, self.sleeps, self.conn_events, self.conflicts = [], [], [], []

    def on_proven_trade(self, t, late, in_gap):
        self.journal.append(("TRADE", t.trade_id.split(":")[0], in_gap))  # sequence part

    def on_gap_open(self, start, ts, reason):
        self.journal.append(("GAP", start, reason))

    def on_host_sleep(self, a, b):
        self.sleeps.append((a, b))

    def on_trade(self, t, late):
        self.trades.append((t, late))

    def on_m1(self, m):
        self.m1.append(m)

    def on_m5(self, m):
        self.m5.append(m)

    def on_gap(self, a, b, r):
        self.gaps.append((a, b, r))

    def on_connection(self, c, ts):
        self.conn.append(c)

    def on_connection_event(self, conn, kind, ts, reason, detail):
        self.conn_events.append((conn, kind, reason))

    def on_live(self, event):
        self.live = getattr(self, "live", [])
        self.live.append(event)

    def on_latency(self, minute, stages):
        pass

    def on_conflict(self, trade_id, first, other):
        self.conflicts.append((trade_id, first, other))


def at(sec: float):
    return T0 + timedelta(seconds=sec)


def make(start=0.0):
    sink = Sink()
    col = Collector(CollectorConfig("BTCUSDT"), sink, clock=lambda: at(start))
    return col, sink


def test_parse_observed_format_and_symbol_mapping():
    assert ws_market("BTCUSDT") == "BTC_USDT"
    t = parse_message(frame(38127895806, at(5)), at(5.1), "BTC_USDT")
    assert t is not None and t.trade_id == "38127895806" and t.price == D("84026.2")
    assert t.exch_ts == at(5) and t.taker_side == "BUY"
    assert parse_message(frame(1, at(5), market="ETH_USDT"), at(5), "BTC_USDT") is None
    assert parse_message("not json", at(5), "BTC_USDT") is None
    with pytest.raises(StreamError):
        parse_message('{"error": "Invalid params"}', at(5), "BTC_USDT")


def test_minutes_wait_for_proven_coverage_then_finalize():
    col, sink = make()
    col.session_started(at(1))
    col.pong(at(1), at(1.2))  # first pong: coverage restarts at 2.2 s; start gap reported
    assert sink.gaps == [(T0, at(2.2), "COLLECTOR_START")]
    col.ingest(frame(1, at(70)), at(70.1))
    assert col.advance(at(125)) == []  # no coverage proof beyond 2.2 s yet
    col.pong(at(124), at(124.1))  # proves coverage through 123 s
    out = col.advance(at(125))
    assert [m.status for m in out] == [M1Status.DATA_GAP, M1Status.OK]  # minute 0 had the gap


def test_quiet_minute_is_synthetic_only_under_proven_coverage():
    col, sink = make()
    col.session_started(at(0))
    col.pong(at(0), at(0.1))
    col.ingest(frame(1, at(65)), at(65.1))
    col.pong(at(200), at(200.1))  # coverage through 199 s: minutes 1 and 2 covered
    out = col.advance(at(200))
    assert [m.status for m in out] == [M1Status.DATA_GAP, M1Status.OK, M1Status.SYNTHETIC_NO_TRADE]


def test_disconnect_marks_gap_and_reconnect_reports_it():
    col, sink = make()
    col.session_started(at(0))
    col.pong(at(0), at(0.1))
    col.ingest(frame(1, at(61)), at(61.1))
    col.pong(at(100), at(100.1))  # healthy through 99 s only
    col.session_ended(at(150), "DISCONNECTED")
    out = col.advance(at(300))  # disconnected: finalize on the clock
    # minute 1 (60-120 s) held a trade, but coverage was never proven through its end:
    # the silent drop may have lost trades, so it must be DATA_GAP, never OK/synthetic
    assert [m.status for m in out] == [M1Status.DATA_GAP] * 4
    col.session_started(at(310))
    col.pong(at(310), at(310.2))
    assert sink.gaps[-1] == (at(99), at(311.2), "DISCONNECTED")
    col.pong(at(482), at(482.1))  # coverage through 481 s
    out = col.advance(at(483))
    # minutes 4-5 overlap the gap; minutes 6-7 are covered but have no post-gap anchor (B34)
    assert [m.status for m in out] == [
        M1Status.DATA_GAP,
        M1Status.DATA_GAP,
        M1Status.UNANCHORED,
        M1Status.UNANCHORED,
    ]
    col.ingest(frame(2, at(485)), at(485.1))  # first genuine post-gap trade
    col.pong(at(610), at(610.1))
    out = col.advance(at(611))
    assert [m.status for m in out] == [M1Status.OK, M1Status.SYNTHETIC_NO_TRADE]


def test_async_loop_with_fake_websocket():
    base = time.monotonic()

    def clock():  # 10 simulated minutes per real second
        return T0 + timedelta(seconds=(time.monotonic() - base) * 600)

    class FakeWS:
        def __init__(self):
            self.seq = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send(self, msg):
            assert msg == "BTC_USDT"

        async def recv(self):
            await asyncio.sleep(0.005)
            self.seq += 1
            return frame(self.seq, clock(), price=str(84000 + self.seq % 7))

        async def ping(self):
            fut = asyncio.get_running_loop().create_future()
            fut.set_result(None)
            return fut

        async def close(self):
            pass

    sink = Sink()
    col = Collector(
        CollectorConfig("BTCUSDT", ping_interval=0.01, tick_interval=0.01),
        sink,
        clock=clock,
        connect=lambda *a, **k: FakeWS(),
    )

    async def go():
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.8, stop.set)
        await col.run(stop)

    asyncio.run(go())
    statuses = [m.status for m in sink.m1]
    assert sink.conn[0] is True and sink.gaps and sink.gaps[0][2] == "COLLECTOR_START"
    assert statuses.count(M1Status.OK) >= 3
    assert M1Status.SYNTHETIC_NO_TRADE not in statuses
    assert sink.m5, "at least one M5 bar"
    assert all(not late for _, late in sink.trades[:50])


def test_trades_are_journaled_only_once_coverage_is_proven_b39():
    col, sink = make()
    col.session_started(at(0))
    assert sink.journal == [("GAP", T0, "COLLECTOR_START")]  # no coverage before start
    col.pong(at(0), at(0.1))
    col.ingest(frame(1, at(5)), at(5.0))
    col.ingest(frame(2, at(6)), at(6.0))
    assert [e for e in sink.journal if e[0] == "TRADE"] == []  # not yet proven
    col.pong(at(5.5), at(5.6))  # proves trades received before 5.5 s
    assert sink.journal[-1] == ("TRADE", "1", False)
    col.pong(at(7), at(7.1))
    assert sink.journal[-1] == ("TRADE", "2", False)


def test_disconnect_journals_gap_before_unproven_trades_flagged_in_gap():
    col, sink = make()
    col.session_started(at(0))
    col.pong(at(0), at(0.1))
    col.ingest(frame(1, at(10)), at(10.0))
    col.pong(at(11), at(11.1))  # trade 1 proven
    col.ingest(frame(2, at(12)), at(12.0))  # never proven: connection drops
    col.session_ended(at(20), "DISCONNECTED")
    assert sink.journal[-3:] == [
        ("TRADE", "1", False),
        ("GAP", at(10), "DISCONNECTED"),
        ("TRADE", "2", True),
    ]


def test_host_sleep_detected_from_wall_vs_monotonic_b45():
    col, sink = make()
    col.tick(at(0), 1000.0)
    col.tick(at(0.25), 1000.25)  # normal tick
    assert sink.sleeps == []
    col.tick(at(600), 1000.5)  # wall jumped 10 min, monotonic 0.25 s: the host slept
    assert sink.sleeps == [(at(0.25), at(600))] and col.stats["host_sleeps"] == 1
