"""The diagnostic replay: with the engine's readings it equals the backtest; the alternative
readings (fill through, target first, target in the fill minute) do what they say; fees are
charged once, on notional."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from sp2l.core.types import Candle, Side
from sp2l.smc.backtest import build_context, run
from sp2l.smc.diagnostics.core import simulate, walk
from sp2l.smc.lifecycle import Part, risk_unit
from sp2l.smc.model import Costs, Setup, SmcParams, Target, ZoneSetup
from sp2l.smc.wallet import part_pnl
from tests.smc.fixtures import SETUP, P, expand
from tests.smc.test_timeframes_strategy import DENSE, m1_walk

T0 = datetime(2026, 1, 1, tzinfo=UTC)
C = Costs(D("0.0008"), D("0.00095"), D("0.000326"))


def _view(t):
    return (t.key, t.state, t.filled_at, t.closed_at, t.exit_price, t.result_r)


def test_default_readings_reproduce_the_backtest_exactly():
    for m1, p in ((m1_walk(6 * 1440, 3), DENSE), (expand(SETUP + [(98, 102, 97.8, 101.8)] * 3), P)):
        ctx = build_context(m1, p)
        ref = run(m1, p, C, ctx=ctx)["trades"]
        _, got = simulate(ctx, p, C)
        assert [_view(t) for _, t in ref] == [_view(w.t) for w in got]


def _order(entry=100, sl=98, tp=104) -> Setup:
    zs = ZoneSetup.__new__(ZoneSetup)  # the walk only reads the order's prices
    return Setup(
        key="k",
        direction=Side.LONG,
        accepted=True,
        reasons=(),
        created_at=T0,
        zone=zs,
        bias=1,
        entry=D(entry),
        sl=D(sl),
        tp=Target(D(tp), "test", D(0), D(tp)),
    )


def bar(i: int, h: float, lo: float, c: float | None = None) -> Candle:
    c = lo if c is None else c
    o = min(max(c, lo), h)
    return Candle(T0 + timedelta(minutes=i), D(str(o)), D(str(h)), D(str(lo)), D(str(c)))


P0 = SmcParams(pending_expiry_min=60, max_hold_min=600)


def test_fill_on_touch_versus_through():
    bars = [bar(0, 101, 100), bar(1, 101, 99.5), bar(2, 104.5, 100.5)]
    touch = walk(_order(), bars, 0, P0, Costs())
    assert touch.t.filled_at == bars[0].open_time and touch.touch_only  # low == entry
    through = walk(_order(), bars, 0, P0, Costs(), fill="through")
    assert through.t.filled_at == bars[1].open_time and not through.touch_only


def test_both_minute_and_target_in_the_fill_minute_readings():
    both = [bar(0, 101, 99.9), bar(1, 105, 97)]
    w = walk(_order(), both, 0, P0, Costs())
    assert w.both_minute and w.t.result_r == -1 and w.alt_both_r == 2
    w2 = walk(_order(), both, 0, P0, Costs(), stop_first=False)
    assert w2.t.result_r == 2 and w2.alt_both_r == -1
    fill_tp = [bar(0, 105, 99.9), bar(1, 101, 99.5)]
    w3 = walk(_order(), fill_tp, 0, P0, Costs())
    assert w3.tp_in_fill_minute and w3.t.state.value == "OPEN" and w3.alt_fill_r == 2
    w4 = walk(_order(), fill_tp, 0, P0, Costs(), tp_in_fill=True)
    assert w4.t.result_r == 2


def test_fees_are_charged_once_on_notional_maker_in_taker_out():
    t = walk(_order(), [bar(0, 101, 99.9), bar(1, 104.5, 100.5)], 0, P0, C).t
    qty = D("0.01")
    part = t.parts[0]
    pnl, fees = part_pnl(t, part, qty, C)
    assert fees == qty * (D(100) * C.maker_fee + D(104) * C.taker_fee)  # notional, once each
    assert pnl == qty * D(4) - fees and t.result_r == pnl / (qty * t.risk)
    assert t.risk == risk_unit(Side.LONG, D(100), D(98), C)  # the same fees in the R unit
    assert replace(part, frac=D(1)) == part
    assert isinstance(part, Part)
