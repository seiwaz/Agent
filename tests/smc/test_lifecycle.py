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
    assert advance(t, bar(0, 101, 100.5), P, FREE) is None
    assert advance(t, bar(1, 101, 99.5), P, FREE) == "FILLED"
    assert advance(t, bar(2, 104.2, 100), P, FREE) == "TP"
    assert (t.state, t.exit_price, t.result_r) == (State.TP, D(104), D(2))


def test_stop_beats_target_in_the_same_minute():
    t = long_signal(state=State.OPEN, filled_at=T0)
    assert advance(t, bar(1, 105, 97), P, FREE) == "SL"
    assert t.result_r == D(-1)


def test_no_target_credit_in_the_fill_minute_but_stop_counts():
    t = long_signal()
    assert advance(t, bar(0, 105, 99.9), P, FREE) == "FILLED" and t.state is State.OPEN
    t2 = long_signal()
    assert advance(t2, bar(0, 101, 97.5), P, FREE) == "SL"


def test_target_before_fill_is_missed_and_wait_expires():
    t = long_signal()
    assert advance(t, bar(0, 104.5, 100.2), P, FREE) == "MISSED"
    t2 = long_signal()
    for i in range(5):
        assert advance(t2, bar(i, 101, 100.5), P, FREE) is None
    assert advance(t2, bar(5, 101, 100.5), P, FREE) == "EXPIRED"


def test_market_entry_is_open_from_creation_and_times_out():
    t = long_signal(market=True)
    assert t.state is State.OPEN and t.filled_at == T0
    for i in range(10):
        advance(t, bar(i, 101, 99, 100.5), P, FREE)
    assert advance(t, bar(10, 101, 99, 101), P, FREE) == "TIMEOUT"
    assert t.exit_price == D(101)


def test_bars_are_applied_once_and_in_order():
    t = long_signal()
    advance(t, bar(3, 101, 100.5), P, FREE)
    assert advance(t, bar(3, 101, 99), P, FREE) is None  # replayed minute: ignored
    assert t.state is State.PENDING


def test_r_is_net_of_fees_and_stop_slippage():
    c = Costs(maker_fee=D("0.001"), taker_fee=D("0.001"), slippage=D("0.001"))
    ru = risk_unit(Side.LONG, D(100), D(98), c)
    assert ru == D(2) + D("0.1") + D("0.098") + D("0.098")
    t = long_signal(risk=ru, state=State.OPEN, filled_at=T0)
    advance(t, bar(1, 101, 97.9), P, c)
    assert t.exit_price == D(98) - D("0.098")
    assert t.result_r == ((t.exit_price - 100) - D("0.1") - t.exit_price * D("0.001")) / ru
    assert abs(t.result_r + 1) < D("0.001")  # a planned stop is -1R: costs sit in the R


def test_break_even_moves_the_stop_after_the_bar_that_reached_one_r():
    p = SmcParams(be_at_r=D(1), max_hold_min=600)
    c = Costs(maker_fee=D("0.001"), taker_fee=D("0.001"))
    t = long_signal(state=State.OPEN, filled_at=T0, risk=risk_unit(Side.LONG, D(100), D(98), c))
    assert advance(t, bar(1, 101.9, 99.5), p, c) is None  # +0.95R: not yet
    assert advance(t, bar(2, 102.1, 99.5), p, c) == "BREAKEVEN"  # +1.05R reached
    assert t.sl == D(100) + D("0.2") and t.sl0 == D(98) and t.at_breakeven
    assert advance(t, bar(3, 101, 100.1), p, c) == "SL"  # back to entry: out near zero
    assert abs(t.result_r) < D("0.01")


def test_no_break_even_inside_the_fill_bar_and_off_by_default():
    p = SmcParams(be_at_r=D(1))
    t = long_signal()
    assert advance(t, bar(0, 103, 99.9), p, FREE) == "FILLED" and not t.at_breakeven
    t2 = long_signal(state=State.OPEN, filled_at=T0)
    assert advance(t2, bar(1, 103, 100.5), P, FREE) is None and not t2.at_breakeven
