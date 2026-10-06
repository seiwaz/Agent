"""The audit tools themselves: the independent references agree with the engine on synthetic
data, and the repainting snapshot finds nothing on a causal analysis (and finds a planted
look-ahead)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

from audit.execution import Rules, ref_walk
from audit.lookahead import snapshot_diff
from audit.reference import engine, reference

from sp2l.core.types import Candle, Side
from sp2l.smc.backtest import build_context, run
from sp2l.smc.model import Costs
from sp2l.smc.structure import analyze
from sp2l.smc.timeframes import aggregate
from tests.smc.test_timeframes_strategy import DENSE, T0, m1_walk

COSTS = Costs(D("0.0008"), D("0.00095"), D("0.000326"))
WALK = m1_walk(3 * 1440, 5)


def test_reference_structure_equals_engine():
    bars = aggregate(WALK, "5m", WALK[-1].open_time + timedelta(minutes=1))
    r, e = reference(bars, DENSE, DENSE.lookback("5m")), engine(bars, "5m", DENSE)
    assert len(r["swings"]) > 50 and len(r["events"]) > 10
    for k in r:
        assert set(r[k]) == set(e[k]), k


def test_reference_lifecycle_equals_engine_trade_for_trade():
    p = replace(DENSE, max_hold_min=240)
    res = run(WALK, p, COSTS)
    assert res["trades"], "the synthetic walk must produce trades"
    index = {b.open_time: i for i, b in enumerate(WALK)}
    for s, t in res["trades"]:
        x = ref_walk(s.direction, s.entry, s.sl, s.tp.price, s.created_at, s.market, WALK,
                     index[s.created_at], p, COSTS, p.tick, Rules())
        assert (x.state, x.filled_at, x.closed_at) == (t.state.value, t.filled_at, t.closed_at)
        if t.result_r is not None:
            assert abs(x.r - t.result_r) < D("1e-12")


def _bar(i, o, h, lo, c):
    return Candle(T0 + timedelta(minutes=i), D(o), D(h), D(lo), D(c), D(1), None)


def test_ref_walk_stop_is_minus_one_r_and_gap_rule_is_worse():
    bars = [_bar(0, 100, 100.5, 99.9, 100.2), _bar(1, 100.2, 100.3, 99.5, 99.6),
            _bar(2, 98.0, 98.1, 97.0, 97.5)]  # minute 2 opens below the stop
    args = (Side.LONG, D(100), D("98.5"), D(104), T0, False, bars, 0, DENSE, COSTS, D("0.1"))
    base = ref_walk(*args, Rules())
    gap = ref_walk(*args, Rules(gap_stop=True))
    assert base.state == "SL" and base.r == -1
    assert gap.gap_stop and gap.r < -1


def test_ref_walk_trade_through_needs_a_tick_beyond():
    bars = [_bar(0, 101, 101.2, 100.0, 100.5), _bar(1, 100.5, 104.5, 100.4, 104)]
    args = (Side.LONG, D(100), D("98.5"), D(104), T0, False, bars, 0, DENSE, COSTS, D("0.1"))
    assert ref_walk(*args, Rules()).filled_at == T0  # touched
    assert ref_walk(*args, Rules(fill="through")).state == "MISSED"  # never traded through


def test_snapshot_finds_nothing_on_the_engine_and_catches_a_planted_repaint():
    a = build_context(WALK, DENSE)["5m"]
    k = len(a.bars) - 200
    snap = analyze(a.bars[: k + 1], "5m", DENSE)
    assert not any(snapshot_diff(a, snap, k).values())
    z = snap.zones[-1]  # widen one zone after the fact: a repaint
    z.top += D(1)
    d = snapshot_diff(a, snap, k)
    assert d["zones_early"] == 1 and d["zones_late"] == 1
