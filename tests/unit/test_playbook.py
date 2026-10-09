# ruff: noqa: E501  (bar tables and expected rows are clearer on one line)
"""The playbook (sp2l.playbook): indicators, each strategy's rules long and short, the
conservative fills and exits, costs in R, no look-ahead (a setup decided on bar i is the same
whatever comes after), the 4h EMA 200 from closed 4h bars only, and the service's answer."""

from __future__ import annotations

import math
import random
from typing import Any

import pytest

from sp2l.playbook import indicators as ind
from sp2l.playbook.engine import (
    STRATEGIES,
    Costs,
    Ctx,
    Params,
    Setup,
    _funding_times,
    donchian_exit,
    outcome,
    run,
    smc,
    stats,
    step,
)

T0 = 1_700_006_400  # a 4h boundary


def bars_from(rows: list[tuple[float, float, float, float]], t0: int = T0) -> list[dict[str, Any]]:
    return [{"t": t0 + i * 3600, "o": o, "h": h, "l": lo, "c": c, "v": 1.0}
            for i, (o, h, lo, c) in enumerate(rows)]


def walk(n: int, seed: int) -> list[dict[str, Any]]:
    """A random walk with changing volatility and slow trends (every pattern shows up)."""
    r = random.Random(seed)
    p, vol, out = 100.0, 0.006, []
    for i in range(n):
        vol = max(0.002, min(0.02, vol * math.exp(r.gauss(0, 0.08))))
        o = p
        c = o * math.exp(0.0006 * math.sin(i / 400) + r.gauss(0, vol))
        h = max(o, c) * (1 + abs(r.gauss(0, vol * 0.6)))
        lo = min(o, c) * (1 - abs(r.gauss(0, vol * 0.6)))
        out.append({"t": T0 + i * 3600, "o": o, "h": h, "l": lo, "c": c, "v": 1 + r.random()})
        p = c
    return out


def agg4(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"t": bars[i]["t"], "o": bars[i]["o"], "h": max(b["h"] for b in bars[i:i + 4]),
             "l": min(b["l"] for b in bars[i:i + 4]), "c": bars[i + 3]["c"], "v": 1.0}
            for i in range(0, len(bars) - 3, 4)]


# ---- indicators -------------------------------------------------------------------------------
def test_indicators():
    assert ind.ema([1, 2, 3, 4], 3) == [None, None, 2.0, 3.0]  # seeded with the average
    assert ind.wilder([2, 4, 6, 8], 2) == [None, 3.0, 4.5, 6.25]
    assert ind.prev_max([1, 5, 2, 3, 4], 2) == [None, None, 5, 5, 3]  # bar i excluded
    assert ind.prev_min([5, 1, 2, 3, 0], 2) == [None, None, 1, 1, 2]
    assert ind.pivots([3, 2, 1, 2, 3, 2, 3], 2, high=False) == [2]
    up = [{"h": 100 + i + 0.5, "l": 100 + i - 0.5, "c": 100 + i} for i in range(60)]
    adx = ind.adx([b["h"] for b in up], [b["l"] for b in up], [b["c"] for b in up], 14)
    assert adx[27] is not None and adx[26] is None and adx[-1] > 90  # a straight trend


# ---- liquidity sweep + BOS + FVG / OB, built bar by bar -----------------------------------------
def smc_rows() -> list[tuple[float, float, float, float]]:
    rows = []
    for i in range(40):  # a calm range near 106
        b = 106 + (0.3 if i % 2 else -0.3)
        rows.append((b, b + 0.5, b - 0.5, b + (0.1 if i % 2 else -0.1)))
    rows += [(106.0, 106.5, 105.2, 105.4), (105.4, 105.6, 104.4, 104.6), (104.6, 104.8, 103.6, 103.8),
             (103.8, 104.0, 102.8, 103.0), (103.0, 103.2, 102.0, 102.2), (102.2, 102.4, 101.2, 101.4),
             (101.4, 101.6, 100.6, 100.8), (100.8, 101.2, 100.3, 101.0),
             (101.0, 101.3, 100.0, 100.4),  # 48: equal low A, 100.00
             (100.4, 101.6, 100.3, 101.5), (101.5, 102.3, 101.2, 102.1), (102.1, 102.8, 101.8, 102.4),
             (102.4, 102.6, 101.6, 101.8), (101.8, 102.0, 101.0, 101.2), (101.2, 101.4, 100.4, 100.6),
             (100.6, 100.9, 100.05, 100.5),  # 55: equal low B, 100.05
             (100.5, 101.6, 100.4, 101.4), (101.4, 102.4, 101.2, 102.2),
             (102.2, 102.7, 101.9, 102.3),  # 58: the swing high standing at the sweep, 102.7
             (102.3, 102.5, 101.5, 101.7), (101.7, 101.9, 100.9, 101.1), (101.1, 101.3, 100.6, 100.8),
             (100.8, 101.0, 99.4, 100.6),  # 62: the sweep: wick 99.4, close back above
             (100.9, 101.0, 100.3, 100.4),  # 63: the order block (body 100.4 - 100.9)
             (100.5, 103.3, 100.45, 103.2),  # 64: displacement closing above 102.7 (BOS)
             (103.2, 103.7, 101.8, 103.5),  # 65: FVG 101.0 - 101.8: the setup is complete
             (103.5, 103.6, 102.6, 102.8), (102.8, 102.9, 101.8, 102.0),
             (102.0, 102.1, 100.8, 101.1),  # 68: back to the order block: filled at 100.9
             (101.1, 102.6, 101.0, 102.5), (102.5, 104.0, 102.3, 103.9), (103.9, 105.3, 103.7, 105.2),
             (105.2, 106.7, 105.0, 106.6), (106.6, 106.9, 106.0, 106.3)]  # 73: the target
    return rows


def mirror(rows):
    return [(200 - o, 200 - lo, 200 - h, 200 - c) for o, h, lo, c in rows]


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_liquidity_sweep_setup_fills_at_the_order_block_and_reaches_its_target(side):
    rows = smc_rows() if side == "LONG" else mirror(smc_rows())
    x = Ctx(bars_from(rows), [], Params(htf_filter=False))
    d = 1 if side == "LONG" else -1
    assert smc(x, 64, d)[1] is None  # the FVG is not known before bar 65 closes
    checks, s = smc(x, 65, d)
    assert s is not None and s.status == "pending" and all(c["ok"] for c in checks)
    entry, sl, tp = (100.9, 99.4, 106.8) if side == "LONG" else (99.1, 100.6, 93.2)
    assert s.kind == "limit" and s.entry == pytest.approx(entry) and s.tp == pytest.approx(tp)
    assert s.sl == pytest.approx(sl - d * 0.1 * x.atr14[62])  # the sweep's extreme - 0.1 ATR
    assert s.marks["eq"][2] == pytest.approx(100.0 if d > 0 else 100.0)
    assert s.marks["ob"][1:] == pytest.approx([100.9, 100.4] if d > 0 else [99.6, 99.1])
    [r] = run(x, STRATEGIES["smc"], Costs(), 0)
    assert (r.side, r.status, r.signal_i, r.fill_i, r.exit_i) == (side, "tp", 65, 68, 73)
    o = outcome(r, x, Costs())
    assert o["r"] == pytest.approx(abs(tp - entry) / abs(entry - r.sl), abs=1e-3)


def test_the_target_before_the_fill_is_a_missed_setup_and_a_short_target_is_refused():
    rows = smc_rows()[:66] + [(103.5, 107.0, 103.4, 106.9), (106.9, 107.0, 100.0, 100.2)]
    x = Ctx(bars_from(rows), [], Params(htf_filter=False))
    [r] = run(x, STRATEGIES["smc"], Costs(), 0)
    assert (r.status, r.fill_i) == ("missed", None)  # no trade, no loss
    far = Ctx(bars_from(smc_rows()), [], Params(htf_filter=False, smc_min_rr=5))
    [rej] = run(far, STRATEGIES["smc"], Costs(), 0)
    assert rej.status == "rejected" and "R:R >= 5" in rej.reason


# ---- fills and exits ---------------------------------------------------------------------------
def ctx(rows):
    return Ctx(bars_from(rows), [], Params(htf_filter=False))


def test_stop_and_target_in_one_bar_is_the_stop_and_a_gap_exits_at_the_open():
    x = ctx([(100, 100, 100, 100), (100, 101, 99, 100), (100, 112, 95, 100), (100, 100, 100, 100)])
    s = Setup("ema", "LONG", 0, "stop", 100.5, 98.0, 110.0, 3)
    st = STRATEGIES["ema"]
    step(s, x, 1, st)
    assert (s.status, s.fill) == ("open", 100.5)
    step(s, x, 2, st)
    assert (s.status, s.exit_price) == ("sl", 98.0)  # the stop first
    g = ctx([(100, 100, 100, 100), (100, 101, 99.5, 100), (96, 97, 95, 96)])
    s = Setup("ema", "LONG", 0, "stop", 100.5, 98.0, 110.0, 3)
    step(s, g, 1, st)
    step(s, g, 2, st)
    assert (s.status, s.exit_price) == ("sl", 96)  # gapped through the stop: the open
    assert outcome(s, g, Costs())["r"] == pytest.approx(-4.5 / 2.5)


def test_a_stop_entry_expires_or_is_cancelled_and_a_gap_fills_at_the_open():
    st = STRATEGIES["ema"]
    x = ctx([(100, 100, 100, 100)] + [(100, 100.2, 99.8, 100)] * 4)
    s = Setup("ema", "LONG", 0, "stop", 100.5, 98.0, 110.0, 3)
    for j in range(1, 5):
        step(s, x, j, st)
    assert (s.status, s.end_i) == ("expired", 3)
    c = ctx([(100, 100, 100, 100), (100, 100.2, 98.5, 98.6)])
    s = Setup("ema", "LONG", 0, "stop", 100.5, 98.0, 110.0, 3, cancel=lambda j: c.c[j] < 99)
    step(s, c, 1, st)
    assert s.status == "cancelled"
    g = ctx([(100, 100, 100, 100), (101, 102, 100.8, 101.5)])
    s = Setup("ema", "LONG", 0, "stop", 100.5, 98.0, 110.0, 3)
    step(s, g, 1, st)
    assert s.fill == 101  # opened above the stop price
    sh = ctx([(100, 100, 100, 100), (100, 100.5, 98.0, 99)])
    s = Setup("ema", "SHORT", 0, "stop", 99.5, 102.0, 92.0, 3)
    step(s, sh, 1, st)
    assert (s.status, s.fill) == ("open", 99.5)


def test_donchian_stop_follows_the_fill_and_the_exit_rule_closes_at_the_next_open():
    bars = walk(3000, 4)
    x = Ctx(bars, agg4(bars), Params(htf_filter=False))
    rows = run(x, STRATEGIES["donchian"], Costs(), 100)
    exits = [s for s in rows if s.status == "exit"]
    assert exits and any(s.status == "sl" for s in rows)
    for s in exits:
        assert s.fill_i == s.signal_i + 1 and s.fill == x.o[s.fill_i]  # the next open
        assert s.sl == pytest.approx(s.fill - s.d * 2 * s.n)
        assert donchian_exit(x, s.exit_i - 1, s.d) and s.exit_price == x.o[s.exit_i]


# ---- costs and statistics ----------------------------------------------------------------------
def test_costs_are_charged_in_r_and_funding_is_counted_at_each_funding_time():
    assert _funding_times(0, 8 * 3600, 8) == 1 and _funding_times(1, 8 * 3600 - 1, 8) == 0
    assert _funding_times(0, 24 * 3600, 8) == 3 and _funding_times(0, 10, 0) == 0
    x = ctx([(100, 100, 100, 100), (100, 101, 99.5, 100), (101, 103.5, 100.5, 103)])
    s = Setup("ema", "LONG", 0, "stop", 100, 98.0, 103.0, 3)
    st = STRATEGIES["ema"]
    step(s, x, 1, st)
    step(s, x, 2, st)
    assert s.status == "tp"
    o = outcome(s, x, Costs(maker=0.001, taker=0.002, slippage=0.0005))
    # entry: stop order = taker + slippage; exit at the target: maker; stop distance 2 %
    assert o["r"] == pytest.approx(1.5) and o["cost_r"] == pytest.approx(0.0035 / 0.02)
    assert o["net_r"] == pytest.approx(1.5 - 0.175)


def test_stats():
    rows = [{"status": "tp", "side": "LONG", "r": 2, "net_r": 1.9}, {"status": "sl", "side": "SHORT", "r": -1, "net_r": -1.1},
            {"status": "sl", "side": "LONG", "r": -1, "net_r": -1.1}, {"status": "exit", "side": "LONG", "r": 3, "net_r": 2.9},
            {"status": "expired", "side": "LONG", "r": None, "net_r": None}, {"status": "rejected", "side": "LONG", "r": None, "net_r": None}]
    s = stats(rows)
    assert (s["trades"], s["wins"], s["win_rate"]) == (4, 2, 0.5)
    assert s["total_r"] == pytest.approx(2.6) and s["profit_factor"] == pytest.approx(2.18, abs=0.01)
    assert s["max_dd_r"] == pytest.approx(2.2) and (s["expired"], s["rejected"]) == (1, 1)
    assert s["long"]["trades"] == 3 and s["short"]["total_r"] == pytest.approx(-1.1)


# ---- no look-ahead -----------------------------------------------------------------------------
KEY = ("strategy", "side", "signal_i", "status", "fill_i", "exit_i", "entry", "sl", "tp", "exit_price")


@pytest.mark.parametrize("name", list(STRATEGIES))
def test_setups_never_depend_on_later_bars(name):
    bars = walk(2600, 7)
    full = run(Ctx(bars, agg4(bars), Params()), STRATEGIES[name], Costs(), 300)
    for cut in (1200, 1777, 2300):
        part = run(Ctx(bars[:cut], agg4(bars[:cut]), Params()), STRATEGIES[name], Costs(), 300)
        done = [s for s in part if s.status not in ("pending", "open")]
        same = [s for s in full if s.signal_i < cut and s.end_i is not None and s.end_i < cut]
        same = same[:len(done)]
        assert [tuple(getattr(s, k) for k in KEY) for s in done] == \
            [tuple(getattr(s, k) for k in KEY) for s in same]
        for s in part:
            if s.status in ("pending", "open"):  # still live at the cut: the same order
                twin = next(f for f in full if f.signal_i == s.signal_i and f.side == s.side)
                assert (twin.entry, twin.tp) == (s.entry, s.tp)


def test_every_strategy_trades_on_a_random_walk_long_and_short():
    bars = walk(5000, 1)
    x = Ctx(bars, agg4(bars), Params())
    for name in ("donchian", "ema", "avwap"):
        rows = run(x, STRATEGIES[name], Costs(), 300)
        sides = {s.side for s in rows if s.status in ("tp", "sl", "exit")}
        assert sides == {"LONG", "SHORT"}, name


def test_the_4h_filter_uses_closed_4h_bars_only():
    h1 = bars_from([(100, 101, 99, 100)] * 8)
    h4 = [{"t": T0 + k * 14400, "o": 100, "h": 101, "l": 99, "c": 100.0 + k, "v": 1}
          for k in range(-205, 2)]
    x = Ctx(h1, h4, Params())
    # 1h bars 0-2 lie inside the 4h bar opening at T0: the EMA is that of the bars before it
    assert x.ema4[0] == x.ema4[2] and x.ema4[3] != x.ema4[2]
    closed = ind.ema([b["c"] for b in h4], 200)
    assert x.ema4[2] == pytest.approx(closed[204]) and x.ema4[3] == pytest.approx(closed[205])


# ---- the service -------------------------------------------------------------------------------
class History:
    def __init__(self, bars1h, bars4h):
        self.b = {"1h": bars1h, "4h": bars4h}

    def tail(self, tf, limit):
        return self.b[tf][-limit:]

    def older(self, tf, before, limit):
        older = [b for b in self.b[tf] if b["t"] < before][-limit:]
        return older, len(older) == limit


def test_the_service_answers_the_chart_and_the_scanner():
    from sp2l.playbook.service import Playbook

    bars = walk(4000, 3)
    now = bars[-1]["t"] + 1800  # the last bar is still forming
    pb = Playbook(History(bars, agg4(bars)), Costs(taker=0.001), clock=lambda: now)  # type: ignore[arg-type]
    d = pb.analyse("ema", days=60)
    assert d["ready"] and d["asof"] == bars[-2]["t"] and d["price"] == bars[-1]["c"]
    assert d["from"] >= bars[-2]["t"] - 60 * 86400 and d["bars"] <= 60 * 24
    assert {s["id"] for s in d["series"]} == {"ema4", "ema50", "adx"}
    assert all(len(s["points"]) == d["bars"] for s in d["series"])
    assert d["setups"] and set(d["checks"]) == {"LONG", "SHORT"}
    assert d["stats"]["trades"] == sum(1 for s in d["setups"] if s["status"] in ("tp", "sl", "exit"))
    live = [s for s in d["setups"] if s["status"] in ("pending", "open")]
    assert d["current"] == (live[-1] if live else d["current"])
    assert pb.analyse("ema", days=60) is not d and pb.analyse("ema", days=60)["setups"] is d["setups"]  # cached
    b = pb.analyse("avwap", days=60, brief=True)
    assert "series" not in b and "setups" not in b and "stats" in b
    for name in STRATEGIES:
        assert pb.analyse(name, days=30, htf_filter=False)["ready"]
