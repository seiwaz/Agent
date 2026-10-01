"""V5.7 continuous REST reconciliation: WS A + WS B + recent-trades -> canonical trades."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D

from sp2l.marketdata.collector import Collector, CollectorConfig
from sp2l.marketdata.m1_builder import M1Status, Quality
from sp2l.marketdata.tabdeal_public import RestTrade
from tests.conftest import T0
from tests.unit.test_collector import frame
from tests.unit.test_collector_repair import RepairSink

REC = timedelta(milliseconds=30)


def at(s: float):
    return T0 + timedelta(seconds=s)


# (seq, t, price): a trade every 3 s; minute 1 = [60, 120)
TAPE = [(i + 1, 1.5 + 3 * i, D("84000") + D(i % 7) - D(i % 3)) for i in range(80)]
HIGH = (1000, 90.0, D("84100"))  # a trade the WebSocket never delivers
TAPE_ALL = sorted([*TAPE, HIGH], key=lambda x: x[1])


def make(conns=("A", "B"), **kw):
    kw.setdefault("rest_stale", timedelta(seconds=300))  # the harness polls REST sparsely
    sink = RepairSink()
    sink.late = []
    sink.on_late_rest_trade = lambda t: sink.late.append(t)
    cfg = CollectorConfig(
        "BTCUSDT", connections=conns, repair_enabled=True, rest_enabled=True, **kw
    )
    return Collector(cfg, sink, clock=lambda: at(0)), sink


def start(col, conns=("A", "B"), t=0.0):
    for c in conns:
        col.session_started(at(t), c)
        col.pong(at(t), at(t + 0.1), c)


def ws(col, lo, hi, conns=("A", "B"), skip=()):
    for seq, t, px in TAPE:
        if lo <= t < hi and seq not in skip:
            for c in conns:
                col.ingest(frame(seq, at(t), price=str(px)), at(t + 0.1), c)
                col.pong(at(t + 0.2), at(t + 0.3), c)


def rest(col, lo, hi, fetched, tape=TAPE_ALL):
    col.ingest_rest(
        [
            RestTrade(at(t) + REC, px, D("0.001"), "BUY", {"seq": seq})
            for seq, t, px in tape
            if lo <= t < hi
        ],
        at(fetched),
    )


def minute(sink, s):
    return {m.open_time: m for m in sink.m1}.get(at(s))


def reference():
    """Every trade (incl. HIGH) delivered on a single connection, no REST involvement."""
    sink = RepairSink()
    col = Collector(CollectorConfig("BTCUSDT"), sink, clock=lambda: at(0))
    col.session_started(at(0), "A")
    col.pong(at(0), at(0.1), "A")
    for seq, t, px in TAPE_ALL:
        col.ingest(frame(seq, at(t), price=str(px)), at(t + 0.1), "A")
        col.pong(at(t + 0.2), at(t + 0.3), "A")
    col.pong(at(400), at(400.1), "A")
    col.advance(at(401))
    return {m.open_time: m.candle for m in sink.m1}


def run(skip=(), tape=TAPE_ALL):
    col, sink = make()
    start(col)
    ws(col, 0, 125, skip=skip)
    col.tick(at(125), 1.0)
    assert minute(sink, 60) is None  # held: not final until REST reconciled it
    rest(col, 20, 125, 128, tape)
    col.tick(at(128), 2.0)
    col.pong(at(129), at(129.1), "A")
    col.advance(at(130))
    return col, sink


def test_ws_omits_a_high_trade_rest_restores_it_before_finalization():
    col, sink = run()
    m = minute(sink, 60)
    assert m.status is M1Status.OK and m.candle == reference()[at(60)]
    assert m.candle.high == D("84100") and m.quality is Quality.LIVE_RECONCILED
    assert m.lineage["rest_only"] == 1 and m.lineage["ws_a"] == m.lineage["ws_b"] == 20
    assert col.stats["rest_only"] == 1


def test_ws_omits_first_last_and_volume_trades_open_close_count_corrected():
    first = min(s for s, t, _ in TAPE if 60 <= t < 120)
    last = max(s for s, t, _ in TAPE if 60 <= t < 120)
    _, sink = run(skip=(first, last))
    m = minute(sink, 60)
    ref = reference()[at(60)]
    assert (m.candle.open, m.candle.close, m.candle.volume, m.candle.trade_count) == (
        ref.open,
        ref.close,
        ref.volume,
        ref.trade_count,
    )


def test_trades_seen_by_a_b_and_rest_are_stored_exactly_once():
    col, sink = run()
    ids = [t.trade_id for t, _ in sink.trades]
    assert len(ids) == len(set(ids)) == len([x for x in TAPE_ALL if x[1] < 125])
    ws_trade = next(
        t
        for t, _ in sink.trades
        if t.source == "WS" and 60 <= (t.exch_ts - T0).total_seconds() < 120
    )
    assert col.provenance[ws_trade.trade_id] == {"A", "B", "REST"}


def test_a_and_b_both_down_rest_covers_the_gap_no_data_gap_and_no_reset():
    col, sink = make()
    start(col)
    ws(col, 0, 70)
    for c in ("A", "B"):
        col.session_ended(at(70), "CLOSED_NO_FRAME", c)
    col.tick(at(80), 1.0)
    start(col, t=95)
    ws(col, 95, 125)
    rest(col, 20, 125, 128)  # the first post-outage window reaches back before the gap
    col.tick(at(128), 2.0)
    col.pong(at(129), at(129.1), "A")
    col.advance(at(130))
    m = minute(sink, 60)
    assert m.status is M1Status.OK and m.candle == reference()[at(60)]
    assert m.quality is Quality.RECENT_TRADES_REPAIRED and m.repaired
    assert sink.repairs[-1][2] == "REPAIRED" and col.stats["gaps_unrecovered"] == 0


def test_genuine_unrecoverable_gap_fails_closed():
    col, sink = make(rest_stale=timedelta(seconds=45))
    start(col)
    ws(col, 0, 70)
    for c in ("A", "B"):
        col.session_ended(at(70), "CLOSED_NO_FRAME", c)
    start(col, t=95)
    ws(col, 95, 125)
    rest(col, 80, 125, 128)  # window no longer reaches the gap start
    col.tick(at(200), 2.0)
    col.pong(at(199), at(199.1), "A")
    col.advance(at(200))
    assert minute(sink, 60).status is M1Status.DATA_GAP
    assert sink.repairs[-1][2:] == ("UNRECOVERED", "REST_WINDOW_DID_NOT_REACH_GAP_START")


def test_rest_unavailable_minutes_finalize_ws_only_after_the_deadline():
    col, sink = make(rest_stale=timedelta(seconds=45))
    start(col)
    ws(col, 0, 125)
    col.ingest_rest(None, at(30))  # errors only
    col.pong(at(190), at(190.1), "A")
    col.tick(at(190), 1.0)  # REST stale, past the reconcile deadline
    m = minute(sink, 60)
    assert m.status is M1Status.OK and m.quality is Quality.LIVE_WS_ONLY


def test_zero_trade_minute_is_synthetic_only_after_rest_reconciliation():
    quiet = [(s, t, p) for s, t, p in TAPE if not 60 <= t < 120]
    col, sink = make()
    start(col)
    for seq, t, px in quiet:
        if t < 125:
            for c in ("A", "B"):
                col.ingest(frame(seq, at(t), price=str(px)), at(t + 0.1), c)
    for c in ("A", "B"):
        col.pong(at(124), at(124.1), c)
    rest(col, 20, 125, 128, quiet)
    col.tick(at(128), 1.0)
    assert minute(sink, 60).status is M1Status.SYNTHETIC_NO_TRADE


def test_collector_restart_resumes_the_canonical_series_without_a_warmup_reset():
    # first process: minutes 0..2 final, then it dies
    col, sink = make()
    start(col)
    ws(col, 0, 181)
    rest(col, 60, 182, 185.5)
    col.tick(at(185.5), 1.0)
    first = {m.open_time: m for m in sink.m1}
    assert first[at(120)].status is M1Status.OK
    # second process continues at 180 s with the final minutes of the open M5 bucket
    bucket = [first[at(0)], first[at(60)], first[at(120)]]
    sink2 = RepairSink()
    sink2.on_late_rest_trade = lambda t: None
    col2 = Collector(
        CollectorConfig(
            "BTCUSDT",
            connections=("A", "B"),
            repair_enabled=True,
            rest_enabled=True,
            rest_stale=timedelta(seconds=300),
        ),
        sink2,
        clock=lambda: at(186),
        resume_from=at(180),
        resume_m1=bucket,
    )
    start(col2, t=186)
    for seq, t, px in TAPE:
        if 186 <= t < 305:
            for c in ("A", "B"):
                col2.ingest(frame(seq, at(t), price=str(px)), at(t + 0.1), c)
                col2.pong(at(t + 0.2), at(t + 0.3), c)
    rest(col2, 150, 305, 308)  # covers the restart downtime [180, ~187)
    col2.tick(at(308), 1.0)
    col2.pong(at(309), at(309.1), "A")
    col2.advance(at(310))
    got = {m.open_time: m for m in sink2.m1}
    ref = reference()
    assert got[at(180)].status is M1Status.OK and got[at(180)].candle == ref[at(180)]
    assert not any(m.status is M1Status.DATA_GAP for m in sink2.m1)
    # the resumed aggregator completes the bucket begun before the restart (no partial-bucket
    # skip, i.e. no M5 hole); its status is DATA_GAP only because minute 0 was a real start gap
    assert sink2.m5 and sink2.m5[0].open_time == T0


def test_slow_record_time_never_duplicates_a_trade():
    """Server finding: a record time 653 ms after the stream time duplicated trades under the
    first +-500 ms matching rule. Alignment by order fixes it."""
    col, sink = make()
    start(col)
    ws(col, 0, 125)
    col.ingest_rest(
        [
            RestTrade(at(t) + timedelta(milliseconds=650), px, D("0.001"), "BUY", {})
            for _, t, px in TAPE
            if 20 <= t < 125
        ],
        at(128),
    )
    assert col.stats["rest_only"] == 0
    ids = [t.trade_id for t, _ in sink.trades]
    assert len(ids) == len(set(ids)) == len([x for x in TAPE if x[1] < 125])


def test_a_run_of_identical_fills_is_not_paired_one_position_off():
    from sp2l.marketdata.collector import _align

    ws_ = [[at(t), D("1"), D("1"), f"w{t}", False] for t in (3.0, 6.0)]  # WS missed t=0
    rest_ = [RestTrade(at(t) + REC, D("1"), D("1"), None, {}) for t in (0.0, 3.0, 6.0)]
    got = _align(rest_, ws_, timedelta(seconds=3))
    assert 0 not in got and got[1][3] == "w3.0" and got[2][3] == "w6.0"  # the missing one is t=0


def test_restart_seeded_with_stored_trades_never_stores_them_twice():
    stored = [
        __import__("sp2l.marketdata.m1_builder", fromlist=["Trade"]).Trade(
            f"{seq}:{px}:0.001#1", at(t), at(t + 0.1), px, D("0.001")
        )
        for seq, t, px in TAPE
        if 100 <= t < 186  # 5 minutes back, incl. already-final minutes
    ]
    sink2 = RepairSink()
    sink2.on_late_rest_trade = lambda t: None
    col2 = Collector(
        CollectorConfig(
            "BTCUSDT",
            connections=("A",),
            repair_enabled=True,
            rest_enabled=True,
            rest_stale=timedelta(seconds=300),
        ),
        sink2,
        clock=lambda: at(186),
        resume_from=at(180),
        resume_trades=stored,
    )
    start(col2, conns=("A",), t=186)
    rest(col2, 120, 186, 190)  # the window reaches into minutes final before the restart
    assert col2.stats["rest_only"] == 0 and col2.stats["rest_revisions"] == 0


def test_feeds_disagreeing_on_the_order_of_simultaneous_trades_still_align():
    """Server finding (16:34:40): two fills of one sequence plus a neighbour listed in a
    different order by REST than by WS were left unaligned by a global alignment."""
    from sp2l.marketdata.collector import _align

    ws_ = [
        [at(40.453), D("84498.7"), D("0.00014"), "f1", False],
        [at(40.453), D("84501.9"), D("0.00014"), "f2", False],
        [at(40.600), D("84500"), D("0.002"), "n", False],
    ]
    rest_ = [
        RestTrade(at(40.55), D("84500"), D("0.002"), None, {}),  # recorded first
        RestTrade(at(40.797786), D("84498.7"), D("0.00014"), None, {}),
        RestTrade(at(40.797861), D("84501.9"), D("0.00014"), None, {}),
    ]
    got = _align(rest_, ws_, timedelta(seconds=3))
    assert {k: v[3] for k, v in got.items()} == {0: "n", 1: "f1", 2: "f2"}


def test_polls_are_phase_aligned_to_the_minute_boundary():
    col, _ = make()
    col.rest_spans_s = [90.0]  # regular overlap cadence: 15 s
    assert col.next_poll_in(at(30)) == 15.0  # mid-minute: overlap cadence rules
    assert abs(col.next_poll_in(at(55)) - 7.2) < 1e-6  # next boundary 60 + 1 + 1 + 0.2
    assert abs(col.next_poll_in(at(61)) - 1.2) < 1e-6
    assert abs(col.next_poll_in(at(62.2)) - 15.0) < 1e-6  # just polled: next cycle


def test_a_poll_just_after_the_close_finalizes_the_minute_within_seconds():
    col, sink = make()
    start(col)
    ws(col, 0, 122.2)
    col.pong(at(121.5), at(121.6), "A")
    rest(col, 20, 122.2, 122.2)  # fetched at boundary + 2.2 s (settle 1 s + coverage bound 1 s)
    col.tick(at(122.3), 1.0)
    m = minute(sink, 60)
    assert m is not None and m.status is M1Status.OK and m.quality is Quality.LIVE_RECONCILED
