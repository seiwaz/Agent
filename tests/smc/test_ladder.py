"""The SMC-2.0 take-profit ladder: TP1 + break-even, TP2 + trailing stop, TP3, time stop,
and the wallet booking of every exit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from sp2l.core.types import Candle, Side
from sp2l.smc.lifecycle import State, Tracked, advance, breakeven, risk_unit
from sp2l.smc.model import Costs, SmcParams
from sp2l.smc.wallet import ladder_fracs, part_pnl

T0 = datetime(2026, 1, 1, tzinfo=UTC)
P = SmcParams(pending_expiry_min=5, max_hold_min=600)
C = Costs(maker_fee=D("0.001"), taker_fee=D("0.001"), slippage=D("0.0005"))


def bar(i: int, h: float, lo: float, c: float | None = None) -> Candle:
    c = lo if c is None else c
    o = min(max(c, lo), h)
    return Candle(T0 + timedelta(minutes=i), D(str(o)), D(str(h)), D(str(lo)), D(str(c)))


def ladder(**kw) -> Tracked:
    base = dict(
        key="k",
        side=Side.LONG,
        entry=D(100),
        sl=D(98),
        tp=D(110),
        created_at=T0,
        risk=risk_unit(Side.LONG, D(100), D(98), C),
        state=State.OPEN,
        filled_at=T0,
        tp1=D(102),
        tp2=D(105),
        frac1=D("0.5"),
        frac2=D("0.3"),
        time_stop_min=180,
    )
    base.update(kw)
    return Tracked(**base)


def test_tp1_books_half_and_moves_the_stop_to_break_even_from_the_next_minute():
    t = ladder()
    assert advance(t, bar(1, 102.5, 100.5), P, C) == ["TP1"]
    assert t.state is State.TP1 and t.sl == breakeven(t, C) > t.entry
    assert t.parts[0].frac == D("0.5") and t.parts[0].price == D(102)
    assert advance(t, bar(2, 101, t.sl - D("0.01")), P, C) == ["BE"]
    assert t.state is State.BE and t.open_frac == 0
    # the rest out at break-even: about TP1's half of the net R, nothing lost on the rest
    assert abs(t.result_r - t.parts[0].r) < D("0.02")


def test_tp2_then_the_trailing_stop_only_tightens_and_closes_the_rest():
    t = ladder()
    assert advance(t, bar(1, 105.5, 100.5), P, C) == ["TP1", "TP2"]  # one minute, both
    assert t.state is State.TP2
    assert advance(t, bar(2, 106, 104, 105), P, C, trail=D(103)) == ["STOP_MOVED"]
    assert t.sl == D(103)
    assert advance(t, bar(3, 106, 104, 105), P, C, trail=D(102)) == []  # never loosened
    assert advance(t, bar(4, 104, 102.9), P, C) == ["TRAIL"]
    assert [x.kind for x in t.parts] == ["TP1", "TP2", "TRAIL"]
    assert sum(x.frac for x in t.parts) == 1 and t.result_r == sum(x.r for x in t.parts)


def test_tp3_closes_the_rest_and_a_stop_beats_targets_in_the_same_minute():
    t = ladder()
    assert advance(t, bar(1, 110.5, 100.5), P, C) == ["TP1", "TP2", "TP"]
    assert t.state is State.TP and t.exit_price == D(110)
    t2 = ladder()
    assert advance(t2, bar(1, 110.5, 97.5), P, C) == ["SL"] and t2.result_r == D(-1)


def test_no_target_in_the_fill_minute():
    t = ladder(state=State.PENDING, filled_at=None)
    assert advance(t, bar(0, 106, 99.9), P, C) == ["FILLED"] and t.parts == []


def test_tp1_before_the_fill_is_missed():
    t = ladder(state=State.PENDING, filled_at=None)
    assert advance(t, bar(0, 102.5, 100.5), P, C) == ["MISSED"]


def test_time_stop_when_neither_tp1_nor_sl_within_the_window():
    t = ladder()
    for i in range(1, 180):
        assert advance(t, bar(i, 101, 99, 100.5), P, C) == []
    assert advance(t, bar(180, 101, 99, 100.7), P, C) == ["TIME_STOP"]
    assert t.exit_price == D("100.7")
    t2 = ladder()
    advance(t2, bar(1, 102.5, 100.5), P, C)  # TP1 in time: no time stop for the rest
    for i in range(2, 200):
        assert "TIME_STOP" not in advance(t2, bar(i, 101.5, 100.5), P, C)


def test_wallet_parts_sum_to_the_whole_trade_with_their_own_fees():
    t = ladder()
    advance(t, bar(1, 105.5, 100.5), P, C)
    advance(t, bar(2, 104, 100.1), P, C)  # the rest at the break-even stop... after TP2
    qty = D(10)
    pnl = [part_pnl(t, x, qty, C) for x in t.parts]
    total = sum(p for p, _ in pnl)
    fees = sum(f for _, f in pnl)
    # the same trade in one piece: qty x sum(frac x net per unit)
    assert total == sum(qty * x.frac * t.net_per_unit(x.price, C) for x in t.parts)
    assert fees == sum(
        qty * x.frac * (t.entry * C.maker_fee + x.price * C.taker_fee) for x in t.parts
    )
    assert t.result_r == total / (qty * t.risk)


def test_ladder_shares_follow_the_quantity_step():
    p = SmcParams()
    assert ladder_fracs(D(10), p, D(1)) == (D("0.5"), D("0.3"))
    f1, f2 = ladder_fracs(D(3), p, D(1))  # 1.5 -> 1, 0.9 -> 0: TP2's share is empty
    assert (f1, f2) == (D(1) / 3, D(0))
    f1, f2 = ladder_fracs(D(1), p, D(1))  # TP1 rounds to 0: its share moves to TP2 (0.8 -> 0)
    assert (f1, f2) == (D(0), D(0))
