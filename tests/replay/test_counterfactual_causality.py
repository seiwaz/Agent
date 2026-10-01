"""Counterfactual causality (cf-2): a counterfactual created while the engine processes
minute t must NOT receive minute t's close again.

Regression for the double-feed bug found in the 2026-09-29 Context study: the engine created
the counterfactual inside ShadowSymbolEngine.on_m1 and then fed it that same minute's close;
Spike.try_extend rejected the duplicate (not exactly one minute after LastSpike) and froze
the spike for good, so the counterfactual E1 never followed the continuing Spike and rested
at a stale level (fills 36-42 minutes later on the live server).

Every engine counterfactual is compared with an independent reference built from the same
rejected machine at the same instant and fed ONLY the market events that follow it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal as D
from typing import Any

from sp2l.counterfactual.simulator import CounterfactualRun
from sp2l.engine.recording import NullRecorder
from sp2l.engine.symbol_engine import CF_STAGES, ShadowSymbolEngine
from sp2l.marketdata.m1_builder import MINUTE, M1Builder
from sp2l.strategy.risk.engine import CostModel, ExchangeFilters
from tests.conftest import T0
from tests.replay.tape import tape

COSTS = CostModel(D("0.0008"), D("0.00095"), D("0.000198"))
FILTERS = ExchangeFilters(D("0.1"), D("0.001"), None, None, verified=False)
MINUTES = 900
END = T0 + timedelta(minutes=MINUTES)


class _Refs(NullRecorder):
    """At archival, build the reference counterfactual exactly as the engine does."""

    def __init__(self) -> None:
        self.engine: ShadowSymbolEngine | None = None
        self.refs: dict[str, tuple[Any, CounterfactualRun]] = {}

    def on_archived(self, machine: Any, broker: Any) -> None:
        stage = CF_STAGES.get(machine.state)
        if stage is None:
            return
        if machine.ever_armed:
            stage += "_AFTER_ARM"
        assert self.engine is not None
        ts = machine.transitions[-1][0]
        ref = CounterfactualRun(machine, stage, COSTS, ts, self.engine.last_trade)
        self.refs[machine.cfg.setup_id] = (ts, ref)


def run() -> tuple[ShadowSymbolEngine, _Refs]:
    rec = _Refs()
    eng = ShadowSymbolEngine(
        "BTCUSDT", tick=D("0.1"), costs=COSTS, filters=FILTERS, warmup_bars=30, recorder=rec
    )
    rec.engine = eng
    b = M1Builder(T0)
    b.mark_healthy_until(END)

    def minutes(now: Any) -> None:
        for m1 in b.advance(now):
            eval_time = m1.open_time + MINUTE
            fresh = {k for k, (ts, _) in rec.refs.items() if ts >= eval_time}
            eng.on_m1(m1)
            for k, (ts, ref) in rec.refs.items():
                if k not in fresh and ts < eval_time and not ref.done:
                    ref.on_m1_close(m1, eval_time, eng.last_trade)

    for t in sorted(tape(MINUTES, 3), key=lambda x: (x.recv_ts, x.exch_ts, x.trade_id)):
        minutes(t.recv_ts)
        eng.on_trade(t.price, t.exch_ts)
        for _, ref in rec.refs.values():
            ref.on_trade(t.price, t.exch_ts)
        b.add_trade(t)
    minutes(END + timedelta(minutes=1))
    return eng, rec


def trail(cf: CounterfactualRun) -> list[tuple[Any, ...]]:
    keep = ("E1_SUBMITTED", "E1_NOT_SAFE", "PULLBACK_START", "FILL", "EXIT", "STATE")
    return [
        (e["kind"], e["ts"], e.get("rev"), e.get("e1"), e.get("price"), e.get("to"))
        for e in cf.machine.events
        if e["kind"] in keep
    ]


def outcome_hash(eng: ShadowSymbolEngine) -> str:
    rows = [(cf.candidate_id, trail(cf), cf.outcome().outcome) for cf in eng.counterfactuals]
    return hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest()


def test_counterfactuals_match_an_independent_reference_exactly():
    eng, rec = run()
    assert len(eng.counterfactuals) >= 20, "the tape must produce enough counterfactuals"
    extended = 0
    for cf in eng.counterfactuals:
        _, ref = rec.refs[cf.candidate_id]
        # LastSpike, E1 revisions, PullbackStart, FillWindow, fills and exits all identical
        assert trail(cf) == trail(ref), cf.candidate_id
        assert cf.machine.spike.last.open_time == ref.machine.spike.last.open_time
        assert cf.outcome() == ref.outcome()
        extended += int(len(cf.machine.revisions) > 1)
    assert extended > 0, "some counterfactual E1 must have followed its extending Spike"


def test_creation_minute_is_processed_exactly_once(monkeypatch):
    """No counterfactual ever receives the close of the minute it was created on (or any
    earlier one): its first M1 close is the next finalized minute."""
    seen: dict[str, list[Any]] = {}
    orig = CounterfactualRun.on_m1_close

    def spy(self: CounterfactualRun, m1: Any, eval_time: Any, last: Any) -> None:
        seen.setdefault(self.candidate_id, []).append((eval_time, self.machine.created_at))
        orig(self, m1, eval_time, last)

    monkeypatch.setattr(CounterfactualRun, "on_m1_close", spy)
    eng, _ = run()
    engine_cfs = {cf.candidate_id for cf in eng.counterfactuals}
    checked = 0
    for key, calls in seen.items():
        if key.startswith("cf-") or key not in engine_cfs:
            continue
        for eval_time, created in calls:
            assert eval_time > created, (key, eval_time, created)
            checked += 1
    assert checked > 0


def test_no_counterfactual_fills_outside_its_fill_window():
    eng, _ = run()
    fills = 0
    for cf in eng.counterfactuals:
        m = cf.machine
        for f in m.fills:
            if "-E1-" not in f.client_order_id:
                continue
            fills += 1
            assert m.pb_minute is not None
            n = (f.ts.replace(second=0, microsecond=0) - m.pb_minute) // MINUTE + 1
            assert 1 <= n <= m.cfg.fill_window_candles, (cf.candidate_id, n)
    assert fills > 0


def test_same_stream_twice_gives_identical_counterfactual_hashes():
    a, _ = run()
    b, _ = run()
    assert outcome_hash(a) == outcome_hash(b)
