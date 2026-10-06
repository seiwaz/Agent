"""SMC-2.3 rules on hand-built candles: displacement discount, 5m confirmation inside the zone,
the refined 5m entry, the structural stop with an ATR buffer, the nearest-liquidity target and
the cost filters; the SMC-2.2 settings still give the SMC-2.2 order."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D

from sp2l.core.types import Candle, Side
from sp2l.smc.backtest import build_context, orders
from sp2l.smc.model import Confirm, Costs
from sp2l.smc.strategy import (
    confirmation,
    entry_price,
    evaluate,
    liquidity_target,
    stop_price,
    zone_setups,
)
from sp2l.smc.structure import analyze
from tests.smc.fixtures import ARMING_BAR, SETUP, T0, H, P, Row, expand, hourly
from tests.smc.test_setups import DEEP_SWEEP, _order, mirror, setups

M5 = timedelta(minutes=5)
T5 = datetime(2026, 1, 6, 10, 0, tzinfo=T0.tzinfo)  # any time on the 5m grid


def five(rows: list[Row], t0: datetime = T5) -> list[Candle]:
    return [
        Candle(t0 + i * M5, D(str(o)), D(str(h)), D(str(lo)), D(str(c)), D(1))
        for i, (o, h, lo, c) in enumerate(rows)
    ]


# 5m after arming, for the long setup of SETUP (zone 94 .. 99: OB 94-97.5, FVG 97.5-99):
#  2: swing high 102; 5: the reaction low 96 inside the zone; 8: close 102.8 above 102 -> BOS
#  FVGs of the move: bars 5-7 (98.6 - 98.7) and 6-8 (99 - 100.7, the newest); 5: last bearish
REACTION: list[Row] = [
    (100.5, 101, 100, 100.6),
    (100.6, 101.5, 100.4, 101.2),
    (101.2, 102, 101, 101.8),
    (101.8, 101.9, 100, 100.2),
    (100.2, 100.3, 98, 98.5),
    (98.5, 98.6, 96, 96.5),
    (96.5, 99, 96.4, 98.8),
    (98.8, 101, 98.7, 100.8),
    (100.8, 103, 100.7, 102.8),
    (102.8, 103.2, 102.5, 103),
]


def confirm(rows: list[Row], p=P):
    _, (zs,) = setups(SETUP)
    ax = analyze(five(rows), "5m", p)
    return zs, confirmation(zs, ax, T5 + timedelta(minutes=1), p)


IN_ZONE = replace(P, confirm_exec=True, confirm_in_zone=True, confirm_window_min=240)


def test_confirmation_must_react_from_inside_the_zone():
    zs, c = confirm(REACTION, IN_ZONE)
    assert isinstance(c, Confirm)
    assert c.t == T5 + 9 * M5 and c.origin == D(96) and c.origin_time == T5 + 5 * M5
    assert c.fvg is not None and (c.fvg.bottom, c.fvg.top) == (D(99), D("100.7"))
    assert c.ob is not None and (c.ob.bottom, c.ob.top) == (D(96), D("98.6"))
    above = [(o + 5, h + 5, lo + 5, c + 5) for o, h, lo, c in REACTION]  # reaction from 101
    _, out = confirm(above, IN_ZONE)
    assert out is None  # no break from inside the zone yet (window still open)
    _, anyw = confirm(above, replace(IN_ZONE, confirm_in_zone=False))
    assert isinstance(anyw, Confirm)  # SMC-2.2: any break with the setup
    _, late = confirm(above, replace(IN_ZONE, confirm_window_min=30))
    assert late == ("NO_CONFIRM", T5 + timedelta(minutes=30))


def test_an_exec_close_beyond_the_ob_far_edge_invalidates_the_setup_first():
    rows = list(REACTION)
    rows[5] = (98.5, 98.6, 93, 93.5)  # closes below the OB's far edge (94)
    _, c = confirm(rows, IN_ZONE)
    assert c == ("ZONE_INVALID", T5 + 6 * M5)


def test_entry_at_the_5m_fvg_ce_else_the_5m_ob_edge_else_the_zone_fvg_ce():
    zs, c = confirm(REACTION, IN_ZONE)
    assert isinstance(c, Confirm)
    assert entry_price(zs, replace(P, entry_ref="ltf_fvg_ce"), c) == D("99.85")
    assert entry_price(zs, replace(P, entry_ref="ltf_ob_edge"), c) == D("98.6")
    no_fvg = replace(c, fvg=None)
    assert entry_price(zs, replace(P, entry_ref="ltf_fvg_ce"), no_fvg) == D("98.6")
    neither = replace(c, fvg=None, ob=None)
    assert entry_price(zs, replace(P, entry_ref="ltf_fvg_ce"), neither) == D("98.25")
    assert entry_price(zs, replace(P, entry_ref="htf_fvg_ce")) == D("98.25")
    assert entry_price(zs, replace(P, entry_ref="fvg_mid")) == D("98.25")  # SMC-2.2 name
    assert entry_price(zs, P) == D("97.5")  # ob_edge


def test_stop_beyond_the_farther_of_ob_and_sweep_wick_plus_buffer():
    p = replace(P, sl_mode="structure", sl_buffer_atr=D("0.2"))
    _, (zs,) = setups(SETUP)  # OB low 94 = the sweep wick
    assert stop_price(zs, p, D(2)) == D("93.6")
    _, (deep,) = setups(DEEP_SWEEP)  # the sweep wick 87 is farther than the OB low
    assert stop_price(deep, p, D("2.03")) == D("86.59")  # 87 - 0.406, rounded down
    _, (short,) = setups(mirror(DEEP_SWEEP))
    assert stop_price(short, p, D("2.03")) == D("113.41")  # 113 + 0.406, rounded up
    assert stop_price(zs, replace(P, sl_mode="ob_height")) == D("90.5")
    assert stop_price(zs, P) == D("93.99")


def test_discount_of_the_ob_edge_in_the_sweep_to_displacement_range():
    p = replace(P, discount_ref="displacement", require_discount=True, min_net_rr=D(0))
    _, s = _order(SETUP, p)  # 94 -> 106 (displacement high at the setup's close): 50 % = 100
    assert s.range_mid == D(100) and s.accepted  # OB edge 97.5 in the discount half
    _, deep = _order(DEEP_SWEEP, p)  # 87 -> 106: 96.5 < 97.5
    assert deep.range_mid == D("96.5") and deep.reasons == ("NOT_DISCOUNT",)
    _, short = _order(mirror(DEEP_SWEEP), p)
    assert short.direction is Side.SHORT and short.reasons == ("NOT_PREMIUM",)


HIGH_BEFORE = list(SETUP)
HIGH_BEFORE[2] = (97.5, 110, 96, 96.5)  # an older swing high at 110, never taken


def test_target_is_the_nearest_unswept_liquidity_never_a_farther_level():
    p = replace(P, tp_mode="liquidity", min_net_rr=D(0))
    ctx = build_context(expand(HIGH_BEFORE), p)
    (zs,) = zone_setups(ctx["1h"], p)
    t = T0 + ARMING_BAR * H + timedelta(minutes=35)
    atr = ctx["1h"].atr[ARMING_BAR - 1]
    assert atr is not None
    lvl, src, px = liquidity_target(zs, ctx, p, t, zs.edge) or (None, None, None)
    # 104 was swept by the break; the leg's HH 107.5 is nearer than the old 110 high
    assert (lvl, src) == (D("107.5"), "1h HH")
    assert px == ((D("107.5") - D("0.05") * atr) / D("0.01")).to_integral_value("ROUND_FLOOR") * D(
        "0.01"
    )
    far = liquidity_target(zs, ctx, p, t, D(108))  # from above the HH: the next one, 110
    assert far is not None and far[:2] == (D(110), "1h swing high")
    assert liquidity_target(zs, ctx, p, t, D(111)) is None  # nothing beyond: NO_TARGET
    (s,) = orders(ctx, p, Costs())
    assert s.tp is not None and s.tp.level == D("107.5") and s.accepted


def test_low_net_rr_and_cost_heavy_reject_without_moving_the_target():
    p = replace(P, tp_mode="liquidity", min_net_rr=D(0))
    ctx = build_context(expand(HIGH_BEFORE), p)
    (zs,) = zone_setups(ctx["1h"], p)
    t = T0 + ARMING_BAR * H + timedelta(minutes=35)
    base = evaluate(zs, ctx, p, Costs(), t)
    low = evaluate(zs, ctx, replace(p, min_net_rr=D(5)), Costs(), t)
    heavy = evaluate(
        zs, ctx, replace(p, max_cost_frac=D("0.2")), Costs(D("0.01"), D("0.01"), D("0.001")), t
    )
    assert base.accepted and low.reasons == ("LOW_NET_RR",) and "COST_HEAVY" in heavy.reasons
    assert base.tp is not None and low.tp is not None and heavy.tp is not None
    assert base.tp.price == low.tp.price == heavy.tp.price and base.sl == low.sl == heavy.sl


def test_smc22_settings_give_the_smc22_order():
    p22 = replace(P, entry_ref="fvg_mid", sl_mode="ob_height", tp_rr=D(3), require_discount=False)
    explicit = replace(p22, entry_ref="htf_fvg_ce", tp_mode="fixed")
    _, a = _order(SETUP, p22)
    _, b = _order(SETUP, explicit)
    assert (a.entry, a.sl, a.tp) == (b.entry, b.sl, b.tp)
    assert (a.entry, a.sl) == (D("98.25"), D("90.5")) and a.tp is not None
    assert a.tp.price == D("121.5") and a.accepted


def test_utc_grid_backtest_reads_the_same_setup_on_15m():
    # the zone TF (here 1h in the fixture) on the UTC grid shifts the hourly bars by 30 min:
    # the strategy runs on either grid without errors and finds setups from the same minutes
    p = replace(P, htf_grid="utc")
    ctx = build_context(expand(SETUP), p)
    assert all(b.open_time.minute == 0 for b in ctx["1h"].bars)
    assert hourly(SETUP)[0].open_time.minute == 30


def test_funding_lowers_the_long_and_lifts_the_short_result_at_each_funding_time():
    from sp2l.smc.lifecycle import Tracked, advance, risk_unit

    costs = Costs(funding_rate=D("0.001"), funding_interval_h=8)
    t0 = datetime(2026, 1, 6, 7, 58, tzinfo=T0.tzinfo)

    def run(side: Side, rate_costs: Costs):
        long = side is Side.LONG
        entry, sl, tp = (D(100), D(90), D(130)) if long else (D(100), D(110), D(70))
        t = Tracked(
            "f", side, entry, sl, tp, t0, risk_unit(side, entry, sl, rate_costs), market=True
        )
        events = []
        for i in range(4):  # 07:58 .. 08:01; funding at 08:00
            events += advance(
                t,
                Candle(t0 + i * timedelta(minutes=1), D(100), D(101), D(99), D(100)),
                P,
                rate_costs,
            )
        return t, events

    lt, lev = run(Side.LONG, costs)
    assert lev == ["FUNDING"] and lt.funding == D("0.1")  # 0.001 x the 08:00 open
    st, _ = run(Side.SHORT, costs)
    assert st.funding == D("-0.1")  # a short receives a positive rate
    _, none = run(Side.LONG, Costs())
    assert none == []  # no rate, no funding


def test_liquidation_guard_cuts_the_size_and_rejects_below_one_step():
    p = replace(
        P, risk_pct=D("0.9"), liq_buffer_r=D(1), require_discount=False, max_leverage=D(100)
    )
    _, s = _order(SETUP, p)  # entry 97.5, stop 93.99: by risk 25.6 units
    cap = D(100) / (2 * D("3.51") + D("0.005") * D("97.5"))  # 13.33
    assert s.qty == cap.quantize(D("0.00001"), "ROUND_DOWN") and s.accepted
    _, none = _order(SETUP, replace(p, liq_buffer_r=D(10) ** 9))
    assert none.reasons == ("LIQ_LIMITED",)
    _, off = _order(SETUP, replace(p, liq_buffer_r=D(0)))
    assert off.qty is not None and s.qty is not None and off.qty > s.qty
