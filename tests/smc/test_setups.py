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


def front_run(level: D, atr: D, *, long: bool, p=P) -> D:
    rounding = "ROUND_FLOOR" if long else "ROUND_CEILING"
    px = level - p.tp_front_run_atr * atr if long else level + p.tp_front_run_atr * atr
    return (px / p.tick).to_integral_value(rounding=rounding) * p.tick


def mirror(rows: list[Row]) -> list[Row]:
    """The same market upside down (around 200): every long setup becomes a short one."""
    return [(200 - o, 200 - lo, 200 - h, 200 - c) for o, h, lo, c in rows]


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
    assert s.sl == D("93.99")  # one tick below the OB's wick (94), nothing else
    # the target: the HH after the OB candle, before price came back (bar 17, 107.5),
    # front-run by 0.05 ATR(1h) and rounded down to the tick
    assert s.tp is not None and (s.tp.level, s.tp.source) == (D("107.5"), "1h HH")
    assert s.tp.price == front_run(D("107.5"), ctx["1h"].atr[ARMING_BAR - 1], long=True)


def test_only_the_first_touch_is_an_entry():
    m1 = expand(SETUP)
    ctx = build_context(m1, P)
    (zs,) = zone_setups(ctx["1h"], P)
    late = T0 + (FILL_BAR + 1) * H  # the OB was touched in bar 20
    assert evaluate(zs, ctx, P, Costs(), late).reasons == ("NOT_FRESH",)
    assert len(orders(ctx, P, Costs())) == 1  # one order per setup


def test_the_target_does_not_depend_on_the_stop_and_the_bias_still_filters():
    m1 = expand(SETUP)
    ctx = build_context(m1, P)
    (zs,) = zone_setups(ctx["1h"], P)
    t = T0 + ARMING_BAR * H + timedelta(minutes=35)

    def why(p, costs=None):
        return evaluate(zs, ctx, p, costs or Costs(), t).reasons

    assert why(P) == ()
    # costs only count through the net R at the TP (max_cost_frac 0 = off)
    assert why(P, Costs(D("0.01"), D("0.01"), D("0.01"))) == ("LOW_NET_RR",)
    assert why(replace(P, bias_tf="4h")) == ("NO_BIAS",)  # 21 hours: no 4h structure yet
    a = evaluate(zs, ctx, P, Costs(), t)
    b = evaluate(zs, ctx, P, Costs(D("0.01"), D("0.01")), t)
    assert a.tp is not None and b.tp is not None and a.tp.price == b.tp.price and a.sl == b.sl
    sw = evaluate(zs, ctx, replace(P, tp_ref="swing"), Costs(), t)
    # bar 17 (107.5) is not a confirmed swing yet at t: the last confirmed one is the broken 104
    assert sw.tp is not None and sw.tp.level == 104


def test_backtest_takes_the_whole_position_out_at_the_previous_hh():
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
    assert [(x.kind, x.price, x.frac) for x in t.parts] == [("TP", s.tp.price, 1)]
    assert s.tp.price < s.tp.level == D("107.5")
    assert t.result_r == t.parts[0].r > 0
    assert res["stats"]["exits"] == {"TP": 1}


def test_without_require_sweep_a_break_with_ob_and_fvg_is_enough():
    rows = with_rows(b12=(97, 97.5, 95.2, 96.5))  # no sweep
    assert setups(rows)[1] == []
    _, (s,) = setups(rows, replace(P, require_sweep=False))
    assert s.sweep is None and s.ob.idx == 12 and s.gap == (D("97.5"), 99)
    _, (t,) = setups(SETUP, replace(P, require_sweep=False))
    assert t.sweep is not None  # recorded when there is one


def _order(rows: list[Row], p=P, costs=None):
    ctx = build_context(expand(rows), p)
    (s,) = orders(ctx, p, costs or Costs())
    return ctx, s


DEEP_SWEEP = with_rows(b12=(97, 97.5, 87, 96.5))  # the wick reaches 87: the OB sits high


def test_discount_long_and_premium_short():
    _, s = _order(SETUP)  # range 94 -> 107.5, 50 % = 100.75, entry 97.5: discount
    assert s.range_mid == D("100.75") and s.accepted
    _, s = _order(DEEP_SWEEP)  # range 87 -> 107.5, 50 % = 97.25 < 97.5: not in the discount
    assert "NOT_DISCOUNT" in s.reasons and s.range_mid == D("97.25")
    _, s = _order(mirror(SETUP))  # the short mirror: entry 102.5 above 50 % = 99.25
    assert s.direction is Side.SHORT and s.range_mid == D("99.25") and s.accepted
    _, s = _order(mirror(DEEP_SWEEP))
    assert "NOT_PREMIUM" in s.reasons
    _, off = _order(DEEP_SWEEP, replace(P, require_discount=False, min_net_rr=D(0)))
    assert off.accepted


def test_short_target_front_runs_the_ll_and_rounds_up():
    ctx, s = _order(mirror(SETUP))
    assert s.tp is not None and s.tp.level == D("92.5")
    assert s.tp.price == front_run(D("92.5"), ctx["1h"].atr[ARMING_BAR - 1], long=False)
    assert s.tp.price > s.tp.level  # short: the target sits above the LL
    _, exact = _order(mirror(SETUP), replace(P, tp_front_run_atr=D(0)))
    assert exact.tp is not None and exact.tp.price == D("92.5")  # 0 = the exact level


def test_low_net_rr_and_cost_heavy_reject_without_moving_the_target():
    heavy = Costs(D("0.01"), D("0.01"), D("0.001"))
    _, base = _order(SETUP)
    _, low = _order(SETUP, replace(P, min_net_rr=D(5)))
    assert low.reasons == ("LOW_NET_RR",)
    _, cost = _order(SETUP, replace(P, max_cost_frac=D("0.15")), heavy)
    assert (
        "COST_HEAVY" in cost.reasons and cost.cost_frac is not None and cost.cost_frac > D("0.15")
    )
    _, free = _order(SETUP, replace(P, max_cost_frac=D(0)), heavy)  # 0 = off
    assert "COST_HEAVY" not in free.reasons
    _, nofilter = _order(SETUP, replace(P, require_discount=False, min_net_rr=D(0)))
    assert base.tp is not None and low.tp == base.tp and nofilter.tp == base.tp
    assert (
        cost.tp is not None
        and free.tp is not None
        and cost.tp.price == free.tp.price == base.tp.price
    )
    assert low.sl == cost.sl == base.sl
