"""V5.1 B05/B25, V5.2 B31/B34: canonical M1 from raw trades and M5 from M1."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D
from fractions import Fraction

from sp2l.core.types import Candle
from sp2l.marketdata.m1_builder import M1Builder, M1Result, M1Status, Trade
from sp2l.marketdata.m5_aggregator import M5Aggregator, M5Status
from tests.conftest import T0, candle

S = timedelta(seconds=1)


def tr(i: int, sec: float, price: str, qty: str = "1", recv_delay: float = 0.1) -> Trade:
    ts = T0 + timedelta(seconds=sec)
    return Trade(str(i), ts, ts + timedelta(seconds=recv_delay), D(price), D(qty))


def builder(healthy_minutes: int = 60) -> M1Builder:
    b = M1Builder(T0)
    b.mark_healthy_until(T0 + timedelta(minutes=healthy_minutes))
    return b


def test_ohlcv_uses_exchange_time_order_and_raw_count():
    b = builder()
    for t in (tr(2, 30, "101"), tr(1, 5, "100"), tr(3, 59.9, "99", "2"), tr(4, 40, "103")):
        b.add_trade(t)
    b.add_trade(tr(4, 40, "103"))  # duplicate delivery counted once
    (res,) = b.advance(T0 + timedelta(minutes=1, seconds=2))
    c = res.candle
    assert res.status is M1Status.OK and c is not None
    assert (c.open, c.high, c.low, c.close) == (D("100"), D("103"), D("99"), D("99"))
    assert c.volume == D("5") and c.trade_count == 4


def test_finalization_waits_for_two_second_grace():
    b = builder()
    b.add_trade(tr(1, 10, "100"))
    assert b.advance(T0 + timedelta(minutes=1, seconds=1.999)) == []
    assert len(b.advance(T0 + timedelta(minutes=1, seconds=2))) == 1


def test_late_trade_logged_never_mutates():
    b = builder()
    b.add_trade(tr(1, 10, "100"))
    (res,) = b.advance(T0 + timedelta(minutes=1, seconds=2))
    late = tr(9, 50, "150")
    assert b.add_trade(late) is False
    assert b.late_trades == [late]
    assert res.candle is not None and res.candle.high == D("100")


def test_healthy_zero_trade_minute_is_synthetic_and_gap_is_data_gap_b31():
    b = builder()
    b.add_trade(tr(1, 10, "100"))
    b.add_trade(tr(2, 20, "101"))
    b.add_trade(tr(3, 130, "100"))  # minute 2
    b.add_coverage_gap(T0 + timedelta(minutes=2, seconds=30), T0 + timedelta(minutes=2, seconds=40))
    out = b.advance(T0 + timedelta(minutes=3, seconds=2))
    assert [r.status for r in out] == [M1Status.OK, M1Status.SYNTHETIC_NO_TRADE, M1Status.DATA_GAP]
    syn = out[1].candle
    assert syn is not None and syn.synthetic
    assert (syn.open, syn.high, syn.low, syn.close, syn.volume, syn.trade_count) == (
        D("101"),
        D("101"),
        D("101"),
        D("101"),
        D("0"),
        0,
    )
    assert not out[1].tradeable and out[0].tradeable
    assert out[2].candle is None  # nothing synthesized inside a gap


def test_unconfirmed_coverage_is_data_gap():
    b = builder(healthy_minutes=1)
    b.add_trade(tr(1, 10, "100"))
    out = b.advance(T0 + timedelta(minutes=2, seconds=2))
    assert [r.status for r in out] == [M1Status.OK, M1Status.DATA_GAP]


def test_no_synthetic_before_first_genuine_post_gap_trade_b34():
    b = builder()
    b.add_trade(tr(1, 10, "100"))
    b.add_coverage_gap(T0 + timedelta(minutes=1), T0 + timedelta(minutes=1, seconds=5))
    out = b.advance(T0 + timedelta(minutes=3, seconds=2))
    # pre-gap close 100 is never carried across the gap
    assert [r.status for r in out] == [M1Status.OK, M1Status.DATA_GAP, M1Status.UNANCHORED]
    assert all(r.candle is None for r in out[1:])


def test_post_gap_trade_in_gap_minute_anchors_next_synthetic_b34():
    b = builder()
    b.add_trade(tr(1, 10, "100"))
    b.add_trade(tr(2, 62, "150"))  # inside the gap: not genuine post-gap
    b.add_trade(tr(3, 100, "103"))  # after the gap ended at 1:30: new anchor
    b.add_coverage_gap(T0 + timedelta(minutes=1), T0 + timedelta(minutes=1, seconds=30))
    out = b.advance(T0 + timedelta(minutes=3, seconds=2))
    assert [r.status for r in out] == [M1Status.OK, M1Status.DATA_GAP, M1Status.SYNTHETIC_NO_TRADE]
    assert out[2].candle is not None and out[2].candle.close == D("103")


def test_m5_unanchored_minute_is_data_gap():
    agg = M5Aggregator()
    mins = [
        _ok(0),
        _ok(1),
        M1Result(T0 + timedelta(minutes=2), M1Status.UNANCHORED, None),
        _ok(3),
        _ok(4),
    ]
    res = [agg.add(m) for m in mins][-1]
    assert res is not None and res.status is M5Status.DATA_GAP


def _ok(i: int, low: str = "99", high: str = "101") -> M1Result:
    return M1Result(
        T0 + timedelta(minutes=i),
        M1Status.OK,
        candle(i, "100", high, low, "100", volume="1", trades=2),
    )


def _syn(i: int, price: str = "100") -> M1Result:
    p = D(price)
    return M1Result(
        T0 + timedelta(minutes=i),
        M1Status.SYNTHETIC_NO_TRADE,
        Candle(T0 + timedelta(minutes=i), p, p, p, p, D(0), 0, synthetic=True),
    )


def test_m5_aggregates_real_and_synthetic_minutes_with_flag():
    agg = M5Aggregator()
    quiet = _syn(2)
    outs = [agg.add(m) for m in (_ok(0), _ok(1, low="98"), quiet, _ok(3, high="105"), _ok(4))]
    assert outs[:4] == [None] * 4
    m5 = outs[4]
    assert m5 is not None and m5.status is M5Status.OK and m5.candle is not None
    assert (m5.candle.high, m5.candle.low, m5.candle.volume, m5.candle.trade_count) == (
        D("105"),
        D("98"),
        D("4"),
        8,
    )


def test_m5_all_synthetic_and_data_gap_inheritance():
    agg = M5Aggregator()
    out = [agg.add(_syn(i)) for i in range(5)][-1]
    assert (
        out is not None
        and out.status is M5Status.OK
        and out.synthetic_m1_count == 5
        and out.synthetic_fraction == 1
        and out.real_trade_count == 0
    )
    assert out.candle is not None and out.candle.synthetic
    mixed = [
        _ok(5),
        _ok(6),
        M1Result(T0 + timedelta(minutes=7), M1Status.DATA_GAP, None),
        _ok(8),
        _ok(9),
    ]
    res = [agg.add(m) for m in mixed][-1]
    assert res is not None and res.status is M5Status.DATA_GAP and res.candle is None


def test_m5_skips_partial_first_bucket():
    agg = M5Aggregator()
    assert [agg.add(_ok(i)) for i in (3, 4)] == [None, None]
    outs = [agg.add(_ok(i)) for i in range(5, 10)]
    assert outs[-1] is not None and outs[-1].open_time == T0 + timedelta(minutes=5)


def test_m5_quality_fields_partial_synthetic_b33():
    agg = M5Aggregator()
    outs = [agg.add(m) for m in (_ok(0), _syn(1), _syn(2), _ok(3), _ok(4))]
    m5 = outs[-1]
    assert m5 is not None and m5.candle is not None and not m5.candle.synthetic
    assert (m5.synthetic_m1_count, m5.synthetic_fraction, m5.real_trade_count) == (
        2,
        Fraction(2, 5),
        6,
    )
