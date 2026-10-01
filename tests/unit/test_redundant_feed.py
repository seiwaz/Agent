"""B46 transport redundancy: merged coverage = union of per-connection pong proofs.
DATA_GAP semantics are unchanged: any instant no connection proves is uncovered."""

from __future__ import annotations

import json
from datetime import timedelta

from sp2l.marketdata.collector import Collector, CollectorConfig
from sp2l.marketdata.m1_builder import M1Status
from tests.conftest import T0
from tests.unit.test_collector import Sink, at, frame


def dual():
    sink = Sink()
    col = Collector(CollectorConfig("BTCUSDT", connections=("A", "B")), sink, clock=lambda: at(0))
    return col, sink


def up(col, conn, t):
    col.session_started(at(t), conn)
    col.pong(at(t), at(t + 0.1), conn)  # coverage from t + 1.1 s


def statuses(out):
    return [m.status for m in out]


def test_one_connection_dropping_while_the_other_covers_is_not_a_gap():
    col, sink = dual()
    up(col, "A", 0)
    up(col, "B", 30)
    for s in range(2, 200, 2):
        col.pong(at(s), at(s + 0.1), "A")
        col.pong(at(s), at(s + 0.1), "B")
        col.ingest(frame(s, at(s + 0.5)), at(s + 0.6), "A")
        col.ingest(frame(s, at(s + 0.5)), at(s + 0.7), "B")  # same trade on B
    col.session_ended(at(150), "CLOSE_1000:please reconnect", "A")
    for s in range(200, 400, 2):
        col.pong(at(s), at(s + 0.1), "B")
    out = col.advance(at(400))
    # minutes 1-3 hold trades, 4-5 are quiet but still proven by B: covered, never DATA_GAP
    assert statuses(out)[1:6] == [M1Status.OK] * 3 + [M1Status.SYNTHETIC_NO_TRADE] * 2
    assert [e for e in sink.journal if e[0] == "GAP"] == [("GAP", T0, "COLLECTOR_START")]
    assert sink.gaps == [(T0, at(1.1), "COLLECTOR_START")]
    assert col.stats["duplicates"] == 99 and col.stats["trades_total"] == 99
    assert sum(1 for t, late in sink.trades) == 99  # each trade stored once


def test_both_connections_down_is_one_merged_gap_from_last_proof():
    col, sink = dual()
    up(col, "A", 0)
    up(col, "B", 10)
    for s in range(12, 100, 2):
        col.pong(at(s), at(s + 0.1), "A")
        col.pong(at(s), at(s + 0.1), "B")
    col.session_ended(at(100), "CLOSE_1000:please reconnect", "A")  # proven through 97 s
    col.session_ended(at(101), "CLOSE_1000:please reconnect", "B")  # proven through 97 s
    gaps = [e for e in sink.journal if e[0] == "GAP"]
    assert gaps[-1] == ("GAP", at(97), "B:CLOSE_1000:please reconnect")
    up(col, "A", 110)  # coverage restarts at 111.1 s
    assert sink.gaps[-1] == (at(97), at(111.1), "B:CLOSE_1000:please reconnect")
    for s in range(112, 250, 2):
        col.pong(at(s), at(s + 0.1), "A")
    out = col.advance(at(250))
    # minute 1 (60-120) contains the merged hole -> DATA_GAP; minutes 2, 3 covered
    assert statuses(out)[1] == M1Status.DATA_GAP
    assert (
        statuses(out)[2:4] == [M1Status.SYNTHETIC_NO_TRADE] * 2
        or statuses(out)[2:4] == [M1Status.UNANCHORED] * 2
    )


def test_handover_union_covers_a_minute_neither_covers_alone():
    col, _ = dual()
    up(col, "A", 0)
    for s in range(2, 92, 2):
        col.pong(at(s), at(s + 0.1), "A")  # A proven 1.1 .. 89
    up(col, "B", 80)  # B proven from 81.1
    col.session_ended(at(91), "CLOSE_1000:please reconnect", "A")
    for s in range(82, 200, 2):
        col.pong(at(s), at(s + 0.1), "B")
    col.ingest(frame(1, at(65)), at(65.1), "A")
    out = col.advance(at(200))
    assert statuses(out)[1] == M1Status.OK  # 60-120: A to 89, B from 81.1 - no hole


def test_pong_on_a_never_proves_a_trade_only_b_received():
    col, sink = dual()
    up(col, "A", 0)
    up(col, "B", 0)
    col.ingest(frame(7, at(20)), at(20.0), "B")
    col.pong(at(25), at(25.1), "A")  # A's proof covers 25 s, but A never received trade 7
    assert [e for e in sink.journal if e[0] == "TRADE"] == []
    col.pong(at(26), at(26.1), "B")
    assert sink.journal[-1] == ("TRADE", "7", False)


def test_one_sequence_can_carry_several_fills_all_are_kept():
    """Observed on Tabdeal 2026-09-26: same sequence + timestamp, different price/amount."""
    col, sink = dual()
    up(col, "A", 0)
    up(col, "B", 0)
    col.ingest(frame(9, at(20), price="84000.1", amount="0.001"), at(20.0), "A")
    col.ingest(frame(9, at(20), price="84000.2", amount="0.004"), at(20.0), "A")
    col.ingest(frame(9, at(20), price="84000.1", amount="0.001"), at(20.1), "B")  # dup
    col.ingest(frame(9, at(20), price="84000.2", amount="0.004"), at(20.1), "B")  # dup
    assert len(sink.trades) == 2 and col.stats["duplicates"] == 2
    assert {str(t.price) for t, _ in sink.trades} == {"84000.1", "84000.2"}
    assert col.stats["multi_fill_sequences"] == 1 and col.stats["conflicts"] == 0
    assert sink.conflicts[0][1]["classification"] == "MULTI_FILL_SAME_SEQUENCE"


def test_identical_repeated_fill_counts_by_max_multiplicity():
    col, sink = dual()
    up(col, "A", 0)
    up(col, "B", 0)
    for _ in range(2):  # two genuinely identical fills on A
        col.ingest(frame(5, at(20)), at(20.0), "A")
    col.ingest(frame(5, at(20)), at(20.1), "B")  # B has seen only one of them so far
    col.ingest(frame(5, at(20)), at(20.2), "B")
    assert len(sink.trades) == 2 and col.stats["duplicates"] == 2
    assert len({t.trade_id for t, _ in sink.trades}) == 2


def test_same_sequence_price_amount_but_different_time_is_a_conflict():
    col, sink = dual()
    up(col, "A", 0)
    up(col, "B", 0)
    col.ingest(frame(7, at(20)), at(20.0), "A")
    col.ingest(frame(7, at(21)), at(21.0), "B")
    assert col.stats["conflicts"] == 1
    assert sink.conflicts[0][1]["classification"] == "TIMESTAMP_CONFLICT"


def test_orphan_trade_when_receiver_dies_unproven_is_in_gap():
    col, sink = dual()
    up(col, "A", 0)
    up(col, "B", 0)
    col.ingest(frame(5, at(20)), at(20.0), "A")  # only A received it
    col.session_ended(at(21), "CLOSE_1000:", "A")  # B still covering
    assert sink.journal[-1] == ("TRADE", "5", True) and col.stats["orphans"] == 1


def test_connection_events_record_close_reasons_and_lifetime():
    col, sink = dual()
    up(col, "A", 0)
    col.session_ended(at(3600), "CLOSE_1000:please reconnect", "A")
    assert ("A", "CLOSED", "CLOSE_1000:please reconnect") in sink.conn_events
    h = col.health(at(3601))
    assert h["connections"]["A"]["disconnects"] == 1 and json.dumps(h, default=str)


def test_single_connection_config_matches_original_semantics():
    sink = Sink()
    col = Collector(CollectorConfig("BTCUSDT"), sink, clock=lambda: at(0))
    col.session_started(at(0))
    col.pong(at(0), at(0.1))
    for s in range(2, 130, 2):
        col.pong(at(s), at(s + 0.1))
    assert statuses(col.advance(at(130)))[0:2] == [M1Status.DATA_GAP, M1Status.UNANCHORED]
    assert timedelta(0) == timedelta(0)
