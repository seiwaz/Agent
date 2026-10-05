"""SMC-2.0 zone setups: sweep -> displacement -> order block + FVG, in that order, and the
order evaluated when price first comes back into the FVG."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

from sp2l.core.types import Side
from sp2l.smc.backtest import build_context, orders, run
from sp2l.smc.model import Costs
from sp2l.smc.strategy import evaluate, find_arming, zone_setups
from sp2l.smc.structure import analyze
from tests.smc.fixtures import (
    ARMING_BAR,
    BREAK_BAR,
    FILL_BAR,
    SETUP,
    SWEEP_BAR,
    T0,
    H,
    P,
    Row,
    expand,
    hourly,
)


def setups(rows: list[Row], p=P):
    a = analyze(hourly(rows), "1h", p)
    return a, zone_setups(a, p)


def with_rows(**changes: Row) -> list[Row]:
    rows = list(SETUP)
    for k, v in changes.items():
        rows[int(k[1:])] = v
    return rows


def test_sweep_then_displacement_then_ob_with_fvg_is_one_setup():
    a, (s,) = setups(SETUP)
    assert (s.direction, s.sweep.idx, s.sweep.level, s.sweep.wick) == (Side.LONG, SWEEP_BAR, 95, 94)
    assert (s.event.kind, s.event.break_idx, s.event.level) == ("BOS", BREAK_BAR, 104)
    assert (s.ob.idx, s.ob.bottom, s.ob.top) == (SWEEP_BAR, 94, D("97.5"))
    assert s.gap == (D("97.5"), 99) and s.edge == D("97.5")
    assert s.confirmed_at == T0 + (BREAK_BAR + 1) * H  # known at the close of the break bar


def test_no_sweep_no_setup():
    _, ss = setups(with_rows(b12=(97, 97.5, 95.2, 96.5)))  # low stays above the 95 swing
    assert ss == []


def test_a_wick_that_closes_beyond_the_level_is_not_a_sweep():
    a, ss = setups(with_rows(b12=(97, 97.5, 94, 94.5)))  # closes below 95: a break, no sweep
    assert not [w for w in a.sweeps if w.direction is Side.LONG] and ss == []


def test_order_block_before_the_sweep_is_out_of_order():
    # bar 12 turns bullish (a hammer): the last bearish candle (11) comes before the sweep
    a, ss = setups(with_rows(b12=(95.5, 97.5, 94, 96.5)))
    assert a.sweeps and a.events and ss == []


def test_break_too_long_after_the_sweep_is_ignored():
    _, ss = setups(SETUP, replace(P, sweep_max_bars=2))  # the break is 3 bars after the sweep
    assert ss == []


def test_order_block_is_the_last_opposite_candle_not_the_extreme():
    rows = with_rows(b13=(96.5, 101, 93.8, 100.8))  # the bullish bar 13 makes the lowest low
    a, (s,) = setups(rows)
    assert s.ob.idx == 12  # last bearish candle
    ext = analyze(hourly(rows), "1h", replace(P, ob_rule="extreme"))
    assert [z.idx for z in ext.order_blocks] == [13]  # SMC-1.0 rule: the extreme candle


def test_the_fvg_must_start_at_the_order_block():
    rows = with_rows(b14=(100.8, 103.5, 97.4, 103.2))  # bar 14 overlaps bar 12: no gap there
    assert setups(rows)[1] == []
    _, (s,) = setups(rows, replace(P, fvg_adjacent=False))  # a later gap of the move counts
    assert s.ob.idx == 12 and s.ob.gap_idx == 14 and s.gap == (101, 103)


def test_a_block_whose_gap_closes_after_the_break_is_known_one_bar_later():
    rows = SETUP[:13] + [(96.5, 105, 96.4, 104.8), (104.8, 106, 99, 105.5)] + SETUP[15:]
    a, (s,) = setups(rows)
    assert s.event.break_idx == 13 and s.ob.created_idx == 14
    assert s.confirmed_at == T0 + 15 * H


def test_equal_lows_are_one_pool_swept_only_when_all_are_taken():
    pre: list[Row] = [
        (100, 100.5, 99.5, 100),
        (100, 100.2, 98, 98.5),
        (98.5, 98.6, 94.8, 95.2),  # a swing low at 94.8, 0.2 below the 95 swing
        (95.2, 99, 95.1, 98.8),
        (98.8, 100.5, 98.5, 100.2),
        (100.2, 100.6, 99.8, 100),
    ]
    rows = pre + SETUP
    sweep_bar = len(pre) + SWEEP_BAR  # wick 94: takes 95 and 94.8
    a = analyze(hourly(rows), "1h", P)
    assert any(w.idx == sweep_bar and w.level == D("94.8") for w in a.sweeps)
    shallow = with_rows(b12=(97, 97.5, 94.9, 96.5))  # takes 95 only; 94.8 is within 0.1 ATR
    b = analyze(hourly(pre + shallow), "1h", replace(P, eq_tol_atr=D(1)))
    assert not [w for w in b.sweeps if w.idx == sweep_bar]
    c = analyze(hourly(pre + shallow), "1h", replace(P, eq_tol_atr=D("0.01")))
    assert [w for w in c.sweeps if w.idx == sweep_bar]  # 94.8 too far: a sweep of 95


def test_arming_is_the_first_minute_back_in_the_fvg_and_the_order_rests_at_the_edge():
    m1 = expand(SETUP)
    ctx = build_context(m1, P)
    (zs,) = zone_setups(ctx["1h"], P)
    bars = ctx["1m"].bars
    i = find_arming(zs, ctx["1h"], len(ctx["1h"].bars) - 1, bars)
    assert i is not None and T0 + ARMING_BAR * H <= bars[i].open_time < T0 + FILL_BAR * H
    assert bars[i].low <= 99 and all(b.low > 99 for b in bars[(BREAK_BAR + 1) * 60 : i])
    (s,) = orders(ctx, P, Costs())
    assert s.accepted and s.created_at == bars[i].open_time and s.entry == D("97.5")
    atr = ctx["1h"].atr[ARMING_BAR - 1]
    assert s.sl == ((94 - D("0.2") * atr) / D("0.01")).to_integral_value(
        rounding="ROUND_FLOOR"
    ) * D("0.01")
    assert s.tp1 is not None and s.tp2 is not None and s.tp1.price < s.tp2.price
    assert s.tp2.fallback and s.tp2.source == "2R" and s.tp2.net_r == 2  # no opposing 1h zone


def test_only_the_first_touch_is_an_entry():
    m1 = expand(SETUP)
    ctx = build_context(m1, P)
    (zs,) = zone_setups(ctx["1h"], P)
    late = T0 + (FILL_BAR + 1) * H  # the OB was touched in bar 20
    assert evaluate(zs, ctx, P, Costs(), late).reasons == ("NOT_FRESH",)
    assert len(orders(ctx, P, Costs())) == 1  # one order per setup


def test_filters_fee_stop_width_rr_and_bias():
    m1 = expand(SETUP)
    ctx = build_context(m1, P)
    (zs,) = zone_setups(ctx["1h"], P)
    t = T0 + ARMING_BAR * H + timedelta(minutes=35)

    def why(p, costs=None):
        return evaluate(zs, ctx, p, costs or Costs(), t).reasons

    assert why(P) == ()
    assert "FEE_TOO_HIGH" in why(replace(P, max_cost_frac=D("0.15")), Costs(D("0.01"), D("0.01")))
    assert "SL_TOO_WIDE" in why(replace(P, max_risk_pct=D("0.01")))
    assert "LOW_RR" in why(replace(P, min_net_rr_tp2=D(5)))
    assert why(replace(P, bias_tf="4h")) == ("NO_BIAS",)  # 21 hours: no 4h structure yet


def test_backtest_walks_the_ladder_to_the_trailing_exit():
    rows = SETUP + [
        (98, 102, 97.8, 101.8),
        (101.8, 105, 101.5, 104.8),
        (104.8, 108.5, 104.5, 108),
        (108, 110, 107.5, 109.5),
        (109.5, 109.8, 104, 104.5),
        (104.5, 105, 101, 101.5),
    ]
    res = run(expand(rows), P, Costs())
    s, t = res["trades"][0]
    assert t.filled_at is not None and T0 + FILL_BAR * H <= t.filled_at < T0 + (FILL_BAR + 1) * H
    assert [x.kind for x in t.parts] == ["TP1", "TP2", "TRAIL"]
    assert [x.frac for x in t.parts] == [D("0.5"), D("0.3"), D("0.2")]
    assert t.result_r == sum(x.r for x in t.parts) and t.result_r > 0
    assert res["stats"]["ladder"] == {"TP1 > TP2 > TRAIL": 1}
