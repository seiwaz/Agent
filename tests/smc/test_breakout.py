# ruff: noqa: E501  (synthetic bar rows are clearer on one line)
"""Breakout research harness: walk rules, range queries, CRT detection, costs, split
membership and no look-ahead in the signals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import numpy as np
import pytest

from sp2l.core.types import Candle
from sp2l.smc.model import Costs, SmcParams
from sp2l.smc.research import breakout as bo
from sp2l.smc.research.data import from_bars
from tests.smc.test_timeframes_strategy import m1_walk

P = SmcParams()
C = Costs(D("0.0008"), D("0.00095"), D("0.000326"))
T0 = datetime(2026, 1, 5, 0, 30, tzinfo=UTC)  # a Monday, on the Tehran hh:30 grid


def _hours(rows):
    """Hourly OHLC rows -> M1 bars (open, high, low and close placed inside the hour)."""
    out = []
    for h, (o, hi, lo, c) in enumerate(rows):
        for k in range(60):
            px = o if k == 0 else c if k == 59 else (hi if k == 20 else lo if k == 40 else (o + c) / 2)
            hh = max(px, o if k == 0 else px)
            out.append(Candle(T0 + timedelta(minutes=60 * h + k), D(str(px)), D(str(hh)), D(str(px)), D(str(px)), D(1)))
    return from_bars("S", out, D("0.1"))


def test_rmq_matches_numpy():
    x = np.random.default_rng(1).normal(size=1000)
    mx, mn = bo.RMQ(x, "max"), bo.RMQ(x, "min")
    for a, b in ((0, 1), (3, 17), (100, 999), (0, 1000)):
        assert mx.q(a, b) == x[a:b].max() and mn.q(a, b) == x[a:b].min()


def test_walk_stop_counts_in_entry_minute_target_does_not_and_stop_first():
    rows = [(100, 103, 98, 100)] * 3  # one minute reaching both
    m = from_bars("S", [Candle(T0 + timedelta(minutes=i), D(o), D(h), D(lo), D(c), D(1)) for i, (o, h, lo, c) in enumerate(rows)], D("0.1"))
    assert bo.walk(m, 0, 3, 1, 100.0, 99.0, 102.0)["kind"] == "SL"  # stop in the entry minute
    m2 = from_bars("S", [Candle(T0 + timedelta(minutes=i), D(o), D(h), D(lo), D(c), D(1)) for i, (o, h, lo, c) in enumerate([(100, 102.5, 99.5, 100), (100, 102.5, 99.5, 102)])], D("0.1"))
    w = bo.walk(m2, 0, 2, 1, 100.0, 99.0, 102.0)
    assert w["kind"] == "TP" and w["exit_i"] == 1  # not credited in the entry minute


def test_full_stop_is_minus_one_r_for_market_and_limit_entries():
    for mode in ("market", "limit50"):
        ev = bo.Ev("H3", mode, "S", "1h", 0, 0.0, 1, 100.0, 99.0, 103.0, "CR", 1.0, 1.0)
        ev.res = {"level": 99.0, "gross": -1.0}
        assert abs(bo.net_r(ev, C) + 1) < 1e-12


def test_crt_detected_with_its_stop_target_and_outcome():
    flat = (100.0, 101.0, 99.0, 100.0)
    rows = [flat] * 700
    rows[201] = (100.0, 102.0, 100.0, 100.5)  # candle 2: wick above CRH 101, close back inside
    m = _hours(rows)
    md = bo.market_data(m, P)
    evs = [e for e in bo.crt_events(md, "1h", False) if e.mode == "market"]
    t201 = (T0 + timedelta(hours=202)).timestamp()
    ev = next(e for e in evs if e.t == t201)
    assert ev.d == -1 and ev.entry == 100.5
    atr = md.tfs["1h"].atr[ev.k]
    assert ev.stop == pytest.approx(np.ceil((102.0 + 0.1 + 0.2 * atr) / 0.1 - 1e-9) * 0.1)
    assert ev.target == pytest.approx(np.ceil((99.0 + 0.05 * atr) / 0.1 - 1e-9) * 0.1)
    assert ev.res["kind"] == "TP"


def test_discovery_events_end_before_the_split():
    m = from_bars("W", m1_walk(60 * 1440, 4), D("0.01"))
    md = bo.market_data(m, P)
    split = m.split.timestamp()
    evs = bo.crt_events(md, "1h", False) + bo.ifvg_events(md, "1h", False) + bo.run_events(md, "1h", False)
    assert evs
    for e in evs:
        assert e.end_i <= int(np.searchsorted(m.t, split, side="left"))


def _sig(e):
    return (e.hyp, e.mode, e.tf, e.k, e.t, e.d, round(e.entry, 8), round(e.stop, 8), round(e.target, 8), e.target_src, sorted(e.flags.items(), key=str))


def test_signals_do_not_change_when_the_future_is_cut(monkeypatch):
    walk = m1_walk(60 * 1440, 9)
    full, part = from_bars("W", walk, D("0.01")), from_bars("W", walk[: 45 * 1440], D("0.01"))
    monkeypatch.setattr(bo, "_in_split", lambda m, t, end_ts, holdout: end_ts <= float(m.t[-1]) + 60)
    cut = float(part.t[-1]) + 60
    for fn, tf in ((bo.crt_events, "1h"), (bo.crt_events, "4h"), (bo.ifvg_events, "1h"), (bo.run_events, "1h")):
        a = [_sig(e) for e in fn(bo.market_data(full, P), tf, False) if float(full.t[e.end_i - 1]) + 60 <= cut]
        b = [_sig(e) for e in fn(bo.market_data(part, P), tf, False)]
        assert a and a == b, (fn.__name__, tf, len(a), len(b))
