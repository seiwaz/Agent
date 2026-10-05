"""The research trade path prices every trade exactly as the engine does (default models)."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D

from sp2l.smc.backtest import build_context, run
from sp2l.smc.model import Costs
from sp2l.smc.research.data import from_bars
from sp2l.smc.research.trades import result, setups, trades
from tests.smc.fixtures import SETUP, P, expand
from tests.smc.test_timeframes_strategy import DENSE, m1_walk

C = Costs(D("0.0008"), D("0.00095"), D("0.000326"))


def _same(m1, p):
    ctx = build_context(m1, p)
    ref = run(m1, p, C, ctx=ctx)["trades"]
    mk = from_bars("TEST", m1, p.tick)
    got = trades(setups(ctx, p, C), mk, p)
    want = [(s.key, t.state.value, t.result_r) for s, t in ref if t.state.value != "OPEN"]
    have = [(x.setup.key, x.kind, result(x, C)) for x in got if x.kind != "OPEN"]
    assert [(k, st) for k, st, _ in want] == [(k, st) for k, st, _ in have]
    for (_, _, r), (_, _, res) in zip(want, have, strict=True):
        if r is None:
            assert res is None
        else:
            assert res is not None and abs(res[1] - float(r)) < 1e-9
    return have


def test_research_paths_reproduce_the_engine():
    _same(m1_walk(6 * 1440, 3), DENSE)
    rows = SETUP + [(98, 102, 97.8, 101.8), (101.8, 105, 101.5, 104.8), (104.8, 108.5, 104.5, 108)]
    have = _same(expand(rows), replace(P, min_net_rr=D(0)))
    assert any(k == "TP" for _, k, _ in have)  # a take-profit is part of the comparison


def _mk(rows):
    from datetime import UTC, datetime, timedelta

    from sp2l.core.types import Candle

    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    bars = [
        Candle(t0 + timedelta(minutes=i), D(str(o)), D(str(h)), D(str(lo)), D(str(c)))
        for i, (o, h, lo, c) in enumerate(rows)
    ]
    return from_bars("T", bars, D("0.1"))


def test_event_outcome_rules():
    from sp2l.smc.research.events import outcome

    # long zone: entry 100, stop 98 (1R = 2); the touch minute reaches +1R too: not counted
    m = _mk([(101, 102.5, 100, 101), (101, 102.1, 100.5, 102), (102, 104.2, 101, 104)])
    o = outcome(m, 0, 1, 100.0, 98.0, 3)
    assert o is not None and o["hit"] == {1: True, 2: True, 3: False}
    assert o["hold_min"] == 1  # +1R first reached in the minute after the touch
    # a minute reaching the stop and +1R together counts as the stop
    m2 = _mk([(100.5, 101, 100, 100.5), (100, 102.5, 97.5, 101), (101, 105, 100, 104)])
    o2 = outcome(m2, 0, 1, 100.0, 98.0, 3)
    assert o2 is not None and o2["hit"] == {1: False, 2: False, 3: False} and o2["mae"] == 1.0
