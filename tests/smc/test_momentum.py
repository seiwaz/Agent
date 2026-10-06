# ruff: noqa: E501  (synthetic bar rows are clearer on one line)
"""Momentum research harness: causal states, the edge study's control method, split
separation and the exit rules."""

from __future__ import annotations

import math
import random
from datetime import timedelta
from decimal import Decimal as D

import numpy as np

from sp2l.core.types import Candle
from sp2l.smc.model import Costs, SmcParams
from sp2l.smc.research.data import from_bars
from sp2l.smc.research.events import controls, zones
from sp2l.smc.research.momentum import (
    KINDS,
    Momentum,
    Trade,
    ZoneBars,
    controls_each,
    price,
    simulate,
    study,
)
from sp2l.smc.timeframes import length
from tests.smc.test_timeframes_strategy import T0, m1_walk

P = SmcParams()
C = Costs(D("0.0008"), D("0.00095"), D("0.000326"))
WALK = m1_walk(40 * 1440, 21)
MK = from_bars("T", WALK, D("0.01"))


def test_momentum_states_are_causal():
    full = Momentum(MK, P)
    cut = 30 * 1440
    part = Momentum(from_bars("T", WALK[:cut], D("0.01")), P)
    idx = np.arange(2 * 1440, cut, 97)
    for s in ("M1", "M2", "M3", "B4"):
        assert (full.state(s, idx) == part.state(s, idx)).all(), s


def test_m1_is_the_sign_of_the_24h_return():
    mom = Momentum(MK, P)
    i = 5 * 1440 + 17
    want = int(np.sign(MK.c[i - 1] - MK.c[i - 1 - 1440]))
    assert int(mom.state("M1", np.array([i]))[0]) == want


def test_controls_each_reproduces_the_edge_study_controls():
    zs, a = zones(MK, "1h", P)
    ln = int(length("1h").total_seconds() // 60)
    atr_close = np.array([b.open_time.timestamp() + 3600 for b in a.bars])
    atr = np.array([float(x) if x is not None else 0.0 for x in a.atr])
    z = next(z for z in zs if z.kind in KINDS and z.atr)
    args = (MK, z, MK.split.timestamp(), 100 * ln, P.lookback("1h") * ln, 0.01, atr_close, atr)
    want = controls(*args[:2], "discovery", *args[2:], random.Random(5))
    got = controls_each(*args, random.Random(5), 5)
    if want is None:
        assert got == []
    else:
        assert math.isclose(want["hit"][1], float(np.mean([o["hit"][1] for *_, o in got])))


def test_discovery_study_never_reads_holdout_touches():
    mom = Momentum(MK, P)
    split = MK.split.timestamp()
    disc = study(MK, "1h", P, mom)
    assert disc and all(MK.t[e.i] < split for e in disc)
    assert all(MK.t[c.i] < split for e in disc for c in e.ctl)


def _market(rows):
    bars = [Candle(T0 + timedelta(minutes=k), D(str(o)), D(str(h)), D(str(lo)), D(str(c)), D(1))
            for k, (o, h, lo, c) in enumerate(rows)]
    return from_bars("S", bars, D("0.1"))


def test_exits_fixed_time_and_stop():
    # flat 100 for 2 days, then a ramp up: a long at 100 with the stop at 99
    rows = [(100, 100.2, 99.8, 100)] * 2880 + [(100 + k * 0.01, 100.05 + k * 0.01, 99.95 + k * 0.01, 100 + k * 0.01) for k in range(2880)]
    m = _market(rows)
    zb, mom = ZoneBars(m, "1h", P), Momentum(m, P)
    i = 2880
    x, lvl, kind = simulate(m, i, 1, 100.0, 99.0, "fixed_2R", zb, mom, "M1")
    assert kind == "TP" and lvl == 102.0 and m.h[x] >= 102.0
    x, lvl, kind = simulate(m, i, 1, 100.0, 99.0, "time_12", zb, mom, "M1")
    assert kind == "TIME" and (m.t[x] + 60 - m.t[i]) / 3600 <= 12.5
    t = Trade("S", i, 1, 100.0, 99.0, 0, 99.0, "SL")
    assert abs(price(t, C)[1] + 1) < 1e-12  # a full stop is exactly -1R


def test_chandelier_ratchets_and_exits_below_the_high():
    up = [(100 + k * 0.02, 100.05 + k * 0.02, 99.95 + k * 0.02, 100 + k * 0.02) for k in range(600)]
    top = up[-1][3]
    down = [(top - k * 0.05, top - k * 0.05 + 0.02, top - k * 0.05 - 0.02, top - k * 0.05) for k in range(600)]
    m = _market([(100, 100.2, 99.8, 100)] * 2880 + up + down)
    zb, mom = ZoneBars(m, "1h", P), Momentum(m, P)
    x, lvl, kind = simulate(m, 2880, 1, 100.0, 95.0, "chandelier_2", zb, mom, "M1")
    assert kind == "TRAIL" and 95.0 < lvl < top and x > 2880 + 600


def test_momentum_flip_exits_at_the_first_1h_close_against_the_trade():
    up = [(100 + k * 0.002, 100.01 + k * 0.002, 99.99 + k * 0.002, 100 + k * 0.002) for k in range(2880)]
    top = up[-1][3]
    down = [(top - k * 0.004, top - k * 0.004 + 0.002, top - k * 0.004 - 0.002, top - k * 0.004) for k in range(2880)]
    m = _market(up + down)
    zb, mom = ZoneBars(m, "1h", P), Momentum(m, P)
    i = 2880 + 10
    x, lvl, kind = simulate(m, i, 1, float(m.o[i]), 90.0, "momentum_flip", zb, mom, "M1")
    assert kind == "FLIP"
    end = m.t[x] + 60
    assert end in set(mom.h1_close)  # on a 1h close
    assert mom.at_ts("M1", end) == -1 and mom.at_ts("M1", end - 3600) != -1  # the first one against
