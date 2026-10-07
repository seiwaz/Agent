# ruff: noqa: E501  (synthetic bar rows are clearer on one line)
"""System B backtest: indicators, fill timing, stops (intraday and gap), channel exit, sizing,
costs, exposure cap, regime filter, pyramiding, CSV loading and no look-ahead."""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from sp2l.core.types import Candle
from sp2l.trend import backtest as tb
from sp2l.trend.data import load_csv
from sp2l.trend.model import TrendParams

T0 = datetime(2024, 1, 1, tzinfo=UTC)
NOFEE = tb.Fees(0.0, 0.0)
# short channels so the synthetic paths stay readable
P = TrendParams(entry_len=5, exit_len=3, atr_len=3, stop_atr=2.0)


def bars(rows: list[tuple[float, float, float, float]]) -> list[Candle]:
    return [
        Candle(T0 + timedelta(days=i), D(str(o)), D(str(h)), D(str(lo)), D(str(c)), D(1))
        for i, (o, h, lo, c) in enumerate(rows)
    ]


def flat(n: int, px: float = 100.0) -> list[tuple[float, float, float, float]]:
    return [(px, px + 1, px - 1, px)] * n


def walk(n: int, seed: int) -> list[Candle]:
    rnd, px, out = random.Random(seed), 100.0, []
    for _ in range(n):
        o = px
        c = max(1.0, o * math.exp(rnd.gauss(0.001, 0.03)))
        out.append(
            (
                o,
                max(o, c) * (1 + abs(rnd.gauss(0, 0.01))),
                min(o, c) * (1 - abs(rnd.gauss(0, 0.01))),
                c,
            )
        )
        px = c
    return bars(out)


def test_indicators():
    assert tb.prior_max([1, 5, 2, 3, 9], 2)[2:] == [5, 5, 3]  # excludes the bar itself
    assert tb.prior_min([4, 1, 3, 2, 0], 2)[2:] == [1, 1, 2]
    assert tb.sma([1, 2, 3, 4], 2)[1:] == [1.5, 2.5, 3.5]
    h, lo, c = [11, 12, 13, 14], [9, 10, 11, 12], [10, 11, 12, 13]
    atr = tb.wilder_atr(h, lo, c, 2)
    assert math.isnan(atr[1]) and atr[2] == 2.0 and atr[3] == 2.0  # TR = 2 every bar


def test_entry_next_open_initial_stop_and_stop_exit_with_slippage():
    rows = flat(8) + [(100, 106, 99, 105), (107, 108, 106, 107), (107, 107.5, 90, 92)] + flat(3, 92)
    r = tb.run(bars(rows), P, tb.Fees(0.0, 0.001))
    t = r.trades[0]
    assert t.entry_time == T0 + timedelta(days=9)  # breakout close on day 8, fill day 9 open
    assert t.entry_price == pytest.approx(107 * 1.001)
    atr8 = tb.wilder_atr(*zip(*[(h, lo, c) for _, h, lo, c in rows], strict=True), 3)[8]
    stop = 107 * 1.001 - 2 * atr8
    assert t.reason == "STOP" and t.exit_time == T0 + timedelta(days=10)
    assert t.exit_price == pytest.approx(stop * 0.999)  # at the stop, minus slippage


def test_gap_through_the_stop_fills_at_the_open():
    rows = flat(8) + [(100, 106, 99, 105), (107, 108, 106, 107), (80, 81, 79, 80)] + flat(3, 80)
    t = tb.run(bars(rows), P, NOFEE).trades[0]
    assert t.reason == "STOP" and t.exit_price == 80


def test_channel_exit_at_next_open_and_risk_sizing_is_one_r():
    up = [(100 + i, 101 + i, 99.5 + i, 100.8 + i) for i in range(12)]
    down = [(112, 112.2, 110.5, 110.6), (110.6, 110.8, 109.5, 109.6), (109.0, 109.2, 108.5, 108.6)]
    rows = flat(8) + up + down + flat(4, 108.6)
    r = tb.run(bars(rows), P, NOFEE)
    t = r.trades[0]
    assert t.reason == "CHANNEL"
    # the exit fills at the open after the first close below the prior 3-day low
    i_sig = next(i for i in range(20, len(rows)) if rows[i][3] < min(x[2] for x in rows[i - 3 : i]))
    assert t.exit_time == T0 + timedelta(days=i_sig + 1) and t.exit_price == rows[i_sig + 1][0]
    assert t.risk == pytest.approx(P.initial_equity * P.risk_pct, rel=1e-9)
    assert t.pnl == pytest.approx(t.qty * (t.exit_price - t.entry_price))


def test_full_stop_without_costs_loses_exactly_risk_pct():
    rows = (
        flat(8) + [(100, 106, 99, 105), (106, 106.5, 105.5, 106), (106, 106, 50, 60)] + flat(3, 60)
    )
    r = tb.run(bars(rows), P, NOFEE)
    t = r.trades[0]
    assert t.reason == "STOP" and t.r == pytest.approx(-1.0)
    assert r.equity[-1] == pytest.approx(P.initial_equity * (1 - P.risk_pct))


def test_fees_lower_the_result_and_are_booked():
    b = walk(600, 3)
    a, f = tb.run(b, P, NOFEE), tb.run(b, P, tb.Fees(0.002, 0.0005))
    assert f.equity[-1] < a.equity[-1]
    assert sum(t.fees for t in f.trades) > 0 and sum(t.fees for t in a.trades) == 0


def test_exposure_cap_and_full_sizing():
    b = walk(800, 5)
    p = TrendParams(entry_len=5, exit_len=3, atr_len=3, risk_pct=0.5, max_exposure=1.0)
    r = tb.run(b, p, NOFEE)
    for t in r.trades:  # never more notional than equity at the fill
        assert t.qty * t.entry_price <= r.equity[t.entry_i - 1] * 1.0000001
    full = tb.run(b, TrendParams(entry_len=5, exit_len=3, atr_len=3, sizing="full"), NOFEE)
    t = full.trades[0]
    assert t.qty * t.entry_price == pytest.approx(full.equity[t.entry_i - 1])


def test_regime_filter_blocks_entries_below_the_average():
    rows = (
        [(200 - i, 201 - i, 199 - i, 200 - i) for i in range(30)]
        + flat(6, 170)
        + [(170, 173, 169, 172)]
        + flat(5, 172)
    )  # breaks the 5-day high, below SMA(20) ~174.7
    assert tb.run(bars(rows), P, NOFEE).trades
    assert not tb.run(
        bars(rows), TrendParams(entry_len=5, exit_len=3, atr_len=3, regime_ma=20), NOFEE
    ).trades


def test_pyramiding_adds_units_and_moves_the_stop():
    up = [(100 + 3 * i, 103.5 + 3 * i, 99.5 + 3 * i, 103 + 3 * i) for i in range(15)]
    rows = flat(8) + up + flat(1, 145)
    p = TrendParams(entry_len=5, exit_len=3, atr_len=3, max_units=3, add_atr=0.5)
    t = tb.run(bars(rows), p, NOFEE).trades[0]
    assert t.units == 3 and t.reason == "OPEN"
    one = tb.run(bars(rows), P, NOFEE).trades[0]
    assert t.qty > one.qty and t.stop > one.stop


def test_no_lookahead_signals_identical_when_the_future_is_cut():
    b = walk(700, 11)
    full = tb.run(b, P, tb.Fees(0.001, 0.0003))
    for cut in (200, 350, 500, 650):
        part = tb.run(b[:cut], P, tb.Fees(0.001, 0.0003))
        assert part.equity == pytest.approx(full.equity[:cut])
        done = [t.as_dict() for t in part.trades if t.reason != "OPEN"]
        assert done == [t.as_dict() for t in full.trades[: len(done)]]


def test_summary_and_grid_cover_the_same_window():
    b = walk(900, 2)
    r = tb.run(b, P, NOFEE)
    s = tb.summary(r, NOFEE)
    assert (
        s["full"]["strategy"]["from"]
        == s["full"]["buy_hold"]["from"]
        == b[P.warmup].open_time.date().isoformat()
    )
    assert sum(y["year"] > 0 for y in s["yearly"]) == len({x.open_time.year for x in b[P.warmup :]})
    rows = tb.grid(b, P, NOFEE, {"entry_len": [5, 10], "exit_len": [3, 5]})
    assert len(rows) == 4 and all(x["cagr_pct"] is not None for x in rows)


def test_load_csv_formats_repairs_and_dedupe(tmp_path: Path):
    f = tmp_path / "x.csv"
    f.write_text(
        "Date,Open,High,Low,Close,Volume\n"
        "2024-01-02,10,12,9,11,5\n"
        "2024-01-01,9,10,8,10,1\n"
        "2024-01-02,10,12,9,11.5,5\n"  # duplicate day: the last row wins
        "2024-01-04,11,11.2,10,11.5,1\n"  # close above high: repaired
    )
    b, q = load_csv(f)
    assert [x.open_time.day for x in b] == [1, 2, 4] and b[1].close == D("11.5")
    assert b[2].high == D("11.5") and q["repaired"] == 1 and q["missing_days"] == 1
    g = tmp_path / "y.csv"
    g.write_text(",open,high,low,close\n1704067200000,1,2,0.5,1.5\n1704153600,1.5,2,1,1.8\n")
    b2, _ = load_csv(g)
    assert [x.open_time for x in b2] == [T0, T0 + timedelta(days=1)]


def test_params_from_mapping_rejects_unknown_and_casts():
    p = TrendParams.from_mapping({"entry_len": "30", "stop_atr": "2.5", "sizing": "full"})
    assert p.entry_len == 30 and p.stop_atr == 2.5 and p.sizing == "full"
    with pytest.raises(ValueError):
        TrendParams.from_mapping({"entry": 3})
    with pytest.raises(ValueError):
        TrendParams(sizing="kelly")


def _mirror(b: list[Candle], k: float = 1000.0) -> list[Candle]:
    """Price -> k - price: highs become lows, every up-trend a down-trend; ATR is unchanged."""
    return [
        Candle(
            x.open_time,
            D(str(k - float(x.open))),
            D(str(k - float(x.low))),
            D(str(k - float(x.high))),
            D(str(k - float(x.close))),
            D(1),
        )
        for x in b
    ]


def test_short_side_is_the_exact_mirror_of_the_long_side():
    b = walk(700, 21)
    b = [
        Candle(x.open_time, x.open + 400, x.high + 400, x.low + 400, x.close + 400, D(1)) for x in b
    ]
    ps = TrendParams(entry_len=5, exit_len=3, atr_len=3, allow_short=True)
    orig, mirr = tb.run(b, ps, NOFEE), tb.run(_mirror(b), ps, NOFEE)
    flip = {"LONG": "SHORT", "SHORT": "LONG"}
    assert len(orig.trades) > 10 and {x.side for x in orig.trades} == {"LONG", "SHORT"}
    assert [(flip[x.side], x.reason, round(x.r, 6)) for x in orig.trades] == [
        (x.side, x.reason, round(x.r, 6)) for x in mirr.trades
    ]
    assert mirr.equity == pytest.approx(orig.equity)


def test_short_stop_above_and_exit_on_a_close_above_the_channel():
    down = [(100 - i, 100.5 - i, 98.9 - i, 99.2 - i) for i in range(10)]
    rows = flat(8) + down + [(90.5, 95.5, 90.4, 95.0)] + flat(3, 95)  # closes above the 3-day high
    p = TrendParams(entry_len=5, exit_len=3, atr_len=3, allow_short=True)
    t = tb.run(bars(rows), p, NOFEE).trades[0]
    assert t.side == "SHORT" and t.stop > t.entry_price
    assert t.reason == "CHANNEL" and t.exit_price == 95.0  # at the next open


def test_funding_is_charged_on_the_notional_held_at_each_close():
    up = [(100 + i, 101.2 + i, 99.8 + i, 101 + i) for i in range(10)]
    rows = flat(8) + up
    b = bars(rows)
    rate = {x.open_time: 0.001 for x in b}  # 0.1 % a day, longs pay
    r0, r1 = tb.run(b, P, NOFEE), tb.run(b, P, NOFEE, rate)
    t = r1.trades[0]
    held = range(t.entry_i, len(b))
    assert t.funding == pytest.approx(sum(t.qty * rows[i][3] * 0.001 for i in held))
    assert r1.equity[-1] == pytest.approx(r0.equity[-1] - t.funding)
    s = tb.run(
        _mirror(b), TrendParams(entry_len=5, exit_len=3, atr_len=3, allow_short=True), NOFEE, rate
    )
    assert s.trades[0].side == "SHORT" and s.trades[0].funding < 0  # shorts receive a positive rate


def test_liquidation_on_a_gap_through_the_liquidation_price_ends_the_run():
    rows = (
        flat(8)
        + [(100, 106, 99, 105), (106, 106.5, 105.5, 106), (80, 81, 79, 80)]
        + flat(3, 80)
        + [(80, 90, 79, 89)]
        + flat(3, 89)
    )
    p = TrendParams(entry_len=5, exit_len=3, atr_len=3, sizing="full", max_exposure=5)
    r = tb.run(bars(rows), p, NOFEE)
    assert r.liquidations == 1 and r.trades[0].reason == "LIQUIDATION" and len(r.trades) == 1
    assert r.equity[-1] < 1  # the equity is gone
    # 5x long from 106: liquidation near 85; at 3x it is near 71, so the gap to 80 is a stop exit
    r3 = tb.run(
        bars(rows),
        TrendParams(entry_len=5, exit_len=3, atr_len=3, sizing="full", max_exposure=3),
        NOFEE,
    )
    assert r3.liquidations == 0 and r3.trades[0].reason == "STOP" and r3.trades[0].exit_price == 80


def test_liquidation_price():
    # long 1 BTC at 100 with equity 25 (4x): cash = 25 - 100 = -75
    px = tb.liquidation_price(-75.0, 1, 1.0, 0.005)
    assert px is not None and -75 + px == pytest.approx(0.005 * px)
    assert tb.liquidation_price(10.0, 1, 1.0, 0.005) is None  # unlevered long
    sp = tb.liquidation_price(125.0, -1, 1.0, 0.005)  # short 1 at 100 with equity 25
    assert sp is not None and 125 - sp == pytest.approx(0.005 * sp)


def test_bool_params_from_strings():
    assert TrendParams.from_mapping({"allow_short": "false"}).allow_short is False
    assert TrendParams.from_mapping({"allow_short": "true"}).allow_short is True


def test_no_lookahead_in_futures_mode():
    b = walk(700, 13)
    rate = {x.open_time: 0.0003 * math.sin(i / 9) for i, x in enumerate(b)}
    p = TrendParams(
        entry_len=5, exit_len=3, atr_len=3, allow_short=True, max_exposure=3, risk_pct=0.03
    )
    full = tb.run(b, p, tb.Fees(0.001, 0.0003), rate)
    for cut in (250, 450, 650):
        part = tb.run(b[:cut], p, tb.Fees(0.001, 0.0003), rate)
        assert part.equity == pytest.approx(full.equity[:cut])
