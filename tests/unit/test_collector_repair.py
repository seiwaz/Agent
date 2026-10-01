"""V5.6 collector gap repair: redundancy first, exact repair second, reset last."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D

from sp2l.marketdata.collector import Collector, CollectorConfig
from sp2l.marketdata.m1_builder import M1Status, Quality
from sp2l.marketdata.tabdeal_public import RestTrade
from tests.conftest import T0
from tests.unit.test_collector import Sink, frame

REC = timedelta(milliseconds=30)  # Tabdeal record time trails the stream time


def at(s: float):
    return T0 + timedelta(seconds=s)


class RepairSink(Sink):
    def __init__(self):
        super().__init__()
        self.repairs = []

    def on_repair(self, start, end, reason, outcome):
        self.repairs.append((start, end, outcome.status, outcome.reason))


# one trade every 3 s, price walking; (seq, t, price)
TAPE = [(i + 1, 1.5 + 3 * i, D("84000") + D(i % 7) - D(i % 3)) for i in range(60)]


def make(conns=("A", "B"), **kw):
    sink = RepairSink()
    cfg = CollectorConfig("BTCUSDT", connections=conns, repair_enabled=True, **kw)
    return Collector(cfg, sink, clock=lambda: at(0)), sink


def feed(col, conns, lo, hi):
    """Deliver TAPE trades with lo <= t < hi on `conns`, with pongs proving them."""
    for seq, t, px in TAPE:
        if lo <= t < hi:
            for c in conns:
                col.ingest(frame(seq, at(t), price=str(px)), at(t + 0.1), c)
                col.pong(at(t + 0.2), at(t + 0.3), c)


def rest_window(lo, hi):
    return [RestTrade(at(t) + REC, px, D("0.001"), "BUY", {}) for _, t, px in TAPE if lo <= t < hi]


def reference():
    col, sink = make(conns=("A",))
    col.session_started(at(0), "A")
    col.pong(at(0), at(0.1), "A")
    feed(col, ("A",), 0, 181)
    col.pong(at(185), at(185.1), "A")
    col.advance(at(186))
    return {m.open_time: m for m in sink.m1}


def test_one_connection_dropping_while_the_other_covers_is_no_gap_at_all():
    col, sink = make()
    for c in ("A", "B"):
        col.session_started(at(0), c)
        col.pong(at(0), at(0.1), c)
    feed(col, ("A", "B"), 0, 70)
    col.session_ended(at(70), "CLOSE_1000:please reconnect", "A")  # B keeps covering
    feed(col, ("B",), 70, 90)
    col.session_started(at(90), "A")
    col.pong(at(90), at(90.1), "A")
    feed(col, ("A", "B"), 90, 181)
    col.pong(at(185), at(185.1), "A")
    col.advance(at(186))
    assert col.jobs == [] and sink.repairs == [] and col.builder.hold_from is None
    assert [m.status for m in sink.m1[1:]] == [M1Status.OK, M1Status.OK]
    ref = reference()
    for m in sink.m1[1:]:
        assert m.candle == ref[m.open_time].candle and m.quality is Quality.LIVE_PROVEN_RAW


def _gap_run(rest):
    """A and B both drop at 70 s, nothing is received until they are back at 100 s."""
    col, sink = make()
    for c in ("A", "B"):
        col.session_started(at(0), c)
        col.pong(at(0), at(0.1), c)
    feed(col, ("A", "B"), 0, 70)
    for c in ("A", "B"):
        col.session_ended(at(70), "CLOSED_NO_FRAME", c)
    assert col.builder.hold_from is not None  # GAP_PENDING_REPAIR: minutes are held
    col.tick(at(99), 1000.0)
    held = [m.open_time for m in sink.m1]
    assert at(60) not in held  # the gap minute is not finalized as DATA_GAP yet
    for c in ("A", "B"):
        col.session_started(at(100), c)
        col.pong(at(100), at(100.1), c)
    feed(col, ("A", "B"), 100, 181)
    (job,) = col.jobs
    col.resolve(job, rest, at(106))
    col.pong(at(185), at(185.1), "A")
    col.advance(at(186))
    return col, sink


def test_exact_repair_gives_the_uninterrupted_candles_without_any_reset():
    col, sink = _gap_run(rest_window(40, 106))
    assert sink.repairs[-1][2] == "REPAIRED", sink.repairs
    got = {m.open_time: m for m in sink.m1}
    ref = reference()
    for t in (at(60), at(120)):
        assert got[t].status is M1Status.OK and got[t].candle == ref[t].candle
    assert got[at(60)].quality is Quality.REPAIRED_TABDEAL  # lineage kept
    assert got[at(120)].quality is Quality.LIVE_PROVEN_RAW
    assert col.builder.hold_from is None
    assert not any(m.status is M1Status.DATA_GAP for m in sink.m1[1:])
    # recovered trades are stored and journaled as repair-sourced, never as live
    recovered = [t for t, _ in sink.trades if t.source == "REPAIR_REST"]
    assert recovered and all(at(70) <= t.exch_ts < at(101.1) for t in recovered)


def test_unrecoverable_gap_falls_back_to_the_existing_data_gap_rule():
    col, sink = _gap_run(rest_window(75, 106))  # REST window does not reach the gap start
    assert sink.repairs[-1][2:] == ("UNRECOVERED", "REST_WINDOW_TOO_SHORT")
    got = {m.open_time: m for m in sink.m1}
    assert got[at(60)].status is M1Status.DATA_GAP  # V5.5 behavior unchanged
    assert col.builder.hold_from is None


def test_a_gap_too_long_for_the_rest_window_is_unrecovered_immediately_and_not_held():
    col, sink = make(repair_max_gap=timedelta(seconds=10))
    for c in ("A", "B"):
        col.session_started(at(0), c)
        col.pong(at(0), at(0.1), c)
    feed(col, ("A", "B"), 0, 70)
    for c in ("A", "B"):
        col.session_ended(at(70), "CLOSED_NO_FRAME", c)
    col.tick(at(90), 1000.0)  # past max_gap: the hold is released while still disconnected
    assert col.builder.hold_from is None
    for c in ("A", "B"):
        col.session_started(at(100), c)
        col.pong(at(100), at(100.1), c)
    assert col.jobs == [] and sink.repairs[-1][2:] == ("UNRECOVERED", "GAP_TOO_LONG_FOR_REPAIR")


def test_fetch_failures_retry_then_fail_closed():
    col, sink = _gap_run(None)
    assert col.jobs  # first failure: retried later
    (job,) = col.jobs
    col.resolve(job, None, at(140))  # past the deadline
    assert sink.repairs[-1][2:] == ("UNRECOVERED", "FETCH_FAILED")
    col.advance(at(200))
    assert {m.open_time: m for m in sink.m1}[at(60)].status is M1Status.DATA_GAP


def test_same_sequence_multi_fills_are_all_kept_and_logged_with_raw_payloads():
    col, sink = make(conns=("A",))
    col.session_started(at(0), "A")
    col.pong(at(0), at(0.1), "A")
    col.ingest(frame(7, at(5), price="100", amount="0.001"), at(5.1), "A")
    col.ingest(frame(7, at(5), price="101", amount="0.002"), at(5.1), "A")
    stored = [t for t, _ in sink.trades]
    assert [str(t.price) for t in stored] == ["100", "101"]  # never silently dropped
    assert all(t.raw and '"sequence": 7' in t.raw for t in stored)
    (conf,) = sink.conflicts
    assert conf[1]["classification"] == "MULTI_FILL_SAME_SEQUENCE" and conf[2]["raw"]
