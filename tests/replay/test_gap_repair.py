"""Engine behavior across a coverage gap: exact repair vs history repair vs unrecovered vs
uninterrupted (V5.6, V5.8).

Driver mirrors production ordering: at the gap the collector journals GAP; minutes that
overlap the gap are HELD until the repair decision (after reconnect), so the engine sees the
GAP, then live post-gap trades, then (exact repair) the recovered trades as source REST, then
the minutes: RECENT_TRADES_REPAIRED (exact), TABDEAL_HISTORY_REPAIRED (OHLCV only, trade
count unknown) or DATA_GAP.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D

import pytest

from sp2l.engine.model import SetupState
from sp2l.engine.symbol_engine import ShadowSymbolEngine
from sp2l.marketdata.m1_builder import MINUTE, M1Builder, M1Status, Quality, Trade
from sp2l.strategy.risk.engine import CostModel, ExchangeFilters
from tests.conftest import T0
from tests.replay.tape import tape

MINUTES = 900
COSTS = CostModel(D("0.0004"), D("0.0006"), D("0.0005"))
FILTERS = ExchangeFilters(D("0.1"), D("0.001"), None, None, verified=False)
END = T0 + timedelta(minutes=MINUTES)
TAPE = tape(MINUTES, 3)


def engine() -> ShadowSymbolEngine:
    return ShadowSymbolEngine(
        "BTCUSDT", tick=D("0.1"), costs=COSTS, filters=FILTERS, warmup_bars=30
    )


def drive(gap: tuple[datetime, datetime] | None, mode: str) -> ShadowSymbolEngine:
    """mode: live (uninterrupted) | repaired (EXACT_RAW_REPAIR) | history
    (CANDLE_HISTORY_REPAIR) | unrecovered (DATA_GAP) | downtime (minutes missing)."""
    eng = engine()
    b = M1Builder(T0)
    b.mark_healthy_until(END)
    g0, g1 = gap or (END, END)
    if mode == "unrecovered":
        b.add_coverage_gap(g0, g1)
    held_from = g0 if mode != "live" else None
    release = g1 + timedelta(seconds=2)
    gap_sent = False
    recovered: list[Trade] = []

    def finalize(now: datetime) -> None:
        if mode == "repaired" and now >= release and recovered:
            for t in recovered:  # journaled after the reconnect, as source REST
                eng.feed_trade(t.price, t.exch_ts, "REST")
            recovered.clear()
        b.hold_from = held_from if now < release else None
        for m1 in b.advance(now):
            if mode == "downtime" and m1.open_time < g1 and m1.open_time + MINUTE > g0:
                continue  # the collector was down: these minutes never reach the journal
            if m1.candle is not None and m1.open_time < g1 and m1.open_time + MINUTE > g0:
                if mode == "repaired":
                    m1 = replace(m1, quality=Quality.RECENT_TRADES_REPAIRED)
                elif mode == "history":
                    c = replace(m1.candle, trade_count=None)
                    m1 = replace(m1, candle=c, quality=Quality.TABDEAL_HISTORY_REPAIRED)
            eng.on_m1(m1)

    for t in sorted(TAPE, key=lambda x: (x.recv_ts, x.exch_ts, x.trade_id)):
        finalize(t.recv_ts)
        in_gap = g0 <= t.exch_ts < g1
        if mode != "live" and not gap_sent and t.recv_ts >= g0:
            eng.on_gap(g0, None, g0, "CLOSED_NO_FRAME")
            gap_sent = True
        if mode == "live" or not in_gap:
            eng.feed_trade(t.price, t.exch_ts, "WS")
        elif mode == "repaired":
            recovered.append(t)
        if mode != "unrecovered" or not in_gap:
            b.add_trade(t)  # repaired/history: the held candles are completed
    finalize(END + timedelta(minutes=1))
    return eng


def m5_state(e: ShadowSymbolEngine) -> str:
    seg = e.m5.segment
    assert seg is not None
    return json.dumps(
        {
            "anchor": seg.anchor_open_time,
            "bars": [(b.open_time, b.candle) for b in seg.bars],
            "last": e.m5.last,
            "warm": e.m5.warm,
        },
        default=str,
        sort_keys=True,
    )


@pytest.fixture(scope="module")
def ref() -> ShadowSymbolEngine:
    return drive(None, "live")


def quiet_gap(ref: ShadowSymbolEngine) -> tuple[datetime, datetime]:
    """A 40 s gap, after warmup, during which no setup of the reference run is alive."""
    busy = [(m.created_at, m.events[-1]["ts"]) for m in ref.finished]
    t = T0 + timedelta(minutes=400, seconds=17)
    while True:
        g0, g1 = t, t + timedelta(seconds=40)
        if all(
            not (
                datetime.fromisoformat(str(e)) >= g0 - timedelta(minutes=20)
                and c <= g1 + timedelta(minutes=20)
            )
            for c, e in busy
        ):
            return g0, g1
        t += timedelta(minutes=7)


def test_exact_repair_reproduces_the_uninterrupted_indicator_and_strategy_state(ref):
    g0, g1 = quiet_gap(ref)
    rep = drive((g0, g1), "repaired")
    assert m5_state(rep) == m5_state(ref)  # M5 bars, anchor, indicators: no re-anchor
    assert rep.m5.breaks == ref.m5.breaks == []

    def after(e: ShadowSymbolEngine) -> list[object]:
        return [
            (m.created_at, str(m.state), m.events) for m in e.finished if m.created_at > g1 + MINUTE
        ]

    assert after(ref), "the comparison must cover real setups"
    assert after(rep) == after(ref)  # identical setups and outcomes after the repair point
    unrec = drive((g0, g1), "unrecovered")
    assert m5_state(unrec) != m5_state(ref)  # the equality above is not vacuous
    assert [p for p in rep.pgaps if p.ts > g1 + MINUTE] == [
        p for p in ref.pgaps if p.ts > g1 + MINUTE
    ]


def test_unrecoverable_gap_keeps_the_fail_closed_reset(ref):
    g0, g1 = quiet_gap(ref)
    unrec = drive((g0, g1), "unrecovered")
    assert unrec.m5.breaks and unrec.m5.breaks[0][1].value == "DATA_GAP"
    assert unrec.m5.segment is not None and unrec.m5.segment.anchor_open_time > g0  # re-anchored


def test_repaired_history_never_creates_a_setup():
    # a gap placed over a P-Gap the reference run promoted
    live = drive(None, "live")
    promoted = [p for p in live.pgaps if p.promoted and p.ts > T0 + timedelta(minutes=200)]
    p = promoted[0]
    g0, g1 = p.right_open_time - timedelta(seconds=30), p.right_open_time + timedelta(seconds=45)
    rep = drive((g0, g1), "repaired")
    same = [x for x in rep.pgaps if x.right_open_time == p.right_open_time]
    assert same and not same[0].promoted and same[0].reason in ("REPAIRED_HISTORY", "CAPACITY_BUSY")
    assert not any(g0 - MINUTE <= m.created_at <= g1 + MINUTE for m in rep.finished)


def _exposed_gap() -> tuple[ShadowSymbolEngine, object, datetime]:
    live = drive(None, "live")
    m = next(
        x
        for x in live.finished
        if x.state is SetupState.CLOSED
        and any(e["kind"] == "FILL" for e in x.events)
        and datetime.fromisoformat(next(e for e in x.events if e["kind"] == "E1_SUBMITTED")["ts"])
        > T0 + timedelta(minutes=200)
    )
    e1 = next(e for e in m.events if e["kind"] == "E1_SUBMITTED")
    return live, m, datetime.fromisoformat(e1["ts"]) + timedelta(seconds=20)  # E1 pending


def test_exposed_setup_exact_raw_repair_replays_causally_identical_to_live():
    """V5.8 AT-6: EXACT_RAW_REPAIR -> causal replay: same setup outcome, same events, same
    indicator state as the uninterrupted run (hash identical)."""
    live, m, g0 = _exposed_gap()
    rep = drive((g0, g0 + timedelta(seconds=40)), "repaired")
    hit = [x for x in rep.finished if x.created_at == m.created_at]
    assert hit and hit[0].state is m.state and hit[0].events == m.events
    assert m5_state(rep) == m5_state(live)
    assert rep.gap_buffer is None  # the gap is resolved; nothing left pending


def test_exposed_setup_history_repair_is_ambiguous_but_context_is_restored():
    """V5.8 AT-8: CANDLE_HISTORY_REPAIR (OHLCV only) -> AMBIGUOUS_DATA_GAP, no invented fill
    or exit, while the M5 series continues without a re-anchor (no 150-bar reset)."""
    live, m, g0 = _exposed_gap()
    hist = drive((g0, g0 + timedelta(seconds=40)), "history")
    hit = [x for x in hist.finished if x.created_at == m.created_at]
    assert hit and hit[0].state is SetupState.AMBIGUOUS_DATA_GAP
    assert not any(
        e["kind"] in ("FILL", "EXIT") and datetime.fromisoformat(e["ts"]) >= g0
        for e in hit[0].events
    )
    assert hist.m5.breaks == [] and hist.m5.warm
    assert hist.m5.segment is not None and live.m5.segment is not None
    assert hist.m5.segment.anchor_open_time == live.m5.segment.anchor_open_time
    prices = [
        (b.open_time, b.candle.high, b.candle.low, b.candle.close, b.atr14)
        for b in hist.m5.segment.bars
    ]
    assert prices == [
        (b.open_time, b.candle.high, b.candle.low, b.candle.close, b.atr14)
        for b in live.m5.segment.bars
    ]


def test_rest_trades_drive_the_engine_only_inside_a_gap():
    """Outside a gap a REST-only trade arrives after later WS trades: never applied (V5.7);
    inside a gap every canonical trade is buffered for causal replay (V5.8)."""
    eng = engine()
    eng.feed_trade(D("1"), T0, "REST")
    assert eng.last_trade is None
    eng.feed_trade(D("2"), T0, "WS")
    assert eng.last_trade == D("2")
    eng.on_gap(T0, None, T0, "CLOSED_NO_FRAME")
    eng.feed_trade(D("3"), T0 + timedelta(seconds=5), "REST")
    assert eng.last_trade == D("2") and eng.gap_buffer == [(T0 + timedelta(seconds=5), D("3"))]


def test_repaired_status_is_ok_with_lineage():
    b = M1Builder(T0)
    b.mark_healthy_until(T0 + timedelta(minutes=5))
    b.mark_repaired(T0 + timedelta(seconds=10), T0 + timedelta(seconds=20))
    b.add_trade(Trade("1", T0 + timedelta(seconds=15), T0, D("5"), D("1")))
    (m,) = b.advance(T0 + timedelta(minutes=1, seconds=3))
    assert m.status is M1Status.OK and m.quality is Quality.REPAIRED_TABDEAL


def test_downtime_hole_decides_before_post_gap_trades_are_replayed():
    """Regression (found in V5.9 testing): trades buffered after a GAP must not fill or exit
    an exposed setup before the missing minutes (a hole) finalize it AMBIGUOUS_DATA_GAP."""
    _, m, g0 = _exposed_gap()
    down = drive((g0, g0 + timedelta(seconds=150)), "downtime")
    hit = [x for x in down.finished if x.created_at == m.created_at]
    assert hit and hit[0].state is SetupState.AMBIGUOUS_DATA_GAP
    assert not any(
        e["kind"] in ("FILL", "EXIT") and datetime.fromisoformat(e["ts"]) >= g0
        for e in hit[0].events
    )
