"""M5 segment state: anchoring/warmup (B06, B25, B31), pivots, dual trend rule (B11), trend age."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D

from sp2l.indicators.m5_state import M5State
from sp2l.indicators.pivots import PivotKind, confirmed_pivots
from sp2l.indicators.trend import Trend
from sp2l.marketdata.m5_aggregator import M5Result, M5Status
from tests.conftest import T0, hl_series, random_walk


def feed(state: M5State, candles) -> None:
    for c in candles:
        state.add(M5Result(c.open_time, M5Status.OK, c))


def test_warm_only_after_150_bars_and_reanchor_on_missing():
    cs = random_walk(320, seed=11)
    st = M5State()
    feed(st, cs[:149])
    assert not st.warm
    feed(st, cs[149:150])
    assert st.warm
    st.add(M5Result(cs[150].open_time, M5Status.DATA_GAP, None))
    assert st.segment is None and not st.warm
    feed(st, cs[151:300])
    assert st.segment is not None and st.segment.anchor_open_time == cs[151].open_time
    assert not st.warm  # 149 bars since the new anchor
    feed(st, cs[300:301])
    assert st.warm


def test_silent_hole_is_a_break_not_bridged():
    cs = random_walk(10, seed=2)
    st = M5State()
    feed(st, cs[:5])
    feed(st, cs[6:])
    assert st.segment is not None and st.segment.anchor_open_time == cs[6].open_time


def test_segment_pivots_match_batch_and_breaks_are_strict_closes(walk600):
    st = M5State()
    feed(st, walk600[:300])
    seg = st.segment
    assert seg is not None
    batch = confirmed_pivots(walk600[:300])
    assert [(p.index, p.kind) for p in seg.pivots] == [(p.index, p.kind) for p in batch]
    for p in seg.pivots:
        if p.high_broken_index is not None:
            k = p.high_broken_index
            assert k > p.confirmed_index and walk600[k].close > p.high
            assert all(walk600[j].close <= p.high for j in range(p.confirmed_index + 1, k))


def _dual_rows():
    # up-trend structure with a DUAL outside bar placed as the latest swing high/low
    return [
        ("10", "5"),
        ("11", "6"),
        ("15", "7"),
        ("12", "6.5"),
        ("11", "4"),  # SH at 2 (15)
        ("12", "4.5"),
        ("11", "5"),
        ("16", "8"),
        ("13", "8.5"),
        ("12", "9"),  # SL at 4 (4), SH at 7 (16)
        ("13", "10"),
        ("12", "9.5"),
        ("20", "1"),
        ("14", "9"),
        ("13", "10"),  # DUAL at 12
    ]


def test_dual_pivot_invalidates_trend_only_when_used():
    st = M5State(warmup_bars=1)
    feed(st, hl_series(_dual_rows()))
    seg = st.segment
    assert seg is not None
    assert [p.kind for p in seg.pivots][-1] is PivotKind.DUAL
    assert seg.bars[-1].trend is Trend.INVALID_DUAL_PIVOT
    assert seg.bars[13].trend is not Trend.INVALID_DUAL_PIVOT  # before DUAL confirmation


def test_trend_age_counts_from_change_bar_inclusive():
    st = M5State(warmup_bars=1)
    feed(st, random_walk(400, seed=5))
    seg = st.segment
    assert seg is not None
    for k in range(1, 400):
        bar = seg.bars[k]
        for direction in (Trend.BULL, Trend.BEAR):
            age = st.trend_age(direction, k)
            if bar.trend is not direction:
                assert age == 0
            else:
                start = k - age + 1
                assert all(seg.bars[j].trend is direction for j in range(start, k + 1))
                assert start == 0 or seg.bars[start - 1].trend is not direction


def test_bar_at_close():
    st = M5State(warmup_bars=1)
    cs = random_walk(5, seed=1)
    feed(st, cs)
    b = st.bar_at_close(cs[2].open_time + timedelta(minutes=5))
    assert b is not None and b.index == 2
    assert st.bar_at_close(T0 + timedelta(hours=1)) is None
    assert D(0) < cs[0].high


def test_all_synthetic_bar_never_becomes_a_pivot_b33():
    from sp2l.core.types import Candle

    rows = [("10", "5"), ("11", "6"), None, ("12", "6.5"), ("11", "6")]
    st = M5State(warmup_bars=1)
    for k, row in enumerate(rows):
        t = T0 + timedelta(minutes=5 * k)
        if row is None:  # flat synthetic bar at 4, strictly below every neighbour's low
            p = D("4")
            c = Candle(t, p, p, p, p, D(0), 0, synthetic=True)
            st.add(M5Result(t, M5Status.OK, c, synthetic_m1_count=5))
        else:
            h, lo = D(row[0]), D(row[1])
            c = Candle(t, (h + lo) / 2, h, lo, (h + lo) / 2, D(1), 3)
            st.add(M5Result(t, M5Status.OK, c))
    seg = st.segment
    assert seg is not None and seg.pivots == []
    bar = seg.bars[2]
    assert bar.all_synthetic and bar.synthetic_m1_count == 5 and bar.real_trade_count == 0


def test_b37_no_pivot_window_spans_a_data_gap():
    # bar 2 would be a swing high, but its right neighbours come after a DATA_GAP
    st = M5State(warmup_bars=1)
    rows = hl_series([("10", "5"), ("11", "6"), ("15", "7")])
    for c in rows:
        st.add(M5Result(c.open_time, M5Status.OK, c))
    st.add(M5Result(T0 + timedelta(minutes=15), M5Status.DATA_GAP, None))
    after = hl_series([("12", "6.5"), ("11", "6"), ("10", "5")])
    for i, c in enumerate(after):
        t = T0 + timedelta(minutes=20 + 5 * i)
        st.add(M5Result(t, M5Status.OK, type(c)(t, c.open, c.high, c.low, c.close)))
    seg = st.segment
    assert seg is not None and seg.pivots == []
    assert seg.anchor_open_time == T0 + timedelta(minutes=20)


def test_b37_all_synthetic_bar_may_be_a_neighbour():
    from sp2l.core.types import Candle

    st = M5State(warmup_bars=1)
    rows = [("10", "5"), ("11", "6"), ("15", "7"), None, ("11", "6")]
    for k, row in enumerate(rows):
        t = T0 + timedelta(minutes=5 * k)
        if row is None:
            p = D("12")
            st.add(M5Result(t, M5Status.OK, Candle(t, p, p, p, p, D(0), 0, synthetic=True), 5))
        else:
            h, lo = D(row[0]), D(row[1])
            st.add(M5Result(t, M5Status.OK, Candle(t, (h + lo) / 2, h, lo, (h + lo) / 2)))
    seg = st.segment
    assert seg is not None and [(p.index, p.kind.value) for p in seg.pivots] == [(2, "HIGH")]
