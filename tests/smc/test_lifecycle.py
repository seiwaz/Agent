"""Signal lifecycle on M1 bars (conservative intrabar reading) and R after costs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from sp2l.core.types import Candle, Side
from sp2l.smc.lifecycle import State, Tracked, advance, risk_unit
from sp2l.smc.model import Costs, SmcParams

T0 = datetime(2026, 1, 1, tzinfo=UTC)
P = SmcParams(pending_expiry_min=5, max_hold_min=10)
FREE = Costs()


def bar(i: int, h: float, lo: float, c: float | None = None) -> Candle:
    c = lo if c is None else c
    o = min(max(c, lo), h)
    return Candle(T0 + timedelta(minutes=i), D(str(o)), D(str(h)), D(str(lo)), D(str(c)))


def long_signal(**kw) -> Tracked:
    base = dict(
        key="k", side=Side.LONG, entry=D(100), sl=D(98), tp=D(104), created_at=T0, risk=D(2)
    )
    base.update(kw)
    return Tracked(**base)


def test_limit_fill_then_target():
    t = long_signal()
    assert advance(t, bar(0, 101, 100.5), P, FREE) == []
    assert advance(t, bar(1, 101, 99.5), P, FREE) == ["FILLED"]
    assert advance(t, bar(2, 104.2, 100), P, FREE) == ["TP"]
    assert (t.state, t.exit_price, t.result_r) == (State.TP, D(104), D(2))


def test_stop_beats_target_in_the_same_minute():
    t = long_signal(state=State.OPEN, filled_at=T0)
    assert advance(t, bar(1, 105, 97), P, FREE) == ["SL"]
    assert t.result_r == D(-1)


def test_no_target_credit_in_the_fill_minute_but_stop_counts():
    t = long_signal()
    assert advance(t, bar(0, 105, 99.9), P, FREE) == ["FILLED"] and t.state is State.OPEN
    t2 = long_signal()
    assert advance(t2, bar(0, 101, 97.5), P, FREE) == ["FILLED", "SL"]


def test_target_before_fill_is_missed_and_wait_expires():
    t = long_signal()
    assert advance(t, bar(0, 104.5, 100.2), P, FREE) == ["MISSED"]
    t2 = long_signal()
    for i in range(5):
        assert advance(t2, bar(i, 101, 100.5), P, FREE) == []
    assert advance(t2, bar(5, 101, 100.5), P, FREE) == ["EXPIRED"]


def test_market_entry_is_open_from_creation_and_times_out():
    t = long_signal(market=True)
    assert t.state is State.OPEN and t.filled_at == T0
    for i in range(10):
        advance(t, bar(i, 101, 99, 100.5), P, FREE)
    assert advance(t, bar(10, 101, 99, 101), P, FREE) == ["TIMEOUT"]
    assert t.exit_price == D(101)


def test_bars_are_applied_once_and_in_order():
    t = long_signal()
    advance(t, bar(3, 101, 100.5), P, FREE)
    assert advance(t, bar(3, 101, 99), P, FREE) == []  # replayed minute: ignored
    assert t.state is State.PENDING


def test_a_full_stop_is_exactly_minus_one_r_after_fees_and_slippage():
    c = Costs(maker_fee=D("0.001"), taker_fee=D("0.001"), slippage=D("0.001"))
    ru = risk_unit(Side.LONG, D(100), D(98), c)
    fill = D(98) - D("0.098")  # the stop fills one slippage allowance beyond it
    assert ru == D(2) + D("0.1") + D("0.098") + fill * D("0.001")
    t = long_signal(risk=ru, state=State.OPEN, filled_at=T0)
    advance(t, bar(1, 101, 97.9), P, c)
    assert t.exit_price == fill and t.result_r == D(-1)
